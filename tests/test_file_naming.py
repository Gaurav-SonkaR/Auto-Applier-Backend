"""FR-004: generated-resume filename convention."""
from datetime import date

import pytest

from backend.app.utils.file_naming import resume_filename, sanitize


class TestSanitize:
    def test_collapses_special_characters(self):
        assert sanitize("Senior Python Engineer!!", 60) == "Senior_Python_Engineer"

    def test_collapses_runs_to_single_underscore(self):
        assert sanitize("A -- B", 60) == "A_B"

    def test_folds_accents_to_ascii(self):
        assert sanitize("Café Söftware", 60) == "Cafe_Software"

    def test_blank_falls_back(self):
        assert sanitize("", 60, fallback="company") == "company"
        assert sanitize("   ", 60, fallback="company") == "company"

    def test_all_punctuation_falls_back(self):
        assert sanitize("!!!***", 60, fallback="role") == "role"

    def test_truncates_on_word_boundary(self):
        out = sanitize("Senior Backend Platform Reliability Engineer", 20)
        assert len(out) <= 20
        assert not out.endswith("_")
        assert out == "Senior_Backend"

    def test_single_long_word_is_hard_truncated(self):
        # No underscore to break on — must still respect the cap.
        assert sanitize("A" * 100, 10) == "A" * 10


class TestResumeFilename:
    def test_matches_fr004_shape(self):
        name = resume_filename(
            job_id=42,
            url="https://techcorp.com/jobs/42",
            title="Senior Python Engineer",
            company="TechCorp",
            candidate="Asha Verma",
            on=date(2026, 9, 4),
        )
        assert name.startswith("Asha_Verma_2026-09-04_Senior_Python_Engineer_TechCorp_")
        assert name.endswith(".pdf")

    def test_the_name_comes_from_the_caller_not_a_hard_coded_person(self):
        """`resume_generator` used to omit `candidate`, so every account's
        resume PDF was named after one particular user."""
        first = resume_filename(job_id=1, url="u", title="t", candidate="Asha Verma")
        second = resume_filename(job_id=1, url="u", title="t", candidate="Ravi Nair")
        assert first.startswith("Asha_Verma_")
        assert second.startswith("Ravi_Nair_")

    def test_the_fallback_is_not_a_real_person(self):
        """A person's name as the default silently put it on other people's
        files. Anonymous is the only safe default."""
        from backend.app.utils.file_naming import DEFAULT_CANDIDATE

        assert "gaurav" not in DEFAULT_CANDIDATE.lower()
        name = resume_filename(job_id=1, url="u", title="t")
        assert name.startswith(f"{DEFAULT_CANDIDATE}_")

    def test_hash_suffix_is_six_hex_chars(self):
        name = resume_filename(1, "https://x.com/1", "Dev", "X", on=date(2026, 1, 1))
        digest = name.removesuffix(".pdf").rsplit("_", 1)[1]
        assert len(digest) == 6
        assert all(c in "0123456789abcdef" for c in digest)

    def test_same_job_is_deterministic(self):
        args = (7, "https://x.com/7", "Dev", "X")
        assert resume_filename(*args, on=date(2026, 1, 1)) == resume_filename(
            *args, on=date(2026, 1, 1)
        )

    def test_different_jobs_with_identical_title_and_company_differ(self):
        """The whole point of the hash: two postings for the same role at the
        same company must not overwrite each other's PDF."""
        a = resume_filename(1, "https://x.com/1", "Dev", "Acme", on=date(2026, 1, 1))
        b = resume_filename(2, "https://x.com/2", "Dev", "Acme", on=date(2026, 1, 1))
        assert a != b

    def test_missing_company_uses_fallback(self):
        name = resume_filename(1, "https://x.com/1", "Dev", None, on=date(2026, 1, 1))
        assert "_company_" in name

    def test_path_separators_cannot_escape_the_directory(self):
        name = resume_filename(
            1, "https://x.com/1", "../../etc/passwd", "../..", on=date(2026, 1, 1)
        )
        assert "/" not in name
        assert "\\" not in name
        assert ".." not in name

    @pytest.mark.parametrize("title", ["Dev", "A" * 300, "日本語のタイトル", "!!!"])
    def test_stays_within_filesystem_limits(self, title):
        name = resume_filename(1, "https://x.com/1", title, "C" * 300, on=date(2026, 1, 1))
        assert len(name.encode()) < 255
