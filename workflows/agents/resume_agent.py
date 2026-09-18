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

from backend.config import get_settings
from backend.db.database import get_session
from backend.models.job import Job, JobStatus
from backend.services.latex_compiler import LaTeXCompileError, compile_resume_from_string
from backend.services.llm_router import LLMTask, get_llm_router
from workflows.types import ParsedJD

logger = logging.getLogger("agent.resume")

# ── Locked fields that LLM must never alter ───────────────────────────────────
LOCKED_STRINGS = [
    "Gaurav Sonkar",
    "Divyal Technology Pvt. Ltd.",
    "Tracsis PLC",
    "Banaras Hindu University",
    "Jun 2025",
    "Feb 2025",
    "May 2025",
]

# Exact category names from the Skills section of gaurav_resume_final.tex
# Used by _inject_skills to reorder rows correctly.
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
You are an expert ATS resume writer. Your task is to tailor Gaurav Sonkar's resume for a specific job.

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
- Return ONLY JSON — no explanation, no markdown fences
"""


async def generate_resume(
    job_id: int,
    parsed_jd: ParsedJD,
    session: Optional[Session] = None,
) -> str:
    """Generate an ATS-optimized PDF resume. Returns absolute path to the PDF."""
    settings = get_settings()
    router = get_llm_router()

    profile = _load_profile(settings.master_profile_path)
    base_latex = Path(settings.resume_template_path).read_text(encoding="utf-8")

    prompt = _build_prompt(profile, parsed_jd)
    output_path = str(Path(settings.resumes_dir) / f"resume_job_{job_id}.pdf")

    last_error: Exception | None = None
    for attempt in range(settings.resume_max_retries):
        try:
            result_json = await router.complete_json(
                LLMTask.RESUME_GENERATION, prompt, system=RESUME_SYSTEM_PROMPT
            )
            _validate_locked_fields(result_json)

            ats_score = _compute_ats_score(result_json, parsed_jd)
            logger.info("Job %d: ATS score=%d%% (attempt %d)", job_id, ats_score, attempt + 1)

            if ats_score < settings.ats_min_score and attempt < settings.resume_max_retries - 1:
                missed = [
                    kw for kw in parsed_jd.keywords
                    if kw.lower() not in json.dumps(result_json).lower()
                ]
                logger.warning(
                    "ATS score %d%% < %d%% — retrying. Missed keywords: %s",
                    ats_score, settings.ats_min_score, missed,
                )
                prompt = _build_prompt(profile, parsed_jd, missed_keywords=missed)
                continue

            latex = _inject_into_template(base_latex, result_json)
            # pdflatex takes 1-5s per compile — run in a worker thread so the
            # event loop (and every dashboard request) isn't stalled meanwhile.
            pdf_path = await asyncio.to_thread(compile_resume_from_string, latex, output_path)
            await _update_job(job_id, pdf_path, session)
            return pdf_path

        except (LaTeXCompileError, ValueError, KeyError) as exc:
            last_error = exc
            logger.warning("Resume attempt %d failed: %s", attempt + 1, exc)

    raise RuntimeError(
        f"Resume generation failed after {settings.resume_max_retries} attempts: {last_error}"
    ) from last_error


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_profile(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"master_profile.json not found at {path}")
    return json.loads(p.read_text(encoding="utf-8"))


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
    parts = [
        "=== CANDIDATE SKILLS / EXPERIENCE / PROJECTS ===",
        json.dumps(resume_relevant, indent=2),
        "\n=== PARSED JOB DESCRIPTION ===",
        json.dumps(parsed_jd.raw, indent=2),
    ]
    if missed_keywords:
        parts.append(
            f"\n=== KEYWORDS MISSING FROM PREVIOUS ATTEMPT — must include all: {missed_keywords}"
        )
    return "\n".join(parts)


_MONTH_YEAR_RE = re.compile(r"^[A-Za-z]+ \d{4}$")


def _validate_locked_fields(result: dict) -> None:
    """
    Guards against the LLM restating a locked field (name/company/college) in an
    altered form inside generated free text (e.g. "Divyal Tech" instead of the full
    legal name). Previously only checked the company name — now checks every
    proper-noun entry in LOCKED_STRINGS. Date strings ("Jun 2025") are skipped: their
    anchor word ("Jun") is common enough in generated prose to false-positive.
    """
    text = json.dumps(result)
    for locked in LOCKED_STRINGS:
        if _MONTH_YEAR_RE.match(locked):
            continue
        anchor = locked.split()[0]
        if anchor in text and locked not in text:
            raise ValueError(
                f"LLM altered locked field '{locked}' (found '{anchor}' without the full "
                f"string). Raw text snippet: {text[:300]}"
            )


def _compute_ats_score(result: dict, parsed_jd: ParsedJD) -> int:
    """Score based on all JD terms: required_skills + nice_to_have + keywords."""
    all_terms = parsed_jd.all_keywords()
    if not all_terms:
        return 100
    result_text = json.dumps(result).lower()
    matched = sum(1 for term in all_terms if term.lower() in result_text)
    return int(matched / len(all_terms) * 100)


def _inject_into_template(base_latex: str, result: dict) -> str:
    """
    Inject LLM-generated content into the LaTeX template.

    Three injection points match the actual structure of gaurav_resume_final.tex:
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
        lambda m: m.group(1) + _escape_latex(summary) + m.group(3),
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

    # Score each row: count how many row_order entries appear in the row text
    def row_priority(row: str) -> int:
        row_lower = row.lower()
        for i, cat in enumerate(row_order):
            if cat.lower() in row_lower:
                return i  # lower index = higher priority
        return len(row_order)  # unrecognised → send to end

    rows_sorted = sorted(rows_clean, key=row_priority)

    # Append secondary skills to the highest-priority row (first row after sort)
    if secondary and rows_sorted:
        extra = ", ".join(_escape_latex(s) for s in secondary)
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

    Block order in gaurav_resume_final.tex:
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

        items_text = "\n".join(f"  \\item {_escape_latex(b)}" for b in new_bullets)
        replacement = f"\\begin{{itemize}}\n{items_text}\n\\end{{itemize}}"
        replacements.append((match.start(), match.end(), replacement))

    # Apply in reverse order so earlier positions remain valid
    for start, end, replacement in reversed(replacements):
        latex = latex[:start] + replacement + latex[end:]
    return latex


# LaTeX commands with file I/O or macro-programming power. If a job description
# prompt-injects the LLM into reproducing one of these in generated resume
# content, pdflatex would execute it on compile — \input/\include can read
# arbitrary local files into the resulting PDF with no -shell-escape required.
# Neutralized (not stripped) so the rest of the sentence still renders.
_DANGEROUS_LATEX_COMMANDS = re.compile(
    r'\\(input|include|write(?:18)?|immediate|openout|closeout'
    r'|catcode|csname|endcsname|read|directlua|InputIfFileExists'
    r'|usepackage|def|let|expandafter|noexpand)\b',
    re.IGNORECASE,
)


def _neutralize_dangerous_commands(text: str) -> str:
    return _DANGEROUS_LATEX_COMMANDS.sub(
        lambda m: m.group(0).replace("\\", "\\textbackslash{}"), text
    )


def _escape_latex(text: str) -> str:
    """
    Escape characters that break LaTeX compilation.
    Does NOT escape backslash or braces so embedded formatting commands
    (e.g. \\textbf{...}) survive — but file-I/O/macro commands are neutralized
    first (see _DANGEROUS_LATEX_COMMANDS) since this text ultimately derives
    from LLM output that processes untrusted, externally-scraped JD text.
    """
    text = _neutralize_dangerous_commands(text)
    replacements = [
        ("&",  "\\&"),
        ("%",  "\\%"),
        ("#",  "\\#"),
        ("$",  "\\$"),
        ("_",  "\\_"),
        ("^",  "\\^{}"),
        ("~",  "\\textasciitilde{}"),
    ]
    for char, escaped in replacements:
        text = text.replace(char, escaped)
    return text


async def _update_job(job_id: int, pdf_path: str, provided_session: Optional[Session]) -> None:
    def _do_update(session: Session) -> None:
        job = session.get(Job, job_id)
        if job is None:
            return
        job.resume_path = pdf_path
        job.status = JobStatus.RESUME_READY
        job.updated_at = datetime.now(timezone.utc)
        session.add(job)
        session.commit()
        logger.info("Job %d → RESUME_READY at %s", job_id, pdf_path)

    if provided_session is not None:
        _do_update(provided_session)
    else:
        with closing(get_session()) as sessions:
            for session in sessions:
                _do_update(session)
