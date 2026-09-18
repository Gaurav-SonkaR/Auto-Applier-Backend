from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


class RunType(str, Enum):
    PORTAL_APPLY = "PORTAL_APPLY"
    COLD_EMAIL = "COLD_EMAIL"


class RunStatus(str, Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


class RunLog(SQLModel, table=True):
    __tablename__ = "run_log"

    run_id: Optional[int] = Field(default=None, primary_key=True)
    run_type: RunType
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)
    completed_at: Optional[datetime] = Field(default=None)
    jobs_scraped: int = Field(default=0)
    jobs_applied: int = Field(default=0)
    jobs_failed: int = Field(default=0)
    jobs_flagged: int = Field(default=0)
    jobs_skipped: int = Field(default=0)
    status: RunStatus = Field(default=RunStatus.RUNNING, index=True)
    error_message: Optional[str] = Field(default=None, description="Last error or stop reason")
    config_snapshot: Optional[str] = Field(default=None, description="JSON of run config for audit")
