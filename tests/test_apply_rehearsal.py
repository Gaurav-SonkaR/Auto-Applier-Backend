"""Rehearsal (dry run): fill a portal application, screenshot it, never submit.

The way to check a portal against the user's real account before letting it
apply for real. A rehearsal that submits even once defeats its purpose, so the
browser tests below use a form whose Submit button records whether it was
pressed, and assert it never was. They also pin that LinkedIn is only marked
applied when LinkedIn says so — it used to be marked applied on the click.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.app.models.job import Job, JobStatus
from backend.app.services import applier
from backend.app.services.portal_templates import get_template, list_portals
from backend.app.services.portal_templates.base import PortalSelectors

PROFILE = {
    "personal": {"name": "Asha Verma", "email": "asha@example.com", "phone": "+91 98765 43210"},
    "primary_skills": ["Python", "FastAPI"],
    "experience": [{"title": "Backend Engineer", "company": "Northwind",
                    "start_date": "Feb 2025", "end_date": "Present"}],
}

# `novalidate` so the real run can submit with a field still blank — the
# `required` attributes are what the rehearsal reports on, not a browser gate.
PORTAL_PAGE = """
<button id="apply" onclick="window.__applyClicked = true;
  document.getElementById('form').hidden = false">Apply</button>
<form id="form" class="application-form" hidden novalidate
      onsubmit="event.preventDefault(); window.__submitted = true;
                document.body.insertAdjacentHTML('beforeend', '<div id=done>Applied</div>')">
  <label for="name">Full name</label><input id="name" name="name" type="text" required>
  <label for="email">Email address</label><input id="email" name="email" type="text" required>
  <label for="code">Employee referral code</label><input id="code" name="code" type="text" required>
  <button type="submit" id="submit">Submit application</button>
</form>
"""

LINKEDIN_STEP = """
<div class="jobs-easy-apply-form-section">
  <button aria-label="Submit application"
          onclick="window.__submitted = true; {confirm}">Submit application</button>
</div>
"""


class Router:
    """Knows nothing — every free-text question comes back unanswerable."""

    async def complete(self, task, prompt, system=""):
        return "UNKNOWN"


class Template:
    def __init__(self, apply_click_submits=False):
        self.apply_click_submits = apply_click_submits
        self.selectors = PortalSelectors(
            apply_button="#apply",
            form_text_inputs="#form input[type='text']",
            submit_button="#submit",
            success_indicator="#done",
        )


@pytest.fixture
def settings(tmp_path):
    return SimpleNamespace(screenshots_dir=str(tmp_path), human_delay_min_ms=0,
                           human_delay_max_ms=0)


@pytest.fixture
def portal_job(tmp_path, session, make_job, monkeypatch):
    """A wellfound job whose URL is the local form above, owned by a complete profile."""
    monkeypatch.setattr(applier, "get_llm_router", Router)
    monkeypatch.setattr(applier.profiles_service, "get_profile", lambda *_: PROFILE)

    def _make(html=PORTAL_PAGE, portal="wellfound"):
        page_file = tmp_path / f"{portal}.html"
        page_file.write_text(html, encoding="utf-8")
        return session.get(Job, make_job(portal=portal, url=page_file.as_uri()))

    return _make


@pytest.fixture
async def page():
    from selenium.common.exceptions import WebDriverException

    from backend.app.config import get_settings
    from backend.app.services.selenium_browser import selenium_session

    manager = selenium_session(get_settings(), headless=True)
    try:
        session = await manager.__aenter__()
    except WebDriverException as exc:
        pytest.skip(f"Chrome WebDriver unavailable: {exc}")
    try:
        yield session.page
    finally:
        await manager.__aexit__(None, None, None)


async def _flag(page, name):
    return await page.execute_script("return window[arguments[0]] === true;", name)


async def _value(page, selector):
    element = await page.find(selector)
    return await element.input_value() if element else ""


class TestSinglePageRehearsal:
    async def test_it_fills_the_form_and_never_submits(self, page, portal_job, settings):
        job = portal_job()
        result = await applier._run_apply_flow(page, job, "", Template(), settings, dry_run=True)

        assert not await _flag(page, "__submitted")
        assert result.status == JobStatus.NEED_HUMAN_ACTION
        assert await _value(page, "#name") == "Asha Verma"
        assert await _value(page, "#email") == "asha@example.com"

    async def test_it_reports_what_is_still_blank(self, page, portal_job, settings, tmp_path):
        job = portal_job()
        result = await applier._run_apply_flow(page, job, "", Template(), settings, dry_run=True)

        assert result.reason.startswith("Rehearsal: form filled, not submitted.")
        assert "Employee referral code" in result.reason
        assert "Full name" not in result.reason
        assert result.screenshot_path and (tmp_path / f"job_{job.job_id}_dry_run.png").exists()

    async def test_a_real_run_still_submits(self, page, portal_job, settings):
        job = portal_job()
        result = await applier._run_apply_flow(page, job, "", Template(), settings)

        assert await _flag(page, "__submitted")
        assert result.status == JobStatus.APPLIED

    async def test_unconfirmed_submission_requires_human_review(
        self, page, portal_job, settings
    ):
        html = PORTAL_PAGE.replace("'<div id=done>Applied</div>'", "''")
        job = portal_job(html)
        result = await applier._run_apply_flow(page, job, "", Template(), settings)

        assert await _flag(page, "__submitted")
        assert result.status == JobStatus.NEED_HUMAN_ACTION
        assert "before retrying" in result.reason

    async def test_it_stops_before_an_apply_button_that_submits(self, page, portal_job, settings):
        """Naukri and Instahyre can apply on the Apply click alone."""
        job = portal_job()
        result = await applier._run_apply_flow(
            page, job, "", Template(apply_click_submits=True), settings, dry_run=True
        )

        assert not await _flag(page, "__applyClicked")
        assert not await _flag(page, "__submitted")
        assert result.status == JobStatus.NEED_HUMAN_ACTION
        assert "Stopped before Apply" in result.reason


class TestLinkedInEasyApply:
    SEL = PortalSelectors(form_text_inputs="div.jobs-easy-apply-form-section input")

    async def _run(self, page, portal_job, settings, dry_run, confirm=""):
        job = portal_job(LINKEDIN_STEP.format(confirm=confirm), portal="linkedin")
        await page.navigate(job.url)
        return await applier._linkedin_easy_apply(page, job, "", self.SEL, settings, dry_run)

    async def test_a_rehearsal_does_not_press_submit(self, page, portal_job, settings):
        result = await self._run(page, portal_job, settings, dry_run=True)
        assert not await _flag(page, "__submitted")
        assert result.status == JobStatus.NEED_HUMAN_ACTION
        assert result.reason.startswith("Rehearsal:")

    async def test_a_click_without_confirmation_is_not_applied(self, page, portal_job, settings):
        result = await self._run(page, portal_job, settings, dry_run=False)
        assert await _flag(page, "__submitted")
        assert result.status == JobStatus.NEED_HUMAN_ACTION
        assert "no confirmation" in result.reason

    async def test_linkedins_confirmation_marks_it_applied(self, page, portal_job, settings):
        confirm = ("document.body.insertAdjacentHTML('beforeend', "
                   "'<h3>Your application was sent to Acme</h3>')")
        result = await self._run(page, portal_job, settings, dry_run=False, confirm=confirm)
        assert result.status == JobStatus.APPLIED


class TestLinkedInConfirmationText:
    @pytest.mark.parametrize("text", [
        "Your application was sent to Acme",
        "Application sent",
        "Applied now",
        "Applied 12 seconds ago",
    ])
    def test_acknowledgements_count(self, text):
        assert applier._LI_CONFIRMATION.search(text)

    @pytest.mark.parametrize("text", [
        "Applied 3 days ago",               # another job in the sidebar
        "Applied 2 hours ago",
        "Apply to 25 applicants' favourite job",
        "Submit application",
    ])
    def test_other_text_does_not(self, text):
        assert not applier._LI_CONFIRMATION.search(text)


class TestWiring:
    """dry_run has to survive every hop from the request to the browser."""

    def test_the_request_defaults_to_a_real_run(self):
        from backend.app.schemas.run import RunStartRequest
        from backend.app.workflows.state import RunConfig

        assert RunStartRequest(portal="linkedin", job_title="x").dry_run is False
        assert RunConfig(portal="linkedin", job_title="x", location="").dry_run is False

    def test_only_naukri_and_instahyre_submit_on_the_apply_click(self):
        assert {p for p in list_portals() if get_template(p).apply_click_submits} == {
            "naukri", "instahyre"}

    async def test_apply_to_job_passes_it_to_the_browser(self, session, make_job, monkeypatch):
        seen = {}

        async def _fake_browser_apply(job, resume_path, template, settings, dry_run=False):
            seen["dry_run"] = dry_run
            return applier.ApplyResult(job_id=job.job_id, status=JobStatus.NEED_HUMAN_ACTION,
                                       reason="Rehearsal: test")

        monkeypatch.setattr(applier, "_browser_apply", _fake_browser_apply)
        job = session.get(Job, make_job(portal="wellfound", url="https://wellfound.com/jobs/1"))
        await applier.apply_to_job(job, "/tmp/r.pdf", session, dry_run=True)
        assert seen == {"dry_run": True}

    @pytest.mark.parametrize("state_flag", [True, False])
    async def test_the_apply_node_passes_the_state_flag(self, make_job, monkeypatch, state_flag):
        from backend.app.workflows import run_loop

        seen = {}

        async def _fake_apply(job, resume_path, session=None, dry_run=False):
            seen["dry_run"] = dry_run
            return applier.ApplyResult(job_id=job.job_id, status=JobStatus.APPLIED)

        monkeypatch.setattr(run_loop, "apply_to_job", _fake_apply)
        job_id = make_job(portal="wellfound")
        await run_loop._apply_node({"job_id": job_id, "resume_path": "/tmp/r.pdf",
                                    "dry_run": state_flag})
        assert seen == {"dry_run": state_flag}
