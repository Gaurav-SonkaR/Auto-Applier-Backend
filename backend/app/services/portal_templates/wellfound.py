from __future__ import annotations

from backend.app.services.portal_templates.base import PortalSelectors, PortalTemplate


class WellfoundTemplate(PortalTemplate):
    """
    Resume-driven fields are best-effort and unverified against the live DOM —
    see NaukriTemplate's docstring for the verification note that applies to
    every portal template in this package.
    """

    @property
    def name(self) -> str:
        return "wellfound"

    @property
    def base_url(self) -> str:
        return "https://wellfound.com"

    @property
    def selectors(self) -> PortalSelectors:
        return PortalSelectors(
            search_url_template=(
                "https://wellfound.com/jobs?q={title}&l={location}"
            ),
            # ── Search results ─────────────────────────────────────────────
            job_card="div[class*='JobListing_jobListing']",
            job_title_in_card="a[class*='JobListing_jobTitle']",
            job_link_in_card="a[class*='JobListing_jobTitle']",
            next_page_button="a[rel='next']",
            total_results_text="",
            # ── Detail page ────────────────────────────────────────────────
            apply_button="a[data-test='StartJobApplication']",
            easy_apply_button="",
            job_description_container="div[class*='JobListingDetails_jobDescription']",
            job_title="h1[class*='JobListingDetails_jobTitle']",
            company_name="a[class*='JobListingDetails_companyLink']",
            # ── Application form ───────────────────────────────────────────
            resume_upload="input[type='file'][accept*='pdf']",
            form_text_inputs="div.application-form input[type='text']",
            form_select_inputs="div.application-form select",
            form_textarea_inputs="div.application-form textarea",
            form_checkbox_inputs="div.application-form input[type='checkbox']",
            submit_button="button[type='submit'][data-test='SubmitApplication']",
            # ── Auth ───────────────────────────────────────────────────────
            login_email_input="input[name='email'][placeholder*='Email']",
            login_password_input="input[name='password'][type='password']",
            login_submit_button="button[type='submit'][data-test='SignIn']",
            login_detection_element="a[href='/profile']",
            # ── Result detection ───────────────────────────────────────────
            success_indicator="div[data-test='ApplicationConfirmation']",
            captcha_indicator="iframe[src*='hcaptcha'], iframe[src*='recaptcha']",
            # ── Resume-driven matching (unverified — see class docstring) ──
            profile_resume_page="profile/resume",
            profile_resume_upload="input[type='file'][accept*='pdf']",
            recommended_jobs_url="jobs/for-you",
        )
