from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class FieldSelector:
    """CSS/XPath locator + optional label used for matching questions."""
    selector: str
    label: str = ""
    is_xpath: bool = False
    optional: bool = False


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

    def is_easy_apply(self) -> bool:
        """True if portal supports one-click apply (e.g. LinkedIn Easy Apply)."""
        return bool(self.selectors.easy_apply_button)

    def requires_login(self) -> bool:
        return bool(self.selectors.login_email_input)

    def __repr__(self) -> str:
        return f"<PortalTemplate: {self.name}>"
