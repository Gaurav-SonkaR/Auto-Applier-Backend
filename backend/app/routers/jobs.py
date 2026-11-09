from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlmodel import Session, func, select

from backend.app.utils.file_serving import resolve_contained_path
from backend.app.schemas import (
    JobListResponse,
    JobResponse,
    JobStageEventResponse,
    JobTimelineResponse,
    SetOutcomeRequest,
    SetStatusRequest,
)
from backend.app.config import get_settings
from backend.app.database import engine
from backend.app.models.application import Application
from backend.app.models.job import Job, JobStatus
from backend.app.models.job_stage_event import JobStageEvent
from backend.app.models.user import User
from backend.app.routers.auth import require_auth

logger = logging.getLogger("api.jobs")

jobs_router = APIRouter(prefix="/api/jobs", tags=["jobs"])

# Statuses eligible for retry/resume — includes both true failures and every
# "needs a human" wait-state. This one endpoint doubles as "resume automation
# after manual intervention" per the human-queue workflow.
_RETRYABLE_STATUSES = (
    JobStatus.FAILED, JobStatus.FLAGGED,
    JobStatus.WAITING_CAPTCHA, JobStatus.WAITING_LOGIN, JobStatus.WAITING_OTP,
    JobStatus.NEED_HUMAN_ACTION,
)


def _owned_job(session: Session, job_id: int, user: User) -> Job:
    """Fetch a job the caller owns, or 404.

    Deliberately 404 and not 403 for someone else's job — a 403 would confirm
    that job id exists, letting anyone enumerate the whole table by id.
    """
    job = session.get(Job, job_id)
    if job is None or job.user_id != user.user_id:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return job


def _to_response(job: Job) -> JobResponse:
    return JobResponse(
        job_id=job.job_id,
        run_id=job.run_id,
        url=job.url,
        title=job.title,
        company=job.company,
        portal=job.portal,
        status=job.status.value,
        fail_reason=job.fail_reason,
        error_message=job.fail_reason,
        resume_path=job.resume_path,
        retry_count=job.retry_count,
        ats_score=job.ats_score,
        redirect_url=job.redirect_url,
        screenshot_path=job.screenshot_path,
        outcome=job.outcome,
        outcome_updated_at=job.outcome_updated_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@jobs_router.get("/flagged", response_model=JobListResponse)
def list_flagged_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_auth),
) -> JobListResponse:
    """List all legacy FLAGGED jobs. Must be before /{id} route.

    Superseded by GET /api/dashboard/human-queue, which covers this status plus
    the newer granular WAITING_*/NEED_HUMAN_ACTION statuses — kept for backward
    compatibility with any existing callers.
    """
    with Session(engine) as session:
        items = session.exec(
            select(Job)
            .where(Job.status == JobStatus.FLAGGED, Job.user_id == user.user_id)
            .order_by(Job.updated_at.desc())  # type: ignore[attr-defined]
            .offset(offset)
            .limit(limit)
        ).all()
        total = session.exec(
            select(func.count()).select_from(Job)
            .where(Job.status == JobStatus.FLAGGED, Job.user_id == user.user_id)
        ).one()
    return JobListResponse(items=[_to_response(j) for j in items], total=total)


@jobs_router.get("/", response_model=JobListResponse)
def list_jobs(
    status: Optional[str] = Query(default=None),
    portal: Optional[str] = Query(default=None),
    source: Optional[str] = Query(default=None, description="portal or career_site"),
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_auth),
) -> JobListResponse:
    """List jobs with optional status/portal/source filters."""
    # Owner filter first and unconditionally — every other filter is optional,
    # this one never is.
    filters = [Job.user_id == user.user_id]
    if status:
        try:
            filters.append(Job.status == JobStatus(status.upper()))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid status: {status}")
    if portal:
        filters.append(Job.portal == portal.lower())
    if source:
        if source.lower() == "career_site":
            filters.append(Job.portal == "web")
        elif source.lower() == "portal":
            filters.append(Job.portal != "web")
        else:
            raise HTTPException(status_code=422, detail="source must be 'portal' or 'career_site'")

    with Session(engine) as session:
        total = session.exec(
            select(func.count()).select_from(Job).where(*filters)
        ).one()
        items = session.exec(
            select(Job)
            .where(*filters)
            .order_by(Job.updated_at.desc())  # type: ignore[attr-defined]
            .offset(offset)
            .limit(limit)
        ).all()

    return JobListResponse(items=[_to_response(j) for j in items], total=total)


@jobs_router.get("/{job_id}", response_model=JobResponse)
def get_job(job_id: int, user: User = Depends(require_auth)) -> JobResponse:
    with Session(engine) as session:
        return _to_response(_owned_job(session, job_id, user))


@jobs_router.get("/{job_id}/timeline", response_model=JobTimelineResponse)
def get_job_timeline(job_id: int, user: User = Depends(require_auth)) -> JobTimelineResponse:
    """Per-job stage-event history — the observability timeline for the job detail page."""
    with Session(engine) as session:
        job = _owned_job(session, job_id, user)
        events = session.exec(
            select(JobStageEvent)
            .where(JobStageEvent.job_id == job_id)
            .order_by(JobStageEvent.created_at.asc())  # type: ignore[attr-defined]
        ).all()

    return JobTimelineResponse(
        job=_to_response(job),
        events=[
            JobStageEventResponse(
                event_id=e.event_id,
                job_id=e.job_id,
                run_id=e.run_id,
                stage=e.stage,
                status=e.status,
                message=e.message,
                created_at=e.created_at,
            )
            for e in events
        ],
    )


@jobs_router.get("/{job_id}/screenshot", response_class=FileResponse)
def get_job_screenshot(job_id: int, user: User = Depends(require_auth)) -> FileResponse:
    """Download the most recent status screenshot for a job (captcha/error/success)."""
    with Session(engine) as session:
        job = _owned_job(session, job_id, user)

    if not job.screenshot_path:
        raise HTTPException(status_code=404, detail=f"No screenshot recorded for job {job_id}")

    screenshot = resolve_contained_path(
        job.screenshot_path, get_settings().screenshots_dir, "Screenshot"
    )
    return FileResponse(path=str(screenshot), media_type="image/png")


@jobs_router.post("/{job_id}/retry", response_model=JobResponse)
def retry_job(job_id: int, user: User = Depends(require_auth)) -> JobResponse:
    """
    Reset a FAILED / FLAGGED / WAITING_* / NEED_HUMAN_ACTION job back to SCRAPED
    (or PARSED, if jd_text is already present) so it will be reprocessed on the
    next run. Also serves as "resume automation after manual intervention" for
    the human queue — retry and resume are the same operation on this endpoint.
    """
    with Session(engine) as session:
        job = _owned_job(session, job_id, user)
        if job.status not in _RETRYABLE_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Job {job_id} has status {job.status.value} — only "
                    f"{'/'.join(s.value for s in _RETRYABLE_STATUSES)} can be retried"
                ),
            )
        job.status = JobStatus.PARSED if job.jd_text and job.parsed_jd else JobStatus.SCRAPED
        job.fail_reason = None
        job.retry_count += 1
        job.updated_at = datetime.now(timezone.utc)
        session.add(job)
        session.add(JobStageEvent(
            job_id=job_id, run_id=job.run_id, stage="retry",
            status=job.status.value, message=f"Retry #{job.retry_count}",
        ))
        session.commit()
        session.refresh(job)
        logger.info("Job %d reset to %s for retry (attempt %d)", job_id, job.status.value, job.retry_count)
        return _to_response(job)


@jobs_router.patch("/{job_id}/status", response_model=JobResponse)
def set_job_status(
    job_id: int, body: SetStatusRequest, user: User = Depends(require_auth)
) -> JobResponse:
    """
    Manually set a job's pipeline status (FR-006 / US-003).

    The workflow this exists for: Smart Job Apply stops at READY_TO_APPLY, the
    user applies on the company's own site using the generated resume, then
    marks the job APPLIED here. SKIPPED closes out a job they decided against.

    Marking APPLIED records an Application row so the manual path and the
    automated path leave the same audit trail — FR-006 requires the application
    date to be stored when status becomes Applied.
    """
    with Session(engine) as session:
        job = _owned_job(session, job_id, user)

        new_status = JobStatus(body.status)
        if job.status == new_status:
            return _to_response(job)

        now = datetime.now(timezone.utc)
        previous = job.status.value
        job.status = new_status
        job.updated_at = now
        session.add(job)
        session.add(JobStageEvent(
            job_id=job_id, run_id=job.run_id, stage="manual_status",
            status=new_status.value, message=f"Set manually from {previous}",
        ))

        if new_status == JobStatus.APPLIED:
            already_logged = session.exec(
                select(Application).where(Application.job_id == job_id)
            ).first()
            if not already_logged:
                session.add(Application(
                    user_id=job.user_id, job_id=job_id, submitted_at=now
                ))

        session.commit()
        session.refresh(job)
        logger.info("Job %d status set manually: %s → %s", job_id, previous, new_status.value)
        return _to_response(job)


@jobs_router.patch("/{job_id}/outcome", response_model=JobResponse)
def set_job_outcome(
    job_id: int, body: SetOutcomeRequest, user: User = Depends(require_auth)
) -> JobResponse:
    """
    Manually record a post-apply recruiting outcome (Rejected/Interview/Assessment/
    Offer/No response). Never set by the automation pipeline or an LLM — there is
    no inbox-parsing integration, so this is always a deliberate user action.
    """
    with Session(engine) as session:
        job = _owned_job(session, job_id, user)
        now = datetime.now(timezone.utc)
        job.outcome = body.outcome
        job.outcome_updated_at = now
        job.updated_at = now
        session.add(job)
        session.add(JobStageEvent(
            job_id=job_id, run_id=job.run_id, stage="outcome_change",
            status=body.outcome, message=None,
        ))
        session.commit()
        session.refresh(job)
        logger.info("Job %d outcome set to %s", job_id, body.outcome)
        return _to_response(job)
