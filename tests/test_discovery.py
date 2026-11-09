"""Company Career Discovery duplicate handling under concurrent inserts."""
from sqlalchemy.exc import IntegrityError
from types import SimpleNamespace
import pytest

from backend.app.workflows import discovery_flow
from backend.app.workflows.discovery_flow import _persist_discovered_postings


class DuplicateSession:
    def __init__(self):
        self.rolled_back = False

    def exec(self, _statement):
        class EmptyResult:
            @staticmethod
            def first():
                return None

        return EmptyResult()

    def add(self, _row):
        pass

    def commit(self):
        # Simulate another run winning the per-user URL unique-index race.
        raise IntegrityError("insert", {}, RuntimeError("duplicate"))

    def rollback(self):
        self.rolled_back = True


def test_concurrent_duplicate_insert_is_skipped_not_raised():
    session = DuplicateSession()

    new_jobs = _persist_discovered_postings(
        session,
        [("https://example.com/jobs/42", "Software Engineer")],
        user_id=7,
        run_id=1,
        display_title="Software Engineer",
    )

    assert session.rolled_back
    assert new_jobs == []


@pytest.fixture
def smart_profile(monkeypatch):
    profile = {
        "experience": [{"title": "Software Developer"}],
        "primary_skills": ["Python", "Django", "FastAPI"],
        "preferences": {
            "total_experience_years": "1",
            "preferred_locations": ["Bengaluru"],
        },
        "llm_rules": {"source_notes": {"target_roles": [
            "Backend Engineer", "AI Engineer", "Full-Stack Engineer",
        ]}},
    }
    monkeypatch.setattr(discovery_flow, "_load_smart_profile", lambda _user_id: profile)
    monkeypatch.setattr(
        discovery_flow, "get_settings", lambda: SimpleNamespace(discovery_recency_days=2)
    )
    return profile


def test_smart_queries_use_profile_roles_skills_experience_and_preferred_location(smart_profile):
    queries = discovery_flow._build_smart_queries("", user_id=7)

    assert 1 <= len(queries) <= 6
    joined = "\n".join(queries)
    assert "Backend Engineer" in joined
    assert "AI Engineer" in joined
    assert "Python" in joined
    assert "1 year experience" in joined
    assert "Bengaluru" in joined
    assert all("after:" in query and "before:" in query for query in queries)


def test_smart_queries_fail_when_role_or_primary_skills_are_missing(monkeypatch):
    monkeypatch.setattr(discovery_flow, "_load_smart_profile", lambda _user_id: {})
    monkeypatch.setattr(
        discovery_flow, "get_settings", lambda: SimpleNamespace(discovery_recency_days=2)
    )

    with pytest.raises(ValueError, match="target role.*primary skills"):
        discovery_flow._build_smart_queries("", user_id=7)


def test_typed_profile_queries_also_include_recency_window():
    queries = discovery_flow._build_profile_queries("Python Developer", "Pune", 2)

    assert queries
    assert all("after:" in query and "before:" in query for query in queries)
