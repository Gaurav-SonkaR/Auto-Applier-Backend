from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from backend.config import get_settings


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
