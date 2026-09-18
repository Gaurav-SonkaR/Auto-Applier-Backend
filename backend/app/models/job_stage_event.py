from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


class JobStageEvent(SQLModel, table=True):
    """Append-only per-job timeline row, written alongside every Job.status change."""

    __tablename__ = "job_stage_event"

    event_id: Optional[int] = Field(default=None, primary_key=True)
    # Owning account. Every query in the app filters on this — without it
    # one user would see every other user's data.
    user_id: Optional[int] = Field(default=None, foreign_key="user.user_id", index=True)
    job_id: int = Field(foreign_key="job.job_id", index=True)
    run_id: Optional[int] = Field(default=None, index=True)
    stage: str = Field(description="fetch_jd / parse / resume / apply / retry / outcome_change")
    status: str = Field(description="The JobStatus (or outcome) value this event transitioned to")
    message: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)
