from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from sqlmodel import Session

from backend.app.database import engine
from backend.app.models.run_log import RunLog
from backend.app.routers.auth import authenticate_token

logger = logging.getLogger("api.ws")

ws_router = APIRouter()


class ConnectionManager:
    """
    In-memory WebSocket pub-sub.
    Clients subscribe to a run_id; the flow's on_event callback broadcasts to them.
    """

    def __init__(self) -> None:
        self._connections: dict[int, list[WebSocket]] = {}

    async def connect(self, run_id: int, ws: WebSocket) -> None:
        await ws.accept()
        self._connections.setdefault(run_id, []).append(ws)
        logger.info("WS client connected — run_id=%d total=%d", run_id, len(self._connections[run_id]))

    def disconnect(self, run_id: int, ws: WebSocket) -> None:
        bucket = self._connections.get(run_id, [])
        if ws in bucket:
            bucket.remove(ws)
        if not bucket:
            self._connections.pop(run_id, None)
        logger.info("WS client disconnected — run_id=%d", run_id)

    async def broadcast(self, run_id: int, payload: dict) -> None:
        dead: list[WebSocket] = []
        for ws in self._connections.get(run_id, []):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(run_id, ws)

    def active_count(self, run_id: int) -> int:
        return len(self._connections.get(run_id, []))


# Module-level singleton — imported by runs.py and cold_email.py routers
manager = ConnectionManager()


@ws_router.websocket("/ws/runs/{run_id}")
async def websocket_run_events(
    ws: WebSocket, run_id: int, token: Optional[str] = None
) -> None:
    """
    WebSocket endpoint for live run progress.
    Connect before or after starting a run — events are pushed as they happen.
    Client receives JSON objects matching the WSEvent schema.

    Auth arrives as `?token=...` rather than an Authorization header, because
    browsers cannot set headers on a WebSocket handshake. Same token, same
    verification as every HTTP route — just a different carrier. Rejected
    before `accept()`, so an unauthenticated client never gets a live socket
    or sees a single event.
    """
    user = authenticate_token(token)
    with Session(engine) as session:
        run = session.get(RunLog, run_id)
        owns_run = run is not None and run.user_id == user.user_id if user else False

    if not owns_run:
        # 1008 = policy violation. Closing pre-accept is the documented way to
        # refuse a WS handshake. Use the same response for missing and
        # cross-account runs so run ids cannot be enumerated.
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        logger.warning("Rejected unauthorized WS connection for run_id=%d", run_id)
        return

    await manager.connect(run_id, ws)
    try:
        while True:
            # Keep connection alive; the server pushes, client just listens
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        # Non-graceful drop (e.g. ConnectionResetError) — must still deregister,
        # or the manager keeps a dead socket forever.
        logger.warning("WS client dropped abnormally — run_id=%d: %s", run_id, exc)
    finally:
        manager.disconnect(run_id, ws)
