from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, func, select

from backend.app.routers.jobs import _to_response
from backend.app.routers.runs import active_run_ids
from backend.app.schemas import (
    DashboardStatsResponse,
    FunnelStage,
    JobListResponse,
    SourceBreakdown,
)
from backend.app.database import engine
from backend.app.models.cold_email import ColdEmailLog, EmailStatus
from backend.app.models.job import Job, JobStatus
from backend.app.models.user import User
from backend.app.routers.auth import require_auth

dashboard_router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

# Jobs sitting in any of these statuses need a human right now
_HUMAN_QUEUE_STATUSES = (
    JobStatus.WAITING_CAPTCHA, JobStatus.WAITING_LOGIN,
    JobStatus.WAITING_OTP, JobStatus.NEED_HUMAN_ACTION, JobStatus.FLAGGED,
)

# Ordered pipeline stages for the funnel visualization
_FUNNEL_ORDER = [
    (JobStatus.SCRAPED, "Jobs Found"),
    (JobStatus.PARSED, "Parsed"),
    (JobStatus.RESUME_READY, "Resume Ready"),
    (JobStatus.READY_TO_APPLY, "Ready to Apply"),
    (JobStatus.APPLIED, "Applied"),
]


@dashboard_router.get("/stats", response_model=DashboardStatsResponse)
def get_stats(user: User = Depends(require_auth)) -> DashboardStatsResponse:
    """Return aggregate stats across all jobs and email campaigns.

    All aggregation happens in SQL (GROUP BY / COUNT) — this endpoint is
    polled every 15s for the app's lifetime, so its cost must not grow with
    total accumulated job count.
    """
    with Session(engine) as session:
        status_rows = session.exec(
            select(Job.status, func.count())
            .where(Job.user_id == user.user_id)
            .group_by(Job.status)  # type: ignore[arg-type]
        ).all()
        status_counts: dict[JobStatus, int] = {status: count for status, count in status_rows}

        portal_rows = session.exec(
            select(Job.portal, func.count())
            .where(Job.user_id == user.user_id)
            .group_by(Job.portal)  # type: ignore[arg-type]
        ).all()

        email_rows = session.exec(
            select(ColdEmailLog.status, func.count())
            .where(ColdEmailLog.user_id == user.user_id)
            .group_by(ColdEmailLog.status)  # type: ignore[arg-type]
        ).all()
        email_counts: dict[EmailStatus, int] = {status: count for status, count in email_rows}

        stuck_cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        stuck_jobs_count = session.exec(
            select(func.count()).select_from(Job).where(
                Job.user_id == user.user_id,
                Job.status.in_(_HUMAN_QUEUE_STATUSES),  # type: ignore[attr-defined]
                Job.updated_at < stuck_cutoff,
            )
        ).one()

    applied = status_counts.get(JobStatus.APPLIED, 0)
    ready_to_apply = status_counts.get(JobStatus.READY_TO_APPLY, 0)
    failed = status_counts.get(JobStatus.FAILED, 0)
    # Success = the pipeline delivered what it is allowed to deliver. For portal
    # jobs that means APPLIED; for Smart Job Apply (portal="web") the pipeline
    # stops at READY_TO_APPLY by design, so counting those as neither success
    # nor failure would understate the system as more web jobs accumulate.
    # Jobs still in flight count as neither.
    succeeded = applied + ready_to_apply
    decided = succeeded + failed
    success_rate = round(succeeded / decided, 4) if decided else 0.0

    by_portal: dict[str, int] = {}
    career_site_jobs = 0
    portal_jobs = 0
    for portal, count in portal_rows:
        if portal == "web":
            career_site_jobs += count
        else:
            portal_jobs += count
            by_portal[portal] = count

    return DashboardStatsResponse(
        total_jobs=sum(status_counts.values()),
        scraped=status_counts.get(JobStatus.SCRAPED, 0),
        parsed=status_counts.get(JobStatus.PARSED, 0),
        resume_ready=status_counts.get(JobStatus.RESUME_READY, 0),
        ready_to_apply=ready_to_apply,
        applied=applied,
        failed=failed,
        flagged=status_counts.get(JobStatus.FLAGGED, 0),
        waiting_captcha=status_counts.get(JobStatus.WAITING_CAPTCHA, 0),
        waiting_login=status_counts.get(JobStatus.WAITING_LOGIN, 0),
        waiting_otp=status_counts.get(JobStatus.WAITING_OTP, 0),
        need_human_action=status_counts.get(JobStatus.NEED_HUMAN_ACTION, 0),
        duplicate=status_counts.get(JobStatus.DUPLICATE, 0),
        skipped=status_counts.get(JobStatus.SKIPPED, 0),
        blacklisted=status_counts.get(JobStatus.BLACKLISTED, 0),
        emails_sent=email_counts.get(EmailStatus.SENT, 0),
        emails_failed=email_counts.get(EmailStatus.FAILED, 0),
        emails_pending=email_counts.get(EmailStatus.PENDING, 0),
        active_runs=len(active_run_ids(user.user_id)),
        funnel=[
            FunnelStage(stage=label, count=status_counts.get(status, 0))
            for status, label in _FUNNEL_ORDER
        ],
        success_rate=success_rate,
        source_breakdown=SourceBreakdown(
            portal_jobs=portal_jobs, career_site_jobs=career_site_jobs, by_portal=by_portal,
        ),
        stuck_jobs_count=stuck_jobs_count,
    )


@dashboard_router.get("/human-queue", response_model=JobListResponse)
def get_human_queue(
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_auth),
) -> JobListResponse:
    """Jobs needing a human right now — CAPTCHA/login/OTP/external-ATS/legacy-flagged, oldest first."""
    with Session(engine) as session:
        base_filter = (
            Job.user_id == user.user_id,
            Job.status.in_(_HUMAN_QUEUE_STATUSES),  # type: ignore[attr-defined]
        )
        total = session.exec(
            select(func.count()).select_from(Job).where(*base_filter)
        ).one()
        items = session.exec(
            select(Job)
            .where(*base_filter)
            .order_by(Job.updated_at.asc())  # type: ignore[attr-defined]
            .offset(offset)
            .limit(limit)
        ).all()

    return JobListResponse(items=[_to_response(j) for j in items], total=total)
