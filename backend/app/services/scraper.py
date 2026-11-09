from __future__ import annotations

import logging
from contextlib import closing
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from backend.app.config import get_settings
from backend.app.database import get_session
from backend.app.models.job import Job, JobStatus
from backend.app.services.browser_pool import random_human_delay as _human_delay
from backend.app.services.portal_templates import get_template, PortalTemplate
from backend.app.services.selenium_browser import SeleniumPage, selenium_session

logger = logging.getLogger("agent.scraper")


async def scrape_jobs(
    portal: str,
    job_title: str,
    location: str,
    batch_size: Optional[int] = None,
    user_id: Optional[int] = None,
) -> list[Job]:
    """
    Scrape job listings from the given portal.
    Returns newly inserted Job records (deduplicates against existing DB rows).
    """
    settings = get_settings()
    template = get_template(portal)
    size = batch_size or settings.scrape_batch_size
    search_url = template.build_search_url(job_title, location)

    logger.info("Scraping %s — query='%s' location='%s' batch=%d", portal, job_title, location, size)

    async with selenium_session(settings, locale="en-IN") as session:
        page = session.page
        await page.navigate(search_url, timeout=30_000)
        await page.handle_cookie_consent()
        await _human_delay(settings)

        if template.requires_login() and not await _is_logged_in(page, template):
            await _login(page, template, settings, portal)

        postings = await _collect_job_urls(page, template, size, settings)
        logger.info("Found %d job links on %s", len(postings), portal)

    # Fall back to the search query as the title only for cards whose own
    # title element didn't resolve (dead selector, layout change) — every job
    # used to get the literal search query as its title regardless of what the
    # posting actually was, because `job_title_in_card` was defined on every
    # portal template but never once read.
    new_jobs = await _upsert_jobs(postings, portal, fallback_title=job_title, user_id=user_id)
    return new_jobs


async def scrape_recommended_jobs(
    portal: str,
    resume_path: str,
    location: str,
    batch_size: Optional[int] = None,
    user_id: Optional[int] = None,
) -> list[Job]:
    """
    Resume-driven discovery: log in, upload `resume_path` to the portal's own
    profile, then scrape whatever job listing the portal curates from it —
    Naukri's Recommended Jobs, Indeed's recommendation feed, Cutshort/
    Instahyre's AI matching, Wellfound's curated roles.

    Returns an empty list — never raises — on any failure (portal doesn't
    support this, login fails, upload fails, selectors have drifted). The
    caller (workflows.portal_apply_flow.run_portal_apply) always has a
    keyword-search fallback for exactly this case, so a wrong selector here
    costs a slightly worse search, never zero results.
    """
    settings = get_settings()
    template = get_template(portal)
    if not template.supports_resume_driven_search():
        logger.info("%s has no resume-driven matching configured — skipping", portal)
        return []

    size = batch_size or settings.scrape_batch_size
    logger.info("Resume-driven scrape on %s — batch=%d", portal, size)

    async with selenium_session(settings, locale="en-IN") as session:
        page = session.page
        await page.navigate(template.base_url, timeout=30_000)
        await page.handle_cookie_consent()
        await _human_delay(settings)

        if template.requires_login() and not await _is_logged_in(page, template):
            await _login(page, template, settings, portal)
            if not await _is_logged_in(page, template):
                logger.warning(
                    "%s: login required for resume-driven search but failed — skipping",
                    portal,
                )
                return []

        if not await _upload_resume_to_profile(page, template, resume_path, settings):
            logger.warning(
                "%s: could not upload resume to profile — skipping resume-driven search",
                portal,
            )
            return []

        postings = await _scrape_recommended_page(page, template, size, settings)
        logger.info("%s: %d recommended job link(s) found", portal, len(postings))

    if not postings:
        return []
    return await _upsert_jobs(
        postings, portal, fallback_title=f"Recommended ({portal})", user_id=user_id
    )


async def _upload_resume_to_profile(
    page: SeleniumPage, template: PortalTemplate, resume_path: str, settings
) -> bool:
    """Navigate to the portal's resume/profile page and attach `resume_path`."""
    sel = template.selectors
    if not sel.profile_resume_upload:
        return False
    try:
        profile_url = template.resolve_url(sel.profile_resume_page) or template.base_url
        await page.navigate(profile_url, timeout=20_000)
        await page.handle_cookie_consent()
        await _human_delay(settings)

        upload_input = await page.find(sel.profile_resume_upload)
        if not upload_input:
            logger.warning("%s: resume upload field not found on profile page", template.name)
            return False
        await upload_input.set_file(resume_path)
        await _human_delay(settings)

        if sel.profile_resume_save_button:
            save_btn = await page.find(sel.profile_resume_save_button)
            if save_btn:
                await save_btn.click()
                await page.wait_ready(timeout=15_000)

        logger.info("Uploaded resume to %s profile", template.name)
        return True
    except Exception as exc:
        logger.warning("Resume upload to %s profile failed: %s", template.name, exc)
        return False


async def _scrape_recommended_page(
    page: SeleniumPage, template: PortalTemplate, max_count: int, settings
) -> list[str]:
    """Navigate to the portal-curated job listing and collect links from it —
    reuses `_collect_job_urls`, since a recommendations page is structurally
    just another card-based listing to the same job_card/job_link_in_card
    selectors already defined for search results."""
    sel = template.selectors
    url = template.resolve_url(sel.recommended_jobs_url)
    if not url:
        return []
    try:
        await page.navigate(url, timeout=20_000)
        await page.handle_cookie_consent()
        await _human_delay(settings)
        return await _collect_job_urls(page, template, max_count, settings)
    except Exception as exc:
        logger.warning("Could not load recommended-jobs page for %s: %s", template.name, exc)
        return []


async def _is_logged_in(page: SeleniumPage, template: PortalTemplate) -> bool:
    indicator = template.selectors.login_detection_element
    if not indicator:
        return True
    try:
        return await page.wait_for(indicator, timeout=3_000, visible=True) is not None
    except WebDriverException:
        return False


async def _login(page: SeleniumPage, template: PortalTemplate, settings, portal: str) -> None:
    sel = template.selectors
    # Generic per-portal credential resolution — mirrors applier._ensure_logged_in.
    # Email falls back to the shared Gmail address (many portals support Google
    # sign-in with the same address); password is strictly the portal's own.
    #
    # This used to be a hardcoded {"linkedin": ..., "naukri": ...} map, so
    # indeed/wellfound/cutshort/instahyre could never log in here even with
    # correct credentials in .env — scraping ran logged-out for those four
    # regardless of configuration. Fixed by resolving generically instead.
    email = getattr(settings, f"{portal}_email", "") or settings.gmail_address
    password = getattr(settings, f"{portal}_password", "")
    if not email or not password:
        logger.warning("No %s_PASSWORD configured — skipping login for %s", portal.upper(), portal)
        return

    try:
        await page.navigate(f"{template.base_url}/login", timeout=20_000)
        await page.handle_cookie_consent()
        email_input = await page.wait_for(sel.login_email_input, timeout=8_000, visible=True)
        password_input = await page.wait_for(sel.login_password_input, timeout=8_000, visible=True)
        submit = await page.wait_for(sel.login_submit_button, timeout=8_000, visible=True)
        if not email_input or not password_input or not submit:
            logger.warning("Login selectors did not resolve for %s", portal)
            return
        await email_input.fill(email)
        await password_input.fill(password)
        await submit.click()
        await page.wait_ready(timeout=15_000)
        logger.info("Logged in to %s", portal)
    except Exception as exc:
        logger.error("Login failed for %s: %s", portal, exc)


async def _collect_job_urls(
    page: SeleniumPage,
    template: PortalTemplate,
    max_count: int,
    settings,
) -> list[tuple[str, str]]:
    """Return (url, title) pairs from the current listing page.

    `job_title_in_card` was defined on every portal template from day one but
    never actually read anywhere — every scraped job's title was just the
    literal search query, regardless of what the posting really was. Now read
    per-card; an empty title here (selector didn't resolve) is a normal,
    expected case the caller falls back on, not an error.
    """
    sel = template.selectors
    postings: list[tuple[str, str]] = []
    seen_urls: set[str] = set()
    page_num = 0

    while len(postings) < max_count:
        cards = await page.find_all(sel.job_card)
        for card in cards:
            if len(postings) >= max_count:
                break
            link_el = await card.find(sel.job_link_in_card)
            if not link_el:
                continue
            href = await link_el.get_attribute("href") or ""
            if not href:
                continue
            full_url = _resolve_url(href, template.base_url)
            if full_url in seen_urls:
                continue

            title = ""
            if sel.job_title_in_card:
                title_el = await card.find(sel.job_title_in_card)
                if title_el:
                    title = (await title_el.text()).strip()

            seen_urls.add(full_url)
            postings.append((full_url, title))

        # Try next page
        if len(postings) < max_count and sel.next_page_button:
            next_btn = await page.find(sel.next_page_button)
            if next_btn and await next_btn.is_enabled():
                page_num += 1
                await _human_delay(settings)
                await next_btn.click()
                await page.wait_ready(timeout=15_000)
            else:
                break
        else:
            break

    return postings


async def _upsert_jobs(
    postings: list[tuple[str, str]], portal: str, fallback_title: str,
    user_id: Optional[int] = None,
) -> list[Job]:
    """Insert new jobs (skip duplicates). Returns only the newly inserted rows.

    `fallback_title` covers any card whose title element didn't resolve —
    the search query for a keyword scrape, or a "Recommended (portal)"
    placeholder for a resume-driven one.
    """
    new_jobs: list[Job] = []
    # closing() guarantees the generator (and its Session) is finalized even if
    # an exception escapes mid-loop — otherwise cleanup waits on GC timing.
    with closing(get_session()) as sessions:
        for session in sessions:
            await _insert_new_jobs(session, postings, portal, fallback_title, new_jobs, user_id)
    return new_jobs


async def _insert_new_jobs(
    session, postings: list[tuple[str, str]], portal: str, fallback_title: str,
    new_jobs: list[Job], user_id: Optional[int] = None,
) -> None:
    for url, title in postings:
        # Dedup is per owner now — another user holding this URL is irrelevant.
        existing = session.exec(
            select(Job).where(Job.url == url, Job.user_id == user_id)
        ).first()
        if existing:
            continue
        job = Job(
            user_id=user_id,
            url=url,
            title=title or fallback_title,
            portal=portal,
            status=JobStatus.SCRAPED,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add(job)
        try:
            session.commit()
            session.refresh(job)
            new_jobs.append(job)
            logger.debug("Inserted job %d: %s", job.job_id, url[:80])
        except IntegrityError:
            session.rollback()
            logger.debug("Duplicate skipped: %s", url[:80])


def _resolve_url(href: str, base_url: str) -> str:
    if href.startswith("http"):
        return href
    return base_url.rstrip("/") + "/" + href.lstrip("/")

