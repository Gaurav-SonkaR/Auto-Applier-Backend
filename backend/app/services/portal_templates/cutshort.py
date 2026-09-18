from __future__ import annotations

from backend.app.services.portal_templates.base import PortalSelectors, PortalTemplate


class CutshortTemplate(PortalTemplate):
    """
    Resume/profile-driven AI matching is Cutshort's core pitch, but the exact
    fields below are unverified against the live DOM — see NaukriTemplate's
    docstring for the verification note that applies to every portal template
    in this package.
    """

    @property
    def name(self) -> str:
        return "cutshort"

    @property
    def base_url(self) -> str:
        return "https://cutshort.io"

    @property
    def selectors(self) -> PortalSelectors:
        return PortalSelectors(
            search_url_template=(
                "https://cutshort.io/jobs?q={title}&location={location}"
            ),
            # ── Search results ─────────────────────────────────────────────
            job_card="div.job-card",
            job_title_in_card="a.job-title",
            job_link_in_card="a.job-title",
            next_page_button="button.load-more, a.pagination-next",
            total_results_text="span.results-count",
            # ── Detail page ────────────────────────────────────────────────
            apply_button="button.apply-btn, a.apply-now-btn",
            easy_apply_button="button.apply-btn",  # Cutshort has in-site apply
            job_description_container="div.job-description-content",
            job_title="h1.job-title",
            company_name="a.company-name, span.company-name",
            # ── Application form ───────────────────────────────────────────
            resume_upload="input[type='file'][accept*='pdf']",
            form_text_inputs="div.apply-form input[type='text']",
            form_select_inputs="div.apply-form select",
            form_textarea_inputs="div.apply-form textarea",
            form_checkbox_inputs="div.apply-form input[type='checkbox']",
            submit_button="button[type='submit'].submit-application",
            # ── Auth ───────────────────────────────────────────────────────
            login_email_input="input[type='email'][name='email']",
            login_password_input="input[type='password'][name='password']",
            login_submit_button="button[type='submit'].login-btn",
            login_detection_element="div.user-avatar, a[href*='/profile/']",
            # ── Result detection ───────────────────────────────────────────
            success_indicator="div.success-message, h2.application-submitted",
            captcha_indicator="iframe[src*='recaptcha'], div.g-recaptcha",
            # ── Resume-driven matching (unverified — see class docstring) ──
            profile_resume_page="candidate/profile",
            profile_resume_upload="input[type='file'][accept*='pdf']",
            recommended_jobs_url="candidate/jobs",
        )
