from __future__ import annotations

from backend.services.portal_templates.base import PortalSelectors, PortalTemplate


class WebTemplate(PortalTemplate):
    """
    Template for company/startup career pages discovered via Google Discovery.

    Covers Greenhouse.io, Lever.co, Workable, and generic company career pages.
    No login required — each job URL is a standalone application page.
    """

    @property
    def name(self) -> str:
        return "web"

    @property
    def base_url(self) -> str:
        return ""  # No base login URL — each job URL is standalone

    @property
    def selectors(self) -> PortalSelectors:
        return PortalSelectors(
            # ── Apply button ──────────────────────────────────────────────────
            apply_button=(
                "a#apply_button, a.postings-btn, "                           # Greenhouse
                "a.template-btn-submit, button.template-btn-submit, "        # Lever
                "a.btn-primary[href*='apply'], button[data-ui='btn-apply'], " # Workable
                "a[href*='apply'][class*='btn'], button[class*='apply-btn'], "
                "a[class*='apply'], button[class*='apply'], a[href*='/apply']"
            ),
            # ── Resume upload ─────────────────────────────────────────────────
            resume_upload=(
                "input#resume, input[name='resume'], input#resume-upload, "
                "input[accept*='pdf'][type='file'], input[type='file']"
            ),
            # ── Form fields ───────────────────────────────────────────────────
            form_text_inputs=(
                "form input[type='text'], form input[type='email'], "
                "form input[type='tel'], form input[type='url']"
            ),
            form_textarea_inputs="form textarea",
            form_select_inputs="form select",
            # ── Submit ────────────────────────────────────────────────────────
            submit_button=(
                "input[type='submit']#submit_app, "
                "input[type='submit'].btn, "
                "input.template-btn-submit[type='submit'], "
                "button[type='submit'][data-ui='submit-btn'], "
                "button[type='submit'], "
                "input[type='submit']"
            ),
            # ── Success indicators ────────────────────────────────────────────
            success_indicator=(
                "p.message, div#confirm_message, div.confirmation, "
                "h1.thanks, div.thanks-page, "
                "div[class*='success'], div[class*='confirmation'], "
                "h1[class*='thank'], div[class*='thank-you'], "
                "p:has-text('application received'), p:has-text('Thank you')"
            ),
        )
