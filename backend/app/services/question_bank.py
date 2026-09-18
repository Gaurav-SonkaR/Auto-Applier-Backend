"""The applicant's answers to application questions, and how a form gets them.

Replaces `qa_cache`, which silently stored whatever a model wrote and replayed it
verbatim. That had two problems beyond being invisible:

* a company-specific answer was reused at the next company. "What interests you
  most about this company?" answered for Acme was typed into Globex's form,
  naming Acme;
* the user could not see, correct or pre-write any of it.

Now the bank is something the user fills in, from the catalogue of questions
portals actually ask (`question_catalogue`). When a form asks one:

1. **Answered by the user** — used. ``fixed`` answers are typed as written, with
   ``{company}`` / ``{role}`` filled in; ``tailor`` answers are adapted to the
   company and job description by a model that may reword but not add claims.
2. **Not answered** — a model drafts one from the profile and the JD. It is
   saved back *with the company and role replaced by placeholders* and marked
   for review, so the user can correct it and the next company never sees the
   last one's name.
3. **Nothing supports an answer** — left blank. A required field that stays
   blank stops the submission and flags the job, which is right when nobody
   knows the answer.

Self-identification questions never reach any of this.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from backend.app.models.qa_answer import QaAnswer
from backend.app.services.form_choices import is_sensitive
from backend.app.services.question_catalogue import CATALOGUE, CatalogueQuestion

logger = logging.getLogger("question_bank")

MAX_QUESTION_LEN = 500
MAX_ANSWER_LEN = 4000
JD_EXCERPT_CHARS = 2500

SOURCE_USER = "user"
SOURCE_GENERATED = "generated"
MODES = ("fixed", "tailor")


# ── Keys ────────────────────────────────────────────────────────────────────

_DECORATION_RE = re.compile(
    r"\(\s*(?:required|optional|max\.?\s*\d+\s*\w*)\s*\)|\*+", re.I
)
_NON_WORD_RE = re.compile(r"[^a-z0-9]+")

# Filler that changes nothing about what is being asked. Without dropping it,
# "Are you serving your notice period?" and "Are you serving notice period?"
# were two different questions, and one of them went unanswered.
_FILLER = frozenset({"a", "an", "the", "your", "my", "any", "please", "kindly"})


def normalise_question(question: str) -> str:
    """Case, punctuation, portal decoration and filler words folded away.

    "Why us?", "Why us? *" and "Why us? (required)" are one question; treating
    them as three means answering it three times.
    """
    text = (question or "").strip().lower()[:MAX_QUESTION_LEN]
    text = _DECORATION_RE.sub(" ", text)
    words = _NON_WORD_RE.sub(" ", text).split()
    return " ".join(w for w in words if w not in _FILLER)


def _swap_names(text: str, company: str, role: str) -> str:
    """Put {company} / {role} where this job's names appear."""
    for name, placeholder in ((company, "{company}"), (role, "{role}")):
        name = (name or "").strip()
        if len(name) >= 2:
            text = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", placeholder, text, flags=re.I)
    return text


def question_key(question: str, company: str = "", role: str = "") -> str:
    """The lookup key, with this job's company and role abstracted away.

    So "Why do you want to join Acme?" and "Why do you want to join Globex?"
    are the same question — and match the catalogue's "{company}" wording.
    """
    return normalise_question(_swap_names(question or "", company, role))


def _build_index() -> dict[str, CatalogueQuestion]:
    index: dict[str, CatalogueQuestion] = {}
    for entry in CATALOGUE:
        for wording in (entry.question, *entry.aliases):
            index.setdefault(normalise_question(wording), entry)
    return index


_INDEX = _build_index()


def catalogue_entry(question: str, company: str = "", role: str = "") -> Optional[CatalogueQuestion]:
    return _INDEX.get(question_key(question, company, role))


def canonical_key(question: str, company: str = "", role: str = "") -> str:
    """Every wording of a catalogue question collapses to the catalogue's own."""
    entry = catalogue_entry(question, company, role)
    return normalise_question(entry.question) if entry else question_key(question, company, role)


# ── Answers: placeholders ───────────────────────────────────────────────────

def templatize(answer: str, company: str, role: str) -> str:
    """A generated answer, made safe to reuse at another company."""
    return _swap_names(answer or "", company, role)


def render(answer: str, company: str, role: str) -> str:
    return (answer or "").replace("{company}", (company or "").strip() or "your company") \
        .replace("{role}", (role or "").strip() or "this role")


_COMPANY_SPECIFIC_RE = re.compile(
    r"\b(?:this|your|our)\s+(?:company|organi[sz]ation|team|role|position|job|opportunity)\b|"
    r"\bwhy\b.*\b(?:us|join|here|apply|applying)\b|\bcover\b|\bhiring manager\b|\{company\}",
    re.I,
)


def infer_mode(question: str) -> str:
    """Questions that refer to the company or role are adapted per job."""
    return "tailor" if _COMPANY_SPECIFIC_RE.search(question or "") else "fixed"


# ── Storage ─────────────────────────────────────────────────────────────────

@dataclass
class BankItem:
    key: str
    question: str
    category: str
    mode: str
    answer: str
    source: str            # "catalogue" (unanswered) | "user" | "generated"
    needs_review: bool
    used_count: int
    qa_id: Optional[int]
    catalogue_id: Optional[str]
    hint: str


def _row(session: Session, user_id: int, key: str) -> Optional[QaAnswer]:
    return session.exec(
        select(QaAnswer).where(QaAnswer.user_id == user_id, QaAnswer.question_key == key)
    ).first()


def find(session: Session, user_id: Optional[int], question: str, company: str = "",
         role: str = "") -> tuple[Optional[CatalogueQuestion], Optional[QaAnswer]]:
    if user_id is None:
        return None, None
    key = canonical_key(question, company, role)
    return catalogue_entry(question, company, role), (_row(session, user_id, key) if key else None)


def list_bank(session: Session, user_id: int) -> list[BankItem]:
    """The whole catalogue — answered or not — then the user's own questions."""
    rows = {r.question_key: r for r in session.exec(
        select(QaAnswer).where(QaAnswer.user_id == user_id)
    ).all()}
    items: list[BankItem] = []

    for entry in CATALOGUE:
        key = normalise_question(entry.question)
        row = rows.pop(key, None)
        items.append(BankItem(
            key=key, question=entry.question, category=entry.category,
            mode=row.mode if row and row.mode in MODES else entry.mode,
            answer=row.answer if row else "",
            source=row.source if row else "catalogue",
            needs_review=bool(row and row.needs_review),
            used_count=row.used_count if row else 0,
            qa_id=row.qa_id if row else None,
            catalogue_id=entry.id, hint=entry.hint,
        ))

    for row in sorted(rows.values(), key=lambda r: (not r.needs_review, r.question.lower())):
        items.append(BankItem(
            key=row.question_key, question=row.question, category=row.category or "Other",
            mode=row.mode if row.mode in MODES else "fixed", answer=row.answer,
            source=row.source or SOURCE_USER, needs_review=bool(row.needs_review),
            used_count=row.used_count, qa_id=row.qa_id, catalogue_id=None, hint="",
        ))
    return items


def save_answer(session: Session, user_id: Optional[int], question: str, answer: str, *,
                mode: Optional[str] = None, category: Optional[str] = None,
                source: str = SOURCE_USER, portal: str = "") -> Optional[QaAnswer]:
    """Upsert an answer. An empty answer from the user clears the entry."""
    if user_id is None or is_sensitive(question):
        return None
    key = canonical_key(question)
    if not key:
        return None
    entry = _INDEX.get(key)
    answer = (answer or "").strip()[:MAX_ANSWER_LEN]
    row = _row(session, user_id, key)

    if not answer:
        if row is not None and source == SOURCE_USER:
            session.delete(row)
            session.commit()
        return None

    if row is None:
        row = QaAnswer(user_id=user_id, question_key=key,
                       question=(entry.question if entry else question.strip())[:MAX_QUESTION_LEN],
                       answer=answer, used_count=0)
    elif source == SOURCE_GENERATED and row.source == SOURCE_USER:
        # A model draft never overwrites what the user wrote.
        return row

    row.answer = answer
    row.mode = mode if mode in MODES else (entry.mode if entry else infer_mode(question))
    row.category = category or (entry.category if entry else "Other")
    row.source = source
    row.needs_review = source == SOURCE_GENERATED
    row.portal = portal or row.portal
    row.updated_at = datetime.now(timezone.utc)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def delete_entry(session: Session, user_id: int, qa_id: int) -> bool:
    row = session.get(QaAnswer, qa_id)
    if row is None or row.user_id != user_id:
        return False
    session.delete(row)
    session.commit()
    return True


def owns_question(session: Session, user_id: Optional[int], company: str = "", role: str = ""):
    """A predicate for the form passes: does the bank hold this question?

    Catalogue wordings always count, answered or not, so that "Are you serving
    your notice period?" is not captured by the notice-period keyword rule and
    filled with "30 days".
    """
    own_keys = set() if user_id is None else {
        r.question_key for r in session.exec(select(QaAnswer).where(QaAnswer.user_id == user_id)).all()
    }

    def _owns(label: str) -> bool:
        if not label or is_sensitive(label):
            return False
        return catalogue_entry(label, company, role) is not None \
            or canonical_key(label, company, role) in own_keys

    return _owns


def stored_answer(user_id: Optional[int], question: str, company: str = "",
                  role: str = "") -> Optional[str]:
    """The user's own answer, rendered, for a dropdown or radio group.

    Choice fields never trigger a draft: picking an option is a claim, and the
    only claim worth making is one the user wrote down.
    """
    from backend.app.database import engine

    if user_id is None or is_sensitive(question):
        return None
    with Session(engine) as session:
        _, row = find(session, user_id, question, company, role)
        if row is None or not row.answer.strip() or row.source != SOURCE_USER:
            return None
        row.used_count += 1
        session.add(row)
        session.commit()
        return render(row.answer, company, role)


def fit_length(text: str, limit: int) -> str:
    """Trim to a field's limit at a sentence or word boundary, not mid-word."""
    text = (text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[:limit]
    sentence = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    # A complete shorter answer reads better on an application than a longer
    # one that stops mid-sentence — unless the last full stop is so early that
    # most of the allowed space would be thrown away.
    if sentence >= limit / 3:
        return cut[:sentence + 1]
    space = cut.rfind(" ")
    return cut[:space] if space > 0 else cut


# ── Resolving a form question ───────────────────────────────────────────────

_GENERATE_SYSTEM = """\
You are answering one question on a job application for the candidate described.
Use only facts from the candidate profile and the job description. Never invent
employers, numbers, dates, certifications or personal circumstances.
If the profile does not give enough to answer truthfully — for example a yes/no
question about something the profile never mentions — reply with exactly: UNKNOWN
Keep it concise: one or two sentences for short fields, at most 150 words for
open questions. Return only the answer text.
"""

_TAILOR_SYSTEM = """\
Rewrite the candidate's own answer so it fits this specific company and role.
Keep every claim it makes and add none: no new skills, numbers, employers or
experiences. You may refer to the company by name and to things the job
description itself says about the role or the company. Keep roughly the same
length. Return only the rewritten answer.
"""


def _profile_brief(profile: dict) -> str:
    personal = profile.get("personal") or {}
    experience = [r for r in (profile.get("experience") or []) if isinstance(r, dict)]
    prefs = profile.get("preferences") or {}
    roles = "; ".join(
        f"{r.get('title', '')} at {r.get('company', '')} ({r.get('start_date', '')}"
        f" - {r.get('end_date', '')})" for r in experience[:3]
    )
    education = profile.get("education") or {}
    return "\n".join(line for line in (
        f"Name: {personal.get('name', '')}",
        f"Location: {personal.get('location', '')}",
        f"Roles: {roles}" if roles else "",
        f"Skills: {', '.join((profile.get('primary_skills') or [])[:25])}",
        f"Education: {education.get('degree', '')} {education.get('institution', '')}".strip(),
        f"Total experience (years): {prefs.get('total_experience_years', '')}"
        if prefs.get("total_experience_years") else "",
    ) if line.strip(" :"))


def _job_brief(job) -> str:
    jd = (getattr(job, "jd_text", "") or "").strip()[:JD_EXCERPT_CHARS]
    return (f"Company: {job.company or 'Unknown'}\nRole: {job.title or ''}\n"
            f"Job description:\n{jd or '(not available)'}")


def _clean_reply(reply: str) -> Optional[str]:
    text = (reply or "").strip().strip('"').strip()
    if not text or text.upper().rstrip(".") == "UNKNOWN":
        return None
    return text[:MAX_ANSWER_LEN]


async def resolve_answer(user_id: Optional[int], question: str, job, profile: dict,
                         router) -> Optional[str]:
    """The text to type for one open form question, or None to leave it blank."""
    from backend.app.database import engine
    from backend.app.services.llm_client import LLMTask

    if user_id is None or not question or is_sensitive(question):
        return None
    company, role = job.company or "", job.title or ""

    with Session(engine) as session:
        entry, row = find(session, user_id, question, company, role)
        base = (row.answer if row else "").strip()
        mode = (row.mode if row and row.mode in MODES else
                entry.mode if entry else infer_mode(question))
        if row is not None and base:
            row.used_count += 1
            session.add(row)
            session.commit()

    if base:
        if mode == "fixed":
            return render(base, company, role)
        prompt = (f"{_job_brief(job)}\n\nCandidate profile:\n{_profile_brief(profile)}\n\n"
                  f"Question: {question}\n\nCandidate's own answer:\n{render(base, company, role)}")
        try:
            tailored = _clean_reply(await router.complete(
                LLMTask.QA_ANSWER, prompt, system=_TAILOR_SYSTEM))
        except Exception as exc:
            logger.warning("Tailoring failed, using the answer as written: %s", exc)
            tailored = None
        return tailored or render(base, company, role)

    prompt = (f"{_job_brief(job)}\n\nCandidate profile:\n{_profile_brief(profile)}\n\n"
              f"Question: {entry.question if entry else question}")
    try:
        drafted = _clean_reply(await router.complete(
            LLMTask.QA_ANSWER, prompt, system=_GENERATE_SYSTEM))
    except Exception as exc:
        logger.warning("Could not draft an answer for %r: %s", question[:60], exc)
        return None
    if not drafted:
        return None

    with Session(engine) as session:
        # The question is templatized as well as the answer: stored as "Why
        # join Acme?" it would never match the same question at Globex.
        save_answer(session, user_id,
                    entry.question if entry else templatize(question, company, role),
                    templatize(drafted, company, role), mode=mode,
                    source=SOURCE_GENERATED, portal=getattr(job, "portal", "") or "")
    return drafted
