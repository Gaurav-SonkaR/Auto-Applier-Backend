from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from backend.app.schemas import DiscoveryQuotaResponse, RunResponse, RunStartRequest, RunStartResponse
from backend.app.routers.websocket import manager
from backend.app.config import get_settings
from backend.app.database import engine
from backend.app.models.run_log import RunLog, RunStatus, RunType
from backend.app.models.user import User
from backend.app.routers.auth import require_auth
from backend.app.services.discovery import get_discovery_agent
from backend.app.workflows.discovery_flow import run_discovery_pipeline
from backend.app.workflows.portal_apply_flow import run_portal_apply
from backend.app.workflows.state import DiscoveryConfig, EventCallback, FlowEvent, RunConfig

logger = logging.getLogger("api.runs")

runs_router = APIRouter(prefix="/api/runs", tags=["runs"])

# run_id → (owning user_id, task). The user_id is kept alongside so /stop
# and the active-run count can't reach across accounts.
_active_tasks: dict[int, tuple[int, asyncio.Task]] = {}


def _make_on_event(run_id: int) -> EventCallback:
    async def _callback(event: FlowEvent) -> None:
        payload = {
            "run_id": event.run_id,
            "job_id": event.job_id,
            "node": event.node,
            "status": event.status,
            "message": event.message,
            "data": event.data,
        }
        await manager.broadcast(run_id, payload)
    return _callback


def _run_log_to_response(rl: RunLog) -> RunResponse:
    config: dict = {}
    if rl.config_snapshot:
        try:
            config = json.loads(rl.config_snapshot)
        except Exception as exc:
            logger.warning("Run %s has unparseable config_snapshot: %s", rl.run_id, exc)
    return RunResponse(
        run_id=rl.run_id,
        run_type=rl.run_type.value,
        status=rl.status.value,
        portal=config.get("portal"),
        job_title=config.get("job_title"),
        location=config.get("location"),
        batch_size=config.get("batch_size"),
        jobs_scraped=rl.jobs_scraped,
        jobs_applied=rl.jobs_applied,
        jobs_ready=rl.jobs_ready,
        jobs_failed=rl.jobs_failed,
        jobs_flagged=rl.jobs_flagged,
        jobs_skipped=rl.jobs_skipped,
        started_at=rl.started_at,
        completed_at=rl.completed_at,
        error_message=rl.error_message,
    )


@runs_router.post("/start", response_model=RunStartResponse, status_code=202)
async def start_run(
    request: RunStartRequest, user: User = Depends(require_auth)
) -> RunStartResponse:
    """Start a portal apply run in the background. Returns run_id immediately."""
    # Pre-create RunLog so client gets a real ID before the task starts
    with Session(engine) as session:
        run_log = RunLog(
            user_id=user.user_id,
            run_type=RunType.PORTAL_APPLY,
            started_at=datetime.now(timezone.utc),
            config_snapshot=json.dumps(request.model_dump()),
        )
        session.add(run_log)
        session.commit()
        session.refresh(run_log)
        run_id = run_log.run_id

    on_event = _make_on_event(run_id)

    async def _task() -> None:
        try:
            if request.portal == "web":
                # Google Discovery: find company career pages, apply directly
                discovery_config = DiscoveryConfig(
                    job_title=request.job_title,
                    location=request.location,
                    batch_size=request.batch_size,
                    user_id=user.user_id,
                )
                await run_discovery_pipeline(
                    discovery_config, on_event=on_event, existing_run_id=run_id
                )
            else:
                config = RunConfig(
                    portal=request.portal,
                    job_title=request.job_title,
                    location=request.location,
                    batch_size=request.batch_size,
                    include_checkpointed=request.include_checkpointed,
                    user_id=user.user_id,
                    dry_run=request.dry_run,
                )
                await run_portal_apply(config, on_event=on_event, existing_run_id=run_id)
        except asyncio.CancelledError:
            _mark_stopped(run_id)
            logger.info("Run %d cancelled", run_id)
        except Exception as exc:
            logger.error("Run %d crashed: %s", run_id, exc, exc_info=True)
            _mark_failed(run_id, str(exc))
        finally:
            _active_tasks.pop(run_id, None)

    task = asyncio.create_task(_task())
    _active_tasks[run_id] = (user.user_id, task)
    logger.info("Run %d created and started", run_id)
    return RunStartResponse(run_id=run_id)


@runs_router.get("/", response_model=list[RunResponse])
def list_runs(
    limit: int = 20, offset: int = 0, user: User = Depends(require_auth)
) -> list[RunResponse]:
    """List all runs, most recent first."""
    with Session(engine) as session:
        logs = session.exec(
            select(RunLog)
            .where(RunLog.user_id == user.user_id)
            .order_by(RunLog.started_at.desc()).offset(offset).limit(limit)  # type: ignore[attr-defined]
        ).all()
    return [_run_log_to_response(rl) for rl in logs]


@runs_router.get("/discovery-quota", response_model=DiscoveryQuotaResponse)
def get_discovery_quota(_user: User = Depends(require_auth)) -> DiscoveryQuotaResponse:
    """Remaining Serper.dev searches today — surfaces the daily quota to the Company Career card."""
    settings = get_settings()
    agent = get_discovery_agent()
    return DiscoveryQuotaResponse(
        remaining_today=agent.remaining_today(),
        daily_limit=settings.daily_search_limit,
        serper_configured=bool(settings.serper_api_key),
    )


@runs_router.get("/{run_id}", response_model=RunResponse)
def get_run(run_id: int, user: User = Depends(require_auth)) -> RunResponse:
    with Session(engine) as session:
        rl = session.get(RunLog, run_id)
    if not rl or rl.user_id != user.user_id:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return _run_log_to_response(rl)


@runs_router.post("/{run_id}/stop", response_model=RunResponse)
def stop_run(run_id: int, user: User = Depends(require_auth)) -> RunResponse:
    """Cancel a running task and mark it STOPPED."""
    # Ownership is checked before cancelling anything — otherwise one account
    # could stop another's run just by guessing the id.
    with Session(engine) as session:
        rl = session.get(RunLog, run_id)
    if not rl or rl.user_id != user.user_id:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    entry = _active_tasks.get(run_id)
    if entry and not entry[1].done():
        entry[1].cancel()
    else:
        _mark_stopped(run_id)

    with Session(engine) as session:
        rl = session.get(RunLog, run_id)
    return _run_log_to_response(rl)


def active_run_ids(user_id: Optional[int] = None) -> list[int]:
    """Run ids in flight. Filtered by owner when one is given, so the dashboard
    count reflects the caller's runs and not the whole process."""
    if user_id is None:
        return list(_active_tasks.keys())
    return [rid for rid, (owner, _) in _active_tasks.items() if owner == user_id]


# ── Internal helpers ───────────────────────────────────────────────────────────

def _mark_stopped(run_id: int) -> None:
    with Session(engine) as session:
        rl = session.get(RunLog, run_id)
        if rl and rl.status == RunStatus.RUNNING:
            rl.status = RunStatus.STOPPED
            rl.completed_at = datetime.now(timezone.utc)
            rl.error_message = "Stopped by user"
            session.add(rl)
            session.commit()


def _mark_failed(run_id: int, error: str = "") -> None:
    with Session(engine) as session:
        rl = session.get(RunLog, run_id)
        if rl:
            rl.status = RunStatus.FAILED
            rl.completed_at = datetime.now(timezone.utc)
            if error:
                rl.error_message = error[:500]
            session.add(rl)
            session.commit()
