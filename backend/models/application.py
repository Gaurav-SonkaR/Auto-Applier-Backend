from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


class ApplicationStatus(str, Enum):
    SUBMITTED = "SUBMITTED"
    BOUNCED = "BOUNCED"
    REJECTED = "REJECTED"


class Application(SQLModel, table=True):
    __tablename__ = "application"

    application_id: Optional[int] = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.job_id", index=True)
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    confirmation_screenshot: Optional[str] = Field(default=None, description="Path to screenshot file")
    status: ApplicationStatus = Field(default=ApplicationStatus.SUBMITTED)
