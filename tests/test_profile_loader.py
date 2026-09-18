"""Phase 5: the single master_profile.json reader.

Two contracts, deliberately different: resume generation must fail loudly when
the profile is unusable, while Smart Job Apply's query builder and form autofill
must degrade instead of sinking a run.
"""
import json

import pytest

from backend.app.utils.profile import (
    load_master_profile,
    personal_details,
    try_load_master_profile,
)


@pytest.fixture
def profile_file(tmp_path):
    def _write(data):
        p = tmp_path / "master_profile.json"
        p.write_text(json.dumps(data), encoding="utf-8")
        return str(p)
    return _write


class TestStrictLoader:
    def test_loads_valid_profile(self, profile_file):
        path = profile_file({"personal": {"name": "Gaurav Sonkar"}})
        assert load_master_profile(path)["personal"]["name"] == "Gaurav Sonkar"

    def test_missing_file_names_the_setting(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="MASTER_PROFILE_PATH"):
            load_master_profile(str(tmp_path / "nope.json"))

    def test_malformed_json_raises_value_error(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match="not valid JSON"):
            load_master_profile(str(p))


class TestLenientLoader:
    def test_loads_valid_profile(self, profile_file):
        path = profile_file({"primary_skills": ["Python"]})
        assert try_load_master_profile(path)["primary_skills"] == ["Python"]

    def test_missing_file_returns_none(self, tmp_path):
        assert try_load_master_profile(str(tmp_path / "nope.json")) is None

    def test_malformed_json_returns_none(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not json", encoding="utf-8")
        assert try_load_master_profile(str(p)) is None


class TestCaching:
    def test_edits_invalidate_the_cache(self, profile_file, tmp_path):
        """The cache is keyed on mtime, so editing the file takes effect without
        restarting the server."""
        import os
        import time

        path = profile_file({"primary_skills": ["Python"]})
        assert load_master_profile(path)["primary_skills"] == ["Python"]

        time.sleep(0.01)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"primary_skills": ["Go"]}, f)
        # Force a distinct mtime even on coarse-grained filesystems.
        os.utime(path, (time.time() + 1, time.time() + 1))

        assert load_master_profile(path)["primary_skills"] == ["Go"]


class TestPersonalDetails:
    def test_returns_personal_block(self):
        assert personal_details({"personal": {"email": "a@b.com"}})["email"] == "a@b.com"

    @pytest.mark.parametrize("profile", [{}, {"personal": None}])
    def test_missing_block_returns_empty_dict(self, profile):
        assert personal_details(profile) == {}


class TestRealProjectProfile:
    """The repo's own master_profile.json must satisfy what the code expects."""

    @pytest.fixture(scope="class")
    def real(self):
        from pathlib import Path
        p = Path(__file__).resolve().parents[1] / "master_profile.json"
        if not p.exists():
            pytest.skip("master_profile.json not present")
        return load_master_profile(str(p))

    def test_has_the_keys_autofill_needs(self, real):
        personal = personal_details(real)
        for key in ("name", "email", "phone"):
            assert personal.get(key), f"personal.{key} is required for form autofill"

    def test_has_the_keys_resume_generation_needs(self, real):
        assert real.get("primary_skills")
        assert real.get("experience")
        assert real.get("projects")

    def test_experience_entries_have_titles(self, real):
        """discovery_flow derives smart-search queries from experience[0].title."""
        assert real["experience"][0].get("title")

    def test_has_enough_blocks_for_the_template(self, real):
        """The LaTeX template maps 2 experience + 4 project itemize blocks."""
        assert len(real["experience"]) >= 2
        assert len(real["projects"]) >= 4
