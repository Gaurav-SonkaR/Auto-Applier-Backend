from __future__ import annotations

from backend.services.portal_templates.base import PortalSelectors, PortalTemplate


class LinkedInTemplate(PortalTemplate):
    @property
    def name(self) -> str:
        return "linkedin"

    @property
    def base_url(self) -> str:
        return "https://www.linkedin.com"

    @property
    def selectors(self) -> PortalSelectors:
        return PortalSelectors(
            search_url_template=(
                "https://www.linkedin.com/jobs/search/?keywords={title}&location={location}"
                "&f_AL=true"  # Easy Apply filter
            ),
            # ── Search results ─────────────────────────────────────────────
            job_card="ul.jobs-search__results-list li.jobs-search-results__list-item",
            job_title_in_card="a.job-card-list__title",
            job_link_in_card="a.job-card-list__title",
            next_page_button="button[aria-label='View next page']",
            total_results_text="span.jobs-search-results-list__subtitle",
            # ── Detail page ────────────────────────────────────────────────
            apply_button="button.jobs-apply-button",
            easy_apply_button="button.jobs-apply-button[data-control-name='jobdetails_topcard_inapply']",
            job_description_container="div.jobs-description__content",
            job_title="h1.job-details-jobs-unified-top-card__job-title",
            company_name="a.job-details-jobs-unified-top-card__company-name",
            # ── Easy Apply form ────────────────────────────────────────────
            resume_upload="input[type='file'][name='resume']",
            form_text_inputs="div.jobs-easy-apply-form-section input[type='text']",
            form_select_inputs="div.jobs-easy-apply-form-section select",
            form_textarea_inputs="div.jobs-easy-apply-form-section textarea",
            form_checkbox_inputs="div.jobs-easy-apply-form-section input[type='checkbox']",
            submit_button="button[aria-label='Submit application']",
            # ── Auth ───────────────────────────────────────────────────────
            login_email_input="input#username",
            login_password_input="input#password",
            login_submit_button="button[data-litms-control-urn='login-submit']",
            login_detection_element="nav.global-nav",
            # ── Result detection ───────────────────────────────────────────
            success_indicator="h3.artdeco-inline-feedback__message",
            captcha_indicator="iframe[src*='recaptcha'], div.challenge-dialog",
        )
