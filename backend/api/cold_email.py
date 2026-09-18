from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File
from sqlmodel import Session, select

from backend.api.schemas import (
    ColdEmailImportResponse,
    ColdEmailStartRequest,
    ColdEmailStartResponse,
    ColdEmailStatsResponse,
)
from backend.api.websocket import manager
from backend.config import get_settings
from backend.db.database import engine
from backend.models.cold_email import ColdEmailLog, EmailStatus
from backend.models.run_log import RunLog, RunStatus, RunType
from backend.services.email_sender import get_email_sender
from workflows.cold_email_flow import run_cold_email
from workflows.state import ColdEmailConfig, EventCallback, FlowEvent

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
async def import_contacts(file: UploadFile = File(...)) -> ColdEmailImportResponse:
    """
    Upload an Excel or CSV contact list.
    Saved to data/uploads/ and path is returned for use with /start.
    """
    if not file.filename:
        raise HTTPException(status_code=422, detail="No filename provided")

    suffix = Path(file.filename).suffix.lower()
    if suffix not in (".xlsx", ".xls", ".csv"):
        raise HTTPException(status_code=422, detail="File must be .xlsx, .xls, or .csv")

    uploads_dir = Path("data/uploads")
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
async def start_cold_email(request: ColdEmailStartRequest) -> ColdEmailStartResponse:
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
    )
    on_event = _make_on_event(run_id)

    async def _task() -> None:
        try:
            await run_cold_email(config, on_event=on_event, existing_run_id=run_id)
        except asyncio.CancelledError:
            _mark_stopped(run_id)
        except Exception as exc:
            logger.error("Cold email run %d crashed: %s", run_id, exc, exc_info=True)
        finally:
            _active_email_tasks.pop(run_id, None)

    task = asyncio.create_task(_task())
    _active_email_tasks[run_id] = task
    return ColdEmailStartResponse(run_id=run_id)


@cold_email_router.get("/stats", response_model=ColdEmailStatsResponse)
def cold_email_stats() -> ColdEmailStatsResponse:
    """Return aggregate cold email campaign statistics."""
    sender = get_email_sender()
    with Session(engine) as session:
        all_logs = session.exec(select(ColdEmailLog)).all()

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


def _mark_stopped(run_id: int) -> None:
    with Session(engine) as session:
        rl = session.get(RunLog, run_id)
        if rl and rl.status == RunStatus.RUNNING:
            rl.status = RunStatus.STOPPED
            rl.completed_at = datetime.now(timezone.utc)
            session.add(rl)
            session.commit()
