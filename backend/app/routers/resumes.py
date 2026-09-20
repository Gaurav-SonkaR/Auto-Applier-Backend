from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlmodel import Session, func, select

from backend.app.config import get_settings
from backend.app.database import engine
from backend.app.models.job import Job
from backend.app.models.resume import Resume
from backend.app.models.user import User
from backend.app.routers.auth import require_auth
from backend.app.routers.jobs import _owned_job
from backend.app.schemas.resume import (
    ResumeListResponse,
    ResumeMatchListResponse,
    ResumeMatchResponse,
    ResumeResponse,
    ResumeUploadResponse,
)
from backend.app.services import resume_library
from backend.app.services.storage import get_resume_store
from backend.app.utils.file_naming import sanitize
from backend.app.utils.file_serving import resolve_contained_path

logger = logging.getLogger("api.resumes")

resumes_router = APIRouter(prefix="/api/resumes", tags=["resumes"])

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB — a resume PDF is well under 1 MB


def _library_dir() -> Path:
    """Uploaded resumes live beside generated ones but in their own subfolder,
    so `resolve_contained_path` containment still holds for both."""
    path = Path(get_settings().resumes_dir) / "library"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _owned_resume(session: Session, resume_id: int, user: User) -> Resume:
    """A resume the caller owns, or 404 — 403 would confirm the id exists."""
    resume = session.get(Resume, resume_id)
    if resume is None or resume.user_id != user.user_id:
        raise HTTPException(status_code=404, detail=f"Resume {resume_id} not found")
    return resume


def _to_response(resume: Resume) -> ResumeResponse:
    return ResumeResponse(
        resume_id=resume.resume_id,
        filename=resume.filename,
        label=resume.label,
        is_master=resume.is_master,
        skills=resume_library.skills_of(resume),
        extracted_title=resume.extracted_title,
        page_count=resume.page_count,
        uploaded_at=resume.uploaded_at,
    )


# ── Library ───────────────────────────────────────────────────────────────────
# Declared before /{job_id} — otherwise FastAPI matches "library" against the
# int path param and returns 422 instead of routing here.

@resumes_router.get("/library", response_model=ResumeListResponse)
def list_library(user: User = Depends(require_auth)) -> ResumeListResponse:
    """All uploaded resumes, master first then newest."""
    with Session(engine) as session:
        items = session.exec(
            select(Resume)
            .where(Resume.user_id == user.user_id)
            .order_by(Resume.is_master.desc(), Resume.uploaded_at.desc())  # type: ignore[attr-defined]
        ).all()
        total = session.exec(
            select(func.count()).select_from(Resume).where(Resume.user_id == user.user_id)
        ).one()
        master = next((r for r in items if r.is_master), None)

    return ResumeListResponse(
        items=[_to_response(r) for r in items],
        total=total,
        master_resume_id=master.resume_id if master else None,
    )


@resumes_router.post("/library", response_model=ResumeUploadResponse, status_code=201)
async def upload_resume(
    file: UploadFile = File(...),
    label: str = Form(default=""),
    is_master: bool = Form(default=False),
    user: User = Depends(require_auth),
) -> ResumeUploadResponse:
    """Upload a resume PDF.

    The text is extracted on upload, not on demand: the skills it yields drive
    portal search and best-resume selection, both of which sit on hot paths
    where re-parsing the PDF every time would be wasteful.
    """
    if not file.filename:
        raise HTTPException(status_code=422, detail="No filename provided")
    if Path(file.filename).suffix.lower() != ".pdf":
        raise HTTPException(status_code=422, detail="Resume must be a .pdf file")

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=422, detail="Uploaded file is empty")
    if len(payload) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Resume exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)}MB",
        )

    # Never trust the client's filename on disk — it can carry path separators.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    safe_stem = sanitize(Path(file.filename).stem, 60, fallback="resume")
    dest = _library_dir() / f"{safe_stem}_{stamp}.pdf"
    dest.write_bytes(payload)

    try:
        analysis = resume_library.analyse(str(dest))
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(
            status_code=422, detail=f"Could not read the PDF: {exc}"
        ) from exc

    with Session(engine) as session:
        first_upload = session.exec(
            select(func.count()).select_from(Resume).where(Resume.user_id == user.user_id)
        ).one() == 0
        resume = Resume(
            user_id=user.user_id,
            filename=file.filename,
            file_path=str(dest.resolve()),
            label=label.strip() or Path(file.filename).stem,
            # The first resume uploaded becomes master automatically — otherwise
            # resume-driven search stays silently disabled until the user
            # notices a toggle they were never told about.
            is_master=is_master or first_upload,
            **analysis,
        )
        session.add(resume)
        session.commit()
        session.refresh(resume)

        if resume.is_master:
            resume_library.set_master(session, resume.resume_id, user.user_id)
            session.refresh(resume)

        skills = resume_library.skills_of(resume)
        response = _to_response(resume)

    logger.info(
        "Resume uploaded: %s (%d pages, %d skills, master=%s)",
        resume.filename, resume.page_count or 0, len(skills), resume.is_master,
    )
    return ResumeUploadResponse(
        resume=response,
        message=f"Recognised {len(skills)} skills and the title '{resume.extracted_title}'.",
    )


@resumes_router.patch("/library/{resume_id}/master", response_model=ResumeResponse)
def make_master(resume_id: int, user: User = Depends(require_auth)) -> ResumeResponse:
    """Promote a resume to master — the one that drives portal job search."""
    with Session(engine) as session:
        _owned_resume(session, resume_id, user)   # 404s if it isn't theirs
        resume = resume_library.set_master(session, resume_id, user.user_id)
        return _to_response(resume)


@resumes_router.delete("/library/{resume_id}", status_code=204)
def delete_resume(resume_id: int, user: User = Depends(require_auth)) -> None:
    with Session(engine) as session:
        _owned_resume(session, resume_id, user)
        resume_library.delete_resume(session, resume_id)


@resumes_router.get("/library/{resume_id}/download", response_class=FileResponse)
def download_library_resume(
    resume_id: int, user: User = Depends(require_auth)
) -> FileResponse:
    with Session(engine) as session:
        resume = _owned_resume(session, resume_id, user)

    pdf = resolve_contained_path(
        resume.file_path, get_settings().resumes_dir, "Resume"
    )
    return FileResponse(
        path=str(pdf), media_type="application/pdf", filename=resume.filename
    )


@resumes_router.get("/match/{job_id}", response_model=ResumeMatchListResponse)
def match_resumes_for_job(
    job_id: int, user: User = Depends(require_auth)
) -> ResumeMatchListResponse:
    """Rank the library against a job's parsed description.

    Surfaces which uploaded resume to attach when no tailored one was generated.
    """
    with Session(engine) as session:
        job = _owned_job(session, job_id, user)

        terms: list[str] = []
        if job.parsed_jd:
            try:
                parsed = json.loads(job.parsed_jd)
                terms = sorted({
                    str(t).lower()
                    for key in ("required_skills", "nice_to_have", "keywords")
                    for t in (parsed.get(key) or [])
                    if t
                })
            except json.JSONDecodeError:
                logger.warning("Job %d has unparseable parsed_jd", job_id)

        ranked = resume_library.rank_for_jd(session, terms, user.user_id)
        return ResumeMatchListResponse(
            job_id=job_id,
            jd_terms=terms,
            matches=[
                ResumeMatchResponse(
                    resume=_to_response(m.resume),
                    score=round(m.score, 4),
                    matched_terms=m.matched,
                )
                for m in ranked
            ],
        )


# ── Generated resumes ─────────────────────────────────────────────────────────

@resumes_router.get("/{job_id}", response_class=FileResponse, response_model=None)
def download_resume(
    job_id: int, user: User = Depends(require_auth)
) -> FileResponse | RedirectResponse:
    """Download the generated PDF resume for a job."""
    with Session(engine) as session:
        job = _owned_job(session, job_id, user)

    if not job.resume_path:
        raise HTTPException(
            status_code=404,
            detail=f"No resume generated for job {job_id} yet (status: {job.status.value})",
        )

    filename = Path(job.resume_path).name or f"resume_job_{job_id}.pdf"

    # Stored in S3: hand the browser a short-lived link rather than streaming
    # the file through this server.
    if job.resume_storage_ref:
        url = get_resume_store().download_url(job.resume_storage_ref, filename)
        if url:
            return RedirectResponse(url, status_code=307)

    pdf = resolve_contained_path(job.resume_path, get_settings().resumes_dir, "Resume")
    return FileResponse(
        path=str(pdf),
        media_type="application/pdf",
        filename=filename,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
