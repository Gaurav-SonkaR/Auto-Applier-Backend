from __future__ import annotations

from urllib.parse import urlparse

# Third-party ATS platforms. A job hosted here can't be auto-applied to
# reliably (each has its own multi-step form and bot defences), so the
# applier hands these to the human queue instead of guessing.
EXTERNAL_ATS_DOMAINS: frozenset[str] = frozenset([
    "greenhouse.io",
    "lever.co",
    "workday.com",
    "myworkdayjobs.com",
    "taleo.net",
    "icims.com",
    "smartrecruiters.com",
    "ashbyhq.com",
])


def is_external_ats(url: str) -> bool:
    """True if `url` points at a third-party ATS rather than the portal itself."""
    host = urlparse(url).netloc.lower()
    return any(domain in host for domain in EXTERNAL_ATS_DOMAINS)
