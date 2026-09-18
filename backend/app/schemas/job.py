from __future__ import annotations

from typing import Optional

from backend.app.schemas.types import UtcDatetime
from pydantic import BaseModel, field_validator


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
    outcome_updated_at: Optional[UtcDatetime] = None
    created_at: UtcDatetime
    updated_at: UtcDatetime


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
    created_at: UtcDatetime


class JobTimelineResponse(BaseModel):
    job: JobResponse
    events: list[JobStageEventResponse]


class SetStatusRequest(BaseModel):
    """FR-006 manual status tracking.

    Only the statuses a human can legitimately assert are accepted — everything
    else is owned by the pipeline. Recruiting outcomes (Interview/Rejected/...)
    are a separate dimension; use PATCH /outcome for those.
    """

    status: str

    @field_validator("status")
    @classmethod
    def status_must_be_manually_settable(cls, v: str) -> str:
        valid = {"APPLIED", "READY_TO_APPLY", "SKIPPED"}
        if v.upper() not in valid:
            raise ValueError(f"status must be one of: {', '.join(sorted(valid))}")
        return v.upper()


class SetOutcomeRequest(BaseModel):
    outcome: str

    @field_validator("outcome")
    @classmethod
    def outcome_must_be_valid(cls, v: str) -> str:
        valid = {"REJECTED", "INTERVIEW_SCHEDULED", "ASSESSMENT_PENDING", "OFFER_RECEIVED", "NO_RESPONSE"}
        if v.upper() not in valid:
            raise ValueError(f"outcome must be one of: {', '.join(sorted(valid))}")
        return v.upper()
