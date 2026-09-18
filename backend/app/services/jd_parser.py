from __future__ import annotations

import json
import logging
from contextlib import closing
from typing import Optional

from sqlmodel import Session

from backend.app.database import get_session
from backend.app.models.job import Job, JobStatus
from backend.app.services.llm_client import LLMTask, get_llm_router
from backend.app.workflows.types import ParsedJD

logger = logging.getLogger("agent.jd_parser")

SYSTEM_PROMPT = """\
You are an expert job description parser. Extract structured information from the job description.

Return ONLY valid JSON matching this exact schema:
{
  "required_skills": ["skill1", "skill2"],      // must-have technical skills
  "nice_to_have": ["skill3"],                    // preferred / bonus skills
  "keywords": ["ats_keyword1"],                  // ATS keywords (tech, frameworks, tools)
  "experience_years": 2,                         // minimum years of experience (integer or null)
  "role_level": "mid"                            // one of: junior / mid / senior / lead
}

Rules:
- Use exact skill names as written in the JD (e.g. "React.js" not "react")
- keywords should include all technical nouns worth including in a resume
- experience_years: extract from "X+ years", "X-Y years" etc. Use the minimum
- Do NOT include soft skills (communication, teamwork, etc.)
- Keep arrays compact — no duplicates
"""


async def parse_jd(
    jd_text: str,
    job_id: int,
    session: Optional[Session] = None,
) -> ParsedJD:
    """
    Parse a job description and update the Job row in SQLite.

    Retries up to 2 times on malformed JSON.
    """
    router = get_llm_router()
    prompt = f"Parse this job description:\n\n{jd_text[:6000]}"

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            raw: dict = await router.complete_json(
                LLMTask.JD_PARSING, prompt, system=SYSTEM_PROMPT
            )
            parsed = _build_parsed_jd(raw)
            break
        except (ValueError, KeyError) as exc:
            last_error = exc
            logger.warning("JD parse attempt %d failed: %s", attempt + 1, exc)
    else:
        raise RuntimeError(f"JD parsing failed after 3 attempts: {last_error}") from last_error

    await _update_job(job_id, parsed, session)
    return parsed


def _to_int(val) -> int | None:
    try:
        return int(val) if val is not None else None
    except (ValueError, TypeError):
        return None


def _build_parsed_jd(raw: dict) -> ParsedJD:
    def as_list(val) -> list[str]:
        if isinstance(val, list):
            return [str(v) for v in val if v]
        return []

    exp = raw.get("experience_years")
    role = raw.get("role_level", "mid")
    if role not in ("junior", "mid", "senior", "lead"):
        role = "mid"

    return ParsedJD(
        required_skills=as_list(raw.get("required_skills")),
        nice_to_have=as_list(raw.get("nice_to_have")),
        keywords=as_list(raw.get("keywords")),
        experience_years=_to_int(exp),
        role_level=role,
        raw=raw,
    )


async def _update_job(job_id: int, parsed: ParsedJD, provided_session: Optional[Session]) -> None:
    ctx = provided_session

    def _do_update(session: Session) -> None:
        job = session.get(Job, job_id)
        if job is None:
            logger.warning("Job %d not found — skipping status update", job_id)
            return
        job.parsed_jd = json.dumps(parsed.raw)
        job.status = JobStatus.PARSED
        from datetime import datetime, timezone
        job.updated_at = datetime.now(timezone.utc)
        session.add(job)
        session.commit()
        logger.info("Job %d → PARSED (required=%d keywords=%d)",
                    job_id, len(parsed.required_skills), len(parsed.keywords))

    if ctx is not None:
        _do_update(ctx)
    else:
        with closing(get_session()) as sessions:
            for session in sessions:
                _do_update(session)
