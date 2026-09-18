from __future__ import annotations

import asyncio
import logging
import random
from contextlib import closing
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from playwright_stealth import Stealth
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from backend.config import get_settings
from backend.db.database import get_session
from backend.models.job import Job, JobStatus
from backend.services.browser_pool import browser_semaphore
from backend.services.llm_router import EXTERNAL_ATS_DOMAINS
from backend.services.portal_templates import get_template, PortalTemplate

logger = logging.getLogger("agent.scraper")


async def scrape_jobs(
    portal: str,
    job_title: str,
    location: str,
    batch_size: Optional[int] = None,
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

    async with browser_semaphore(), async_playwright() as pw:
        browser = await pw.chromium.launch(headless=settings.playwright_headless)
        try:
            ctx = await _make_context(browser, settings.playwright_headless)
            page = await ctx.new_page()
            await Stealth().apply_stealth_async(page)

            await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
            await _human_delay(settings)

            if template.requires_login() and not await _is_logged_in(page, template):
                await _login(page, template, settings, portal)

            job_urls = await _collect_job_urls(page, template, size, settings)
            logger.info("Found %d job links on %s", len(job_urls), portal)

        finally:
            await browser.close()

    new_jobs = await _upsert_jobs(job_urls, portal, job_title)
    return new_jobs


async def _make_context(browser: Browser, headless: bool) -> BrowserContext:
    return await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
        locale="en-IN",
        java_script_enabled=True,
    )


async def _is_logged_in(page: Page, template: PortalTemplate) -> bool:
    indicator = template.selectors.login_detection_element
    if not indicator:
        return True
    try:
        await page.wait_for_selector(indicator, timeout=3_000)
        return True
    except Exception:
        return False


async def _login(page: Page, template: PortalTemplate, settings, portal: str) -> None:
    sel = template.selectors
    email = settings.gmail_address
    # Portal-specific password — fall back to gmail address as login
    password_map = {
        "linkedin": settings.linkedin_password,
        "naukri": settings.naukri_password,
    }
    password = password_map.get(portal, "")
    if not email or not password:
        logger.warning("No credentials configured for %s — skipping login", portal)
        return

    try:
        await page.goto(f"{template.base_url}/login", wait_until="domcontentloaded", timeout=20_000)
        await page.fill(sel.login_email_input, email)
        await page.fill(sel.login_password_input, password)
        await page.click(sel.login_submit_button)
        await page.wait_for_load_state("networkidle", timeout=15_000)
        logger.info("Logged in to %s", portal)
    except Exception as exc:
        logger.error("Login failed for %s: %s", portal, exc)


async def _collect_job_urls(
    page: Page,
    template: PortalTemplate,
    max_count: int,
    settings,
) -> list[str]:
    sel = template.selectors
    urls: list[str] = []
    page_num = 0

    while len(urls) < max_count:
        await page.wait_for_load_state("domcontentloaded")
        cards = await page.query_selector_all(sel.job_card)
        for card in cards:
            if len(urls) >= max_count:
                break
            link_el = await card.query_selector(sel.job_link_in_card)
            if link_el:
                href = await link_el.get_attribute("href") or ""
                if href:
                    full_url = _resolve_url(href, template.base_url)
                    if full_url not in urls:
                        urls.append(full_url)

        # Try next page
        if len(urls) < max_count and sel.next_page_button:
            next_btn = await page.query_selector(sel.next_page_button)
            if next_btn and await next_btn.is_enabled():
                page_num += 1
                await _human_delay(settings)
                await next_btn.click()
                await page.wait_for_load_state("domcontentloaded", timeout=15_000)
            else:
                break
        else:
            break

    return urls


async def _upsert_jobs(urls: list[str], portal: str, job_title: str) -> list[Job]:
    """Insert new jobs (skip duplicates). Returns only the newly inserted rows."""
    new_jobs: list[Job] = []
    # closing() guarantees the generator (and its Session) is finalized even if
    # an exception escapes mid-loop — otherwise cleanup waits on GC timing.
    with closing(get_session()) as sessions:
        for session in sessions:
            await _insert_new_jobs(session, urls, portal, job_title, new_jobs)
    return new_jobs


async def _insert_new_jobs(session, urls: list[str], portal: str, job_title: str, new_jobs: list[Job]) -> None:
    for url in urls:
        existing = session.exec(select(Job).where(Job.url == url)).first()
        if existing:
            continue
        job = Job(
            url=url,
            title=job_title,
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


def _is_external_ats(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(domain in host for domain in EXTERNAL_ATS_DOMAINS)


async def _human_delay(settings) -> None:
    ms = random.randint(settings.human_delay_min_ms, settings.human_delay_max_ms)
    await asyncio.sleep(ms / 1000)
