from __future__ import annotations

from backend.app.services.portal_templates.base import PortalSelectors, PortalTemplate


class WebTemplate(PortalTemplate):
    """
    Template for company/startup career pages discovered via Google Discovery.

    Covers Greenhouse.io, Lever.co, Workable, and generic company career pages.
    No login required — each job URL is a standalone application page.

    **Deliberately selector-free.** This template used to carry a full set of
    apply-page selectors (apply button, resume upload, form fields, submit,
    success indicator). Nothing read them: Smart Job Apply stops at
    READY_TO_APPLY and the user submits by hand, so `apply_to_job` is never
    called for this portal. Carrying them implied an auto-apply path that does
    not exist, and they would have rotted untested — every career page has a
    different DOM.

    `job_description_container` is left blank on purpose too: `_fetch_jd_node`
    falls back to the page's body text, which is the only thing that generalises
    across arbitrary company career sites.
    """

    @property
    def name(self) -> str:
        return "web"

    @property
    def base_url(self) -> str:
        return ""  # No base login URL — each job URL is standalone

    @property
    def selectors(self) -> PortalSelectors:
        return PortalSelectors()
