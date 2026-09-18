from __future__ import annotations

import asyncio
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

from backend.config import get_settings
from backend.constants import EXTERNAL_ATS_DOMAINS
from backend.db.database import get_session
from backend.services.browser_pool import browser_semaphore
from backend.models.application import Application, ApplicationStatus
from backend.models.job import Job, JobStatus
from backend.models.job_stage_event import JobStageEvent
from backend.services.llm_router import LLMTask, get_llm_router
from backend.services.portal_templates import get_template
from backend.services.qa_cache import get_qa_cache
from workflows.types import ApplyResult

logger = logging.getLogger("agent.apply")

QA_LLM_SYSTEM = """\
You are filling out a job application form for Gaurav Sonkar.
Answer the question truthfully based on his profile.
Keep answers concise — 1-2 sentences max for text, single values for dropdowns.
Return ONLY the answer text, no explanation.
"""

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


async def apply_to_job(
    job: Job,
    resume_path: str,
    session: Optional[Session] = None,
) -> ApplyResult:
    """
    Navigate to the job URL and submit an application.
    Returns ApplyResult with status APPLIED / FAILED / FLAGGED.

    Web-discovered jobs (portal="web") skip the external-ATS URL check because
    they may legitimately point to Greenhouse/Lever pages owned by the company.
    """
    settings = get_settings()

    # Pre-flight: external ATS check (skipped for web-discovered jobs)
    if job.portal != "web" and _is_external_ats(job.url):
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
        result = await _browser_apply(job, resume_path, template, settings)
    except Exception as exc:
        logger.error("Apply error for job %d: %s", job.job_id, exc, exc_info=True)
        result = ApplyResult(
            job_id=job.job_id,
            status=JobStatus.FAILED,
            reason=str(exc)[:500],
        )

    await _update_job(job, result, session)
    return result


async def _browser_apply(job: Job, resume_path: str, template, settings) -> ApplyResult:
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
                result = await _run_apply_flow(page, job, resume_path, template, settings)
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
    page: Page, job: Job, resume_path: str, template, settings
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
        # Only check for external ATS redirect on portal jobs (not web-discovered)
        if href and job.portal != "web" and _is_external_ats(href):
            return ApplyResult(
                job_id=job.job_id,
                status=JobStatus.NEED_HUMAN_ACTION,
                reason=f"Apply button redirects to external ATS: {urlparse(href).netloc}",
                redirect_url=href,
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
        return await _linkedin_easy_apply(page, job, resume_path, sel, settings)

    # 6. Generic single-page flow (web / naukri / wellfound / cutshort / etc.)

    # Upload resume if visible
    if sel.resume_upload:
        upload_input = await page.query_selector(sel.resume_upload)
        if upload_input and await upload_input.is_visible():
            await upload_input.set_input_files(resume_path)
            await _human_delay(settings)
            logger.debug("Job %d — resume uploaded", job.job_id)

    # Fill visible form fields
    await _fill_form(page, sel, job, settings)

    # Submit
    submit_btn = await page.query_selector(sel.submit_button)
    if not submit_btn:
        return ApplyResult(
            job_id=job.job_id,
            status=JobStatus.FAILED,
            reason="Submit button not found",
        )
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
    page: Page, job: Job, resume_path: str, sel, settings
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
        await _fill_form(page, sel, job, settings)

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

        logger.debug("Job %d — step %d: clicking '%s'", job.job_id, step, action_label)
        await action_btn.click()

        if action_label == _LI_SUBMIT_LABEL:
            await asyncio.sleep(2)
            screenshot = await _screenshot(page, job.job_id, settings.screenshots_dir, "success_li")
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


async def _fill_form(page: Page, sel, job: Job, settings) -> None:
    """Fill visible text/textarea form fields using QA cache + LLM fallback."""
    cache = get_qa_cache()
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
            # Skip personal-info fields (pre-filled or handled by profile)
            if any(kw in label.lower() for kw in ("name", "email", "phone", "resume", "cv")):
                continue

            answer = await cache.get_answer(label)
            if not answer:
                prompt = (
                    f"Job portal: {job.portal}\n"
                    f"Company: {job.company or 'Unknown'}\n"
                    f"Role: {job.title}\n"
                    f"Question: {label}"
                )
                answer = await router.complete(LLMTask.QA_ANSWER, prompt, system=QA_LLM_SYSTEM)
                await cache.store_answer(label, answer, portal=job.portal)

            await _human_delay(settings)
            await field.fill(answer[:500])
        except Exception as exc:
            logger.debug("Field fill error: %s", exc)

    # Handle select dropdowns
    if sel.form_select_inputs:
        selects = await page.query_selector_all(sel.form_select_inputs)
        for sel_el in selects:
            try:
                if not await sel_el.is_visible():
                    continue
                options = await sel_el.query_selector_all("option")
                if len(options) > 1:
                    for opt in options[1:]:
                        val = await opt.get_attribute("value") or ""
                        if val:
                            await sel_el.select_option(value=val)
                            break
            except Exception as exc:
                logger.debug("Select-dropdown fill skipped: %s", exc)


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


def _is_external_ats(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(domain in host for domain in EXTERNAL_ATS_DOMAINS)


async def _human_delay(settings) -> None:
    ms = random.randint(settings.human_delay_min_ms, settings.human_delay_max_ms)
    await asyncio.sleep(ms / 1000)
