from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlmodel import Session, select

from backend.config import get_settings
from backend.db.database import engine
from backend.db.init_db import init
from backend.models.cold_email import ColdEmailLog, EmailStatus
from backend.models.run_log import RunLog, RunStatus, RunType
from backend.services.email_sender import DailyLimitReached, get_email_sender
from backend.services.llm_router import LLMTask, get_llm_router
from workflows.agents.resume_agent import generate_resume
from workflows.state import ColdEmailConfig, ColdEmailResult, EventCallback, FlowEvent
from workflows.types import ParsedJD

logger = logging.getLogger("flow.cold_email")

_CLASSIFY_SYSTEM = """\
You are classifying a company's tech stack from minimal context.
Return a single lowercase tech_stack label — one of: python, java, nodejs, dotnet, php, ruby, data, mobile, fullstack, other.
Return ONLY the label, nothing else.
"""

_EMAIL_BODY_TEMPLATE = """\
Hi {first_name},

I came across {company} and was impressed by your work. I'm Gaurav Sonkar, a Software Developer with hands-on experience building production-grade {tech_area} backends, REST APIs, and cloud infrastructure (AWS, Docker, CI/CD).

At my current role at Divyal Technology (client: Tracsis PLC, UK), I've:
• Designed 25+ production REST APIs used by web and Android clients
• Reduced latency by 30% through DB query tuning and connection pool optimization
• Containerized microservices with Docker, deployed to AWS EC2

I'd love to explore if there's a fit for a {role} role at {company}. I've attached my resume for your review.

Would you have 15 minutes for a quick call this week?

Best regards,
Gaurav Sonkar
+91-9131930576 | gauravsonkar.bhu@gmail.com
LinkedIn: linkedin.com/in/gauravsonkar953bhu
"""

_TECH_AREA_MAP = {
    "python": "Python/Django/FastAPI",
    "java": "Java/Spring Boot",
    "nodejs": "Node.js",
    "dotnet": ".NET/C#",
    "php": "PHP/Laravel",
    "ruby": "Ruby on Rails",
    "data": "Data Engineering/ML",
    "mobile": "Mobile (Android/iOS)",
    "fullstack": "Full-Stack",
    "other": "Backend",
}

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
    settings = get_settings()
    sender = get_email_sender()

    # Use pre-created RunLog ID (from API) or create a new one
    run_log_id: Optional[int] = existing_run_id
    if run_log_id is None:
        with Session(engine) as session:
            run_log = RunLog(
                run_type=RunType.COLD_EMAIL,
                started_at=datetime.now(timezone.utc),
                config_snapshot=f'{{"excel_path": "{config.excel_path}", "role": "{config.role}"}}',
            )
            session.add(run_log)
            session.commit()
            session.refresh(run_log)
            run_log_id = run_log.run_id
    logger.info("Cold email run %d started — file=%s", run_log_id, config.excel_path)

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
    for idx, row in enumerate(rows[:config.batch_size]):
        company = row.get("company", "Unknown Company")
        email = row.get("email", "")
        tech_stack = row.get("tech_stack", "other")
        role = row.get("role") or config.role or "Software Developer"
        first_name = row.get("first_name") or _extract_first_name(email)

        if not email:
            result.emails_skipped += 1
            continue

        # DB dedup check
        with Session(engine) as session:
            existing = session.exec(
                select(ColdEmailLog).where(
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
        if config.resume_per_tech_stack:
            if tech_stack not in resume_cache:
                try:
                    pseudo_jd = _tech_stack_to_parsed_jd(tech_stack, role)
                    pdf_path = await generate_resume(
                        job_id=-(idx + 1),  # negative IDs for cold email resumes
                        parsed_jd=pseudo_jd,
                    )
                    resume_cache[tech_stack] = pdf_path
                    logger.info("Generated resume for tech_stack=%s at %s", tech_stack, pdf_path)
                except Exception as exc:
                    logger.warning("Resume gen failed for %s: %s", tech_stack, exc)
                    result.errors.append(f"resume:{tech_stack}: {exc}")
            pdf_path = resume_cache.get(tech_stack)

        # Build email body
        tech_area = _TECH_AREA_MAP.get(tech_stack, "Backend")
        subject = config.subject_template.format(role=role, company=company)
        body = _EMAIL_BODY_TEMPLATE.format(
            first_name=first_name,
            company=company,
            tech_area=tech_area,
            role=role,
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
            result.emails_skipped += len(rows) - idx
            break
        except Exception as exc:
            logger.error("Send failed for %s: %s", email, exc)
            sent = False
            status = EmailStatus.FAILED
            result.errors.append(f"{email}: {exc}")

        # Persist to DB
        _upsert_email_log(company, email, tech_stack, role, pdf_path, status)

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


def _tech_stack_to_parsed_jd(tech_stack: str, role: str) -> ParsedJD:
    """Create a ParsedJD that emphasizes the given tech stack for resume tailoring."""
    skill_map: dict[str, list[str]] = {
        "python":   ["Python", "Django", "FastAPI", "REST API", "PostgreSQL", "Docker", "AWS"],
        "java":     ["Java", "Spring Boot", "REST API", "PostgreSQL", "Maven", "Docker"],
        "nodejs":   ["Node.js", "Express.js", "REST API", "MongoDB", "Docker", "AWS"],
        "dotnet":   [".NET", "C#", "ASP.NET", "REST API", "SQL Server", "Azure"],
        "php":      ["PHP", "Laravel", "REST API", "MySQL", "Docker"],
        "ruby":     ["Ruby", "Rails", "REST API", "PostgreSQL"],
        "data":     ["Python", "Pandas", "Scikit-learn", "SQL", "Power BI", "ETL Pipelines"],
        "mobile":   ["Python", "REST API", "Django", "PostgreSQL", "Android"],
        "fullstack":["Python", "Django", "React.js", "REST API", "PostgreSQL", "Docker", "AWS"],
        "other":    ["Python", "REST API", "Docker", "AWS", "PostgreSQL"],
    }
    skills = skill_map.get(tech_stack, skill_map["other"])
    return ParsedJD(
        required_skills=skills,
        nice_to_have=[],
        keywords=skills + ["CI/CD", "Git", "Microservices"],
        role_level="mid",
        raw={"required_skills": skills, "role_level": "mid"},
    )


def _extract_first_name(email: str) -> str:
    local = email.split("@")[0] if "@" in email else email
    name = local.replace(".", " ").replace("_", " ").split()[0]
    return name.capitalize() if name else "there"


def _upsert_email_log(
    company: str,
    email: str,
    tech_stack: str,
    role: str,
    resume_path: Optional[str],
    status: EmailStatus,
) -> None:
    with Session(engine) as session:
        existing = session.exec(
            select(ColdEmailLog).where(
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


async def _emit(callback: Optional[EventCallback], event: FlowEvent) -> None:
    if callback is not None:
        try:
            await callback(event)
        except Exception as exc:
            logger.debug("Event callback error: %s", exc)
