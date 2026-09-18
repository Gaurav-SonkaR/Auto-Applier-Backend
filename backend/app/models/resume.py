from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


class Resume(SQLModel, table=True):
    """A resume PDF the user uploaded (their *own* file, not a generated one).

    Two jobs:
      1. The one flagged `is_master` drives portal job search — its extracted
         title and skills become the search query when the user doesn't type one.
      2. Any of them can be picked as the upload for an application when no
         tailored resume was generated, scored against the parsed JD.

    Generated resumes are not rows here — they live on `Job.resume_path`, one
    per job, and are addressed by job id.
    """

    __tablename__ = "resume"

    resume_id: Optional[int] = Field(default=None, primary_key=True)
    # Owning account. Every query in the app filters on this — without it
    # one user would see every other user's data.
    user_id: Optional[int] = Field(default=None, foreign_key="user.user_id", index=True)
    filename: str = Field(description="Original upload filename")
    file_path: str = Field(description="Absolute path on disk")
    label: str = Field(default="", description="User-facing name, e.g. 'Backend-focused'")
    is_master: bool = Field(
        default=False, index=True,
        description="The profile resume used to derive portal search terms. At most one.",
    )
    extracted_text: Optional[str] = Field(
        default=None, description="Plain text pulled from the PDF, for skill matching"
    )
    extracted_skills: Optional[str] = Field(
        default=None, description="JSON array of skills recognised in the text"
    )
    extracted_title: Optional[str] = Field(
        default=None, description="Best-guess job title, used as the portal search keyword"
    )
    page_count: Optional[int] = Field(default=None)
    uploaded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
