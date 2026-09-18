from __future__ import annotations


from pydantic import BaseModel, Field


class FunnelStage(BaseModel):
    stage: str
    count: int


class SourceBreakdown(BaseModel):
    portal_jobs: int
    career_site_jobs: int
    by_portal: dict[str, int]


class DashboardStatsResponse(BaseModel):
    total_jobs: int
    scraped: int
    parsed: int
    resume_ready: int
    ready_to_apply: int = 0
    applied: int
    failed: int
    flagged: int
    # Granular "needs human" / pre-pipeline counts
    waiting_captcha: int = 0
    waiting_login: int = 0
    waiting_otp: int = 0
    need_human_action: int = 0
    duplicate: int = 0
    skipped: int = 0
    blacklisted: int = 0
    emails_sent: int
    emails_failed: int
    emails_pending: int
    active_runs: int
    funnel: list[FunnelStage] = Field(default_factory=list)
    success_rate: float = 0.0
    source_breakdown: SourceBreakdown
    stuck_jobs_count: int = 0
