from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class PortalSelectors:
    """All CSS/XPath selectors for a job portal."""
    # ── Search page ──────────────────────────────────────────────────────────
    search_url_template: str = ""
    job_card: str = ""
    job_title_in_card: str = ""
    job_link_in_card: str = ""
    next_page_button: str = ""

    # ── Job detail page ──────────────────────────────────────────────────────
    apply_button: str = ""
    easy_apply_button: str = ""
    job_description_container: str = ""
    job_title: str = ""
    company_name: str = ""

    # ── Application form ─────────────────────────────────────────────────────
    resume_upload: str = ""
    form_text_inputs: str = ""
    form_select_inputs: str = ""
    form_textarea_inputs: str = ""
    form_checkbox_inputs: str = ""
    submit_button: str = ""

    # ── Auth ─────────────────────────────────────────────────────────────────
    login_email_input: str = ""
    login_password_input: str = ""
    login_submit_button: str = ""
    login_detection_element: str = ""

    # ── Post-submit ──────────────────────────────────────────────────────────
    success_indicator: str = ""
    captcha_indicator: str = ""

    # ── Pagination ───────────────────────────────────────────────────────────
    total_results_text: str = ""

    # ── Resume-driven job matching ────────────────────────────────────────────
    # When the user has an uploaded master resume and leaves the job title
    # blank, some portals can match jobs against that resume directly instead
    # of a typed keyword search — Naukri's "Recommended Jobs", Indeed's
    # recommendation feed, Cutshort/Instahyre's AI matching, Wellfound's
    # curated roles. Leave both blank for a portal that has no such feature
    # (or where it isn't resume-file-driven, e.g. LinkedIn recommends from
    # profile fields, not an uploaded PDF) — scraper.scrape_recommended_jobs
    # treats an empty recommended_jobs_url as "not supported" and the caller
    # falls back to keyword search.
    profile_resume_page: str = ""        # profile/resume-management page to upload to
    profile_resume_upload: str = ""      # <input type=file> selector there
    profile_resume_save_button: str = "" # optional explicit save/update button
    recommended_jobs_url: str = ""       # portal-curated job listing, once matched


class PortalTemplate(ABC):
    """
    Abstract base for job portal templates.

    Each portal subclass provides:
    - CSS/XPath selectors for its DOM structure
    - Portal-specific logic hooks (pre/post apply, login, etc.)
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Portal identifier — must match portal column values in DB."""
        ...

    @property
    @abstractmethod
    def base_url(self) -> str:
        ...

    @property
    @abstractmethod
    def selectors(self) -> PortalSelectors:
        ...

    def build_search_url(self, job_title: str, location: str) -> str:
        """Return a search URL for the given query."""
        from urllib.parse import quote_plus
        t = quote_plus(job_title)
        loc = quote_plus(location)
        template = self.selectors.search_url_template
        return template.format(title=t, location=loc)

    def requires_login(self) -> bool:
        return bool(self.selectors.login_email_input)

    # True where clicking Apply can itself submit — no form in between when a
    # posting has no screening questions. A rehearsal stops before that click.
    apply_click_submits: bool = False

    def supports_resume_driven_search(self) -> bool:
        """True if this portal can be told to match jobs against an uploaded resume
        (as opposed to only a typed keyword search)."""
        return bool(self.selectors.profile_resume_upload and self.selectors.recommended_jobs_url)

    def resolve_url(self, path: str) -> str:
        """Absolute URL from a selector-configured path — relative or already absolute."""
        if not path:
            return ""
        if path.startswith("http"):
            return path
        return self.base_url.rstrip("/") + "/" + path.lstrip("/")

    def __repr__(self) -> str:
        return f"<PortalTemplate: {self.name}>"
