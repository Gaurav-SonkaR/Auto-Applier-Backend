from __future__ import annotations

import logging

from sqlalchemy import text
from sqlmodel import SQLModel

# Import all models so SQLModel.metadata knows about them
import backend.models  # noqa: F401
from backend.db.database import engine

logger = logging.getLogger(__name__)


def init() -> None:
    SQLModel.metadata.create_all(engine)
    _migrate_run_log()
    _migrate_job()
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
    ])


def _create_indexes() -> None:
    indexes = [
        # Fast status filtering (dashboard, pipeline resume)
        "CREATE INDEX IF NOT EXISTS idx_job_status ON job (status)",
        # Fast portal filtering
        "CREATE INDEX IF NOT EXISTS idx_job_portal ON job (portal)",
        # Unique URL — deduplication guard (also covered by UNIQUE constraint, belt-and-suspenders)
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_job_url ON job (url)",
        # Run-log backref lookups (checkpoint/resume linkage)
        "CREATE INDEX IF NOT EXISTS idx_job_run_id ON job (run_id)",
        # Cold email deduplication
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_email_unique ON cold_email_log (company_name, contact_email)",
        # Date-based daily limit queries
        "CREATE INDEX IF NOT EXISTS idx_email_sent_at ON cold_email_log (sent_at)",
        # Run status filtering
        "CREATE INDEX IF NOT EXISTS idx_run_status ON run_log (status)",
        # Per-job timeline lookups (job detail page, human-queue stuck-time ordering)
        "CREATE INDEX IF NOT EXISTS idx_stage_event_job ON job_stage_event (job_id)",
        # updated_at ordering appears in list_jobs (DESC), human-queue (ASC),
        # and the stuck-jobs cutoff filter — the one sort column used everywhere
        "CREATE INDEX IF NOT EXISTS idx_job_updated_at ON job (updated_at)",
    ]
    with engine.connect() as conn:
        for stmt in indexes:
            conn.execute(text(stmt))
        conn.commit()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init()
    print("Database initialized successfully.")
