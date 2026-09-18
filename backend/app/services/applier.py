from __future__ import annotations

import asyncio
import re
import logging
from contextlib import closing
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, async_playwright
from playwright_stealth import Stealth
from sqlmodel import Session

from backend.app.config import get_settings
from backend.app.constants import is_external_ats
from backend.app.database import get_session
from backend.app.services.browser_pool import browser_semaphore
from backend.app.models.application import Application, ApplicationStatus
from backend.app.models.job import Job, JobStatus
from backend.app.models.job_stage_event import JobStageEvent
from backend.app.services.llm_client import LLMTask, get_llm_router
from backend.app.services.portal_templates import get_template
from backend.app.services.form_autofill import (
    unanswered_required,
    RADIO_GROUPS_JS,
    FieldContext,
    autofill_form,
    match_field,
)
from backend.app.services.form_choices import (
    choose_from_answer,
    Option,
    decline_option,
    is_placeholder,
    is_sensitive,
    real_options,
    resolve_llm_choice,
)
from backend.app.services import question_bank
from backend.app.database import engine
from backend.app.services import profiles as profiles_service
from backend.app.workflows.types import ApplyResult

logger = logging.getLogger("agent.apply")

# The candidate's name is interpolated per run. Hard-coding it meant every
# account's form answers were written as one particular person.
QA_LLM_SYSTEM_TEMPLATE = """\
You are filling out a job application form on behalf of {candidate}.
Answer the question truthfully from the profile below. If the profile does not
say, answer with what it does support — never invent a fact about them.
Keep answers concise — 1-2 sentences max for text, single values for dropdowns.
Return ONLY the answer text, no explanation.
"""


def _qa_system_prompt(profile: dict) -> str:
    name = str((profile.get("personal") or {}).get("name") or "").strip()
    return QA_LLM_SYSTEM_TEMPLATE.format(candidate=name or "the candidate")

CAPTCHA_SELECTORS = [
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    "div.g-recaptcha",
    "div[id*='captcha']",
    "div.cf-challenge-running",
    ".challenge-form",
]

# LinkedIn Easy Apply multi-step wizard constants
_LI_MAX_STEPS = 10
_LI_LABELS = ["Continue to next step", "Review your application", "Submit application"]
_LI_SUBMIT_LABEL = "Submit application"


def _auth_state_path(portal: str, settings) -> Path:
    """Return the path where the browser session cookies are saved."""
    return Path(settings.log_dir) / f"auth_{portal}.json"


# Smart Job Apply. Defined here rather than in run_loop because applier.py is
# the module that must refuse it, and run_loop already imports from here.
MANUAL_APPLY_PORTAL = "web"


async def apply_to_job(
    job: Job,
    resume_path: str,
    session: Optional[Session] = None,
    dry_run: bool = False,
) -> ApplyResult:
    """
    Navigate to the job URL and submit an application.
    Returns ApplyResult with status APPLIED / FAILED / FLAGGED.

    Job-portal applications only. Smart Job Apply (portal="web") is routed to
    READY_TO_APPLY by the pipeline and never reaches here — see the guard below.
    """
    settings = get_settings()

    # Smart Job Apply must never auto-submit: those are company career pages and
    # third-party ATS forms the user applies to by hand. `_after_resume` already
    # routes them to READY_TO_APPLY, so this is unreachable today — it is a guard
    # rather than an assumption because the cost of a future mis-wiring is an
    # application submitted on the user's behalf that they never approved.
    if job.portal == MANUAL_APPLY_PORTAL:
        logger.error("Refusing to auto-apply to job %d: %s jobs are manual-apply only",
                     job.job_id, MANUAL_APPLY_PORTAL)
        result = ApplyResult(
            job_id=job.job_id,
            status=JobStatus.READY_TO_APPLY,
            reason="Career-site jobs are applied to manually, not auto-submitted",
        )
        await _update_job(job, result, session)
        return result

    # Pre-flight: external ATS check. Unconditional — the only caller is the
    # apply node, which the guard above and the pipeline routing both keep
    # portal-only, so the old `portal != "web"` half of this was dead.
    if is_external_ats(job.url):
        logger.info("Job %d is on external ATS domain — flagging", job.job_id)
        result = ApplyResult(
            job_id=job.job_id,
            status=JobStatus.NEED_HUMAN_ACTION,
            reason=f"External ATS domain: {urlparse(job.url).netloc}",
            redirect_url=job.url,
        )
        await _update_job(job, result, session)
        return result

    template = get_template(job.portal)

    try:
        result = await _browser_apply(job, resume_path, template, settings, dry_run)
    except Exception as exc:
        logger.error("Apply error for job %d: %s", job.job_id, exc, exc_info=True)
        result = ApplyResult(
            job_id=job.job_id,
            status=JobStatus.FAILED,
            reason=str(exc)[:500],
        )

    await _update_job(job, result, session)
    return result


async def _browser_apply(job: Job, resume_path: str, template, settings,
                         dry_run: bool = False) -> ApplyResult:
    """Launch a browser, restore any saved session, and run the apply flow."""
    auth_path = _auth_state_path(job.portal, settings)

    async with browser_semaphore(), async_playwright() as pw:
        browser = await pw.chromium.launch(headless=settings.playwright_headless)
        try:
            ctx = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                # Restore saved cookies/localStorage if we've logged in before
                storage_state=str(auth_path) if auth_path.exists() else None,
            )
            page = await ctx.new_page()
            await Stealth().apply_stealth_async(page)

            # Login if the portal requires it (LinkedIn, Naukri, etc.)
            if template.requires_login():
                logged_in = await _ensure_logged_in(
                    page, ctx, template, settings, job.portal, auth_path
                )
                if not logged_in:
                    return ApplyResult(
                        job_id=job.job_id,
                        status=JobStatus.WAITING_LOGIN,
                        reason="Login failed — check credentials in .env (bad creds, 2FA, or changed login flow)",
                    )

            try:
                result = await _run_apply_flow(page, job, resume_path, template, settings,
                                               dry_run=dry_run)
            except Exception as exc:
                logger.error("Apply flow error for job %d: %s", job.job_id, exc, exc_info=True)
                screenshot = await _screenshot(page, job.job_id, settings.screenshots_dir, "error")
                result = ApplyResult(
                    job_id=job.job_id,
                    status=JobStatus.FAILED,
                    reason=str(exc)[:500],
                    screenshot_path=screenshot,
                )
        finally:
            await browser.close()

    return result


async def _ensure_logged_in(
    page: Page,
    ctx: BrowserContext,
    template,
    settings,
    portal: str,
    auth_path: Path,
) -> bool:
    """
    Navigate to the portal homepage. If not logged in, fill credentials and submit.
    Saves session state on success so subsequent jobs skip the login step.
    """
    try:
        await page.goto(template.base_url, wait_until="domcontentloaded", timeout=20_000)
        await asyncio.sleep(2)

        if await _is_logged_in(page, template):
            return True

        sel = template.selectors

        # Resolve credentials per-portal. The login email falls back to the Gmail
        # address (common when portals use Google sign-in), but the password is
        # strictly the portal's own — never the Gmail app password, which would
        # only cause failed logins (and repeated failures can lock the account).
        portal_email = getattr(settings, f"{portal}_email", "") or settings.gmail_address
        portal_password = getattr(settings, f"{portal}_password", "")
        if not portal_password:
            logger.warning(
                "No %s_PASSWORD configured — cannot log into %s. Set it in .env.",
                portal.upper(), portal,
            )
            return False

        await page.fill(sel.login_email_input, portal_email)
        await asyncio.sleep(0.5)
        await page.fill(sel.login_password_input, portal_password)
        await asyncio.sleep(0.5)
        await page.click(sel.login_submit_button)
        await asyncio.sleep(3)

        if await _is_logged_in(page, template):
            await ctx.storage_state(path=str(auth_path))
            logger.info("Logged into %s — session saved to %s", portal, auth_path)
            return True

        logger.warning("Login to %s failed — current URL: %s", portal, page.url)
        return False

    except Exception as exc:
        logger.error("_ensure_logged_in(%s) error: %s", portal, exc)
        return False


async def _is_logged_in(page: Page, template) -> bool:
    """Return True if the current page indicates an active session."""
    url = page.url.lower()
    for marker in ("/login", "/signin", "/checkpoint", "/authwall"):
        if marker in url:
            return False

    detection_sel = template.selectors.login_detection_element
    if detection_sel:
        try:
            el = await page.query_selector(detection_sel)
            return el is not None
        except Exception as exc:
            logger.warning("Login-detection selector check failed (%s): %s", detection_sel, exc)
            return False
    return True


async def _run_apply_flow(
    page: Page, job: Job, resume_path: str, template, settings, dry_run: bool = False
) -> ApplyResult:
    sel = template.selectors

    # 1. Navigate to job page
    await page.goto(job.url, wait_until="domcontentloaded", timeout=30_000)
    await _human_delay(settings)

    # 2. CAPTCHA check on landing page
    if await _has_captcha(page):
        screenshot = await _screenshot(page, job.job_id, settings.screenshots_dir, "captcha")
        logger.info("Job %d — CAPTCHA on landing page", job.job_id)
        return ApplyResult(
            job_id=job.job_id,
            status=JobStatus.WAITING_CAPTCHA,
            reason="CAPTCHA detected — manual intervention required",
            screenshot_path=screenshot,
        )

    # 3. Find apply button
    apply_selector = sel.easy_apply_button or sel.apply_button
    apply_btn = await page.query_selector(apply_selector) if apply_selector else None

    if apply_btn:
        href = await apply_btn.get_attribute("href") or ""
        # Same as the pre-flight check: this only ever runs for portal jobs.
        if href and is_external_ats(href):
            return ApplyResult(
                job_id=job.job_id,
                status=JobStatus.NEED_HUMAN_ACTION,
                reason=f"Apply button redirects to external ATS: {urlparse(href).netloc}",
                redirect_url=href,
            )
        if dry_run and template.apply_click_submits:
            return await _rehearsal_result(
                page, job, settings, "dry_run",
                stopped="Stopped before Apply: on this portal the Apply button itself can submit.",
            )
        await apply_btn.click()
        await _human_delay(settings)
    else:
        return ApplyResult(
            job_id=job.job_id,
            status=JobStatus.FAILED,
            reason="Apply button not found on page",
        )

    # 4. Post-click CAPTCHA check
    if await _has_captcha(page):
        screenshot = await _screenshot(page, job.job_id, settings.screenshots_dir, "captcha_post")
        return ApplyResult(
            job_id=job.job_id,
            status=JobStatus.WAITING_CAPTCHA,
            reason="CAPTCHA appeared after clicking Apply",
            screenshot_path=screenshot,
        )

    # 5. LinkedIn Easy Apply is a multi-step modal — handled separately
    if job.portal == "linkedin":
        return await _linkedin_easy_apply(page, job, resume_path, sel, settings, dry_run)

    # 6. Generic single-page flow (web / naukri / wellfound / cutshort / etc.)

    # Upload resume if visible
    if sel.resume_upload:
        upload_input = await page.query_selector(sel.resume_upload)
        if upload_input and await upload_input.is_visible():
            await upload_input.set_input_files(resume_path)
            await _human_delay(settings)
            logger.debug("Job %d — resume uploaded", job.job_id)

    # Fill visible form fields (profile autofill, then LLM for open questions)
    gate_status = await _fill_form(page, sel, job, settings)
    if gate_status:
        screenshot = await _screenshot(page, job.job_id, settings.screenshots_dir, "gate")
        return ApplyResult(
            job_id=job.job_id,
            status=gate_status,
            reason="Verification step requires a human — complete it, then retry the job",
            screenshot_path=screenshot,
        )

    # Submit
    submit_btn = await page.query_selector(sel.submit_button)
    if not submit_btn:
        return ApplyResult(
            job_id=job.job_id,
            status=JobStatus.FAILED,
            reason="Submit button not found",
        )
    if dry_run:
        return await _rehearsal_result(page, job, settings, "dry_run")
    await _human_delay(settings)
    await submit_btn.click()
    await page.wait_for_load_state("networkidle", timeout=20_000)

    # Confirm success
    if sel.success_indicator:
        try:
            await page.wait_for_selector(sel.success_indicator, timeout=8_000)
            screenshot = await _screenshot(page, job.job_id, settings.screenshots_dir, "success")
            logger.info("Job %d — APPLIED successfully", job.job_id)
            return ApplyResult(
                job_id=job.job_id,
                status=JobStatus.APPLIED,
                screenshot_path=screenshot,
            )
        except Exception as exc:
            # Success indicator didn't appear — not necessarily a failure yet
            # (CAPTCHA/unknown checks below), but never swallow this silently:
            # it's the difference between "applied" and "unconfirmed".
            logger.warning(
                "Job %d — success indicator '%s' not confirmed: %s",
                job.job_id, sel.success_indicator, exc,
            )

    if await _has_captcha(page):
        screenshot = await _screenshot(page, job.job_id, settings.screenshots_dir, "captcha_submit")
        return ApplyResult(
            job_id=job.job_id,
            status=JobStatus.WAITING_CAPTCHA,
            reason="CAPTCHA on submission page",
            screenshot_path=screenshot,
        )

    screenshot = await _screenshot(page, job.job_id, settings.screenshots_dir, "unknown")
    return ApplyResult(
        job_id=job.job_id,
        status=JobStatus.FAILED,
        reason="Could not confirm submission — no success indicator found",
        screenshot_path=screenshot,
    )


async def _linkedin_easy_apply(
    page: Page, job: Job, resume_path: str, sel, settings, dry_run: bool = False
) -> ApplyResult:
    """
    Handle LinkedIn's multi-step Easy Apply modal.
    Each step shows a different button label; we click through until 'Submit application'.
    """
    for step in range(_LI_MAX_STEPS):
        await asyncio.sleep(1.5)  # wait for modal transition animation

        if await _has_captcha(page):
            screenshot = await _screenshot(page, job.job_id, settings.screenshots_dir, "captcha_li")
            return ApplyResult(
                job_id=job.job_id,
                status=JobStatus.WAITING_CAPTCHA,
                reason="CAPTCHA detected in LinkedIn Easy Apply",
                screenshot_path=screenshot,
            )

        # Upload resume only when the file input is visible in this step
        if sel.resume_upload:
            upload_input = await page.query_selector(sel.resume_upload)
            if upload_input and await upload_input.is_visible():
                await upload_input.set_input_files(resume_path)
                await asyncio.sleep(1)

        # Fill any visible form fields in the current step
        gate_status = await _fill_form(page, sel, job, settings)
        if gate_status:
            screenshot = await _screenshot(
                page, job.job_id, settings.screenshots_dir, "gate_li"
            )
            return ApplyResult(
                job_id=job.job_id,
                status=gate_status,
                reason=f"Verification step on Easy Apply step {step} requires a human",
                screenshot_path=screenshot,
            )

        # Find the action button for this step
        action_btn = None
        action_label = ""
        for label in _LI_LABELS:
            btn = await page.query_selector(f"button[aria-label='{label}']")
            if btn and await btn.is_visible():
                action_btn = btn
                action_label = label
                break

        if action_btn is None:
            logger.warning("Job %d — no action button on step %d; bailing", job.job_id, step)
            break

        if action_label == _LI_SUBMIT_LABEL and dry_run:
            return await _rehearsal_result(page, job, settings, "dry_run_li")

        logger.debug("Job %d — step %d: clicking '%s'", job.job_id, step, action_label)
        await action_btn.click()

        if action_label == _LI_SUBMIT_LABEL:
            await asyncio.sleep(2)
            screenshot = await _screenshot(page, job.job_id, settings.screenshots_dir, "success_li")
            # Clicking the button is not the same as LinkedIn accepting it.
            # This used to report APPLIED on the click alone.
            if not await _linkedin_confirmed(page):
                logger.warning("Job %d — LinkedIn did not confirm the submission", job.job_id)
                return ApplyResult(
                    job_id=job.job_id,
                    status=JobStatus.NEED_HUMAN_ACTION,
                    reason="Clicked Submit application, but LinkedIn showed no confirmation "
                           "— check your LinkedIn applications before retrying",
                    screenshot_path=screenshot,
                )
            logger.info("Job %d — APPLIED via LinkedIn Easy Apply", job.job_id)
            return ApplyResult(
                job_id=job.job_id,
                status=JobStatus.APPLIED,
                screenshot_path=screenshot,
            )

    screenshot = await _screenshot(page, job.job_id, settings.screenshots_dir, "li_timeout")
    return ApplyResult(
        job_id=job.job_id,
        status=JobStatus.FAILED,
        reason=f"LinkedIn Easy Apply: did not reach submit after {_LI_MAX_STEPS} steps",
        screenshot_path=screenshot,
    )


# LinkedIn's acknowledgement. Deliberately not a bare "applied": the sidebar lists
# other jobs with "Applied 3 days ago", which would confirm an application
# that never went through.
_LI_CONFIRMATION = re.compile(
    r"your application was sent|application sent|your application has been submitted|"
    r"\bapplied (?:now|just now|\d+\s*(?:s|sec|seconds?|m|min|minutes?) ago)\b",
    re.I,
)


async def _linkedin_confirmed(page: Page) -> bool:
    """LinkedIn's own acknowledgement that the application went through."""
    try:
        return bool(_LI_CONFIRMATION.search(await page.inner_text("body")))
    except Exception:
        return False


async def _rehearsal_result(page: Page, job: Job, settings, tag: str,
                            stopped: str = "") -> ApplyResult:
    """End a rehearsal: screenshot the page and say what's missing. Never submits.

    Lands the job in the Human Queue rather than marking it applied, with the
    screenshot to check and the list of required fields still blank — which
    is exactly what has to be fixed (usually in the question bank) before a
    real run. `stopped` replaces the summary when the rehearsal ended before
    any form was reached.
    """
    screenshot = await _screenshot(page, job.job_id, settings.screenshots_dir, tag)
    if stopped:
        reason = f"Rehearsal: {stopped}"
    else:
        blanks = await unanswered_required(page)
        reason = "Rehearsal: form filled, not submitted."
        reason += (f" Still blank: {'; '.join(blanks)[:350]}" if blanks
                   else " Every required field is filled.")
    logger.info("Job %d — %s", job.job_id, reason)
    return ApplyResult(
        job_id=job.job_id,
        status=JobStatus.NEED_HUMAN_ACTION,
        reason=reason,
        screenshot_path=screenshot,
    )


async def _fill_form(page: Page, sel, job: Job, settings) -> Optional[JobStatus]:
    """Fill the visible form: profile-driven fields first, then LLM for the rest.

    Two passes, in this order:

    1. `autofill_form` fills everything derivable from the job owner's profile —
       name, email, phone, LinkedIn, GitHub, location, current employer — by
       matching each input's `autocomplete` attribute, then its name/id/
       placeholder/label. It also reports any human-only verification gate.
    2. The LLM/QA-cache pass below answers the remaining free-text questions.

    Pass 1 exists because pass 2 deliberately skips every name/email/phone
    field (they are facts, not questions — asking an LLM for them invites
    hallucinated contact details). Before these were wired together nothing
    filled them at all, so most application forms failed validation on submit.

    Returns a JobStatus if a verification gate was hit and the caller must stop,
    else None.
    """
    # The owner's profile, not a shared file — these are the contact details
    # typed into a real application form.
    with Session(engine) as profile_session:
        profile = profiles_service.get_profile(profile_session, job.user_id)
    with Session(engine) as bank_session:
        owns = question_bank.owns_question(bank_session, job.user_id,
                                           job.company or "", job.title or "")
    if profiles_service.is_complete(profile):
        autofill = await autofill_form(page, profile, owns_question=owns)
        if autofill.needs_human:
            logger.info(
                "Job %d — %s gate during autofill; handing to human queue",
                job.job_id, autofill.gate.value,
            )
            return autofill.status
    else:
        logger.warning(
            "Job %d — owner's profile is incomplete; personal fields left blank",
            job.job_id,
        )

    router = get_llm_router()

    input_fields = []
    if sel.form_text_inputs:
        input_fields += await page.query_selector_all(sel.form_text_inputs)
    if sel.form_textarea_inputs:
        input_fields += await page.query_selector_all(sel.form_textarea_inputs)

    for field in input_fields:
        try:
            if not await field.is_visible():
                continue
            label = await _get_field_label(page, field)
            if not label:
                continue
            # Gender, date of birth, nationality as a free-text box are exactly
            # as off-limits as the dropdown versions. They used to reach the
            # model here because only the dropdown pass checked.
            if is_sensitive(label):
                continue
            # Facts the profile pass owns stay with it — unless the question
            # bank holds this exact question, which is then authoritative.
            if not owns(label) and match_field(FieldContext(label=label)) is not None:
                continue
            if any(kw in label.lower() for kw in ("resume", "cv", "upload")):
                continue
            # Already filled by pass 1 (or genuinely pre-populated by the portal)
            # — don't spend a model call overwriting a correct value.
            if (await field.input_value() or "").strip():
                continue

            answer = await question_bank.resolve_answer(job.user_id, label, job, profile, router)
            if not answer:
                continue

            maxlength = await field.get_attribute("maxlength")
            tag = await field.evaluate("e => e.tagName.toLowerCase()")
            limit = int(maxlength) if (maxlength or "").isdigit() else (
                2000 if tag == "textarea" else 500
            )
            await _human_delay(settings)
            await field.fill(question_bank.fit_length(answer, limit))
        except Exception as exc:
            logger.debug("Field fill error: %s", exc)

    if sel.form_select_inputs:
        await _fill_remaining_dropdowns(page, sel.form_select_inputs, job, profile, router, owns)
    await _fill_remaining_radios(page, job, profile, router, owns)


_SELECT_OPTIONS_JS = (
    "s => [...s.options].map(o => ({value: o.value, text: (o.textContent || '').trim()}))"
)

DROPDOWN_LLM_SYSTEM = """\
You are completing one dropdown on a job application for the candidate described.
Reply with the exact text of ONE option from the list, copied character for
character. If the candidate's profile does not clearly support one option, or
the question is about gender, ethnicity, disability, veteran status or any
other personal characteristic, reply with exactly: UNKNOWN
"""


async def _fill_remaining_dropdowns(page: Page, selector: str, job: Job, profile: dict,
                                    router, owns=None) -> None:
    """Dropdowns the profile pass did not answer.

    This used to select the first non-placeholder option of every dropdown on
    the page: "Willing to relocate?" answered by list order, the lowest salary
    bracket, the alphabetically first country, and — on equal-opportunity
    questions — a false self-declaration submitted to an employer.

    Now, in order:
      * already set to a real value (by the profile pass or the portal): left;
      * a self-identification question: "prefer not to say" if offered, else
        left — never answered;
      * a field the profile pass recognises but could not match: left, because
        the profile has a value that no option fits and a human should choose;
      * anything else: a model may pick, but only an exact option it names.
    A required dropdown left blank stops the submission and flags the job,
    which is the correct outcome when nobody knows the answer.
    """
    for dropdown in await page.query_selector_all(selector):
        try:
            if not await dropdown.is_visible():
                continue
            options = [Option(**o) for o in await dropdown.evaluate(_SELECT_OPTIONS_JS)]
            current = await dropdown.evaluate("s => s.value")
            if any(o.value == current and not is_placeholder(o) for o in options):
                continue
            if not real_options(options):
                continue

            label = await _get_field_label(page, dropdown)
            if is_sensitive(label):
                decline = decline_option(options)
                if decline:
                    await dropdown.select_option(value=decline.value)
                continue
            if owns is not None and owns(label):
                # The user's own written answer decides, or nothing does.
                written = question_bank.stored_answer(
                    job.user_id, label, job.company or "", job.title or "")
                choice = choose_from_answer(written, options) if written else None
                if choice:
                    await dropdown.select_option(value=choice.value)
                continue
            if match_field(FieldContext(label=label)) is not None:
                continue

            choice = await _model_choice(router, job, profile, label, options)
            if choice:
                await dropdown.select_option(value=choice.value)
        except Exception as exc:
            logger.debug("Dropdown skipped for job %d: %s", job.job_id, exc)


async def _fill_remaining_radios(page: Page, job: Job, profile: dict, router, owns) -> None:
    """Radio groups the profile pass left unanswered — the same rules as dropdowns.

    Portals put most yes/no questions ("Comfortable working from office?",
    "Do you have a passport?") in radio groups, and nothing answered those
    beyond the handful of profile basics.
    """
    try:
        groups = await page.evaluate(RADIO_GROUPS_JS)
    except Exception as exc:
        logger.debug("Radio discovery skipped for job %d: %s", job.job_id, exc)
        return

    for group in groups:
        if group["answered"]:
            continue
        question = group["question"] or ""
        options = [Option(value=o["value"], text=o["text"]) for o in group["options"]]
        tags = {(o["value"], o["text"]): o["tag"] for o in group["options"]}

        if is_sensitive(question):
            choice = decline_option(options)
        elif owns(question):
            written = question_bank.stored_answer(
                job.user_id, question, job.company or "", job.title or "")
            choice = choose_from_answer(written, options) if written else None
        elif match_field(FieldContext(name=group["name"], label=question)) is not None:
            continue
        else:
            choice = await _model_choice(router, job, profile, question, options)

        if choice:
            try:
                await page.check(f'[data-autoapply-radio="{tags[(choice.value, choice.text)]}"]')
            except Exception as exc:
                logger.debug("Radio %s skipped: %s", group["name"], exc)


async def _model_choice(router, job: Job, profile: dict, question: str,
                        options: list[Option]) -> Optional[Option]:
    """A model's pick among listed options, accepted only verbatim."""
    if not question:
        return None
    listed = "\n".join(f"- {o.text}" for o in real_options(options))
    prompt = (
        f"Company: {job.company or 'Unknown'}\n"
        f"Role: {job.title}\n"
        f"Candidate skills: {', '.join((profile.get('primary_skills') or [])[:15])}\n"
        f"Question: {question}\n"
        f"Options:\n{listed}"
    )
    try:
        reply = await router.complete(LLMTask.QA_ANSWER, prompt, system=DROPDOWN_LLM_SYSTEM)
    except Exception as exc:
        logger.debug("Dropdown model call failed: %s", exc)
        return None
    return resolve_llm_choice(reply, options)


async def _get_field_label(page: Page, field) -> str:
    """Resolve a human-readable label for a form field via label[for], aria-label, or placeholder."""
    try:
        field_id = await field.get_attribute("id")
        if field_id:
            label_el = await page.query_selector(f"label[for='{field_id}']")
            if label_el:
                return (await label_el.inner_text()).strip()
        aria = await field.get_attribute("aria-label") or ""
        if aria:
            return aria.strip()
        placeholder = await field.get_attribute("placeholder") or ""
        return placeholder.strip()
    except Exception as exc:
        logger.debug("Field-label resolution failed: %s", exc)
        return ""


async def _has_captcha(page: Page) -> bool:
    for selector in CAPTCHA_SELECTORS:
        try:
            el = await page.query_selector(selector)
            if el:
                return True
        except Exception:
            pass
    return False


async def _screenshot(
    page: Page, job_id: int, screenshots_dir: str, suffix: str
) -> Optional[str]:
    try:
        path = str(Path(screenshots_dir) / f"job_{job_id}_{suffix}.png")
        await page.screenshot(path=path, full_page=False)
        return path
    except Exception as exc:
        logger.debug("Screenshot failed: %s", exc)
        return None


async def _update_job(
    job: Job, result: ApplyResult, provided_session: Optional[Session]
) -> None:
    def _do_update(session: Session) -> None:
        db_job = session.get(Job, job.job_id)
        if db_job is None:
            return
        db_job.status = JobStatus(result.status)
        db_job.fail_reason = result.reason or None
        db_job.redirect_url = result.redirect_url or None
        db_job.screenshot_path = result.screenshot_path or None
        db_job.updated_at = datetime.now(timezone.utc)
        session.add(db_job)

        session.add(
            JobStageEvent(
                job_id=job.job_id,
                run_id=job.run_id,
                stage="apply",
                status=str(result.status),
                message=result.reason or None,
            )
        )

        if result.status == JobStatus.APPLIED:
            app = Application(
                job_id=job.job_id,
                confirmation_screenshot=result.screenshot_path,
                status=ApplicationStatus.SUBMITTED,
                submitted_at=datetime.now(timezone.utc),
            )
            session.add(app)
        session.commit()
        logger.info("Job %d → %s", job.job_id, result.status)

    if provided_session is not None:
        _do_update(provided_session)
    else:
        with closing(get_session()) as sessions:
            for session in sessions:
                _do_update(session)




async def _human_delay(settings) -> None:
    ms = random.randint(settings.human_delay_min_ms, settings.human_delay_max_ms)
    await asyncio.sleep(ms / 1000)
