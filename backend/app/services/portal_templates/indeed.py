from __future__ import annotations

from backend.app.services.portal_templates.base import PortalSelectors, PortalTemplate


class IndeedTemplate(PortalTemplate):
    """
    Resume-driven fields are best-effort — Indeed's resume upload and "Jobs for
    you" recommendation feed are real features, but the paths/selectors below
    are unverified against the live DOM. See NaukriTemplate's docstring for the
    verification note that applies to every portal template in this package.
    """

    @property
    def name(self) -> str:
        return "indeed"

    @property
    def base_url(self) -> str:
        return "https://in.indeed.com"

    @property
    def selectors(self) -> PortalSelectors:
        return PortalSelectors(
            search_url_template=(
                "https://in.indeed.com/jobs?q={title}&l={location}"
            ),
            # ── Search results ─────────────────────────────────────────────
            job_card="div.job_seen_beacon",
            job_title_in_card="h2.jobTitle a",
            job_link_in_card="h2.jobTitle a",
            next_page_button="a[data-testid='pagination-page-next']",
            total_results_text="div.jobsearch-JobCountAndSortPane span",
            # ── Detail page ────────────────────────────────────────────────
            apply_button="button#indeedApplyButton, a.indeed-apply-button",
            easy_apply_button="button#indeedApplyButton",
            job_description_container="div#jobDescriptionText",
            job_title="h1.jobsearch-JobInfoHeader-title",
            company_name="div[data-company-name] span",
            # ── Application form (Indeed Resume/Quick Apply modal) ─────────
            resume_upload="input[type='file'][name='resume']",
            form_text_inputs="div.ia-continuum-modal input[type='text']",
            form_select_inputs="div.ia-continuum-modal select",
            form_textarea_inputs="div.ia-continuum-modal textarea",
            form_checkbox_inputs="div.ia-continuum-modal input[type='checkbox']",
            submit_button="button[type='submit'][data-tn-element='submit-btn']",
            # ── Auth ───────────────────────────────────────────────────────
            login_email_input="input#login-email-input",
            login_password_input="input#login-password-input",
            login_submit_button="button#login-submit-button",
            login_detection_element="a[href*='/account/view?hl=']",
            # ── Result detection ───────────────────────────────────────────
            success_indicator="div.ia-PostApply-successMessage, h1[data-tn-element='postApplyTitle']",
            captcha_indicator="iframe[src*='recaptcha'], div.g-recaptcha",
            # ── Resume-driven matching (unverified — see class docstring) ──
            profile_resume_page="career-services/resume",
            profile_resume_upload="input[type='file'][accept*='pdf']",
            recommended_jobs_url="jobs?from=recjobs",
        )
