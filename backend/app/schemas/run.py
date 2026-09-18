from __future__ import annotations

from typing import Optional

from backend.app.schemas.types import UtcDatetime
from pydantic import BaseModel, Field, field_validator


class RunStartRequest(BaseModel):
    portal: str = Field(
        ..., description="linkedin / naukri / indeed / wellfound / cutshort / instahyre / web"
    )
    job_title: str = Field(
        default="", max_length=200,
        description=(
            "Optional. Leave blank to search from your uploaded master resume "
            "(its extracted title and skills), falling back to master_profile.json."
        ),
    )
    location: str = Field(default="", max_length=200)
    batch_size: int = Field(default=10, ge=1, le=50)
    include_checkpointed: bool = Field(default=True)
    dry_run: bool = Field(
        default=False,
        description="Portal runs only: fill each application and screenshot it, but never "
                    "submit. For checking a portal before letting it apply for real.",
    )

    @field_validator("portal")
    @classmethod
    def portal_must_be_valid(cls, v: str) -> str:
        valid = {"linkedin", "naukri", "indeed", "wellfound", "cutshort", "instahyre", "web"}
        if v.lower() not in valid:
            raise ValueError(f"portal must be one of: {', '.join(sorted(valid))}")
        return v.lower()

    @field_validator("job_title", "location")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        return v.strip()

    # No "job_title is required" rule any more: a blank title on *any* portal now
    # means "search from my master resume". That is the point of uploading one —
    # requiring the keyword anyway would make resume-driven search unreachable.


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
    jobs_ready: int = 0
    jobs_failed: int
    jobs_flagged: int
    jobs_skipped: int = 0
    # Timestamps
    started_at: Optional[UtcDatetime]
    completed_at: Optional[UtcDatetime]
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
