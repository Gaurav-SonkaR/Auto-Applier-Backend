from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlmodel import Session, select

from backend.app.database import engine
from backend.app.database import init
from backend.app.models.cold_email import ColdEmailLog, EmailStatus
from backend.app.models.run_log import RunLog, RunStatus, RunType
from backend.app.services import cold_email_composer as composer
from backend.app.services import profiles
from backend.app.services.email_sender import DailyLimitReached, get_email_sender
from backend.app.services.llm_client import LLMTask, get_llm_router
from backend.app.services.resume_generator import generate_resume
from backend.app.workflows.state import (
    ColdEmailConfig,
    ColdEmailResult,
    EventCallback,
    FlowEvent,
    emit_event as _emit,
)
from backend.app.workflows.types import ParsedJD

logger = logging.getLogger("flow.cold_email")

_CLASSIFY_SYSTEM = """\
You are classifying a company's tech stack from minimal context.
Return a single lowercase tech_stack label — one of: python, java, nodejs, dotnet, php, ruby, data, mobile, fullstack, other.
Return ONLY the label, nothing else.
"""

# Expected Excel columns (case-insensitive match)
_COL_ALIASES = {
    "company": ["company", "company_name", "organization"],
    "email": ["email", "contact_email", "email_address"],
    "role": ["role", "position", "job_title", "designation"],
    "tech_stack": ["tech_stack", "stack", "technology"],
    "first_name": ["first_name", "name", "contact_name", "contact"],
}


async def run_cold_email(
    config: ColdEmailConfig,
    on_event: Optional[EventCallback] = None,
    existing_run_id: Optional[int] = None,
) -> ColdEmailResult:
    """
    Run a cold email campaign:
    1. Read Excel/CSV contact list
    2. Classify tech stack per row (LLM, skips if column present)
    3. Generate one tailored resume per tech_stack group
    4. Send email to each contact (respects daily limits + DB dedup)
    """
    init()
    t0 = time.monotonic()
    result = ColdEmailResult()
    sender = get_email_sender()

    # Use pre-created RunLog ID (from API) or create a new one
    run_log_id: Optional[int] = existing_run_id
    if run_log_id is None:
        with Session(engine) as session:
            run_log = RunLog(
                user_id=config.user_id,
                run_type=RunType.COLD_EMAIL,
                started_at=datetime.now(timezone.utc),
                config_snapshot=f'{{"excel_path": "{config.excel_path}", "role": "{config.role}"}}',
            )
            session.add(run_log)
            session.commit()
            session.refresh(run_log)
            run_log_id = run_log.run_id
    logger.info("Cold email run %d started — file=%s", run_log_id, config.excel_path)

    # The sender's own details. Loaded once: it is the same person for every
    # row, and failing here is far better than sending 40 emails signed by
    # nobody.
    with Session(engine) as session:
        profile = profiles.get_profile(session, config.user_id)
    missing = composer.missing_requirements(profile)
    if missing:
        message = "Cannot send cold emails \u2014 your profile is missing " + ", ".join(missing)
        logger.error("Run %d: %s", run_log_id, message)
        result.errors.append(message)
        await _finalize_run(run_log_id, result, t0)
        return result

    # A link beats an attachment: a PDF from a stranger on first contact is the
    # loudest spam signal this email can carry. Only attach when the sender has
    # nowhere public to point at.
    resume_url = composer.sender_identity(profile)["resume_url"]
    attach_resume = not resume_url

    # 1. Read contact list
    try:
        rows = _read_contact_list(config.excel_path)
        result.total_rows = len(rows)
        logger.info("Run %d: loaded %d contacts", run_log_id, len(rows))
    except Exception as exc:
        logger.error("Failed to read contact list: %s", exc)
        result.errors.append(f"read: {exc}")
        await _finalize_run(run_log_id, result, t0)
        return result

    # 2. Classify missing tech_stacks
    rows = await _classify_tech_stacks(rows, config)

    # 3. Build one ParsedJD per tech_stack group for resume generation
    resume_cache: dict[str, str] = {}  # tech_stack → pdf_path

    # 4. Process each contact row
    batch = rows[:config.batch_size]
    for idx, row in enumerate(batch):
        email = (row.get("email") or "").strip()

        # Bail out before deriving anything from the row. A blank cell is the
        # most ordinary defect a spreadsheet has, and it must cost one skipped
        # contact, never the rest of the run.
        if not email:
            result.emails_skipped += 1
            continue

        company = row.get("company") or "Unknown Company"
        tech_stack = row.get("tech_stack") or "other"
        role = row.get("role") or config.role or "Software Developer"
        first_name = row.get("first_name") or _extract_first_name(email)

        # DB dedup check
        with Session(engine) as session:
            existing = session.exec(
                select(ColdEmailLog).where(
                    ColdEmailLog.user_id == config.user_id,
                    ColdEmailLog.company_name == company,
                    ColdEmailLog.contact_email == email,
                )
            ).first()
            if existing and existing.status == EmailStatus.SENT:
                logger.debug("Skip duplicate: %s <%s>", company, email)
                result.emails_skipped += 1
                continue

        # Generate resume for this tech_stack (once per group)
        pdf_path: Optional[str] = None
        if config.resume_per_tech_stack and attach_resume:
            if tech_stack not in resume_cache:
                try:
                    pseudo_jd = _tech_stack_to_parsed_jd(profile, tech_stack)
                    pdf_path = await generate_resume(
                        job_id=-(idx + 1),  # negative IDs for cold email resumes
                        parsed_jd=pseudo_jd,
                        # There is no Job row for a cold-email resume, so the
                        # owner has to be passed explicitly.
                        user_id=config.user_id,
                    )
                    resume_cache[tech_stack] = pdf_path
                    logger.info("Generated resume for tech_stack=%s at %s", tech_stack, pdf_path)
                except Exception as exc:
                    logger.warning("Resume gen failed for %s: %s", tech_stack, exc)
                    result.errors.append(f"resume:{tech_stack}: {exc}")
            pdf_path = resume_cache.get(tech_stack)

        # Build the email from the sender's own profile, with wording that
        # varies per company so a batch isn't 40 identical messages.
        tech_area = composer.tech_area_from_profile(profile, tech_stack)
        subject, body = composer.compose(
            profile,
            company=company,
            first_name=first_name,
            role=role,
            tech_area=tech_area,
            subject_template=config.subject_template,
            resume_attached=bool(pdf_path),
        )

        await _emit(on_event, FlowEvent(
            run_id=run_log_id, job_id=None, node="email",
            status="sending", message=f"Sending to {email}",
        ))

        # Send
        try:
            sent = await sender.send(
                to=email,
                subject=subject,
                body_text=body,
                attachment_path=pdf_path,
            )
            status = EmailStatus.SENT if sent else EmailStatus.FAILED
        except DailyLimitReached as exc:
            logger.warning("Daily limit reached: %s", exc)
            # Only the rest of *this batch* was skipped. Counting the whole
            # file reported 497 skipped on a 10-row batch of a 500-row list.
            result.emails_skipped += len(batch) - idx
            break
        except Exception as exc:
            logger.error("Send failed for %s: %s", email, exc)
            sent = False
            status = EmailStatus.FAILED
            result.errors.append(f"{email}: {exc}")

        # Persist to DB
        _upsert_email_log(company, email, tech_stack, role, pdf_path, status, config.user_id)


        if sent:
            result.emails_sent += 1
            await _emit(on_event, FlowEvent(
                run_id=run_log_id, job_id=None, node="email",
                status="sent", message=f"Sent to {email}",
            ))
        else:
            result.emails_failed += 1

    await _finalize_run(run_log_id, result, t0)
    logger.info(
        "Cold email run %d done — sent=%d failed=%d skipped=%d",
        run_log_id, result.emails_sent, result.emails_failed, result.emails_skipped,
    )
    return result


# ── Helpers ────────────────────────────────────────────────────────────────────

def _read_contact_list(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Contact list not found: {path}")
    if p.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(path, dtype=str)
    else:
        df = pd.read_csv(path, dtype=str)
    df.columns = [c.strip().lower() for c in df.columns]
    df = df.fillna("")

    # Normalize column names using aliases
    rename_map = {}
    for canonical, aliases in _COL_ALIASES.items():
        for col in df.columns:
            if col in aliases and col != canonical:
                rename_map[col] = canonical
    if rename_map:
        df = df.rename(columns=rename_map)

    return df.to_dict("records")


async def _classify_tech_stacks(rows: list[dict], config: ColdEmailConfig) -> list[dict]:
    router = get_llm_router()
    for row in rows:
        if row.get("tech_stack"):
            continue
        # Build context from company + role for classification
        context = f"Company: {row.get('company', '')}. Role: {row.get('role') or config.role or ''}."
        try:
            label = await router.complete(LLMTask.CLASSIFICATION, context, system=_CLASSIFY_SYSTEM)
            row["tech_stack"] = label.strip().lower()[:20]
        except Exception:
            row["tech_stack"] = "other"
    return rows


def _tech_stack_to_parsed_jd(profile: dict, tech_stack: str) -> ParsedJD:
    """A pseudo job description for tailoring the cold-email resume.

    Skills come from the sender's own profile, ordered so anything matching the
    target stack leads. The previous version returned a fixed list per stack,
    which told the resume generator to emphasise skills the person may never
    have had.
    """
    skills = composer.parsed_jd_skills(profile, tech_stack)
    return ParsedJD(
        required_skills=skills,
        nice_to_have=[],
        keywords=skills,
        role_level="mid",
        raw={"required_skills": skills, "role_level": "mid"},
    )


def _extract_first_name(email: str) -> str:
    """Best guess at a first name from an address, or a neutral greeting.

    The `else "there"` fallback used to be unreachable: `"".split()[0]` raises
    before it is ever reached, so a single blank email cell — the most ordinary
    defect a hand-maintained spreadsheet has — took down the entire campaign
    with an IndexError, leaving every later contact unmailed.
    """
    local = (email or "").split("@")[0]
    parts = local.replace(".", " ").replace("_", " ").replace("-", " ").split()
    return parts[0].capitalize() if parts else "there"


def _upsert_email_log(
    company: str,
    email: str,
    tech_stack: str,
    role: str,
    resume_path: Optional[str],
    status: EmailStatus,
    user_id: Optional[int] = None,
) -> None:
    with Session(engine) as session:
        existing = session.exec(
            select(ColdEmailLog).where(
                ColdEmailLog.user_id == user_id,
                ColdEmailLog.company_name == company,
                ColdEmailLog.contact_email == email,
            )
        ).first()
        if existing:
            existing.status = status
            existing.sent_at = datetime.now(timezone.utc) if status == EmailStatus.SENT else None
            existing.resume_path = resume_path
            session.add(existing)
        else:
            log = ColdEmailLog(
                user_id=user_id,
                company_name=company,
                contact_email=email,
                tech_stack=tech_stack,
                role=role,
                resume_path=resume_path,
                status=status,
                sent_at=datetime.now(timezone.utc) if status == EmailStatus.SENT else None,
            )
            session.add(log)
        session.commit()


async def _finalize_run(run_log_id: Optional[int], result: ColdEmailResult, t0: float) -> None:
    result.elapsed_seconds = round(time.monotonic() - t0, 2)
    if run_log_id is None:
        return
    with Session(engine) as session:
        run_log = session.get(RunLog, run_log_id)
        if run_log:
            run_log.status = RunStatus.COMPLETED
            run_log.completed_at = datetime.now(timezone.utc)
            run_log.jobs_applied = result.emails_sent
            run_log.jobs_failed = result.emails_failed
            session.add(run_log)
            session.commit()

