"""portal_apply_flow's decision chain: try resume-driven scraping when it
applies, always fall back to keyword search so a run is never left with
nothing just because a portal's automation didn't pan out.
"""

import pytest

from backend.app.models.job import Job, JobStatus
from backend.app.workflows.portal_apply_flow import _master_resume_path_for


# ── _master_resume_path_for ─────────────────────────────────────────────────

class TestMasterResumePathFor:
    def test_none_when_no_master_resume(self, clean_library):
        assert _master_resume_path_for("naukri") is None

    def test_none_for_a_portal_without_resume_driven_support(self, upload):
        """LinkedIn has no recommended-jobs config — must not return a path
        that nothing downstream knows how to use."""
        upload()
        assert _master_resume_path_for("linkedin") is None

    def test_returns_the_master_resumes_path_for_a_supported_portal(self, upload, session):
        from backend.app.models.resume import Resume

        rid = upload().json()["resume"]["resume_id"]
        expected = session.get(Resume, rid).file_path
        assert _master_resume_path_for("naukri") == expected

    def test_ignores_a_non_master_resume(self, upload, session):
        """Two resumes uploaded, only the master one should ever be used for
        portal automation — never a resume the user didn't designate."""
        from backend.app.models.resume import Resume

        first = upload("a.pdf").json()["resume"]["resume_id"]
        upload("b.pdf")  # second upload — first upload keeps master (Phase 4 rule)
        expected = session.get(Resume, first).file_path
        assert _master_resume_path_for("naukri") == expected


# ── Fixtures: reuse test_resume_library.py's known-good PDF generator ──────

@pytest.fixture
def minimal_pdf():
    from tests.test_resume_library import make_pdf
    return make_pdf("Gaurav Sonkar\nSoftware Developer\nSkills: Python, Django")


@pytest.fixture
def clean_library(session):
    from sqlmodel import delete

    from backend.app.models.resume import Resume

    session.exec(delete(Resume))
    session.commit()
    yield session
    session.exec(delete(Resume))
    session.commit()


@pytest.fixture
def upload(client, clean_library, minimal_pdf):
    def _upload(filename="resume.pdf"):
        return client.post(
            "/api/resumes/library",
            files={"file": (filename, minimal_pdf, "application/pdf")},
            data={"label": "", "is_master": "false"},
        )
    return _upload


# ── run_portal_apply's fallback chain (patched at the module level) ───────

class TestRunPortalApplyResumeDrivenFallback:
    """`monkeypatch` replaces the two scrape functions portal_apply_flow
    actually calls, so these test the DECISION LOGIC — which one gets tried,
    and in what order — without touching Selenium or a real portal."""

    @pytest.fixture(autouse=True)
    def _clean_runs(self, session):
        from sqlmodel import delete

        from backend.app.models.job import Job as JobModel
        from backend.app.models.job_stage_event import JobStageEvent
        from backend.app.models.run_log import RunLog

        for model in (JobStageEvent, JobModel, RunLog):
            session.exec(delete(model))
        session.commit()

    async def test_resume_driven_success_skips_keyword_search(self, upload, monkeypatch, session):
        import backend.app.workflows.portal_apply_flow as flow

        upload()  # master resume now set

        keyword_search_called = False

        async def fake_recommended(portal, resume_path, location, batch_size, user_id=None):
            return [Job(job_id=1, url="https://naukri.com/r1", title="Recommended Role",
                        portal="naukri", status=JobStatus.SCRAPED)]

        async def fake_keyword_search(*a, **kw):
            nonlocal keyword_search_called
            keyword_search_called = True
            return []

        monkeypatch.setattr(flow, "scrape_recommended_jobs", fake_recommended)
        monkeypatch.setattr(flow, "scrape_jobs", fake_keyword_search)

        from backend.app.workflows.state import RunConfig

        result = await flow.run_portal_apply(
            RunConfig(portal="naukri", job_title="", location="", batch_size=5,
                      include_checkpointed=False)
        )

        assert result.jobs_scraped == 1
        assert keyword_search_called is False

    async def test_resume_driven_empty_result_falls_back_to_keyword_search(self, upload, monkeypatch):
        import backend.app.workflows.portal_apply_flow as flow

        upload()

        async def fake_recommended(*a, **kw):
            return []  # portal supported it, but nothing came back

        async def fake_keyword_search(portal, query, location, batch_size, user_id=None):
            return [Job(job_id=2, url="https://naukri.com/k1", title=query,
                        portal="naukri", status=JobStatus.SCRAPED)]

        monkeypatch.setattr(flow, "scrape_recommended_jobs", fake_recommended)
        monkeypatch.setattr(flow, "scrape_jobs", fake_keyword_search)

        from backend.app.workflows.state import RunConfig

        result = await flow.run_portal_apply(
            RunConfig(portal="naukri", job_title="", location="", batch_size=5,
                      include_checkpointed=False)
        )
        assert result.jobs_scraped == 1

    async def test_resume_driven_exception_falls_back_to_keyword_search(self, upload, monkeypatch):
        """A selector drift / navigation failure inside resume-driven scraping
        must not sink the whole run — it degrades to keyword search."""
        import backend.app.workflows.portal_apply_flow as flow

        upload()

        async def fake_recommended(*a, **kw):
            raise RuntimeError("selector drift: profile page changed")

        async def fake_keyword_search(portal, query, location, batch_size, user_id=None):
            return [Job(job_id=3, url="https://naukri.com/k2", title=query,
                        portal="naukri", status=JobStatus.SCRAPED)]

        monkeypatch.setattr(flow, "scrape_recommended_jobs", fake_recommended)
        monkeypatch.setattr(flow, "scrape_jobs", fake_keyword_search)

        from backend.app.workflows.state import RunConfig

        result = await flow.run_portal_apply(
            RunConfig(portal="naukri", job_title="", location="", batch_size=5,
                      include_checkpointed=False)
        )
        assert result.jobs_scraped == 1
        assert result.errors == []  # a caught, logged fallback is not a run error

    async def test_typed_job_title_never_attempts_resume_driven_scraping(self, upload, monkeypatch):
        """A typed title means the user chose keyword search explicitly —
        resume-driven scraping must not run even if a master resume exists."""
        import backend.app.workflows.portal_apply_flow as flow

        upload()
        resume_driven_called = False

        async def fake_recommended(*a, **kw):
            nonlocal resume_driven_called
            resume_driven_called = True
            return []

        async def fake_keyword_search(portal, query, location, batch_size, user_id=None):
            return []

        monkeypatch.setattr(flow, "scrape_recommended_jobs", fake_recommended)
        monkeypatch.setattr(flow, "scrape_jobs", fake_keyword_search)

        from backend.app.workflows.state import RunConfig

        await flow.run_portal_apply(
            RunConfig(portal="naukri", job_title="Data Engineer", location="", batch_size=5,
                      include_checkpointed=False)
        )
        assert resume_driven_called is False

    async def test_linkedin_never_attempts_resume_driven_scraping(self, upload, monkeypatch):
        """LinkedIn has no resume-driven config — must go straight to keyword
        search without ever calling scrape_recommended_jobs."""
        import backend.app.workflows.portal_apply_flow as flow

        upload()
        resume_driven_called = False

        async def fake_recommended(*a, **kw):
            nonlocal resume_driven_called
            resume_driven_called = True
            return []

        async def fake_keyword_search(portal, query, location, batch_size, user_id=None):
            return []

        monkeypatch.setattr(flow, "scrape_recommended_jobs", fake_recommended)
        monkeypatch.setattr(flow, "scrape_jobs", fake_keyword_search)

        from backend.app.workflows.state import RunConfig

        await flow.run_portal_apply(
            RunConfig(portal="linkedin", job_title="", location="", batch_size=5,
                      include_checkpointed=False)
        )
        assert resume_driven_called is False

    async def test_no_master_resume_never_attempts_resume_driven_scraping(self, clean_library, monkeypatch):
        import backend.app.workflows.portal_apply_flow as flow

        resume_driven_called = False

        async def fake_recommended(*a, **kw):
            nonlocal resume_driven_called
            resume_driven_called = True
            return []

        async def fake_keyword_search(portal, query, location, batch_size, user_id=None):
            return []

        monkeypatch.setattr(flow, "scrape_recommended_jobs", fake_recommended)
        monkeypatch.setattr(flow, "scrape_jobs", fake_keyword_search)

        from backend.app.workflows.state import RunConfig

        await flow.run_portal_apply(
            RunConfig(portal="naukri", job_title="", location="", batch_size=5,
                      include_checkpointed=False)
        )
        assert resume_driven_called is False
