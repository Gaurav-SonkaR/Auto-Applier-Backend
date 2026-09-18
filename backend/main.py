from __future__ import annotations

import logging
import logging.config
import logging.handlers
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from datetime import datetime, timezone

from sqlmodel import Session, select

from backend.api.cold_email import cold_email_router
from backend.api.dashboard import dashboard_router
from backend.api.jobs import jobs_router
from backend.api.resumes import resumes_router
from backend.api.runs import runs_router
from backend.api.websocket import ws_router
from backend.config import get_settings
from backend.db.database import engine
from backend.db.init_db import init
from backend.models.run_log import RunLog, RunStatus

logger = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _configure_logging(settings.log_level, settings.log_dir)
    settings.ensure_directories()
    init()
    _reconcile_stale_runs()
    logger.info("AutoApply AI backend starting on %s:%d", settings.api_host, settings.api_port)
    yield
    logger.info("AutoApply AI backend shutting down")


def _reconcile_stale_runs() -> None:
    """
    Mark any RunLog left RUNNING as FAILED at startup.

    _active_tasks (backend/api/runs.py) is in-memory only — it's empty on every
    process start, so a run that was RUNNING when the previous process crashed,
    was killed, or was restarted has no task to resume and would otherwise stay
    RUNNING forever, permanently confusing the dashboard's active-run detection.
    """
    with Session(engine) as session:
        stale = session.exec(
            select(RunLog).where(RunLog.status == RunStatus.RUNNING)
        ).all()
        for run in stale:
            run.status = RunStatus.FAILED
            run.completed_at = datetime.now(timezone.utc)
            run.error_message = "Interrupted by backend restart"
            session.add(run)
        if stale:
            session.commit()
            logger.warning("Reconciled %d stale RUNNING run(s) on startup", len(stale))


def _configure_logging(level: str, log_dir: str) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s %(levelname)-8s %(name)s — %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            # Console — all levels
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "default",
            },
            # app.log — everything (rotating 10 MB × 3 backups)
            "file_app": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": f"{log_dir}/app.log",
                "maxBytes": 10_485_760,
                "backupCount": 3,
                "formatter": "default",
                "encoding": "utf-8",
            },
            # errors.log — ERROR and above only
            "file_errors": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": f"{log_dir}/errors.log",
                "maxBytes": 5_242_880,
                "backupCount": 2,
                "formatter": "default",
                "encoding": "utf-8",
                "level": "ERROR",
            },
            # scraper.log — Playwright agent events (agent.scraper + agent.apply)
            "file_scraper": {
                "class": "logging.FileHandler",
                "filename": f"{log_dir}/scraper.log",
                "formatter": "default",
                "encoding": "utf-8",
            },
            # llm.log — LLM router calls, token counts, fallbacks
            "file_llm": {
                "class": "logging.FileHandler",
                "filename": f"{log_dir}/llm.log",
                "formatter": "default",
                "encoding": "utf-8",
            },
        },
        "loggers": {
            # All agent.* loggers (scraper, apply, resume, jd_parser, discovery)
            # go to scraper.log AND propagate to root (→ app.log + console)
            "agent": {
                "handlers": ["file_scraper"],
                "level": "DEBUG",
                "propagate": True,
            },
            # LLM router goes to llm.log AND propagates to root
            "llm": {
                "handlers": ["file_llm"],
                "level": "DEBUG",
                "propagate": True,
            },
        },
        "root": {
            "handlers": ["console", "file_app", "file_errors"],
            "level": level,
        },
    })


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="AutoApply AI",
        description="Autonomous job application system — local only",
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # CORS — only allow local frontend (Vite dev server)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            f"http://127.0.0.1:{settings.frontend_port}",
            f"http://localhost:{settings.frontend_port}",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Routers
    app.include_router(runs_router)
    app.include_router(jobs_router)
    app.include_router(resumes_router)
    app.include_router(cold_email_router)
    app.include_router(dashboard_router)
    app.include_router(ws_router)  # WebSocket (no prefix — path is /ws/runs/{id})

    # Global 500 handler — never expose tracebacks to client
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled exception on %s: %s", request.url.path, exc, exc_info=True)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "AutoApply AI"}

    return app


app = create_app()
