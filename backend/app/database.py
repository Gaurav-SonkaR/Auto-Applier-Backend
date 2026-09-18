from __future__ import annotations

import logging
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import event, text
from sqlmodel import Session, SQLModel, create_engine

from backend.app.config import get_settings

logger = logging.getLogger(__name__)


def _make_engine():
    settings = get_settings()
    db_path = settings.database_url.replace("sqlite:///", "")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False is required for FastAPI (multiple threads share one engine)
    connect_args = {"check_same_thread": False}
    eng = create_engine(
        settings.database_url,
        connect_args=connect_args,
        echo=False,
    )

    @event.listens_for(eng, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _record):
        # WAL lets dashboard reads proceed while a run is writing (readers
        # never block behind the writer); busy_timeout makes any residual
        # lock contention wait up to 5s instead of failing immediately with
        # "database is locked".
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return eng


engine = _make_engine()


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


# ── Schema creation / migrations ──────────────────────────────────────────────

def init() -> None:
    # Imported inside init() so SQLModel.metadata knows every table before
    # create_all. Deferring it also keeps module import order irrelevant —
    # models never have to be loaded just to get at `engine`.
    import backend.app.models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    _migrate_run_log()
    _migrate_job()
    _migrate_user()
    _migrate_qa_answer()
    _create_indexes()
    logger.info("Database initialized at %s", engine.url)


def _apply_idempotent_migrations(statements: list[str]) -> None:
    """Run ALTER TABLE statements, ignoring only 'column already exists' —
    any other failure (locked DB, syntax error, disk full) is a real problem
    and must be surfaced, not silently swallowed."""
    with engine.connect() as conn:
        for stmt in statements:
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception as exc:
                if "duplicate column" in str(exc).lower():
                    continue  # already migrated — expected on every startup
                logger.error("Migration failed: %s — %s", stmt, exc)
                raise


def _migrate_run_log() -> None:
    """Add columns introduced after initial schema creation (safe to re-run)."""
    _apply_idempotent_migrations([
        "ALTER TABLE run_log ADD COLUMN jobs_skipped INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE run_log ADD COLUMN error_message TEXT",
        "ALTER TABLE run_log ADD COLUMN jobs_ready INTEGER NOT NULL DEFAULT 0",
    ])


def _migrate_job() -> None:
    """Add columns introduced after initial schema creation (safe to re-run)."""
    _apply_idempotent_migrations([
        # run_id predates this migration but was never actually added to older DBs —
        # closing that pre-existing gap here since it silently broke checkpoint/resume linkage.
        "ALTER TABLE job ADD COLUMN run_id INTEGER",
        "ALTER TABLE job ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE job ADD COLUMN ats_score INTEGER",
        "ALTER TABLE job ADD COLUMN redirect_url TEXT",
        "ALTER TABLE job ADD COLUMN screenshot_path TEXT",
        "ALTER TABLE job ADD COLUMN outcome TEXT",
        "ALTER TABLE job ADD COLUMN outcome_updated_at TIMESTAMP",
        "ALTER TABLE job ADD COLUMN resume_storage_ref TEXT",
    ])


def _migrate_user() -> None:
    """Password-reset columns, added after accounts moved into the DB."""
    _apply_idempotent_migrations([
        "ALTER TABLE user ADD COLUMN reset_requested_at TIMESTAMP",
        "ALTER TABLE user ADD COLUMN reset_code_hash TEXT",
        "ALTER TABLE user ADD COLUMN reset_approved_at TIMESTAMP",
        "ALTER TABLE user ADD COLUMN reset_approved_by TEXT",
    ])


def _migrate_qa_answer() -> None:
    """Question-bank columns on what was a hidden answer cache."""
    _apply_idempotent_migrations([
        "ALTER TABLE qa_answer ADD COLUMN mode TEXT NOT NULL DEFAULT 'fixed'",
        "ALTER TABLE qa_answer ADD COLUMN category TEXT NOT NULL DEFAULT 'Other'",
        # Existing rows were written by a model without anyone checking them.
        "ALTER TABLE qa_answer ADD COLUMN source TEXT NOT NULL DEFAULT 'generated'",
        "ALTER TABLE qa_answer ADD COLUMN needs_review BOOLEAN NOT NULL DEFAULT 1",
        "ALTER TABLE qa_answer ADD COLUMN updated_at TIMESTAMP",
    ])


def _create_indexes() -> None:
    indexes = [
        # Fast status filtering (dashboard, pipeline resume)
        "CREATE INDEX IF NOT EXISTS idx_job_status ON job (status)",
        # Fast portal filtering
        "CREATE INDEX IF NOT EXISTS idx_job_portal ON job (portal)",
        # Deduplication guard, scoped per owner — two users may each track
        # the same posting, but neither may hold it twice.
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_job_user_url ON job (user_id, url)",
        # Run-log backref lookups (checkpoint/resume linkage)
        "CREATE INDEX IF NOT EXISTS idx_job_run_id ON job (run_id)",
        # Cold email deduplication, also per owner
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_email_unique "
        "ON cold_email_log (user_id, company_name, contact_email)",
        # Date-based daily limit queries
        "CREATE INDEX IF NOT EXISTS idx_email_sent_at ON cold_email_log (sent_at)",
        # Run status filtering
        "CREATE INDEX IF NOT EXISTS idx_run_status ON run_log (status)",
        # Per-job timeline lookups (job detail page, human-queue stuck-time ordering)
        "CREATE INDEX IF NOT EXISTS idx_stage_event_job ON job_stage_event (job_id)",
        # updated_at ordering appears in list_jobs (DESC), human-queue (ASC),
        # and the stuck-jobs cutoff filter — the one sort column used everywhere
        "CREATE INDEX IF NOT EXISTS idx_job_updated_at ON job (updated_at)",
        # One banked answer per account per question. Unique rather than plain:
        # the upsert in question_bank relies on there being at most one row to find,
        # and the old vector store had no per-account key at all.
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_qa_user_question "
        "ON qa_answer (user_id, question_key)",
        # Every list endpoint filters by owner first.
        "CREATE INDEX IF NOT EXISTS idx_job_user ON job (user_id)",
        "CREATE INDEX IF NOT EXISTS idx_resume_user ON resume (user_id)",
        "CREATE INDEX IF NOT EXISTS idx_run_user ON run_log (user_id)",
    ]
    with engine.connect() as conn:
        for stmt in indexes:
            conn.execute(text(stmt))
        conn.commit()
