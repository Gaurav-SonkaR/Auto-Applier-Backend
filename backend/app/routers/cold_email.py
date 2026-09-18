from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlmodel import Session, select

from backend.app.schemas import (
    ColdEmailImportResponse,
    ColdEmailPreviewResponse,
    ColdEmailStartRequest,
    ColdEmailStartResponse,
    ColdEmailStatsResponse,
)
from backend.app.routers.websocket import manager
from backend.app.config import get_settings
from backend.app.database import engine
from backend.app.models.cold_email import ColdEmailLog, EmailStatus
from backend.app.models.run_log import RunLog, RunStatus, RunType
from backend.app.models.user import User
from backend.app.routers.auth import require_auth
from backend.app.services import cold_email_composer as composer
from backend.app.services import profiles
from backend.app.services.email_sender import get_email_sender
from backend.app.workflows.cold_email_flow import run_cold_email
from backend.app.workflows.state import (
    DEFAULT_SUBJECT_TEMPLATE,
    ColdEmailConfig,
    EventCallback,
    FlowEvent,
)

logger = logging.getLogger("api.cold_email")

cold_email_router = APIRouter(prefix="/api/cold-email", tags=["cold-email"])

_active_email_tasks: dict[int, asyncio.Task] = {}


def _make_on_event(run_id: int) -> EventCallback:
    async def _callback(event: FlowEvent) -> None:
        await manager.broadcast(run_id, {
            "run_id": event.run_id, "job_id": event.job_id,
            "node": event.node, "status": event.status, "message": event.message,
        })
    return _callback


@cold_email_router.post("/import", response_model=ColdEmailImportResponse)
async def import_contacts(
    file: UploadFile = File(...), user: User = Depends(require_auth)
) -> ColdEmailImportResponse:
    """
    Upload an Excel or CSV contact list.
    Saved to data/uploads/ and path is returned for use with /start.
    """
    if not file.filename:
        raise HTTPException(status_code=422, detail="No filename provided")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in (".xlsx", ".xls", ".csv"):
        raise HTTPException(status_code=422, detail="File must be .xlsx, .xls, or .csv")

    # Per-user upload dir — contact lists are personal data and must not
    # land where another account could name the same file.
    uploads_dir = Path("data/uploads") / str(user.user_id)
    uploads_dir.mkdir(parents=True, exist_ok=True)
    save_path = uploads_dir / file.filename

    with save_path.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    # Quick row count
    try:
        import pandas as pd
        df = pd.read_excel(save_path, dtype=str) if suffix != ".csv" else pd.read_csv(save_path, dtype=str)
        row_count = len(df)
        columns = list(df.columns)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Could not parse file: {exc}")

    logger.info("Imported %d contacts from %s", row_count, file.filename)
    return ColdEmailImportResponse(
        saved_path=str(save_path.resolve()),
        row_count=row_count,
        columns=columns,
    )


@cold_email_router.post("/start", response_model=ColdEmailStartResponse, status_code=202)
async def start_cold_email(
    request: ColdEmailStartRequest, user: User = Depends(require_auth)
) -> ColdEmailStartResponse:
    """Start a cold email campaign in the background."""
    if not Path(request.excel_path).exists():
        raise HTTPException(status_code=422, detail=f"File not found: {request.excel_path}")

    settings = get_settings()
    try:
        settings.validate_required_for_email()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # Pre-create RunLog
    with Session(engine) as session:
        run_log = RunLog(
            user_id=user.user_id,
            run_type=RunType.COLD_EMAIL,
            started_at=datetime.now(timezone.utc),
            config_snapshot=json.dumps(request.model_dump()),
        )
        session.add(run_log)
        session.commit()
        session.refresh(run_log)
        run_id = run_log.run_id

    config = ColdEmailConfig(
        excel_path=request.excel_path,
        role=request.role,
        batch_size=request.batch_size,
        user_id=user.user_id,
    )
    on_event = _make_on_event(run_id)

    async def _task() -> None:
        try:
            await run_cold_email(config, on_event=on_event, existing_run_id=run_id)
        except asyncio.CancelledError:
            _mark_stopped(run_id)
        except Exception as exc:
            logger.error("Cold email run %d crashed: %s", run_id, exc, exc_info=True)
            # Without this the row stays RUNNING for ever: `_active_email_tasks`
            # is in-memory, so nothing else will ever revisit it, and the
            # dashboard goes on showing a campaign that died.
            _mark_failed(run_id, str(exc))
        finally:
            _active_email_tasks.pop(run_id, None)

    task = asyncio.create_task(_task())
    _active_email_tasks[run_id] = task
    return ColdEmailStartResponse(run_id=run_id)


@cold_email_router.get("/preview", response_model=ColdEmailPreviewResponse)
def preview_cold_email(
    user: User = Depends(require_auth),
) -> ColdEmailPreviewResponse:
    """Show the exact email a contact would receive, plus what would hurt it.

    This is the only feature in the app that writes to real strangers under the
    user's own name and burns their sending reputation when it goes wrong, so
    the body is shown in full before a campaign can be started rather than
    described in the UI.

    It takes no parameters on purpose. It used to accept a `subject_template`
    that `/start` has no field for, so a caller could preview one subject and
    send a different one — a preview that can disagree with the thing it
    previews is worse than none.
    """
    settings = get_settings()
    sender = get_email_sender()

    with Session(engine) as session:
        profile = profiles.get_profile(session, user.user_id)

    missing = composer.missing_requirements(profile)
    attaches = not composer.sender_identity(profile)["resume_url"]

    subject, body = "", ""
    if not missing:
        try:
            sample = composer.preview(profile, DEFAULT_SUBJECT_TEMPLATE)
            subject, body = sample["subject"], sample["body"]
        except composer.ComposeError as exc:
            missing.append(str(exc))

    return ColdEmailPreviewResponse(
        subject=subject,
        body=body,
        can_send=not missing,
        missing=missing,
        warnings=composer.spam_warnings(profile, attaches),
        attaches_resume=attaches,
        daily_limit=sender.effective_daily_limit(),
        configured_limit=settings.cold_email_daily_limit,
        warmup_days=settings.cold_email_warmup_days,
    )


@cold_email_router.get("/stats", response_model=ColdEmailStatsResponse)
def cold_email_stats(user: User = Depends(require_auth)) -> ColdEmailStatsResponse:
    """Return aggregate cold email campaign statistics."""
    sender = get_email_sender()
    with Session(engine) as session:
        all_logs = session.exec(
            select(ColdEmailLog).where(ColdEmailLog.user_id == user.user_id)
        ).all()

    total = len(all_logs)
    sent = sum(1 for e in all_logs if e.status == EmailStatus.SENT)
    failed = sum(1 for e in all_logs if e.status == EmailStatus.FAILED)
    pending = sum(1 for e in all_logs if e.status == EmailStatus.PENDING)

    return ColdEmailStatsResponse(
        total=total,
        sent=sent,
        failed=failed,
        pending=pending,
        remaining_today=sender.remaining_today(),
    )


def _mark_failed(run_id: int, message: str) -> None:
    with Session(engine) as session:
        rl = session.get(RunLog, run_id)
        if rl and rl.status == RunStatus.RUNNING:
            rl.status = RunStatus.FAILED
            rl.completed_at = datetime.now(timezone.utc)
            rl.error_message = message[:500]
            session.add(rl)
            session.commit()


def _mark_stopped(run_id: int) -> None:
    with Session(engine) as session:
        rl = session.get(RunLog, run_id)
        if rl and rl.status == RunStatus.RUNNING:
            rl.status = RunStatus.STOPPED
            rl.completed_at = datetime.now(timezone.utc)
            session.add(rl)
            session.commit()
