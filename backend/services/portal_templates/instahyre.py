from __future__ import annotations

from backend.services.portal_templates.base import PortalSelectors, PortalTemplate


class InstahyreTemplate(PortalTemplate):
    """
    Instahyre (Indian tech job platform) template.

    NOTE: The CSS selectors below are best-effort starting points and MUST be
    verified against the live Instahyre DOM before relying on this portal in an
    unattended run — Instahyre renders much of its UI dynamically and the class
    names change. Run one headed apply (PLAYWRIGHT_HEADLESS=false) and correct any
    selector that doesn't resolve. This same caveat applies to every portal
    template in this package.
    """

    @property
    def name(self) -> str:
        return "instahyre"

    @property
    def base_url(self) -> str:
        return "https://www.instahyre.com"

    @property
    def selectors(self) -> PortalSelectors:
        return PortalSelectors(
            search_url_template=(
                "https://www.instahyre.com/search-jobs/?q={title}&location={location}"
            ),
            # ── Search results ─────────────────────────────────────────────
            job_card="div.opportunity-card, div.job-card",
            job_title_in_card="a.opportunity-title, a.job-title",
            job_link_in_card="a.opportunity-title, a.job-title",
            next_page_button="button.load-more, a.pagination-next",
            total_results_text="span.results-count",
            # ── Detail page ────────────────────────────────────────────────
            apply_button="button.apply-button, a.apply-now",
            easy_apply_button="button.apply-button",  # Instahyre applies in-site
            job_description_container="div.opportunity-description, div.job-description",
            job_title="h1.opportunity-title, h1.job-title",
            company_name="a.company-name, div.company-name",
            # ── Application form ───────────────────────────────────────────
            resume_upload="input[type='file'][accept*='pdf']",
            form_text_inputs="form.apply-form input[type='text']",
            form_select_inputs="form.apply-form select",
            form_textarea_inputs="form.apply-form textarea",
            form_checkbox_inputs="form.apply-form input[type='checkbox']",
            submit_button="button[type='submit'].submit-application",
            # ── Auth ───────────────────────────────────────────────────────
            login_email_input="input[type='email'][name='email']",
            login_password_input="input[type='password'][name='password']",
            login_submit_button="button[type='submit'].login-button",
            login_detection_element="div.user-menu, a[href*='/candidate/']",
            # ── Result detection ───────────────────────────────────────────
            success_indicator="div.application-success, div.applied-badge",
            captcha_indicator="iframe[src*='recaptcha'], div.g-recaptcha",
        )
