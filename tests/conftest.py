"""Shared fixtures. Every test runs against a throwaway SQLite file.

Environment is set before importing anything from `backend.app`, because
`database.engine` is created at module import time from the settings singleton.
"""
import os
import tempfile
from pathlib import Path

import pytest

_SCRATCH = Path(tempfile.mkdtemp(prefix="autoapply_tests_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_SCRATCH / 'test.db').as_posix()}"
os.environ["LOG_DIR"] = str(_SCRATCH / "logs")
os.environ["RESUMES_DIR"] = str(_SCRATCH / "resumes")
os.environ["SCREENSHOTS_DIR"] = str(_SCRATCH / "screenshots")
os.environ["CHROMA_PERSIST_DIR"] = str(_SCRATCH / "chroma")
os.environ.setdefault("GROQ_API_KEY", "test-key-never-called")

# ── Auth ─────────────────────────────────────────────────────────────────────
# Every /api route is authenticated, so the shared `client` fixture is signed
# in by default and the whole existing suite keeps working unchanged. Tests
# that need to prove a route *rejects* an anonymous caller use `anon_client`.
#
# Set as env vars before `backend.app` is imported, so the settings singleton
# picks them up naturally rather than needing a monkeypatch on every test.
TEST_USERNAME = "test-user"
TEST_PASSWORD = "test-password-123"
TEST_SECRET = "test-secret-key-for-suite-only"

# Only the signing key is env-configured now; the account itself is a database
# row, created by the `admin_user` fixture once the schema exists.
os.environ["AUTH_SECRET_KEY"] = TEST_SECRET


@pytest.fixture(scope="session")
def anon_client():
    """TestClient with no credentials — for asserting routes reject anonymous
    callers. Runs the real lifespan (schema creation + migrations).

    A separate instance from `client`, not the same one with headers toggled:
    both are session-scoped, so mutating shared headers would leak auth state
    between tests depending on which ran first.
    """
    from fastapi.testclient import TestClient

    from backend.app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def admin_user(anon_client):
    """The suite's own ACTIVE admin. Created directly rather than via signup,
    because signup deliberately produces a PENDING account that cannot log in."""
    from sqlmodel import Session

    from backend.app.database import engine
    from backend.app.services import users as user_service

    with Session(engine) as session:
        user = user_service.ensure_admin(session, TEST_USERNAME, TEST_PASSWORD)
        session.expunge(user)
    return user


@pytest.fixture(scope="session")
def auth_token(anon_client, admin_user):
    r = anon_client.post(
        "/api/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    )
    assert r.status_code == 200, f"test login failed: {r.text}"
    return r.json()["access_token"]


@pytest.fixture(scope="session")
def client(auth_token):
    """Signed-in TestClient. The default for essentially every test.

    `init()` is idempotent, so a second lifespan run here is harmless.
    """
    from fastapi.testclient import TestClient

    from backend.app.main import app

    with TestClient(app, headers={"Authorization": f"Bearer {auth_token}"}) as c:
        yield c


@pytest.fixture
def session():
    """A raw DB session, schema guaranteed to exist.

    `init()` (schema creation + migrations) previously only ran via the
    `client` fixture's lifespan — a test file whose first-run test used only
    `session`-based fixtures with no `client` anywhere before it would hit
    "no such table" when run in isolation (`pytest path/to/that_file.py`),
    while passing in the full suite purely because some *other* file happened
    to instantiate `client` first. `init()` is idempotent
    (`SQLModel.metadata.create_all` + ALTER-TABLE-if-missing migrations), so
    calling it here removes the ordering dependency entirely.
    """
    from sqlmodel import Session

    from backend.app.database import engine, init

    init()
    with Session(engine) as s:
        yield s


@pytest.fixture
def clean_jobs(session):
    """Truncate job-related tables so each test starts from a known state."""
    from sqlmodel import delete

    from backend.app.models.application import Application
    from backend.app.models.job import Job
    from backend.app.models.job_stage_event import JobStageEvent

    for model in (JobStageEvent, Application, Job):
        session.exec(delete(model))
    session.commit()
    yield session


@pytest.fixture
def make_job(clean_jobs, admin_user):
    """Factory for Job rows owned by the test admin.

    The owner matters: every read path filters on user_id, so an unowned row
    would be invisible to the very endpoints under test.
    """
    from backend.app.models.job import Job, JobStatus

    counter = {"n": 0}

    def _make(**kwargs):
        counter["n"] += 1
        n = counter["n"]
        defaults = dict(
            user_id=admin_user.user_id,
            url=f"https://example.com/jobs/{n}",
            title=f"Job {n}",
            company="Acme",
            portal="linkedin",
            status=JobStatus.SCRAPED,
        )
        defaults.update(kwargs)
        job = Job(**defaults)
        clean_jobs.add(job)
        clean_jobs.commit()
        clean_jobs.refresh(job)
        return job.job_id

    return _make
