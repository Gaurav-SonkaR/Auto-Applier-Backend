"""The uploaded-resume library.

Two features rest on this:

* **Resume-driven portal search** — the master resume's own text supplies the
  search keywords, so the user uploads a resume instead of typing a job title.
* **Best-resume selection** — when no tailored resume was generated for a job,
  pick whichever uploaded resume best matches the parsed job description.

Skill extraction is deliberately deterministic (vocabulary match against
`master_profile.json` plus a small built-in list) rather than an LLM call: it
runs on every upload and every apply, must work offline, and "does this resume
contain the word Django" needs no model.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from pypdf import PdfReader
from sqlmodel import Session, select

from backend.app.models.resume import Resume

logger = logging.getLogger("resume_library")

MAX_EXTRACTED_CHARS = 20_000

# Fallback vocabulary for when master_profile.json is unavailable, and to cover
# common skills a given user's profile happens not to list.
_BASE_SKILL_VOCABULARY = frozenset({
    "python", "java", "javascript", "typescript", "go", "golang", "rust", "c++", "c#",
    "sql", "bash", "php", "ruby", "kotlin", "swift", "scala", "r",
    "django", "flask", "fastapi", "spring", "spring boot", "express", "rails", ".net",
    "react", "react.js", "vue", "vue.js", "angular", "next.js", "svelte",
    "node.js", "graphql", "grpc", "rest api", "microservices",
    "postgresql", "mysql", "mongodb", "redis", "elasticsearch", "sqlite", "cassandra",
    "aws", "gcp", "azure", "docker", "kubernetes", "terraform", "ansible", "jenkins",
    "ci/cd", "linux", "nginx", "kafka", "rabbitmq", "git",
    "pytorch", "tensorflow", "scikit-learn", "pandas", "numpy", "spark", "airflow",
    "langchain", "langgraph", "rag", "llm", "faiss", "chroma", "embeddings",
    "playwright", "selenium", "beautifulsoup", "scrapy", "pytest",
    "power bi", "tableau", "matplotlib", "seaborn",
})

# Ordered most-senior first so "senior software engineer" wins over "engineer".
_TITLE_PATTERNS = [
    r"(principal|staff|lead|senior|sr\.?|junior|jr\.?)?\s*"
    r"(software|backend|back-end|frontend|front-end|full[\s-]?stack|data|machine learning|ml|ai|devops|cloud|platform|site reliability|qa|test)?\s*"
    r"(engineer|developer|architect|scientist|analyst|programmer)",
]

_DEFAULT_TITLE = "Software Engineer"


@dataclass
class ResumeMatch:
    """A scored candidate from the library."""
    resume: Resume
    score: float           # 0.0-1.0, share of JD terms present in the resume
    matched: list[str]


# ── Extraction ────────────────────────────────────────────────────────────────

def extract_text(pdf_path: str) -> tuple[str, int]:
    """Return (plain text, page count). Text is truncated to a sane size."""
    reader = PdfReader(pdf_path)
    pages = [(p.extract_text() or "") for p in reader.pages]
    return "\n".join(pages)[:MAX_EXTRACTED_CHARS], len(reader.pages)


def skill_vocabulary(session: Optional[Session] = None,
                     user_id: Optional[int] = None) -> frozenset[str]:
    """Built-in vocabulary, plus every skill this account names in its profile.

    The extra terms only widen what can be recognised in an uploaded PDF, so a
    missing profile degrades to the built-in list rather than failing.
    """
    profile: dict = {}
    if session is not None and user_id is not None:
        from backend.app.services import profiles

        profile = profiles.get_profile(session, user_id)
    from_profile = {
        s.lower()
        for key in ("primary_skills", "secondary_skills")
        for s in (profile.get(key) or [])
        if isinstance(s, str)
    }
    return frozenset(_BASE_SKILL_VOCABULARY | from_profile)


def extract_skills(text: str, vocabulary: Optional[Iterable[str]] = None) -> list[str]:
    """Skills from `vocabulary` that appear in `text`, order-stable.

    Matching is word-boundary-aware so "R" doesn't match every word containing
    an r, and "go" doesn't match "google".
    """
    vocab = sorted(vocabulary if vocabulary is not None else skill_vocabulary())
    lowered = text.lower()
    found = []
    for skill in vocab:
        if re.search(rf"(?<![\w+#.]){re.escape(skill)}(?![\w+#])", lowered):
            found.append(skill)
    return found


def extract_title(text: str) -> str:
    """Best-guess role title from the top of the resume.

    Only the first ~40 lines are searched — that's where the headline sits, and
    scanning the whole document would match any job title in the experience
    section instead.
    """
    head = "\n".join(text.splitlines()[:40]).lower()
    for pattern in _TITLE_PATTERNS:
        m = re.search(pattern, head)
        if m:
            return " ".join(w for w in m.group(0).split() if w).title()
    return _DEFAULT_TITLE


def analyse(pdf_path: str) -> dict:
    """Extract everything the library stores about an uploaded PDF."""
    text, pages = extract_text(pdf_path)
    return {
        "extracted_text": text,
        "extracted_skills": json.dumps(extract_skills(text)),
        "extracted_title": extract_title(text),
        "page_count": pages,
    }


# ── Queries ───────────────────────────────────────────────────────────────────

def get_master(session: Session, user_id: Optional[int] = None) -> Optional[Resume]:
    """The caller's master resume. `is_master` is one-per-user, not one-per-app."""
    stmt = select(Resume).where(Resume.is_master == True)  # noqa: E712
    if user_id is not None:
        stmt = stmt.where(Resume.user_id == user_id)
    return session.exec(stmt).first()


def set_master(
    session: Session, resume_id: int, user_id: Optional[int] = None
) -> Optional[Resume]:
    """Promote one resume to master, demoting whichever held it.

    At most one *per user* — demoting must not reach into another account's
    library, so the demote query is scoped by owner.
    """
    target = session.get(Resume, resume_id)
    if target is None:
        return None
    owner = user_id if user_id is not None else target.user_id
    demote = select(Resume).where(Resume.is_master == True)  # noqa: E712
    if owner is not None:
        demote = demote.where(Resume.user_id == owner)
    for other in session.exec(demote).all():
        if other.resume_id != resume_id:
            other.is_master = False
            session.add(other)
    target.is_master = True
    session.add(target)
    session.commit()
    session.refresh(target)
    return target


def search_terms_from_master(
    session: Session, user_id: Optional[int] = None
) -> Optional[tuple[str, list[str]]]:
    """(title, skills) from the master resume, for portal search.

    Returns None when there is no usable master resume, so callers can fall
    back to master_profile.json or a typed keyword.
    """
    master = get_master(session, user_id)
    if master is None:
        return None
    skills = json.loads(master.extracted_skills) if master.extracted_skills else []
    title = master.extracted_title or _DEFAULT_TITLE
    return title, skills


def skills_of(resume: Resume) -> list[str]:
    if not resume.extracted_skills:
        return []
    try:
        return json.loads(resume.extracted_skills)
    except json.JSONDecodeError:
        return []


def rank_for_jd(
    session: Session, jd_terms: Iterable[str], user_id: Optional[int] = None
) -> list[ResumeMatch]:
    """Score every uploaded resume by how much of the JD it already covers.

    Ties break toward the master resume, then most recently uploaded — so with
    no signal at all the user still gets their preferred file.
    """
    terms = [t.lower() for t in jd_terms if t]
    stmt = select(Resume)
    if user_id is not None:
        stmt = stmt.where(Resume.user_id == user_id)
    resumes = session.exec(stmt).all()
    matches: list[ResumeMatch] = []

    for r in resumes:
        haystack = f"{' '.join(skills_of(r))}\n{r.extracted_text or ''}".lower()
        matched = [
            t for t in terms
            if re.search(rf"(?<![\w+#.]){re.escape(t)}(?![\w+#])", haystack)
        ]
        score = len(matched) / len(terms) if terms else 0.0
        matches.append(ResumeMatch(resume=r, score=score, matched=matched))

    matches.sort(
        key=lambda m: (m.score, m.resume.is_master, m.resume.uploaded_at), reverse=True
    )
    return matches


def best_for_jd(
    session: Session, jd_terms: Iterable[str], user_id: Optional[int] = None
) -> Optional[Resume]:
    """The uploaded resume that best matches a job description, if any exist."""
    ranked = rank_for_jd(session, jd_terms, user_id)
    if not ranked:
        return None
    best = ranked[0]
    logger.info(
        "Best uploaded resume for JD: '%s' (score %.0f%%, %d/%d terms)",
        best.resume.label or best.resume.filename,
        best.score * 100, len(best.matched), len(list(jd_terms)) or 0,
    )
    return best.resume


def delete_resume(session: Session, resume_id: int) -> bool:
    """Remove a library entry and its file. Returns False if it didn't exist."""
    resume = session.get(Resume, resume_id)
    if resume is None:
        return False
    path = Path(resume.file_path)
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        # The row must still go — a file we can't remove (locked, permissions)
        # shouldn't leave a dangling library entry pointing at it.
        logger.warning("Could not delete %s: %s", path, exc)
    session.delete(resume)
    session.commit()
    return True
