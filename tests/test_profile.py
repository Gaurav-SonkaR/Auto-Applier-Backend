"""Per-account master profile.

The profile is the one place in the app that is pure personal data — the name,
email and phone printed on every generated resume and typed into every
application form. It used to be a single `master_profile.json` read by every
run, which was fine for a one-person tool and wrong the moment signup opened:
one account's resume would carry another account's contact details.

These tests pin down that isolation, the shape guarantees the resume prompt
depends on, and the one-time import that keeps the original install's data.
"""
from __future__ import annotations

import json

import pytest
from sqlmodel import delete

from backend.app.models.user import User
from backend.app.models.user_profile import UserProfile
from backend.app.services import profiles, users as user_service

FULL_PROFILE = {
    "personal": {
        "name": "Gaurav Sonkar",
        "email": "gaurav@example.com",
        "phone": "+91-9131930576",
        "linkedin": "linkedin.com/in/example",
        "github": "github.com/example",
        "kaggle": "kaggle.com/example",
        "location": "India",
    },
    "primary_skills": ["Python", "FastAPI"],
    "secondary_skills": ["Kubernetes"],
    "experience": [{"title": "Software Developer", "company": "Divyal", "bullets": ["Did a thing"]}],
    "projects": [{"name": "AutoApply", "tech_stack": ["FastAPI"], "bullets": ["Built it"]}],
    "education": {"degree": "MCA", "institution": "BHU"},
    "achievements": ["Shipped something"],
}


@pytest.fixture
def other_user(session):
    """A second real account, to prove one cannot see the other's profile."""
    username = "profile-neighbour"
    session.exec(delete(User).where(User.username == username))
    session.commit()
    user = user_service.create_user(
        session, username, "neighbour-pass-1",
        status=user_service.UserStatus.ACTIVE,
    )
    session.expunge(user)
    yield user
    session.exec(delete(UserProfile).where(UserProfile.user_id == user.user_id))
    session.exec(delete(User).where(User.username == username))
    session.commit()


@pytest.fixture
def clean_profile(session, admin_user):
    """Start each test from a known state for the suite's admin."""
    session.exec(delete(UserProfile).where(UserProfile.user_id == admin_user.user_id))
    session.commit()
    yield admin_user
    session.exec(delete(UserProfile).where(UserProfile.user_id == admin_user.user_id))
    session.commit()


# ── Shape ───────────────────────────────────────────────────────────────────

class TestShape:
    def test_blank_profile_has_every_key_consumers_read(self):
        """The resume prompt and form autofill index straight into this
        document; a missing key surfaces as a KeyError mid-run."""
        blank = profiles.blank_profile()
        for key in ("personal", "primary_skills", "secondary_skills", "experience",
                    "projects", "education", "achievements", "llm_rules"):
            assert key in blank

    def test_blank_profile_carries_the_llm_guardrails(self):
        rules = profiles.blank_profile()["llm_rules"]
        assert rules["locked_fields"]
        assert rules["content_limits"]["summary_max_words"] == 70

    def test_blank_profiles_do_not_share_mutable_state(self):
        """A shallow copy here would let one account's edits leak into the next
        blank profile handed out."""
        first = profiles.blank_profile()
        first["llm_rules"]["content_limits"]["summary_max_words"] = 1
        assert profiles.blank_profile()["llm_rules"]["content_limits"]["summary_max_words"] == 70

    def test_partial_input_is_filled_out(self):
        out = profiles.normalise({"personal": {"name": "X"}})
        assert out["primary_skills"] == []
        assert out["education"]["degree"] == ""
        assert out["llm_rules"]["locked_fields"]

    def test_unknown_personal_fields_are_dropped(self):
        out = profiles.normalise({"personal": {"name": "X", "ssn": "secret"}})
        assert "ssn" not in out["personal"]

    def test_omitting_llm_rules_does_not_wipe_them(self):
        """The editor never sends them, so a replace-not-merge would silently
        remove every guardrail on the resume LLM."""
        out = profiles.normalise({"personal": {"name": "X"}})
        assert out["llm_rules"]["secondary_skill_rule"]

    def test_non_object_is_rejected(self):
        with pytest.raises(profiles.ProfileError):
            profiles.normalise(["not", "an", "object"])


class TestValidation:
    def test_accepts_a_full_profile(self):
        assert profiles.validate(FULL_PROFILE)["personal"]["name"] == "Gaurav Sonkar"

    def test_experience_needs_a_title(self):
        bad = {**FULL_PROFILE, "experience": [{"company": "Divyal"}]}
        with pytest.raises(profiles.ProfileError, match="title"):
            profiles.validate(bad)

    def test_project_needs_a_name(self):
        bad = {**FULL_PROFILE, "projects": [{"tech_stack": ["x"]}]}
        with pytest.raises(profiles.ProfileError, match="name"):
            profiles.validate(bad)

    def test_skills_must_be_strings(self):
        bad = {**FULL_PROFILE, "primary_skills": [{"name": "Python"}]}
        with pytest.raises(profiles.ProfileError, match="primary_skills"):
            profiles.validate(bad)

    def test_obviously_wrong_email_is_rejected(self):
        bad = {**FULL_PROFILE, "personal": {**FULL_PROFILE["personal"], "email": "nope"}}
        with pytest.raises(profiles.ProfileError, match="Email"):
            profiles.validate(bad)

    def test_blank_email_is_allowed_while_still_filling_it_in(self):
        ok = {**FULL_PROFILE, "personal": {**FULL_PROFILE["personal"], "email": ""}}
        assert profiles.validate(ok)["personal"]["email"] == ""


class TestCompleteness:
    def test_blank_is_not_complete(self):
        assert not profiles.is_complete(profiles.blank_profile())

    def test_full_is_complete(self):
        assert profiles.is_complete(profiles.validate(FULL_PROFILE))

    def test_name_and_email_are_both_required(self):
        no_email = {**FULL_PROFILE, "personal": {**FULL_PROFILE["personal"], "email": ""}}
        assert not profiles.is_complete(profiles.validate(no_email))


# ── Storage ─────────────────────────────────────────────────────────────────

class TestStorage:
    def test_round_trips(self, session, clean_profile):
        profiles.save_profile(session, clean_profile.user_id, FULL_PROFILE)
        loaded = profiles.get_profile(session, clean_profile.user_id)
        assert loaded["personal"]["phone"] == "+91-9131930576"
        assert loaded["experience"][0]["bullets"] == ["Did a thing"]

    def test_unsaved_account_gets_a_blank_profile(self, session, clean_profile):
        assert profiles.get_profile(session, clean_profile.user_id) == profiles.blank_profile()

    def test_a_corrupt_row_degrades_instead_of_crashing_a_run(
        self, session, clean_profile
    ):
        """A run that dies on unparseable JSON is worse than one that applies
        with a blank profile and says so in the log."""
        session.add(UserProfile(user_id=clean_profile.user_id, data="{not json"))
        session.commit()
        assert profiles.get_profile(session, clean_profile.user_id) == profiles.blank_profile()

    def test_null_user_id_does_not_hit_the_database(self, session):
        assert profiles.get_profile(session, None) == profiles.blank_profile()

    def test_saving_twice_updates_rather_than_duplicating(self, session, clean_profile):
        profiles.save_profile(session, clean_profile.user_id, FULL_PROFILE)
        changed = {**FULL_PROFILE, "personal": {**FULL_PROFILE["personal"], "name": "Renamed"}}
        profiles.save_profile(session, clean_profile.user_id, changed)
        assert profiles.get_profile(session, clean_profile.user_id)["personal"]["name"] == "Renamed"


class TestSizeLimit:
    """The whole profile is pasted into the resume prompt on every generation,
    so an unbounded one costs tokens on every run, not just disk."""

    def _huge(self):
        return {**FULL_PROFILE,
                "achievements": ["x" * 1000] * (profiles.MAX_PROFILE_CHARS // 1000 + 5)}

    def test_an_oversized_profile_is_refused(self, session, clean_profile):
        with pytest.raises(profiles.ProfileError, match="too large"):
            profiles.save_profile(session, clean_profile.user_id, self._huge())

    def test_refusing_it_does_not_store_a_partial_document(self, session, clean_profile):
        profiles.save_profile(session, clean_profile.user_id, FULL_PROFILE)
        with pytest.raises(profiles.ProfileError):
            profiles.save_profile(session, clean_profile.user_id, self._huge())
        assert profiles.get_profile(session, clean_profile.user_id)["personal"]["name"]             == "Gaurav Sonkar"

    def test_a_normal_profile_is_nowhere_near_the_limit(self):
        import json

        assert len(json.dumps(profiles.validate(FULL_PROFILE))) < profiles.MAX_PROFILE_CHARS / 10

    def test_the_api_reports_it_as_a_422(self, client, clean_profile):
        r = client.put("/api/profile", json={"profile": self._huge()})
        assert r.status_code == 422
        assert "too large" in r.json()["detail"].lower()


class TestIsolation:
    """The reason this moved off disk in the first place."""

    def test_one_account_cannot_see_another_profile(
        self, session, clean_profile, other_user
    ):
        profiles.save_profile(session, clean_profile.user_id, FULL_PROFILE)
        neighbour = profiles.get_profile(session, other_user.user_id)
        assert neighbour["personal"]["name"] == ""
        assert neighbour["personal"]["email"] == ""

    def test_saving_one_does_not_touch_the_other(self, session, clean_profile, other_user):
        other = {**FULL_PROFILE, "personal": {**FULL_PROFILE["personal"],
                                              "name": "Someone Else",
                                              "email": "else@example.com"}}
        profiles.save_profile(session, clean_profile.user_id, FULL_PROFILE)
        profiles.save_profile(session, other_user.user_id, other)

        assert profiles.get_profile(session, clean_profile.user_id)["personal"]["name"] \
            == "Gaurav Sonkar"
        assert profiles.get_profile(session, other_user.user_id)["personal"]["name"] \
            == "Someone Else"


# ── API ─────────────────────────────────────────────────────────────────────

class TestProfileApi:
    def test_requires_auth(self, anon_client):
        assert anon_client.get("/api/profile").status_code == 401
        assert anon_client.put("/api/profile", json={"profile": {}}).status_code == 401

    def test_get_returns_a_usable_shape_before_anything_is_saved(
        self, client, clean_profile
    ):
        body = client.get("/api/profile").json()
        assert body["is_complete"] is False
        assert "personal" in body["profile"]
        assert body["updated_at"] is None

    def test_put_then_get(self, client, clean_profile):
        put = client.put("/api/profile", json={"profile": FULL_PROFILE})
        assert put.status_code == 200, put.text
        assert put.json()["is_complete"] is True

        body = client.get("/api/profile").json()
        assert body["profile"]["personal"]["name"] == "Gaurav Sonkar"
        assert body["updated_at"] is not None

    def test_put_rejects_invalid_input_with_a_readable_message(self, client, clean_profile):
        r = client.put("/api/profile", json={
            "profile": {**FULL_PROFILE, "experience": [{"company": "No title here"}]},
        })
        assert r.status_code == 422
        assert "title" in r.json()["detail"].lower()

    def test_a_rejected_put_does_not_partially_save(self, client, clean_profile):
        client.put("/api/profile", json={"profile": FULL_PROFILE})
        client.put("/api/profile", json={
            "profile": {**FULL_PROFILE,
                        "personal": {**FULL_PROFILE["personal"], "name": "Half Saved",
                                     "email": "broken"}},
        })
        assert client.get("/api/profile").json()["profile"]["personal"]["name"] \
            == "Gaurav Sonkar"

    def test_admin_has_no_special_access_to_anyone_else(
        self, client, clean_profile, other_user, session
    ):
        """Admins manage accounts, not the people in them — the profile is not
        an exception."""
        profiles.save_profile(session, other_user.user_id, FULL_PROFILE)
        # The signed-in client is the admin; its own profile is still blank.
        assert client.get("/api/profile").json()["profile"]["personal"]["name"] == ""


# ── The one-time import ─────────────────────────────────────────────────────

class TestLegacyImport:
    def test_imports_the_file_into_the_first_admin(self, session, clean_profile, tmp_path):
        legacy = tmp_path / "master_profile.json"
        legacy.write_text(json.dumps(FULL_PROFILE), encoding="utf-8")

        seeded = profiles.seed_from_legacy_file(session, str(legacy))
        assert seeded == clean_profile.user_id
        assert profiles.get_profile(session, clean_profile.user_id)["personal"]["name"] \
            == "Gaurav Sonkar"

    def test_does_nothing_when_a_profile_already_exists(
        self, session, clean_profile, tmp_path
    ):
        """Startup runs this every boot — it must never overwrite edits."""
        profiles.save_profile(session, clean_profile.user_id, {
            **FULL_PROFILE, "personal": {**FULL_PROFILE["personal"], "name": "Edited"},
        })
        legacy = tmp_path / "master_profile.json"
        legacy.write_text(json.dumps(FULL_PROFILE), encoding="utf-8")

        assert profiles.seed_from_legacy_file(session, str(legacy)) is None
        assert profiles.get_profile(session, clean_profile.user_id)["personal"]["name"] \
            == "Edited"

    def test_missing_file_is_not_an_error(self, session, clean_profile, tmp_path):
        assert profiles.seed_from_legacy_file(session, str(tmp_path / "nope.json")) is None

    def test_malformed_file_does_not_block_startup(self, session, clean_profile, tmp_path):
        legacy = tmp_path / "master_profile.json"
        legacy.write_text("{ not json", encoding="utf-8")
        assert profiles.seed_from_legacy_file(session, str(legacy)) is None

    def test_a_later_account_is_not_seeded_from_someone_elses_file(
        self, session, clean_profile, other_user, tmp_path
    ):
        """The import exists to preserve the original install's own data, not
        to hand it to everyone who signs up afterwards."""
        legacy = tmp_path / "master_profile.json"
        legacy.write_text(json.dumps(FULL_PROFILE), encoding="utf-8")
        profiles.seed_from_legacy_file(session, str(legacy))

        assert profiles.get_profile(session, other_user.user_id)["personal"]["name"] == ""
