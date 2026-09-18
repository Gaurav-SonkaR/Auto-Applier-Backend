from __future__ import annotations

import logging
from datetime import date

import httpx

from backend.app.config import get_settings

logger = logging.getLogger("agent.discovery")

SERPER_URL = "https://google.serper.dev/search"

# Job portal aggregator domains to EXCLUDE from results.
# We want company/startup career pages only — not portal listings.
_JOB_PORTAL_DOMAINS: frozenset[str] = frozenset([
    "linkedin.com",
    "naukri.com",
    "indeed.com",
    "wellfound.com",
    "cutshort.io",
    "unstop.com",
    "internshala.com",
    "angel.co",
    "glassdoor.com",
    "monster.com",
    "shine.com",
    "timesjobs.com",
    "foundit.in",
    "ziprecruiter.com",
    "simplyhired.com",
])

# Path segments / subdomains that signal a job listing on a company page
_JOB_PATH_KEYWORDS: frozenset[str] = frozenset([
    "/job", "/career", "/vacancy", "/opening", "/position",
    "/apply", "/join-us", "jobs.", "careers.", "recruiting", "talent.",
])


class DailySearchLimitReached(Exception):
    pass


class DiscoveryAgent:
    """Searches for job URLs on company career pages via Serper.dev Google Search API."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._count_today: int = 0
        self._count_date: date = date.today()

    async def search_jobs(self, query: str, num: int = 10) -> list[str]:
        """Return a list of company career page job URLs for the given query."""
        self._maybe_reset()
        if self._count_today >= self._settings.daily_search_limit:
            raise DailySearchLimitReached(
                f"Daily search cap ({self._settings.daily_search_limit}) reached."
            )
        if not self._settings.serper_api_key:
            raise RuntimeError("SERPER_API_KEY not set — job discovery is unavailable.")

        results = await self._call_serper(query, num)
        urls = _filter_job_urls(results)
        self._count_today += 1
        logger.info("Search '%s' → %d URLs (today=%d)", query, len(urls), self._count_today)
        return urls

    async def _call_serper(self, query: str, num: int) -> list[dict]:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                SERPER_URL,
                headers={
                    "X-API-KEY": self._settings.serper_api_key,
                    "Content-Type": "application/json",
                },
                json={"q": query, "num": min(num, 20)},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("organic", [])

    def _maybe_reset(self) -> None:
        today = date.today()
        if today != self._count_date:
            self._count_today = 0
            self._count_date = today

    def remaining_today(self) -> int:
        self._maybe_reset()
        return max(0, self._settings.daily_search_limit - self._count_today)


def _filter_job_urls(organic: list[dict]) -> list[str]:
    """
    Keep only company career page URLs.

    Rules:
    - EXCLUDE any URL whose domain matches a known job portal aggregator.
    - KEEP URLs that contain job-listing path patterns (/job, /career, jobs., etc.).
    """
    urls = []
    for item in organic:
        link = item.get("link", "")
        if not link:
            continue
        # Drop portal aggregator URLs
        if any(domain in link for domain in _JOB_PORTAL_DOMAINS):
            continue
        # Keep company career / job listing URLs
        if any(kw in link for kw in _JOB_PATH_KEYWORDS):
            urls.append(link)
    return urls


# Module-level singleton
_agent: DiscoveryAgent | None = None


def get_discovery_agent() -> DiscoveryAgent:
    global _agent
    if _agent is None:
        _agent = DiscoveryAgent()
    return _agent
