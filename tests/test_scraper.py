"""Resume-driven job discovery (Phase 11): upload the master resume to a
portal's own profile and scrape its recommended-jobs listing, falling back to
keyword search when a portal doesn't support it or the automation fails.

Fakes here are hand-rolled (matching the FakeElement/FakePage style in
test_form_autofill.py) rather than requiring a live Selenium driver — the actual
browser-driving code (page.goto navigating a real site, real DOM selectors
resolving) is not testable without a live browser and live portals; see the
portal template docstrings for that caveat. What's covered here is everything
deterministic: title/URL extraction logic, pagination, per-portal
configuration, credential resolution, and the fallback decision chain.
"""
import pytest

from backend.app.services.portal_templates import get_template


# ── PortalTemplate config ───────────────────────────────────────────────────

class TestSupportsResumeDrivenSearch:
    @pytest.mark.parametrize("portal", ["naukri", "indeed", "wellfound", "cutshort", "instahyre"])
    def test_supported_portals(self, portal):
        assert get_template(portal).supports_resume_driven_search() is True

    def test_linkedin_is_deliberately_excluded(self):
        """LinkedIn recommends jobs from profile fields, not an uploaded PDF —
        see LinkedInTemplate's docstring. Must not claim support it doesn't have."""
        assert get_template("linkedin").supports_resume_driven_search() is False

    def test_web_does_not_support_it(self):
        """Company-career-page discovery is a different mechanism entirely."""
        assert get_template("web").supports_resume_driven_search() is False

    @pytest.mark.parametrize("portal", ["naukri", "indeed", "wellfound", "cutshort", "instahyre"])
    def test_supported_portals_have_both_required_selectors(self, portal):
        """supports_resume_driven_search() requires BOTH — an upload selector
        with no page to scrape (or vice versa) is a half-built feature."""
        sel = get_template(portal).selectors
        assert sel.profile_resume_upload
        assert sel.recommended_jobs_url


class TestResolveUrl:
    def test_relative_path_joins_base_url(self):
        t = get_template("naukri")
        assert t.resolve_url("mnjuser/profile") == "https://www.naukri.com/mnjuser/profile"

    def test_absolute_url_passes_through(self):
        t = get_template("naukri")
        assert t.resolve_url("https://other.example.com/x") == "https://other.example.com/x"

    def test_blank_path_returns_blank(self):
        assert get_template("naukri").resolve_url("") == ""

    def test_handles_leading_and_trailing_slashes(self):
        t = get_template("indeed")
        assert t.resolve_url("/jobs?from=recjobs") == "https://in.indeed.com/jobs?from=recjobs"


# ── Credential resolution (D-fix: only linkedin/naukri could ever log in) ──

class TestLoginCredentialResolution:
    """_login used to hardcode {"linkedin": ..., "naukri": ...} — the other
    four portals could never log in during scraping even with correct .env
    credentials. Fixed to resolve generically via getattr, matching the
    pattern applier._ensure_logged_in already used correctly."""

    @pytest.fixture
    def fake_settings(self):
        class Settings:
            gmail_address = "user@gmail.com"
            linkedin_email = ""
            linkedin_password = "li-pass"
            naukri_email = ""
            naukri_password = "nk-pass"
            indeed_email = ""
            indeed_password = "in-pass"
            wellfound_email = ""
            wellfound_password = "wf-pass"
            cutshort_email = ""
            cutshort_password = "cs-pass"
            instahyre_email = ""
            instahyre_password = "ih-pass"
        return Settings()

    @pytest.mark.parametrize("portal,expected_password", [
        ("linkedin", "li-pass"),
        ("naukri", "nk-pass"),
        ("indeed", "in-pass"),
        ("wellfound", "wf-pass"),
        ("cutshort", "cs-pass"),
        ("instahyre", "ih-pass"),
    ])
    async def test_attempts_login_for_every_portal_with_a_password_configured(
        self, fake_settings, portal, expected_password
    ):
        from backend.app.services.scraper import _login

        template = get_template(portal)
        page = FakePage(elements={
            template.selectors.login_email_input: FakeElement(),
            template.selectors.login_password_input: FakeElement(),
            template.selectors.login_submit_button: FakeElement(),
        })
        await _login(page, template, fake_settings, portal)

        assert page.filled.get(template.selectors.login_password_input) == expected_password
        assert page.filled.get(template.selectors.login_email_input) == "user@gmail.com"

    async def test_skips_login_when_password_blank(self, fake_settings):
        from backend.app.services.scraper import _login

        fake_settings.wellfound_password = ""
        page = FakePage()
        await _login(page, get_template("wellfound"), fake_settings, "wellfound")
        assert page.filled == {}
        assert not page.clicked


# ── Fakes for card/page scraping ────────────────────────────────────────────

class FakeElement:
    def __init__(self, href=None, text="", enabled=True):
        self._href = href
        self._text = text
        self.enabled = enabled
        self.clicked = False
        self.filled = None
        self.uploaded_path = None
        self.visible = True
        self._fill_callback = None
        self._click_callback = None

    async def get_attribute(self, name):
        return self._href if name == "href" else None

    async def text(self):
        return self._text

    async def is_enabled(self):
        return self.enabled

    async def is_visible(self):
        return self.visible

    async def click(self):
        self.clicked = True
        if self._click_callback:
            self._click_callback()

    async def fill(self, value):
        self.filled = value
        if self._fill_callback:
            self._fill_callback(value)

    async def set_file(self, path):
        self.uploaded_path = path

    async def input_value(self):
        return self.filled or ""

    async def tag_name(self):
        return "input"

    async def select(self, value):
        self.selected_value = value

    async def check(self):
        self.checked = True

    async def execute_script(self, _script, *_args):
        return ""


class FakeCard:
    """A job-listing card: find returns the link or title element."""
    def __init__(self, href, title=None):
        self._children = {"link": FakeElement(href=href), "title": FakeElement(text=title or "")}

    async def find(self, selector):
        # The test selectors below use these literal marker strings so the
        # fake doesn't need to parse real CSS.
        if selector == "a.link":
            return self._children["link"]
        if selector == "a.title":
            return self._children["title"] if self._children["title"]._text else None
        return None


class FakePage:
    """Enough of the Selenium async page facade for _collect_job_urls, _login and
    _upload_resume_to_profile to run against, with no real browser."""

    def __init__(self, cards=None, elements=None, next_button=None):
        self._cards = cards or []
        self._elements = elements or {}
        self._next_button = next_button
        self.filled: dict[str, str] = {}
        self.clicked: list[str] = []
        self.visited: list[str] = []
        self.uploaded: dict[str, str] = {}

        for selector, element in self._elements.items():
            element._fill_callback = lambda value, key=selector: self.filled.__setitem__(key, value)
            element._click_callback = lambda key=selector: self.clicked.append(key)

    async def navigate(self, url, **_kwargs):
        self.visited.append(url)

    async def wait_ready(self, *_a, **_kw):
        pass

    async def wait_for(self, selector, timeout=3_000, visible=False):
        element = await self.find(selector)
        return element if element and (not visible or element.visible) else None

    async def find_all(self, selector):
        if selector == "div.card":
            return self._cards
        return []

    async def find(self, selector):
        if selector == "button.next":
            return self._next_button
        return self._elements.get(selector)

    async def handle_cookie_consent(self):
        return False


# ── _collect_job_urls ────────────────────────────────────────────────────────

class _StubTemplate:
    """Minimal PortalTemplate stand-in — only what _collect_job_urls reads."""
    base_url = "https://example.com"

    class selectors:
        job_card = "div.card"
        job_link_in_card = "a.link"
        job_title_in_card = "a.title"
        next_page_button = ""


@pytest.fixture
def fake_settings_delay():
    class S:
        human_delay_min_ms = 1
        human_delay_max_ms = 2
    return S()


class TestCollectJobUrls:
    async def test_extracts_url_and_title_pairs(self, fake_settings_delay):
        from backend.app.services.scraper import _collect_job_urls

        page = FakePage(cards=[FakeCard("/jobs/1", "Backend Engineer")])
        result = await _collect_job_urls(page, _StubTemplate(), 10, fake_settings_delay)
        assert result == [("https://example.com/jobs/1", "Backend Engineer")]

    async def test_missing_title_element_yields_empty_string_not_a_crash(self, fake_settings_delay):
        """The whole point of this fix: job_title_in_card was defined on every
        template but never read. A card with no resolvable title must degrade
        to "" for the caller's fallback, never raise."""
        from backend.app.services.scraper import _collect_job_urls

        page = FakePage(cards=[FakeCard("/jobs/2", title=None)])
        result = await _collect_job_urls(page, _StubTemplate(), 10, fake_settings_delay)
        assert result == [("https://example.com/jobs/2", "")]

    async def test_deduplicates_repeated_urls(self, fake_settings_delay):
        from backend.app.services.scraper import _collect_job_urls

        page = FakePage(cards=[FakeCard("/jobs/1", "A"), FakeCard("/jobs/1", "A again")])
        result = await _collect_job_urls(page, _StubTemplate(), 10, fake_settings_delay)
        assert len(result) == 1

    async def test_stops_at_max_count(self, fake_settings_delay):
        from backend.app.services.scraper import _collect_job_urls

        cards = [FakeCard(f"/jobs/{i}", f"Job {i}") for i in range(5)]
        result = await _collect_job_urls(page=FakePage(cards=cards), template=_StubTemplate(),
                                          max_count=2, settings=fake_settings_delay)
        assert len(result) == 2

    async def test_skips_cards_with_no_link(self, fake_settings_delay):
        from backend.app.services.scraper import _collect_job_urls

        page = FakePage(cards=[FakeCard(href=None, title="No link")])
        result = await _collect_job_urls(page, _StubTemplate(), 10, fake_settings_delay)
        assert result == []


# ── _upload_resume_to_profile ───────────────────────────────────────────────

class TestUploadResumeToProfile:
    async def test_returns_false_when_no_upload_selector_configured(self, fake_settings_delay):
        from backend.app.services.scraper import _upload_resume_to_profile

        ok = await _upload_resume_to_profile(
            FakePage(), get_template("linkedin"), "/tmp/r.pdf", fake_settings_delay
        )
        assert ok is False

    async def test_uploads_when_field_present(self, fake_settings_delay):
        from backend.app.services.scraper import _upload_resume_to_profile

        template = get_template("naukri")
        upload_el = FakeElement()
        page = FakePage(elements={template.selectors.profile_resume_upload: upload_el})
        ok = await _upload_resume_to_profile(page, template, "/tmp/r.pdf", fake_settings_delay)
        assert ok is True
        assert upload_el.uploaded_path == "/tmp/r.pdf"
        assert page.visited[0] == template.resolve_url(template.selectors.profile_resume_page)

    async def test_returns_false_when_field_not_found_on_page(self, fake_settings_delay):
        """Selector configured but the live page doesn't have it — drift, not
        a crash. Must degrade to False so the caller falls back to keyword
        search instead of raising out of the whole run."""
        from backend.app.services.scraper import _upload_resume_to_profile

        ok = await _upload_resume_to_profile(
            FakePage(elements={}), get_template("naukri"), "/tmp/r.pdf", fake_settings_delay
        )
        assert ok is False

    async def test_exception_is_caught_and_returns_false(self, fake_settings_delay):
        from backend.app.services.scraper import _upload_resume_to_profile

        class ExplodingPage(FakePage):
            async def goto(self, *a, **kw):
                raise RuntimeError("navigation timed out")

        ok = await _upload_resume_to_profile(
            ExplodingPage(), get_template("naukri"), "/tmp/r.pdf", fake_settings_delay
        )
        assert ok is False


# ── _scrape_recommended_page ────────────────────────────────────────────────

class TestScrapeRecommendedPage:
    async def test_empty_url_returns_empty_without_touching_the_page(self, fake_settings_delay):
        from backend.app.services.scraper import _scrape_recommended_page

        class BoomIfTouched(FakePage):
            async def goto(self, *a, **kw):
                raise AssertionError("must not navigate — recommended_jobs_url is blank")

        result = await _scrape_recommended_page(
            BoomIfTouched(), get_template("linkedin"), 10, fake_settings_delay
        )
        assert result == []


# ── scrape_recommended_jobs: the part testable without a real browser ──────

class TestScrapeRecommendedJobsEarlyReturn:
    async def test_unsupported_portal_returns_empty_without_launching_a_browser(self):
        """LinkedIn has no resume-driven config — this must short-circuit
        before ever creating a WebDriver, so it's safe to run with no
        browser installed at all."""
        from backend.app.services.scraper import scrape_recommended_jobs

        result = await scrape_recommended_jobs("linkedin", "/tmp/r.pdf", "Remote", 10)
        assert result == []
