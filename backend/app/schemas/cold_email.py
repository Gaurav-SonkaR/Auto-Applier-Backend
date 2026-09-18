from __future__ import annotations


from pydantic import BaseModel, Field


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


class ColdEmailPreviewResponse(BaseModel):
    """What recipients will actually receive, before anything is sent.

    Cold email is the one feature here that reaches real strangers under the
    user's own name and burns their sending reputation if it is wrong, so the
    body is shown in full first rather than described.
    """

    subject: str
    body: str
    can_send: bool = Field(description="Profile has everything the email needs")
    missing: list[str] = Field(default_factory=list, description="What to fill in first")
    warnings: list[str] = Field(
        default_factory=list, description="Deliverability problems worth fixing"
    )
    attaches_resume: bool = Field(
        description="True when no public resume link is set, so a PDF is attached instead"
    )
    daily_limit: int = Field(description="Today's cap, after the warm-up ramp")
    configured_limit: int
    warmup_days: int
