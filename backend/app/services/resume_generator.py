from __future__ import annotations

import asyncio
import json
import logging
from contextlib import closing
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlmodel import Session

from backend.app.config import get_settings
from backend.app.database import get_session
from backend.app.models.job import Job, JobStatus
from backend.app.services.latex_compiler import LaTeXCompileError, compile_resume_from_string
from backend.app.services.llm_client import LLMTask, get_llm_router
from backend.app.utils.file_naming import resume_filename
from backend.app.utils.validators import escape_latex
from backend.app.workflows.types import ParsedJD

logger = logging.getLogger("agent.resume")

def locked_strings(profile: dict) -> list[str]:
    """Proper nouns and dates the LLM must reproduce exactly, from the profile.

    This was a literal list of one person's name, employers and dates. It made
    the guard useless for every other account — their own name and employers
    were not in it, so nothing about them was checked — while the names it did
    contain belonged to someone else entirely.
    """
    personal = profile.get("personal") or {}
    education = profile.get("education") or {}

    values = [personal.get("name")]
    for role in profile.get("experience") or []:
        if isinstance(role, dict):
            values += [role.get("company"), role.get("client"),
                       role.get("start_date"), role.get("end_date")]
    values += [education.get("institution"),
               education.get("start_date"), education.get("end_date")]

    # De-duplicated, order-stable. "Present" is not a fact to protect.
    seen: dict[str, None] = {}
    for value in values:
        text = str(value or "").strip()
        if text and text.lower() != "present":
            seen.setdefault(text, None)
    return list(seen)

# Exact category names from the Skills section of templates/resume_template.tex.
# _inject_skills matches rows against these, so the two lists must stay in sync.
SKILL_CATEGORIES = [
    "Languages",
    "Backend",
    "AI / GenAI",
    "Frontend",
    "Databases",
    "Cloud & DevOps",
    "Automation",
    "ML / Data",
]

RESUME_SYSTEM_PROMPT = """\
You are an expert ATS resume writer. Your task is to tailor the candidate's resume for a specific job.

The candidate is whoever the profile below describes. Never assume a name, an
employer or a length of career that is not in it.

You will receive:
1. The candidate's master profile (JSON)
2. The parsed job description (required_skills, keywords, role_level, experience_years)
3. The current LaTeX resume template

Return ONLY valid JSON with these fields:
{
  "professional_summary": "...",
  "skills_row_order": ["Backend", "Languages", "AI / GenAI", ...],
  "secondary_skills_to_include": [],
  "experience_bullets": {
    "job_0": ["bullet1", "bullet2", "bullet3", "bullet4", "bullet5"],
    "job_1": ["bullet1", "bullet2"]
  },
  "project_bullets": {
    "project_0": ["b1", "b2", "b3"],
    "project_1": ["b1", "b2", "b3"],
    "project_2": ["b1", "b2", "b3"],
    "project_3": ["b1", "b2", "b3"]
  },
  "ats_keywords_used": ["kw1", "kw2"]
}

FIELD RULES:
- professional_summary: max 70 words, no "I"/"my", must include ≥3 JD keywords, plain text (no LaTeX)
- skills_row_order: reorder the 8 skill category rows so most JD-relevant category appears FIRST.
  Use EXACT names from this list: Languages, Backend, AI / GenAI, Frontend, Databases, Cloud & DevOps, Automation, ML / Data
- secondary_skills_to_include: ONLY include if skill appears verbatim in required_skills AND is not already in the template
- experience_bullets: max 25 words each, start with past-tense action verb, quantify where possible
- project_bullets: max 22 words each, technical and specific
- NEVER change: candidate name, company names, dates, college name, degree
- NEVER state a total years-of-experience figure ("3+ years", "5 years of
  experience"). The dates already on the resume say it, and `experience_years`
  in the job description is what the *employer wants*, not what the candidate
  has — echoing it back invents a claim. Describe the work, not its duration.
- Rewrite phrasing, never facts. Every metric, employer and technology must
  come from the profile; do not add, round up or invent one.
- Return ONLY JSON — no explanation, no markdown fences
"""


def _owner_profile(
    job_id: int, session: Optional[Session], user_id: Optional[int] = None
) -> dict:
    """Master profile of the account this resume is for.

    `user_id` wins when given. Cold email needs it: those resumes have no Job
    row at all (they are keyed by a negative pseudo id), so there is nothing to
    look an owner up from.

    Raises rather than falling back to a blank profile: a resume with no name
    or contact details is worse than a failed run, and the failure message can
    tell the user exactly what to fix.
    """
    from backend.app.models.job import Job
    from backend.app.services import profiles

    def _load(s: Session) -> dict:
        if user_id is not None:
            return profiles.get_profile(s, user_id)
        job = s.get(Job, job_id)
        if job is None:
            raise ValueError(f"Job {job_id} not found")
        return profiles.get_profile(s, job.user_id)

    profile = _load(session) if session is not None else _load_with_new_session(_load)

    if not profiles.is_complete(profile):
        raise ValueError(
            "Your profile is missing a name, email or work history \u2014 open "
            "Settings \u2192 Profile and fill it in before generating resumes."
        )
    return profile


def _load_with_new_session(fn):
    from backend.app.database import engine as _engine

    with Session(_engine) as s:
        return fn(s)


async def generate_resume(
    job_id: int,
    parsed_jd: ParsedJD,
    session: Optional[Session] = None,
    user_id: Optional[int] = None,
) -> str:
    """Generate an ATS-optimized PDF resume. Returns absolute path to the PDF."""
    settings = get_settings()
    router = get_llm_router()

    # The job's owner, not a shared file: this profile supplies the name,
    # email and phone printed on the PDF, so reading one global file would put
    # one account's contact details on another account's resume.
    profile = _owner_profile(job_id, session, user_id)
    base_latex = _load_template(settings.resume_template_path)

    prompt = _build_prompt(profile, parsed_jd)
    # FR-004 filename: Candidate_YYYY-MM-DD_{title}_{company}_{hash}.pdf
    url, title, company = _job_identity(job_id, session)
    # Named after the profile's owner. Left to the default, every account's
    # resume came out as Gaurav_Sonkar_*.
    candidate = str((profile.get("personal") or {}).get("name") or "").strip()
    output_path = str(
        Path(settings.resumes_dir)
        / resume_filename(job_id, url, title, company, candidate=candidate or "candidate")
    )

    last_error: Exception | None = None
    for attempt in range(settings.resume_max_retries):
        try:
            result_json = await router.complete_json(
                LLMTask.RESUME_GENERATION, prompt, system=RESUME_SYSTEM_PROMPT
            )
            _validate_locked_fields(result_json, locked_strings(profile))

            latex = _inject_into_template(base_latex, result_json)
            # Score the content that will actually be compiled, not the
            # model's `ats_keywords_used` self-report (which is not rendered).
            scoring_input = {"rendered_resume": latex}
            ats_score = _compute_ats_score(scoring_input, parsed_jd)
            logger.info("Job %d: ATS score=%d%% (attempt %d)", job_id, ats_score, attempt + 1)

            if ats_score < settings.ats_min_score and attempt < settings.resume_max_retries - 1:
                # Built from the same set the score is computed over. Using only
                # `parsed_jd.keywords` meant a retry triggered by missing
                # *required_skills* was handed an empty hint list, and the next
                # attempt had no more to go on than the first.
                missed = _missing_terms(scoring_input, parsed_jd)
                logger.warning(
                    "ATS score %d%% < %d%% — retrying. Missed keywords: %s",
                    ats_score, settings.ats_min_score, missed,
                )
                prompt = _build_prompt(profile, parsed_jd, missed_keywords=missed)
                continue

            if ats_score < settings.ats_min_score:
                raise ValueError(
                    f"Rendered resume scored {ats_score}% ATS keyword coverage; "
                    f"minimum is {settings.ats_min_score}%"
                )

            # pdflatex takes 1-5s per compile — run in a worker thread so the
            # event loop (and every dashboard request) isn't stalled meanwhile.
            pdf_path = await asyncio.to_thread(compile_resume_from_string, latex, output_path)
            await _update_job(job_id, pdf_path, ats_score, session)
            return pdf_path

        except (LaTeXCompileError, ValueError, KeyError) as exc:
            last_error = exc
            logger.warning("Resume attempt %d failed: %s", attempt + 1, exc)

    raise RuntimeError(
        f"Resume generation failed after {settings.resume_max_retries} attempts: {last_error}"
    ) from last_error


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_template(path: str) -> str:
    """Read the LaTeX template, failing with an actionable message.

    A bare FileNotFoundError here reads as an internal crash on the dashboard;
    resume generation is dead in the water without this file, so say which
    setting points at it.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"LaTeX resume template not found at '{path}'. "
            f"Set RESUME_TEMPLATE_PATH in .env to a readable .tex file "
            f"(default: templates/resume_template.tex)."
        )
    return p.read_text(encoding="utf-8")


def _job_identity(job_id: int, provided_session: Optional[Session]) -> tuple[str, str, str]:
    """Return (url, title, company) for the FR-004 filename.

    Falls back to placeholders rather than raising: a missing row must not sink
    a resume that has already been generated — sanitize() supplies its own
    defaults for blank parts.
    """
    def _read(session: Session) -> tuple[str, str, str]:
        job = session.get(Job, job_id)
        if job is None:
            logger.warning("Job %d not found — using placeholder resume filename", job_id)
            return "", "", ""
        return job.url or "", job.title or "", job.company or ""

    if provided_session is not None:
        return _read(provided_session)
    with closing(get_session()) as sessions:
        for session in sessions:
            return _read(session)
    return "", "", ""


def _get_skills(profile: dict) -> dict:
    return {
        "primary_skills": profile.get("primary_skills", []),
        "secondary_skills": profile.get("secondary_skills", []),
    }


def _get_experience(profile: dict) -> list[dict]:
    return [
        {"title": e.get("title"), "bullets": e.get("bullets", [])}
        for e in profile.get("experience", [])
    ]


def _get_projects(profile: dict) -> list[dict]:
    return [
        {"name": p.get("name"), "tech_stack": p.get("tech_stack", []), "bullets": p.get("bullets", [])}
        for p in profile.get("projects", [])
    ]


def _build_prompt(
    profile: dict,
    parsed_jd: ParsedJD,
    missed_keywords: Optional[list[str]] = None,
) -> str:
    """
    Sends only the profile keys the LLM actually rewrites — personal/education/
    achievements/llm_rules are either locked (never generated) or already covered
    by RESUME_SYSTEM_PROMPT, so omitting them keeps the prompt smaller, avoids the
    old truncation cutting off projects, and keeps PII (email/phone) out of the
    third-party LLM call. The current LaTeX isn't needed either: the profile already
    carries the canonical bullets, and skill category names are listed verbatim in
    RESUME_SYSTEM_PROMPT.
    """
    resume_relevant = {
        "skills": _get_skills(profile),
        "experience": _get_experience(profile),
        "projects": _get_projects(profile),
    }
    # `experience_years` is dropped: it states how much experience the employer
    # is asking for, and passing it to a resume writer is an invitation to claim
    # it. Everything else about the JD is what the tailoring is *for*.
    jd = {k: v for k, v in (parsed_jd.raw or {}).items() if k != "experience_years"}
    parts = [
        "=== CANDIDATE SKILLS / EXPERIENCE / PROJECTS ===",
        json.dumps(resume_relevant, indent=2),
        "\n=== PARSED JOB DESCRIPTION ===",
        json.dumps(jd, indent=2),
    ]
    if missed_keywords:
        parts.append(
            f"\n=== KEYWORDS MISSING FROM PREVIOUS ATTEMPT — must include all: {missed_keywords}"
        )
    return "\n".join(parts)


_MONTH_YEAR_RE = re.compile(r"^[A-Za-z]+ \d{4}$")


def _validate_locked_fields(result: dict, locked: list[str]) -> None:
    """
    Guards against the LLM restating a locked field (name/company/college) in an
    altered form inside generated free text (e.g. "Divyal Tech" instead of the full
    legal name). Previously only checked the company name — now checks every
    proper-noun entry in LOCKED_STRINGS. Date strings ("Jun 2025") are skipped: their
    anchor word ("Jun") is common enough in generated prose to false-positive.
    """
    text = json.dumps(result)
    for value in locked:
        if _MONTH_YEAR_RE.match(value):
            continue
        anchor = value.split()[0]
        if anchor in text and value not in text:
            raise ValueError(
                f"LLM altered locked field '{value}' (found '{anchor}' without the full "
                f"string). Raw text snippet: {text[:300]}"
            )


def _missing_terms(result: dict, parsed_jd: ParsedJD) -> list[str]:
    """JD terms absent from the generated resume, scored-set and all.

    Shared with `_compute_ats_score` so the number and the explanation can
    never disagree: what the score penalises is exactly what the retry is told
    to add.
    """
    rendered_resume = result.get("rendered_resume")
    if isinstance(rendered_resume, str):
        result_text = rendered_resume.lower()
    else:
        # Do not let the model's non-rendered ats_keywords_used field improve
        # its own score. Only content that may actually enter the template is
        # considered here.
        visible = {
            key: result.get(key)
            for key in (
                "professional_summary", "experience_bullets",
                "project_bullets", "secondary_skills_to_include",
            )
        }
        result_text = json.dumps(visible).lower()
    return [term for term in parsed_jd.all_keywords() if term not in result_text]


def _compute_ats_score(result: dict, parsed_jd: ParsedJD) -> int:
    """Score based on all JD terms: required_skills + nice_to_have + keywords."""
    all_terms = parsed_jd.all_keywords()
    if not all_terms:
        return 100
    missing = len(_missing_terms(result, parsed_jd))
    return int((len(all_terms) - missing) / len(all_terms) * 100)


def _inject_into_template(base_latex: str, result: dict) -> str:
    """
    Inject LLM-generated content into the LaTeX template.

    Three injection points match the actual structure of templates/resume_template.tex:
      1. Professional Summary -- the {\\small ...} block under \\section{Professional Summary}
      2. Skills -- reorder the 8 categorised rows by JD relevance
      3. Experience + Project bullets -- replace \\begin{itemize}...\\end{itemize} blocks
    """
    latex = base_latex

    # 1. Professional Summary
    summary = result.get("professional_summary", "").strip()
    if summary:
        latex = _inject_summary(latex, summary)

    # 2. Skills — reorder rows so most JD-relevant category appears first
    row_order = result.get("skills_row_order", [])
    secondary = result.get("secondary_skills_to_include", [])
    if row_order or secondary:
        latex = _inject_skills(latex, row_order, secondary)

    # 3. Experience + Project bullets
    latex = _replace_itemize_blocks(
        latex,
        result.get("experience_bullets", {}),
        result.get("project_bullets", {}),
    )

    return latex


def _inject_summary(latex: str, summary: str) -> str:
    """
    Replace content inside the {\\small ...} block under \\section{Professional Summary}.

    Template structure:
        \\section{Professional Summary}
        {\\small
        <content to replace>
        }
    """
    pattern = re.compile(
        r'(\\section\{Professional Summary\}\n\{\\small\n)(.*?)(\n\})',
        re.DOTALL,
    )
    replaced, count = pattern.subn(
        lambda m: m.group(1) + escape_latex(summary) + m.group(3),
        latex,
    )
    if count == 0:
        logger.warning("Professional Summary section not found in template — skipping injection")
    return replaced


def _inject_skills(latex: str, row_order: list[str], secondary: list[str]) -> str:
    """
    Reorder the 8 skill category rows inside the {\\small ...} block under \\section{Skills}.

    Each row has the format:
        \\noindent\\textbf{Category:}\\ skill1, skill2, ...\\\\
    The last row has no trailing \\\\.

    row_order: list of category names (from SKILL_CATEGORIES) in priority order.
    secondary: extra skills to append to the most relevant row.
    """
    pattern = re.compile(
        r'(\\section\{Skills\}\n\{\\small\n)(.*?)(\n\})',
        re.DOTALL,
    )
    match = pattern.search(latex)
    if not match:
        logger.warning("Skills section not found in template — skipping injection")
        return latex

    skills_block = match.group(2)

    # Parse rows — each line that starts with \noindent\textbf is one skill row
    raw_lines = [l for l in skills_block.split('\n') if l.strip()]
    if not raw_lines:
        return latex

    # Strip trailing \\ from all but preserve — we'll re-add after reorder
    def strip_trailing_bs(line: str) -> str:
        s = line.rstrip()
        if s.endswith(r'\\'):
            return s[:-2].rstrip()
        return s

    rows_clean = [strip_trailing_bs(l) for l in raw_lines]

    # Score each row by where its category appears in row_order.
    #
    # Both sides are stripped of backslashes before comparing: the template
    # stores "Cloud \& DevOps" (LaTeX-escaped) while the LLM returns the plain
    # "Cloud & DevOps" from SKILL_CATEGORIES. Without this, every category
    # containing an escaped character silently failed to match and got sorted
    # to the bottom regardless of how relevant the job description made it.
    def _norm(s: str) -> str:
        return s.replace("\\", "").lower()

    def row_priority(row: str) -> int:
        row_norm = _norm(row)
        for i, cat in enumerate(row_order):
            if _norm(cat) in row_norm:
                return i  # lower index = higher priority
        return len(row_order)  # unrecognised → send to end

    rows_sorted = sorted(rows_clean, key=row_priority)

    # Append secondary skills to the highest-priority row (first row after sort)
    if secondary and rows_sorted:
        extra = ", ".join(escape_latex(s) for s in secondary)
        rows_sorted[0] = rows_sorted[0] + ", " + extra

    # Reconstruct: every row except the last gets \\, last row doesn't
    result_lines = []
    for i, row in enumerate(rows_sorted):
        if i < len(rows_sorted) - 1:
            result_lines.append(row + r'\\')
        else:
            result_lines.append(row)

    new_skills_block = '\n'.join(result_lines)
    return pattern.sub(
        lambda m: m.group(1) + new_skills_block + m.group(3),
        latex,
    )


def _replace_itemize_blocks(latex: str, exp_bullets: dict, proj_bullets: dict) -> str:
    """
    Replace \\begin{itemize}...\\end{itemize} blocks with LLM-generated bullets.

    Block order in templates/resume_template.tex:
        block 0 -- job_0  (Software Developer, current role -- 5 bullets)
        block 1 -- job_1  (Software Developer Intern -- 2 bullets)
        block 2 -- project_0  (AI Research Agentic System)
        block 3 -- project_1  (Enterprise E-Commerce Platform)
        block 4 -- project_2  (Online Fraud Detection System)
        block 5 -- project_3  (Automated Web Scraping)

    Any block with no matching bullets key is left unchanged.
    """
    ITEM_PATTERN = re.compile(
        r'(\\begin\{itemize\})(.*?)(\\end\{itemize\})',
        re.DOTALL,
    )
    blocks = list(ITEM_PATTERN.finditer(latex))
    if not blocks:
        return latex

    replacements: list[tuple[int, int, str]] = []

    for idx, match in enumerate(blocks):
        if idx < 2:
            key = f"job_{idx}"
            new_bullets = exp_bullets.get(key)
        elif idx < 6:
            key = f"project_{idx - 2}"
            new_bullets = proj_bullets.get(key)
        else:
            continue  # extra blocks (e.g. in header) — leave untouched

        if not new_bullets:
            continue

        items_text = "\n".join(f"  \\item {escape_latex(b)}" for b in new_bullets)
        replacement = f"\\begin{{itemize}}\n{items_text}\n\\end{{itemize}}"
        replacements.append((match.start(), match.end(), replacement))

    # Apply in reverse order so earlier positions remain valid
    for start, end, replacement in reversed(replacements):
        latex = latex[:start] + replacement + latex[end:]
    return latex


def _store_resume(job: Job, pdf_path: str) -> Optional[str]:
    """Copy the finished PDF to the configured store; None if it isn't stored.

    A failed upload does not fail the resume. The PDF exists locally and the
    download still works from there — losing a finished resume over an S3
    outage would be the worse outcome. It is logged loudly so it is noticed.
    """
    from backend.app.services.storage import get_resume_store, resume_key

    store = get_resume_store()
    try:
        return store.put(pdf_path, resume_key(job.user_id, job.job_id, Path(pdf_path).name))
    except Exception as exc:
        logger.error("Job %d: resume kept locally, %s upload failed: %s",
                     job.job_id, store.name, exc)
        return None


async def _update_job(
    job_id: int, pdf_path: str, ats_score: int, provided_session: Optional[Session]
) -> None:
    def _do_update(session: Session) -> None:
        job = session.get(Job, job_id)
        if job is None:
            return
        job.resume_path = pdf_path
        job.resume_storage_ref = _store_resume(job, pdf_path)
        # ats_score was computed on every attempt but never stored, so the
        # column the dashboard and JobResponse both read stayed NULL forever.
        job.ats_score = ats_score
        job.status = JobStatus.RESUME_READY
        job.updated_at = datetime.now(timezone.utc)
        session.add(job)
        session.commit()
        logger.info("Job %d → RESUME_READY (ATS %d%%) at %s", job_id, ats_score, pdf_path)

    if provided_session is not None:
        _do_update(provided_session)
    else:
        with closing(get_session()) as sessions:
            for session in sessions:
                _do_update(session)
