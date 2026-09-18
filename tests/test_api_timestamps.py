"""Every timestamp the API returns must carry a timezone.

SQLite has no timezone type, so a `datetime` written as UTC comes back naive and
Pydantic emitted it as "2026-09-08T07:52:07" with no suffix. JavaScript reads an
offset-less timestamp as *local* time, which in IST (UTC+5:30) put every date in
the app 5.5 hours in the past — an account created seconds earlier displayed as
"6h ago", and the same was true of jobs, runs, resumes and the dashboard.

`schemas/types.UtcDatetime` fixes it, but only for fields that actually use it.
The static test below is the part that matters long term: it fails the moment
someone adds a plain `datetime` to a response schema, which is exactly how the
bug got in.
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
from datetime import datetime
from typing import Annotated, get_args, get_origin, get_type_hints

import pytest
from pydantic import BaseModel, PlainSerializer

import backend.app.schemas as schemas_pkg

# "2026-09-08T07:52:07" and friends, with or without fractional seconds.
ISO_LIKE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
HAS_TIMEZONE = re.compile(r"(?:Z|[+-]\d{2}:?\d{2})$")


def _response_models() -> list[type[BaseModel]]:
    """Every Pydantic model defined under backend.app.schemas."""
    models: list[type[BaseModel]] = []
    for mod_info in pkgutil.iter_modules(schemas_pkg.__path__):
        module = importlib.import_module(f"{schemas_pkg.__name__}.{mod_info.name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, BaseModel) and obj is not BaseModel \
                    and obj.__module__ == module.__name__:
                models.append(obj)
    return models


def _reaches_bare_datetime(annotation) -> bool:
    """True if a `datetime` is reachable without passing our serializer.

    Walks the annotation tree so `Optional[UtcDatetime]` and
    `list[UtcDatetime]` count as covered, while `Optional[datetime]` does not.
    """
    origin = get_origin(annotation)

    if origin is Annotated:
        inner, *metadata = get_args(annotation)
        if inner is datetime and any(isinstance(m, PlainSerializer) for m in metadata):
            return False
        return _reaches_bare_datetime(inner)

    if annotation is datetime:
        return True

    return any(_reaches_bare_datetime(arg) for arg in get_args(annotation))


class TestSchemasDeclareTimezones:
    def test_no_response_schema_uses_a_bare_datetime(self):
        """The guard that keeps the fix from rotting: a new `created_at:
        datetime` on any schema fails here rather than shipping wrong dates."""
        offenders = []
        for model in _response_models():
            hints = get_type_hints(model, include_extras=True)
            for field, annotation in hints.items():
                if _reaches_bare_datetime(annotation):
                    offenders.append(f"{model.__module__}.{model.__name__}.{field}")

        assert not offenders, (
            "these fields must use schemas.types.UtcDatetime, not a plain datetime: "
            + ", ".join(sorted(offenders))
        )

    def test_the_check_would_actually_catch_a_regression(self):
        """A test that can never fail is worse than no test — prove this one
        rejects the exact shape it is meant to reject."""
        from typing import Optional

        assert _reaches_bare_datetime(datetime)
        assert _reaches_bare_datetime(Optional[datetime])
        assert _reaches_bare_datetime(list[datetime])

    def test_the_check_accepts_the_annotated_type(self):
        from typing import Optional

        from backend.app.schemas.types import UtcDatetime

        assert not _reaches_bare_datetime(UtcDatetime)
        assert not _reaches_bare_datetime(Optional[UtcDatetime])


class TestSerializerBehaviour:
    def test_naive_value_is_stamped_utc(self):
        from backend.app.schemas.types import _to_utc_iso

        assert _to_utc_iso(datetime(2026, 9, 8, 7, 52, 7)) == "2026-09-08T07:52:07+00:00"

    def test_aware_value_is_converted_not_relabelled(self):
        """A +05:30 timestamp is 5.5 hours ahead of UTC — the clock time has to
        move, not just the suffix."""
        from datetime import timedelta, timezone

        from backend.app.schemas.types import _to_utc_iso

        ist = timezone(timedelta(hours=5, minutes=30))
        assert _to_utc_iso(datetime(2026, 9, 8, 13, 22, 7, tzinfo=ist)) \
            == "2026-09-08T07:52:07+00:00"

    def test_a_model_field_serialises_with_an_offset(self):
        from backend.app.schemas.auth import UserSummary

        summary = UserSummary(
            user_id=1, username="x", status="ACTIVE", role="USER",
            created_at=datetime(2026, 9, 8, 7, 52, 7),
        )
        dumped = summary.model_dump(mode="json")
        assert HAS_TIMEZONE.search(dumped["created_at"]), dumped["created_at"]

    def test_none_stays_none(self):
        from backend.app.schemas.auth import UserSummary

        summary = UserSummary(
            user_id=1, username="x", status="ACTIVE", role="USER",
            created_at=datetime(2026, 9, 8, 7, 52, 7), approved_at=None,
        )
        assert summary.model_dump(mode="json")["approved_at"] is None


class TestLiveResponses:
    """The static check proves the annotations are right; these prove the wire
    format is, through the real app and a real database."""

    def _assert_all_timestamps_zoned(self, payload, where: str):
        stack = [(payload, where)]
        checked = 0
        while stack:
            node, path = stack.pop()
            if isinstance(node, dict):
                stack.extend((v, f"{path}.{k}") for k, v in node.items())
            elif isinstance(node, list):
                stack.extend((v, f"{path}[{i}]") for i, v in enumerate(node))
            elif isinstance(node, str) and ISO_LIKE.match(node):
                assert HAS_TIMEZONE.search(node), f"{path} has no timezone: {node!r}"
                checked += 1
        return checked

    def test_accounts_list(self, client):
        body = client.get("/api/auth/users").json()
        assert self._assert_all_timestamps_zoned(body, "users") > 0

    def test_login_response(self, anon_client, admin_user):
        from tests.conftest import TEST_PASSWORD, TEST_USERNAME

        from backend.app.routers import auth as auth_router_module
        auth_router_module._reset_throttle_for_tests()

        body = anon_client.post("/api/auth/login", json={
            "username": TEST_USERNAME, "password": TEST_PASSWORD,
        }).json()
        assert self._assert_all_timestamps_zoned(body, "login") > 0

    def test_jobs_list(self, client, make_job):
        make_job()
        body = client.get("/api/jobs").json()
        assert self._assert_all_timestamps_zoned(body, "jobs") > 0

    def test_job_timeline(self, client, make_job):
        job_id = make_job()
        body = client.get(f"/api/jobs/{job_id}/timeline").json()
        assert self._assert_all_timestamps_zoned(body, "timeline") > 0

    def test_runs_list(self, client):
        self._assert_all_timestamps_zoned(client.get("/api/runs").json(), "runs")

    def test_resume_library(self, client):
        self._assert_all_timestamps_zoned(
            client.get("/api/resumes/library").json(), "resumes"
        )

    def test_dashboard_stats(self, client):
        self._assert_all_timestamps_zoned(
            client.get("/api/dashboard/stats").json(), "dashboard"
        )

    @pytest.mark.parametrize("value", [
        "2026-09-08T07:52:07",            # the exact shape of the bug
        "2026-09-08T07:52:07.454210",
    ])
    def test_the_live_check_would_catch_the_original_bug(self, value):
        assert ISO_LIKE.match(value) and not HAS_TIMEZONE.search(value)
