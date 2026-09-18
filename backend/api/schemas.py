from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ── Run schemas ────────────────────────────────────────────────────────────────

class RunStartRequest(BaseModel):
    portal: str = Field(..., description="linkedin / naukri / indeed / wellfound / cutshort / web")
    job_title: str = Field(
        default="", max_length=200,
        description="Required for portal scraping. Optional for portal='web' — "
                     "left blank triggers smart search derived from master_profile.json.",
    )
    location: str = Field(default="", max_length=200)
    batch_size: int = Field(default=10, ge=1, le=50)
    include_checkpointed: bool = Field(default=True)

    @field_validator("portal")
    @classmethod
    def portal_must_be_valid(cls, v: str) -> str:
        valid = {"linkedin", "naukri", "indeed", "wellfound", "cutshort", "web"}
        if v.lower() not in valid:
            raise ValueError(f"portal must be one of: {', '.join(sorted(valid))}")
        return v.lower()

    @model_validator(mode="after")
    def job_title_required_unless_web(self) -> "RunStartRequest":
        if self.portal != "web" and len(self.job_title.strip()) < 2:
            raise ValueError("job_title is required (min 2 characters) for portal scraping runs")
        return self


class RunResponse(BaseModel):
    run_id: int
    run_type: str
    status: str
    # Config fields (parsed from config_snapshot)
    portal: Optional[str] = None
    job_title: Optional[str] = None
    location: Optional[str] = None
    batch_size: Optional[int] = None
    # Progress counters
    jobs_scraped: int
    jobs_applied: int
    jobs_failed: int
    jobs_flagged: int
    jobs_skipped: int = 0
    # Timestamps
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    # Error info
    error_message: Optional[str] = None


class RunStartResponse(BaseModel):
    run_id: int
    status: str = "RUNNING"
    message: str = ""


class DiscoveryQuotaResponse(BaseModel):
    remaining_today: int
    daily_limit: int
    serper_configured: bool


# ── Job schemas ────────────────────────────────────────────────────────────────

class JobResponse(BaseModel):
    job_id: int
    run_id: Optional[int] = None
    url: str
    title: str
    company: Optional[str]
    portal: str
    status: str
    fail_reason: Optional[str]
    error_message: Optional[str] = None  # mirrors fail_reason — closes a pre-existing frontend gap
    resume_path: Optional[str]
    retry_count: int = 0
    ats_score: Optional[int] = None
    redirect_url: Optional[str] = None
    screenshot_path: Optional[str] = None
    outcome: Optional[str] = None
    outcome_updated_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class JobListResponse(BaseModel):
    items: list[JobResponse]
    total: int


class JobStageEventResponse(BaseModel):
    event_id: int
    job_id: int
    run_id: Optional[int] = None
    stage: str
    status: str
    message: Optional[str] = None
    created_at: datetime


class JobTimelineResponse(BaseModel):
    job: JobResponse
    events: list[JobStageEventResponse]


class SetOutcomeRequest(BaseModel):
    outcome: str

    @field_validator("outcome")
    @classmethod
    def outcome_must_be_valid(cls, v: str) -> str:
        valid = {"REJECTED", "INTERVIEW_SCHEDULED", "ASSESSMENT_PENDING", "OFFER_RECEIVED", "NO_RESPONSE"}
        if v.upper() not in valid:
            raise ValueError(f"outcome must be one of: {', '.join(sorted(valid))}")
        return v.upper()


# ── Dashboard schemas ──────────────────────────────────────────────────────────

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


# ── Cold email schemas ─────────────────────────────────────────────────────────

class ColdEmailStartRequest(BaseModel):
    excel_path: str = Field(..., description="Absolute path to Excel/CSV contact list")
    role: str = Field(default="", max_length=200)
    batch_size: int = Field(default=10, ge=1, le=40)


class ColdEmailStartResponse(BaseModel):
    run_id: int
    status: str = "RUNNING"


class ColdEmailStatsResponse(BaseModel):
    total: int
    sent: int
    failed: int
    pending: int
    remaining_today: int


class ColdEmailImportResponse(BaseModel):
    saved_path: str
    row_count: int
    columns: list[str]


# ── WebSocket event schema ─────────────────────────────────────────────────────

class WSEvent(BaseModel):
    run_id: Optional[int]
    job_id: Optional[int]
    node: str
    status: str
    message: str = ""
    data: dict = Field(default_factory=dict)
