from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session

from backend.api.file_serving import resolve_contained_path
from backend.config import get_settings
from backend.db.database import engine
from backend.models.job import Job

logger = logging.getLogger("api.resumes")

resumes_router = APIRouter(prefix="/api/resumes", tags=["resumes"])


@resumes_router.get("/{job_id}", response_class=FileResponse)
def download_resume(job_id: int) -> FileResponse:
    """Download the generated PDF resume for a job."""
    with Session(engine) as session:
        job = session.get(Job, job_id)

    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    if not job.resume_path:
        raise HTTPException(
            status_code=404,
            detail=f"No resume generated for job {job_id} yet (status: {job.status.value})",
        )

    pdf = resolve_contained_path(job.resume_path, get_settings().resumes_dir, "Resume")

    filename = f"resume_job_{job_id}.pdf"
    return FileResponse(
        path=str(pdf),
        media_type="application/pdf",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
