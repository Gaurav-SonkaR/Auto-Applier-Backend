from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


class JobStatus(str, Enum):
    SCRAPED = "SCRAPED"
    PARSED = "PARSED"
    RESUME_READY = "RESUME_READY"
    # Terminal success for Smart Job Apply (portal="web"): the tailored resume
    # is built and the apply link is ready, but submission is the user's to make.
    # Distinct from RESUME_READY, which means "mid-pipeline, resume done, apply
    # still to run" and is picked back up by include_checkpointed on the next run.
    READY_TO_APPLY = "READY_TO_APPLY"
    APPLIED = "APPLIED"
    FAILED = "FAILED"
    FLAGGED = "FLAGGED"
    # Granular "needs human" sub-states — replace the generic FLAGGED collapse
    WAITING_CAPTCHA = "WAITING_CAPTCHA"
    WAITING_LOGIN = "WAITING_LOGIN"
    WAITING_OTP = "WAITING_OTP"  # reserved — no detector wired yet
    NEED_HUMAN_ACTION = "NEED_HUMAN_ACTION"  # generic catch-all (external ATS redirect, unknown block)
    # Deterministic pre-pipeline exclusion states
    DUPLICATE = "DUPLICATE"
    SKIPPED = "SKIPPED"
    BLACKLISTED = "BLACKLISTED"  # reserved — no enforcement wired yet


class JobOutcome(str, Enum):
    """Manually-set post-apply recruiting outcome — a separate dimension from `status`.

    Never set by the automation pipeline or any LLM call; there is no inbox-parsing
    integration to auto-detect these, so they're only ever written via the
    outcome API endpoint at the user's own instruction.
    """

    REJECTED = "REJECTED"
    INTERVIEW_SCHEDULED = "INTERVIEW_SCHEDULED"
    ASSESSMENT_PENDING = "ASSESSMENT_PENDING"
    OFFER_RECEIVED = "OFFER_RECEIVED"
    NO_RESPONSE = "NO_RESPONSE"


class Job(SQLModel, table=True):
    __tablename__ = "job"

    job_id: Optional[int] = Field(default=None, primary_key=True)
    # Owning account. Every query in the app filters on this — without it
    # one user would see every other user's data.
    user_id: Optional[int] = Field(default=None, foreign_key="user.user_id", index=True)
    # Indexed but NOT globally unique any more: two users may legitimately
    # track the same posting. Uniqueness is per-owner, enforced by the
    # composite index idx_job_user_url in database.py.
    url: str = Field(index=True, description="Application URL — deduplication key, per user")
    title: str = Field(description="Job title")
    company: Optional[str] = Field(default=None)
    portal: str = Field(index=True, description="Source portal: linkedin/naukri/indeed/wellfound/cutshort/web(=career site)")
    jd_text: Optional[str] = Field(default=None, description="Raw job description text")
    parsed_jd: Optional[str] = Field(default=None, description="JSON: required_skills, keywords, etc.")
    resume_path: Optional[str] = Field(default=None, description="Absolute path to generated PDF")
    # s3://bucket/key when STORAGE_BACKEND=s3 and the upload succeeded. The
    # local file at resume_path is kept either way: auto-apply uploads it
    # through a browser file input, which needs a path on disk.
    resume_storage_ref: Optional[str] = Field(default=None)
    status: JobStatus = Field(default=JobStatus.SCRAPED, index=True)
    run_id: Optional[int] = Field(default=None, index=True, description="Run log ID that created this job")
    fail_reason: Optional[str] = Field(default=None, description="Error message for FAILED/FLAGGED/WAITING_*/NEED_HUMAN_ACTION")
    retry_count: int = Field(default=0, description="Number of times this job has been retried/resumed")
    ats_score: Optional[int] = Field(default=None, description="0-100 keyword-match score from resume generation")
    redirect_url: Optional[str] = Field(default=None, description="External ATS / redirect target captured at apply time")
    screenshot_path: Optional[str] = Field(default=None, description="Path to the most recent status screenshot")
    outcome: Optional[str] = Field(default=None, description="Manually-set post-apply outcome (JobOutcome value)")
    outcome_updated_at: Optional[datetime] = Field(default=None, description="Timestamp of last manual outcome update")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
