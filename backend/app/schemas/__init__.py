"""Pydantic request/response schemas, grouped by domain.

Re-exported flat so routers import one place (`from backend.app.schemas import X`)
regardless of which module a schema lives in.
"""
from backend.app.schemas.cold_email import (
    ColdEmailImportResponse,
    ColdEmailPreviewResponse,
    ColdEmailStartRequest,
    ColdEmailStartResponse,
    ColdEmailStatsResponse,
)
from backend.app.schemas.dashboard import DashboardStatsResponse, FunnelStage, SourceBreakdown
from backend.app.schemas.event import WSEvent
from backend.app.schemas.job import (
    JobListResponse,
    JobResponse,
    JobStageEventResponse,
    JobTimelineResponse,
    SetOutcomeRequest,
    SetStatusRequest,
)
from backend.app.schemas.resume import (
    ResumeListResponse,
    ResumeMatchListResponse,
    ResumeMatchResponse,
    ResumeResponse,
    ResumeUploadResponse,
)
from backend.app.schemas.run import (
    DiscoveryQuotaResponse,
    RunResponse,
    RunStartRequest,
    RunStartResponse,
)

__all__ = [
    # job
    "JobResponse", "JobListResponse", "JobStageEventResponse",
    "JobTimelineResponse", "SetOutcomeRequest", "SetStatusRequest",
    # run
    "RunStartRequest", "RunStartResponse", "RunResponse", "DiscoveryQuotaResponse",
    # resume library
    "ResumeResponse", "ResumeListResponse", "ResumeUploadResponse",
    "ResumeMatchResponse", "ResumeMatchListResponse",
    # dashboard
    "DashboardStatsResponse", "FunnelStage", "SourceBreakdown",
    # cold email
    "ColdEmailStartRequest", "ColdEmailStartResponse",
    "ColdEmailStatsResponse", "ColdEmailImportResponse",
    "ColdEmailPreviewResponse",
    # events
    "WSEvent",
]
