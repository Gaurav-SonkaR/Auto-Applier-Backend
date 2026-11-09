from __future__ import annotations

from backend.app.services.portal_templates.base import PortalSelectors, PortalTemplate


class NaukriTemplate(PortalTemplate):
    """
    Naukri resume-driven fields (profile_resume_*, recommended_jobs_url) are
    best-effort — Naukri's own "Recommended Jobs" / "Jobs 4 u" widget is a real,
    long-standing feature keyed off the resume in your profile, but the exact
    paths/selectors below are unverified against the live DOM. Run one headed
    scrape (SELENIUM_HEADLESS=false) with a master resume set before relying
    on this in an unattended run; scraper.scrape_recommended_jobs falls back to
    keyword search automatically if upload or scraping fails.
    """

    # Naukri applies on the Apply click unless the recruiter added questions.
    apply_click_submits = True

    @property
    def name(self) -> str:
        return "naukri"

    @property
    def base_url(self) -> str:
        return "https://www.naukri.com"

    @property
    def selectors(self) -> PortalSelectors:
        return PortalSelectors(
            search_url_template=(
                "https://www.naukri.com/{title}-jobs-in-{location}"
            ),
            # ── Search results ─────────────────────────────────────────────
            job_card="div.srp-jobtuple-wrapper",
            job_title_in_card="a.title",
            job_link_in_card="a.title",
            next_page_button="a[data-testid='pagination-next']",
            total_results_text="span.count",
            # ── Detail page ────────────────────────────────────────────────
            apply_button="button#apply-button, button.apply-button",
            easy_apply_button="",  # Naukri does not have true Easy Apply
            job_description_container="div.job-description",
            job_title="h1.jd-header-title",
            company_name="a.comp-name",
            # ── Application form ───────────────────────────────────────────
            resume_upload="input[type='file']#resumeFile",
            form_text_inputs="div.chatbot_InputBox input[type='text']",
            form_select_inputs="div.chatbot_InputBox select",
            form_textarea_inputs="div.chatbot_InputBox textarea",
            form_checkbox_inputs="div.chatbot_InputBox input[type='checkbox']",
            submit_button="button.sendMsg",
            # ── Auth ───────────────────────────────────────────────────────
            login_email_input="input#usernameField",
            login_password_input="input#passwordField",
            login_submit_button="button[data-ga-track='login|submit']",
            login_detection_element="div.naukri-header",
            # ── Result detection ───────────────────────────────────────────
            success_indicator="div.confirmation-msg, div.apply-success",
            captcha_indicator="iframe[src*='recaptcha'], div[id*='captcha']",
            # ── Resume-driven matching (unverified — see class docstring) ──
            profile_resume_page="mnjuser/profile",
            profile_resume_upload="input#attachCV, input[type='file'][name='resume']",
            recommended_jobs_url="mnjuser/homepage",
        )
