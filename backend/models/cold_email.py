from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


class EmailStatus(str, Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    BOUNCED = "BOUNCED"
    FAILED = "FAILED"


class ColdEmailLog(SQLModel, table=True):
    __tablename__ = "cold_email_log"

    email_id: Optional[int] = Field(default=None, primary_key=True)
    company_name: str = Field(description="Target company name")
    contact_email: str = Field(index=True, description="Recipient email address")
    tech_stack: Optional[str] = Field(default=None, description="Detected tech stack for resume grouping")
    role: Optional[str] = Field(default=None, description="Target role")
    resume_path: Optional[str] = Field(default=None, description="Path to tailored resume PDF")
    sent_at: Optional[datetime] = Field(default=None, index=True)
    status: EmailStatus = Field(default=EmailStatus.PENDING)
