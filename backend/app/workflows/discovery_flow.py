from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from sqlmodel import Session, select

from backend.app.config import get_settings
from backend.app.database import engine
from backend.app.database import init
from backend.app.models.job import Job, JobStatus
from backend.app.models.run_log import RunLog, RunType
from backend.app.services.browser_pool import browser_semaphore
from backend.app.services.discovery import DailySearchLimitReached, get_discovery_agent
from backend.app.workflows.run_loop import finalize_run, run_job_graph_loop
from backend.app.workflows.state import DiscoveryConfig, EventCallback, RunResult

logger = logging.getLogger("flow.discovery")

# Google Search exclusions — prevents portal aggregator results
_PORTAL_EXCLUSIONS = (
    "-site:linkedin.com -site:naukri.com -site:indeed.com "
    "-site:wellfound.com -site:cutshort.io -site:glassdoor.com "
    "-site:monster.com -site:shine.com -site:foundit.in"
)

# ATS hosts where startups/scale-ups post individual openings — Serper results
# from these are direct job postings, the highest-quality discovery source.
_ATS_SITES_QUERY = (
    "site:boards.greenhouse.io OR site:jobs.lever.co "
    "OR site:jobs.ashbyhq.com OR site:apply.workable.com"
)

# URL shapes that identify an INDIVIDUAL job posting (vs. a career/listing page).
# Serper often returns career-index pages like ibm.com/careers/software-engineering —
# those must be crawled for actual posting links, never treated as postings themselves.
_POSTING_URL_RE = re.compile(
    r"(boards\.greenhouse\.io/[^/]+/jobs/\d+"
    r"|jobs\.lever\.co/[^/]+/[0-9a-fA-F-]{8,}"
    r"|jobs\.ashbyhq\.com/[^/]+/[0-9a-fA-F-]{8,}"
    r"|apply\.workable\.com/[^/]+/j/[A-Za-z0-9]+"
    r"|/jobs?/view/\d+"
    r"|/jobs?/[^/]*\d[^/]*/?$"      # /job/12345, /jobs/senior-engineer-4211
    r"|/positions?/[^/?#]+\d[^/?#]*$"
    r"|/openings?/[^/?#]+\d[^/?#]*$"
    r")",
    re.IGNORECASE,
)

# Hints that an anchor on a career page points at a job posting
_JOB_LINK_HINTS = ("/job/", "/jobs/", "/position", "/opening", "/vacancy", "/apply")

_MAX_LINKS_PER_LISTING = 5


def _looks_like_posting(url: str) -> bool:
    return bool(_POSTING_URL_RE.search(url))


def _registrable_domain(host: str) -> str:
    """'careers.ibm.com' and 'www.ibm.com' → 'ibm.com' — good enough for
    matching a company's own career subdomains without a public-suffix list."""
    parts = host.lower().split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


def _extract_posting_links(hrefs: list[str], listing_url: str, limit: int) -> list[str]:
    """Deterministically pick individual job-posting links out of a career page's
    anchors: known ATS posting shapes anywhere, or job-path links on the same
    company domain (subdomains included — career sites often live on careers.*)."""
    listing_domain = _registrable_domain(urlparse(listing_url).netloc)
    seen: set[str] = set()
    links: list[str] = []
    for href in hrefs:
        if not href or href.startswith(("mailto:", "javascript:", "#")):
            continue
        clean = href.split("#")[0]
        if clean in seen or clean.rstrip("/") == listing_url.rstrip("/"):
            continue
        seen.add(clean)
        domain = _registrable_domain(urlparse(clean).netloc)
        is_ats_posting = _looks_like_posting(clean)
        is_same_company_job_link = (
            domain == listing_domain and any(h in clean.lower() for h in _JOB_LINK_HINTS)
        )
        if is_ats_posting or is_same_company_job_link:
            links.append(clean)
            if len(links) >= limit:
                break
    return links


async def _expand_career_pages(urls: set[str], batch_size: int, settings) -> list[str]:
    """
    Split discovered URLs into individual postings (kept as-is) and career/listing
    pages (crawled once each — no clicks, anchors only — to extract real posting
    links). Deterministic, no LLM. Listing pages that yield no posting links are
    dropped rather than fed to the pipeline as fake 'jobs'.
    """
    postings = [u for u in urls if _looks_like_posting(u)]
    listings = [u for u in urls if u not in postings]

    if listings and len(postings) < batch_size:
        from playwright.async_api import async_playwright
        from playwright_stealth import Stealth

        async with browser_semaphore(), async_playwright() as pw:
            browser = await pw.chromium.launch(headless=settings.playwright_headless)
            try:
                ctx = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0.0.0 Safari/537.36"
                    )
                )
                page = await ctx.new_page()
                await Stealth().apply_stealth_async(page)
                for listing_url in listings:
                    if len(postings) >= batch_size:
                        break
                    try:
                        await page.goto(listing_url, wait_until="domcontentloaded", timeout=20_000)
                        # Career pages are usually JS SPAs — anchors render after
                        # domcontentloaded, so give the framework a beat to paint.
                        await page.wait_for_timeout(3_000)
                        hrefs = await page.eval_on_selector_all(
                            "a[href]", "els => els.map(e => e.href)"
                        )
                        found = _extract_posting_links(hrefs, listing_url, _MAX_LINKS_PER_LISTING)
                        for link in found:
                            if link not in postings:
                                postings.append(link)
                        logger.info(
                            "Career page %s → %d posting link(s)", listing_url[:70], len(found)
                        )
                    except Exception as exc:
                        logger.warning("Career-page crawl failed for %s: %s", listing_url[:80], exc)
            finally:
                await browser.close()

    logger.info(
        "Discovery expansion: %d direct posting(s), %d listing page(s) crawled → %d posting URL(s) total",
        sum(1 for u in urls if _looks_like_posting(u)), len(listings), len(postings),
    )
    return postings[:batch_size]


def _build_profile_queries(job_title: str, location: str) -> list[str]:
    """
    Build Google search queries that target company career pages only,
    from a manually-supplied job title. All queries append portal
    exclusions via -site: operators.
    """
    ex = _PORTAL_EXCLUSIONS
    location_tag = f"in {location}" if location else ""

    title_parts = job_title.strip().lower().split()
    b = title_parts[0] if title_parts else job_title
    d = " ".join(title_parts[1:]) if len(title_parts) > 1 else ""

    queries = [
        f"{job_title} careers {location_tag} {ex}",
        f"{job_title} job opening {location_tag} {ex}",
        f"{b} {d} developer careers {location_tag} {ex}",
        f"{job_title} {_ATS_SITES_QUERY}",
        f"{job_title} startup company job {location_tag} {ex}",
    ]
    return [q.strip() for q in queries if q.strip()]


_DEFAULT_SMART_TITLE = "Software Engineer"


def _load_profile_summary(user_id: int) -> tuple[str, list[str]]:
    """
    Return (most_recent_job_title, top_primary_skills) from this account's own
    profile. Falls back to a generic title and no skills when the profile is
    empty — smart search should degrade gracefully, never crash the run.
    """
    from backend.app.services import profiles

    with Session(engine) as session:
        data = profiles.get_profile(session, user_id)

    experience = data.get("experience") or []
    recent_title = (experience[0].get("title") if experience else None) or _DEFAULT_SMART_TITLE
    top_skills = [s for s in (data.get("primary_skills") or []) if isinstance(s, str)][:8]
    return recent_title, top_skills


def _build_smart_queries(location: str, user_id: int) -> list[str]:
    """
    Build Google search queries derived automatically from master_profile.json —
    no manual job title required. Uses the most recent role's title plus the
    top primary skills to target company career pages matching the user's
    actual experience, not a manually-typed keyword.
    """
    ex = _PORTAL_EXCLUSIONS
    location_tag = f"in {location}" if location else ""
    recent_title, top_skills = _load_profile_summary(user_id)
    skills = top_skills or [recent_title]

    def _pair(i: int, j: int) -> str:
        a = skills[i] if i < len(skills) else recent_title
        b = skills[j] if j < len(skills) else ""
        return f"{a} {b}".strip()

    queries = [
        f"{recent_title} careers {location_tag} {ex}",
        f"{_pair(0, 1)} developer jobs {location_tag} {ex}",
        f"{_pair(2, 3)} engineer job opening {location_tag} {ex}",
        f"{recent_title} {_ATS_SITES_QUERY}",
        f"{skills[0]} {recent_title} startup jobs {_ATS_SITES_QUERY}",
        f"{skills[0]} {recent_title} startup company job {location_tag} {ex}",
    ]
    return [q.strip() for q in queries if q.strip()]


async def run_discovery_pipeline(
    config: DiscoveryConfig,
    on_event: Optional[EventCallback] = None,
    existing_run_id: Optional[int] = None,
) -> RunResult:
    """
    Discover job URLs on company career pages via Google Search (Serper.dev),
    persist them as portal="web" jobs, then run each through the shared
    per-job pipeline (backend.app.workflows.run_loop).

    Unlike run_portal_apply(), this does NOT scrape portal job listings —
    it finds and applies directly to company career pages.

    config.job_title is optional: leave it blank for "smart search", which
    derives search queries from master_profile.json (most recent role + top
    skills) instead of requiring a manually-typed keyword.
    """
    init()
    t0 = time.monotonic()
    result = RunResult(run_id=None)
    settings = get_settings()

    job_title, location, batch_size = config.job_title, config.location, config.batch_size
    is_smart_search = not job_title.strip()
    display_title = job_title.strip() or _load_profile_summary(config.user_id)[0]

    # Create RunLog entry
    run_log_id: Optional[int] = existing_run_id
    if run_log_id is None:
        with Session(engine) as session:
            run_log = RunLog(
                user_id=config.user_id,
                run_type=RunType.PORTAL_APPLY,
                started_at=datetime.now(timezone.utc),
                config_snapshot=json.dumps({
                    "portal": "web",
                    "job_title": display_title,
                    "location": location,
                    "batch_size": batch_size,
                    "smart_search": is_smart_search,
                }),
            )
            session.add(run_log)
            session.commit()
            session.refresh(run_log)
            run_log_id = run_log.run_id
    else:
        # RunLog was pre-created by the API before job_title/smart-search were
        # resolved — patch the snapshot so run history shows a meaningful title.
        with Session(engine) as session:
            run_log = session.get(RunLog, run_log_id)
            if run_log:
                cfg = json.loads(run_log.config_snapshot) if run_log.config_snapshot else {}
                cfg["job_title"] = display_title
                cfg["smart_search"] = is_smart_search
                run_log.config_snapshot = json.dumps(cfg)
                session.add(run_log)
                session.commit()
    result.run_id = run_log_id

    if not settings.serper_api_key:
        logger.warning("SERPER_API_KEY not configured — discovery pipeline unavailable")
        finalize_run(run_log_id, result, t0, log_prefix="Discovery run")
        return result

    # 1. Discover job URLs
    agent = get_discovery_agent()
    queries = _build_smart_queries(location, config.user_id) if is_smart_search else _build_profile_queries(job_title, location)
    discovered_urls: set[str] = set()

    for query in queries:
        if len(discovered_urls) >= batch_size:
            break
        try:
            urls = await agent.search_jobs(query, num=min(10, batch_size))
            discovered_urls.update(urls)
            logger.info(
                "Query '%s' → %d URLs (total=%d)", query, len(urls), len(discovered_urls)
            )
        except DailySearchLimitReached:
            logger.warning("Daily Serper search limit reached")
            break
        except Exception as exc:
            logger.error("Discovery search error: %s", exc)
            result.errors.append(str(exc))

    # 2. Expand career/listing pages into individual job postings, then persist
    posting_urls = await _expand_career_pages(discovered_urls, batch_size, settings)

    new_job_ids: list[int] = []
    with Session(engine) as session:
        for url in posting_urls:
            existing = session.exec(
                select(Job).where(Job.url == url, Job.user_id == config.user_id)
            ).first()
            if existing:
                continue
            job = Job(
                user_id=config.user_id,
                url=url,
                portal="web",
                title=display_title,
                status=JobStatus.SCRAPED,
                run_id=run_log_id,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            new_job_ids.append(job.job_id)

    result.jobs_scraped = len(new_job_ids)
    logger.info("Run %d: saved %d new web jobs", run_log_id, len(new_job_ids))

    # 3. Run each job through the shared pipeline
    await run_job_graph_loop(
        new_job_ids, run_log_id, on_event, result,
        start_message="Processing web job: {url}",
    )

    finalize_run(run_log_id, result, t0, log_prefix="Discovery run")
    return result
