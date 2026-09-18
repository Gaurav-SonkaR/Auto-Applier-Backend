from __future__ import annotations

from typing import Optional

from backend.app.schemas.types import UtcDatetime
from pydantic import BaseModel, Field


class ResumeResponse(BaseModel):
    """One uploaded resume in the library.

    `extracted_text` is deliberately absent — it can run to 20k characters and
    no client needs it; `skills` is the useful distillation.
    """

    resume_id: int
    filename: str
    label: str = ""
    is_master: bool = False
    skills: list[str] = Field(default_factory=list)
    extracted_title: Optional[str] = None
    page_count: Optional[int] = None
    uploaded_at: UtcDatetime


class ResumeListResponse(BaseModel):
    items: list[ResumeResponse]
    total: int
    master_resume_id: Optional[int] = None


class ResumeUploadResponse(BaseModel):
    resume: ResumeResponse
    message: str = ""


class ResumeMatchResponse(BaseModel):
    """A library resume scored against a job's parsed description."""

    resume: ResumeResponse
    score: float = Field(ge=0.0, le=1.0, description="Share of JD terms found in the resume")
    matched_terms: list[str] = Field(default_factory=list)


class ResumeMatchListResponse(BaseModel):
    job_id: int
    jd_terms: list[str] = Field(default_factory=list)
    matches: list[ResumeMatchResponse] = Field(default_factory=list)
