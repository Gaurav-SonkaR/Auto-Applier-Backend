"""Phase 3: Smart Job Apply stops at READY_TO_APPLY; the user applies manually.

Covers the graph routing decision, the terminal-state guard, and the manual
status endpoint that closes the loop (FR-006 / US-003).
"""
import pytest

from backend.app.models.job import Job, JobStatus


class TestReadyToApplyStatus:
    def test_status_exists(self):
        assert JobStatus.READY_TO_APPLY.value == "READY_TO_APPLY"

    def test_is_distinct_from_resume_ready(self):
        """RESUME_READY means 'mid-pipeline, apply still to run' and is resumed
        by include_checkpointed. READY_TO_APPLY is terminal. Collapsing them
        would make every finished web job get reprocessed on the next run."""
        assert JobStatus.READY_TO_APPLY != JobStatus.RESUME_READY


class TestGraphRouting:
    """_after_resume decides auto-apply vs. hand-off."""

    @pytest.fixture(autouse=True)
    def _routing(self):
        from backend.app.workflows.run_loop import MANUAL_APPLY_PORTAL, _after_resume
        self.route = _after_resume
        self.manual_portal = MANUAL_APPLY_PORTAL

    def _state(self, portal, status=JobStatus.RESUME_READY.value):
        return {"portal": portal, "status": status, "job_id": 1}

    def test_web_jobs_stop_for_manual_apply(self):
        assert self.route(self._state("web")) == "ready"

    @pytest.mark.parametrize(
        "portal", ["linkedin", "naukri", "indeed", "wellfound", "cutshort", "instahyre"]
    )
    def test_portal_jobs_still_auto_apply(self, portal):
        """The user explicitly kept auto-apply for job portals."""
        assert self.route(self._state(portal)) == "apply"

    def test_manual_portal_constant_is_web(self):
        assert self.manual_portal == "web"

    @pytest.mark.parametrize("halting", [
        JobStatus.FAILED.value,
        JobStatus.WAITING_CAPTCHA.value,
        JobStatus.NEED_HUMAN_ACTION.value,
    ])
    def test_halting_status_ends_regardless_of_portal(self, halting):
        from langgraph.graph import END
        assert self.route(self._state("web", halting)) == END
        assert self.route(self._state("linkedin", halting)) == END


class TestTerminalStates:
    """entry_state_for returns None for jobs that must never be reprocessed."""

    @pytest.fixture(autouse=True)
    def _entry(self):
        from backend.app.workflows.run_loop import entry_state_for
        self.entry = entry_state_for

    def _job(self, status):
        return Job(job_id=1, url="https://x.com/1", title="T", portal="web", status=status)

    @pytest.mark.parametrize("status", [JobStatus.APPLIED, JobStatus.READY_TO_APPLY])
    def test_terminal_states_are_skipped(self, status):
        assert self.entry(self._job(status)) is None

    @pytest.mark.parametrize(
        "status", [JobStatus.DUPLICATE, JobStatus.SKIPPED, JobStatus.BLACKLISTED]
    )
    def test_pre_excluded_states_are_skipped(self, status):
        assert self.entry(self._job(status)) is None

    @pytest.mark.parametrize(
        "status", [JobStatus.SCRAPED, JobStatus.PARSED, JobStatus.RESUME_READY,
                   JobStatus.FAILED, JobStatus.WAITING_LOGIN]
    )
    def test_non_terminal_states_are_processed(self, status):
        assert self.entry(self._job(status)) is not None


class TestManualStatusEndpoint:
    """PATCH /api/jobs/{id}/status — FR-006 manual tracking."""

    def test_marks_ready_job_applied(self, client, make_job):
        job_id = make_job(portal="web", status=JobStatus.READY_TO_APPLY)
        r = client.patch(f"/api/jobs/{job_id}/status", json={"status": "APPLIED"})
        assert r.status_code == 200
        assert r.json()["status"] == "APPLIED"

    def test_applying_records_an_application_row(self, client, make_job, session):
        """FR-006: 'Store application date when status changed to Applied'."""
        from sqlmodel import select

        from backend.app.models.application import Application

        job_id = make_job(portal="web", status=JobStatus.READY_TO_APPLY)
        client.patch(f"/api/jobs/{job_id}/status", json={"status": "APPLIED"})
        rows = session.exec(select(Application).where(Application.job_id == job_id)).all()
        assert len(rows) == 1
        assert rows[0].submitted_at is not None
        assert rows[0].user_id == session.get(Job, job_id).user_id

    def test_applying_twice_does_not_duplicate_the_application(self, client, make_job, session):
        from sqlmodel import select

        from backend.app.models.application import Application

        job_id = make_job(portal="web", status=JobStatus.READY_TO_APPLY)
        client.patch(f"/api/jobs/{job_id}/status", json={"status": "APPLIED"})
        client.patch(f"/api/jobs/{job_id}/status", json={"status": "READY_TO_APPLY"})
        client.patch(f"/api/jobs/{job_id}/status", json={"status": "APPLIED"})
        rows = session.exec(select(Application).where(Application.job_id == job_id)).all()
        assert len(rows) == 1

    def test_writes_a_timeline_event(self, client, make_job, session):
        from sqlmodel import select

        from backend.app.models.job_stage_event import JobStageEvent

        job_id = make_job(portal="web", status=JobStatus.READY_TO_APPLY)
        client.patch(f"/api/jobs/{job_id}/status", json={"status": "APPLIED"})
        events = session.exec(
            select(JobStageEvent).where(JobStageEvent.job_id == job_id)
        ).all()
        assert any(e.stage == "manual_status" and e.status == "APPLIED" for e in events)

    def test_no_op_when_status_unchanged(self, client, make_job, session):
        from sqlmodel import select

        from backend.app.models.job_stage_event import JobStageEvent

        job_id = make_job(portal="web", status=JobStatus.APPLIED)
        r = client.patch(f"/api/jobs/{job_id}/status", json={"status": "APPLIED"})
        assert r.status_code == 200
        events = session.exec(
            select(JobStageEvent).where(JobStageEvent.job_id == job_id)
        ).all()
        assert events == []

    @pytest.mark.parametrize("status", ["SCRAPED", "PARSED", "FAILED", "WAITING_CAPTCHA", "bogus"])
    def test_rejects_pipeline_owned_statuses(self, client, make_job, status):
        """Only statuses a human can legitimately assert are accepted."""
        job_id = make_job(portal="web", status=JobStatus.READY_TO_APPLY)
        r = client.patch(f"/api/jobs/{job_id}/status", json={"status": status})
        assert r.status_code == 422

    def test_accepts_lowercase(self, client, make_job):
        job_id = make_job(portal="web", status=JobStatus.READY_TO_APPLY)
        r = client.patch(f"/api/jobs/{job_id}/status", json={"status": "applied"})
        assert r.status_code == 200
        assert r.json()["status"] == "APPLIED"

    def test_unknown_job_is_404(self, client):
        r = client.patch("/api/jobs/999999/status", json={"status": "APPLIED"})
        assert r.status_code == 404


class TestDashboardReporting:
    def test_ready_to_apply_is_counted(self, client, make_job):
        make_job(portal="web", status=JobStatus.READY_TO_APPLY)
        make_job(portal="web", status=JobStatus.READY_TO_APPLY)
        make_job(portal="linkedin", status=JobStatus.APPLIED)
        stats = client.get("/api/dashboard/stats").json()
        assert stats["ready_to_apply"] == 2
        assert stats["applied"] == 1

    def test_appears_in_funnel(self, client, make_job):
        make_job(portal="web", status=JobStatus.READY_TO_APPLY)
        stats = client.get("/api/dashboard/stats").json()
        stages = [s["stage"] for s in stats["funnel"]]
        assert "Ready to Apply" in stages
        assert stages.index("Ready to Apply") < stages.index("Applied")

    def test_ready_jobs_count_toward_success_rate(self, client, make_job):
        """A web job reaching READY_TO_APPLY is a full success of what the
        automation is permitted to do — it must not read as a failure."""
        make_job(portal="web", status=JobStatus.READY_TO_APPLY)
        make_job(portal="linkedin", status=JobStatus.FAILED)
        stats = client.get("/api/dashboard/stats").json()
        assert stats["success_rate"] == 0.5

    def test_listing_by_status_returns_apply_link_and_resume(self, client, make_job):
        make_job(portal="web", status=JobStatus.READY_TO_APPLY,
                 url="https://acme.com/careers/42", resume_path="/tmp/r.pdf", ats_score=88)
        items = client.get("/api/jobs/", params={"status": "READY_TO_APPLY"}).json()["items"]
        assert len(items) == 1
        assert items[0]["url"] == "https://acme.com/careers/42"
        assert items[0]["resume_path"] == "/tmp/r.pdf"
        assert items[0]["ats_score"] == 88


class TestApplierRefusesCareerSiteJobs:
    """`apply_to_job` must never auto-submit a Smart Job Apply job.

    The pipeline already routes those to READY_TO_APPLY, so this path is
    unreachable today. It is still enforced rather than assumed: the failure
    mode of a future mis-wiring is an application submitted on the user's
    behalf that they never saw, which is the one thing this design promises
    cannot happen.
    """

    async def test_web_job_is_refused_without_opening_a_browser(
        self, session, make_job, monkeypatch
    ):
        from backend.app.services import applier

        # If the guard leaks, the next thing apply_to_job does is look up a
        # template and launch Chromium — blow up loudly instead.
        def _explode(*_args, **_kwargs):
            raise AssertionError("guard leaked: apply_to_job proceeded for a web job")

        monkeypatch.setattr(applier, "get_template", _explode)

        job_id = make_job(portal="web", status=JobStatus.RESUME_READY)
        job = session.get(Job, job_id)

        result = await applier.apply_to_job(job, "/tmp/resume.pdf", session)

        assert result.status == JobStatus.READY_TO_APPLY
        assert "manual" in result.reason.lower()

    async def test_the_guard_uses_the_same_constant_as_the_router(self):
        """Two independent literals would let the router and the guard drift
        apart, which is how the dead branches got there in the first place."""
        from backend.app.services.applier import MANUAL_APPLY_PORTAL as guard
        from backend.app.workflows.run_loop import MANUAL_APPLY_PORTAL as router

        assert guard is router


class TestPipelineRecovery:
    async def test_discovery_does_not_substitute_uploaded_resume_for_failed_tailoring(
        self, make_job, session, admin_user, monkeypatch
    ):
        from backend.app.models.job import Job
        from backend.app.workflows import run_loop

        job_id = make_job(portal="web", status=JobStatus.PARSED)
        monkeypatch.setattr(
            run_loop, "generate_resume",
            lambda *_args, **_kwargs: _raise_generation_failure(),
        )
        monkeypatch.setattr(
            run_loop, "_best_uploaded_resume",
            lambda *_args, **_kwargs: pytest.fail(
                "Company Career Discovery must not substitute a library resume"
            ),
        )

        result = await run_loop._resume_node({
            "job_id": job_id,
            "user_id": admin_user.user_id,
            "portal": "web",
            "parsed_jd": {},
            "status": JobStatus.PARSED,
        })

        session.expire_all()
        assert result["status"] == JobStatus.FAILED
        assert "tailored resume generation failed" in result["error"].lower()
        assert session.get(Job, job_id).resume_path is None

    async def test_only_explicitly_retried_scraped_jobs_are_checkpoints(
        self, make_job, admin_user, monkeypatch
    ):
        from backend.app.workflows import portal_apply_flow as flow
        from backend.app.workflows.state import RunConfig

        retried = make_job(status=JobStatus.SCRAPED, retry_count=1)
        untouched = make_job(status=JobStatus.SCRAPED, retry_count=0)
        parsed = make_job(status=JobStatus.PARSED)
        processed = []

        async def no_new_jobs(*_args, **_kwargs):
            return []

        async def record_jobs(job_ids, *_args, **_kwargs):
            processed.extend(job_ids)

        monkeypatch.setattr(flow, "scrape_jobs", no_new_jobs)
        monkeypatch.setattr(flow, "run_job_graph_loop", record_jobs)

        await flow.run_portal_apply(RunConfig(
            portal="linkedin",
            job_title="Software Engineer",
            location="",
            include_checkpointed=True,
            user_id=admin_user.user_id,
        ))

        assert set(processed) == {retried, parsed}
        assert untouched not in processed

    async def test_daily_apply_limit_blocks_real_submit(self, make_job, session, monkeypatch):
        from backend.app.config import get_settings
        from backend.app.models.application import Application, ApplicationStatus
        from backend.app.models.job import Job
        from backend.app.workflows import run_loop

        job_id = make_job(status=JobStatus.RESUME_READY)
        job = session.get(Job, job_id)
        session.add(Application(
            # Pre-owner application rows have NULL user_id; the cap must still
            # count them through the owning job relationship.
            user_id=None,
            job_id=job_id,
            status=ApplicationStatus.SUBMITTED,
        ))
        session.commit()
        monkeypatch.setattr(get_settings(), "daily_apply_limit", 1, raising=False)

        async def should_not_apply(*_args, **_kwargs):
            raise AssertionError("daily application limit was bypassed")

        monkeypatch.setattr(run_loop, "apply_to_job", should_not_apply)
        result = await run_loop._apply_node({
            "job_id": job_id,
            "resume_path": "/tmp/resume.pdf",
            "dry_run": False,
        })

        assert result["status"] == JobStatus.SKIPPED
        assert "daily application limit" in result["error"].lower()

    async def test_dry_run_does_not_consume_or_enforce_application_limit(
        self, make_job, session, monkeypatch
    ):
        from backend.app.config import get_settings
        from backend.app.models.application import Application, ApplicationStatus
        from backend.app.models.job import Job
        from backend.app.services import applier
        from backend.app.workflows import run_loop

        job_id = make_job(status=JobStatus.RESUME_READY)
        job = session.get(Job, job_id)
        session.add(Application(
            user_id=job.user_id, job_id=job_id, status=ApplicationStatus.SUBMITTED
        ))
        session.commit()
        monkeypatch.setattr(get_settings(), "daily_apply_limit", 1, raising=False)

        async def rehearsal(*_args, **_kwargs):
            return applier.ApplyResult(
                job_id=job_id,
                status=JobStatus.NEED_HUMAN_ACTION,
                reason="Rehearsal: test",
            )

        monkeypatch.setattr(run_loop, "apply_to_job", rehearsal)
        result = await run_loop._apply_node({
            "job_id": job_id,
            "resume_path": "/tmp/resume.pdf",
            "dry_run": True,
        })

        assert result["status"] == JobStatus.NEED_HUMAN_ACTION


def _raise_generation_failure():
    raise RuntimeError("simulated resume generation failure")


class TestDiscoveryJDQuality:
    def test_navigation_shell_is_too_short_to_be_a_job_description(self):
        from backend.app.workflows.run_loop import _usable_discovery_jd

        assert not _usable_discovery_jd("Skip to main content\n-")
        assert _usable_discovery_jd("Responsibilities and requirements " * 10)

    async def test_short_cached_discovery_jd_fails_before_parsing(self):
        from backend.app.workflows.run_loop import _fetch_jd_node

        result = await _fetch_jd_node({
            "job_id": 1,
            "portal": "web",
            "jd_text": "Skip to main content\n-",
            "status": JobStatus.SCRAPED,
        })

        assert result["status"] == JobStatus.FAILED
        assert "too little JD text" in result["error"]

    async def test_discovery_jd_without_fit_signals_does_not_advance(
        self, monkeypatch
    ):
        from backend.app.workflows import run_loop
        from backend.app.workflows.types import ParsedJD

        async def no_signals(*_args, **_kwargs):
            return ParsedJD(raw={
                "required_skills": [], "nice_to_have": [], "keywords": [],
                "experience_years": None,
            })

        monkeypatch.setattr(run_loop, "parse_jd", no_signals)
        result = await run_loop._parse_node({
            "job_id": 1,
            "portal": "web",
            "jd_text": "A sufficiently long but unstructured job description " * 10,
            "status": JobStatus.SCRAPED,
        })

        assert result["status"] == JobStatus.FAILED
        assert "not treated as a match" in result["error"]


class TestWebTemplateCarriesNoApplySelectors:
    """WebTemplate used to declare apply/upload/submit/success selectors that
    nothing read. They implied an auto-apply path that does not exist."""

    def test_no_apply_selectors_are_declared(self):
        from backend.app.services.portal_templates import get_template

        sel = get_template("web").selectors
        for field in ("apply_button", "easy_apply_button", "resume_upload",
                      "form_text_inputs", "form_select_inputs", "form_textarea_inputs",
                      "submit_button", "success_indicator"):
            assert getattr(sel, field) == "", f"{field} is dead but still declared"

    def test_jd_extraction_still_falls_back_to_body_text(self):
        """Blank on purpose: no selector generalises across arbitrary career
        pages, so _fetch_jd_node reads the page body instead."""
        from backend.app.services.portal_templates import get_template

        assert get_template("web").selectors.job_description_container == ""
