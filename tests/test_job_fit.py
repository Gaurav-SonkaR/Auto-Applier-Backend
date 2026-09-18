"""Skipping postings the profile clearly doesn't fit — before a resume is written.

`experience_years` and `required_skills` were parsed from every JD and then
ignored, so an eight-year role went through resume generation and, on portals,
an application. The user asked for the pipeline to only pursue jobs that match
their experience and stack. These tests pin that rule, its tolerance, and the
conservative default: missing information is never a reason to skip.
"""
from __future__ import annotations

from datetime import date

import pytest

from backend.app.services.job_fit import assess_fit, candidate_years

TODAY = date(2026, 9, 14)

PROFILE = {
    "primary_skills": ["Python", "FastAPI", "Django REST Framework", "PostgreSQL",
                       "Docker", "AWS", "React.js", "REST API Design"],
    "secondary_skills": ["Kubernetes"],
    "experience": [
        {"title": "Software Developer", "start_date": "Jun 2025", "end_date": "Present"},
        {"title": "Intern", "start_date": "Feb 2025", "end_date": "May 2025"},
    ],
}


def fit(jd, profile=PROFILE, **kw):
    return assess_fit(profile, jd, today=TODAY, **kw)


# ── How many years the profile supports ─────────────────────────────────────

class TestCandidateYears:
    def test_measured_from_earliest_start_to_latest_end(self):
        assert candidate_years(PROFILE, TODAY) == 1.6

    def test_overlapping_roles_are_not_double_counted(self):
        overlapping = {"experience": [
            {"start_date": "Jan 2024", "end_date": "Jan 2026"},
            {"start_date": "Jan 2025", "end_date": "Jan 2026"},
        ]}
        assert candidate_years(overlapping, TODAY) == 2.0

    def test_the_applicants_own_figure_wins(self):
        stated = {**PROFILE, "preferences": {"total_experience_years": "3"}}
        assert candidate_years(stated, TODAY) == 3.0

    def test_no_dates_and_no_figure_is_unknown_not_zero(self):
        """Zero would skip every job with a requirement. Unknown skips none."""
        assert candidate_years({"experience": [{"title": "Dev"}]}, TODAY) is None

    @pytest.mark.parametrize("end", ["Present", "present", "Current", "Till date"])
    def test_ongoing_roles_run_to_today(self, end):
        profile = {"experience": [{"start_date": "Sep 2025", "end_date": end}]}
        assert candidate_years(profile, TODAY) == 1.0


# ── Experience ──────────────────────────────────────────────────────────────

class TestExperienceRule:
    def test_a_senior_role_is_skipped(self):
        result = fit({"experience_years": 8, "required_skills": ["Python"]})
        assert not result.fits
        assert "8+ years" in result.summary()

    def test_a_matching_role_goes_ahead(self):
        assert fit({"experience_years": 2, "required_skills": ["Python"]}).fits

    def test_the_tolerance_allows_a_stretch(self):
        """Postings overstate — "2+ years" routinely hires at 1.6."""
        assert fit({"experience_years": 2}, experience_tolerance_years=1.0).fits
        assert not fit({"experience_years": 2}, experience_tolerance_years=0.0).fits

    def test_no_stated_requirement_is_not_a_reason_to_skip(self):
        assert fit({"experience_years": None, "required_skills": ["Python"]}).fits

    def test_an_unknown_candidate_history_is_not_a_reason_to_skip(self):
        assert fit({"experience_years": 8}, profile={"primary_skills": ["Python"]}).fits

    @pytest.mark.parametrize("junk", ["", "several", None, "N/A"])
    def test_a_malformed_requirement_is_ignored(self, junk):
        assert fit({"experience_years": junk}).fits


# ── Skills ──────────────────────────────────────────────────────────────────

class TestSkillRule:
    def test_mostly_foreign_stack_is_skipped(self):
        result = fit({"required_skills": ["Scala", "Spark", "Hadoop", "Kafka"]})
        assert not result.fits
        assert result.skill_overlap == 0.0
        assert "required skills" in result.summary()

    def test_matching_stack_goes_ahead(self):
        result = fit({"required_skills": ["Python", "FastAPI", "PostgreSQL", "Go"]})
        assert result.fits
        assert result.skill_overlap == 0.75
        assert result.missing_skills == ["Go"]

    @pytest.mark.parametrize("required,owned", [
        ("React", "React.js"),
        ("REST API", "REST API Design"),
        ("postgresql", "PostgreSQL"),
        ("Django REST Framework", "Django REST Framework"),
    ])
    def test_reasonable_spellings_match(self, required, owned):
        assert fit({"required_skills": [required]},
                   profile={"primary_skills": [owned]}).matched_skills == [required]

    def test_java_is_not_javascript(self):
        result = fit({"required_skills": ["Java"]},
                     profile={"primary_skills": ["JavaScript"]})
        assert result.matched_skills == []

    def test_secondary_skills_count(self):
        assert fit({"required_skills": ["Kubernetes"]}).matched_skills == ["Kubernetes"]

    def test_the_threshold_is_configurable(self):
        jd = {"required_skills": ["Python", "Scala", "Spark", "Hadoop"]}   # 25%
        assert not fit(jd, min_skill_overlap=0.3).fits
        assert fit(jd, min_skill_overlap=0.2).fits

    def test_no_required_skills_is_not_a_reason_to_skip(self):
        assert fit({"required_skills": []}).fits

    def test_an_empty_profile_is_not_a_reason_to_skip(self):
        assert fit({"required_skills": ["Scala"]}, profile={}).fits


class TestReasons:
    def test_both_reasons_are_reported_together(self):
        result = fit({"experience_years": 10, "required_skills": ["Scala", "Spark"]})
        assert not result.fits
        assert "years" in result.summary() and "skills" in result.summary()


# ── In the pipeline ─────────────────────────────────────────────────────────

class TestPipelinePlacement:
    def test_parse_now_routes_to_fit_not_straight_to_resume(self):
        from backend.app.workflows.run_loop import _after_parse

        assert _after_parse({"status": "PARSED"}) == "fit"

    def test_a_skipped_job_never_reaches_resume_generation(self):
        from backend.app.workflows.run_loop import _after_fit

        assert _after_fit({"status": "SKIPPED"}) != "resume"

    def test_a_fitting_job_continues_to_resume(self):
        from backend.app.workflows.run_loop import _after_fit

        assert _after_fit({"status": "PARSED"}) == "resume"

    def test_the_node_is_in_the_compiled_graph(self):
        from backend.app.workflows.run_loop import JOB_GRAPH

        assert "fit" in JOB_GRAPH.get_graph().nodes

    async def test_the_node_skips_and_records_why(self, make_job, session, admin_user):
        from backend.app.models.job import Job, JobStatus
        from backend.app.models.job_stage_event import JobStageEvent
        from backend.app.services import profiles
        from backend.app.workflows.run_loop import _fit_node
        from sqlmodel import select

        profiles.save_profile(session, admin_user.user_id, {
            "personal": {"name": "Asha", "email": "a@example.com"},
            **PROFILE,
        })
        job_id = make_job(status=JobStatus.PARSED)

        out = await _fit_node({
            "job_id": job_id, "status": "PARSED",
            "parsed_jd": {"experience_years": 9, "required_skills": ["Python"]},
        })

        assert out["status"] == JobStatus.SKIPPED
        session.expire_all()
        assert session.get(Job, job_id).status == JobStatus.SKIPPED
        event = session.exec(
            select(JobStageEvent).where(JobStageEvent.job_id == job_id,
                                        JobStageEvent.stage == "fit")
        ).first()
        assert event and "9+ years" in event.message

    async def test_the_node_lets_a_fitting_job_through_untouched(
        self, make_job, session, admin_user
    ):
        from backend.app.models.job import Job, JobStatus
        from backend.app.services import profiles
        from backend.app.workflows.run_loop import _fit_node

        profiles.save_profile(session, admin_user.user_id, {
            "personal": {"name": "Asha", "email": "a@example.com"}, **PROFILE,
        })
        job_id = make_job(status=JobStatus.PARSED)

        out = await _fit_node({
            "job_id": job_id, "status": "PARSED",
            "parsed_jd": {"experience_years": 2, "required_skills": ["Python"]},
        })
        assert out["status"] == "PARSED"
        session.expire_all()
        assert session.get(Job, job_id).status == JobStatus.PARSED

    async def test_the_filter_can_be_switched_off(self, make_job, monkeypatch):
        from backend.app.config import get_settings
        from backend.app.workflows.run_loop import _fit_node

        monkeypatch.setattr(get_settings(), "fit_filter_enabled", False, raising=False)
        job_id = make_job()
        out = await _fit_node({
            "job_id": job_id, "status": "PARSED",
            "parsed_jd": {"experience_years": 99},
        })
        assert out["status"] == "PARSED"

    async def test_a_resumed_job_is_not_re_judged(self, make_job):
        """Past this node on an earlier run; the decision was already made."""
        from backend.app.workflows.run_loop import _fit_node

        job_id = make_job()
        out = await _fit_node({
            "job_id": job_id, "status": "RESUME_READY",
            "parsed_jd": {"experience_years": 99},
        })
        assert out["status"] == "RESUME_READY"
