"""Generated-resume filenames (SRS FR-004).

Required shape:  ``{Candidate}_{YYYY-MM-DD}_{job-title}_{company}_{hash}.pdf``
e.g.             ``Asha_Verma_2026-09-04_Senior_Python_Engineer_TechCorp_a7f3d2.pdf``

The acceptance criteria this satisfies:
  * special characters stripped from title and company
  * collisions prevented by a hash suffix
  * the resulting path is safe to hand to a download endpoint

Kept free of any DB or settings import so it stays trivially unit-testable.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date, datetime, timezone

# Anything that is not alphanumeric becomes a single underscore. Applied after
# NFKD-folding so "Café" → "Cafe" rather than "Caf_".
_NON_ALNUM = re.compile(r"[^A-Za-z0-9]+")

# Per-component caps. A long JD title plus a long company name could otherwise
# push the path past the 255-byte limit most filesystems enforce.
_MAX_TITLE_LEN = 60
_MAX_COMPANY_LEN = 40
_HASH_LEN = 6

# Used only when a profile has no name yet. It must not be a real person's:
# as one, it silently put that name on every other account's resume file.
DEFAULT_CANDIDATE = "candidate"


def sanitize(value: str, max_len: int, fallback: str = "unknown") -> str:
    """Fold to ASCII, collapse every run of non-alphanumerics to one underscore, trim."""
    if not value or not value.strip():
        return fallback
    ascii_only = (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    cleaned = _NON_ALNUM.sub("_", ascii_only).strip("_")
    if not cleaned:
        return fallback
    # Trim on an underscore boundary so the name never ends mid-word.
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rsplit("_", 1)[0] or cleaned[:max_len]
    return cleaned.strip("_")


def _collision_hash(job_id: int, url: str) -> str:
    """Short stable digest. Keyed on job_id + url so two different postings that
    share a title and company still get different filenames, while regenerating
    the same job's resume deterministically overwrites its own file."""
    return hashlib.sha256(f"{job_id}:{url}".encode()).hexdigest()[:_HASH_LEN]


def resume_filename(
    job_id: int,
    url: str,
    title: str,
    company: str | None = None,
    candidate: str = DEFAULT_CANDIDATE,
    on: date | None = None,
) -> str:
    """Build the FR-004 filename for a generated resume PDF."""
    stamp = (on or datetime.now(timezone.utc).date()).isoformat()
    return "_".join([
        sanitize(candidate, 40, fallback="candidate"),
        stamp,
        sanitize(title, _MAX_TITLE_LEN, fallback="role"),
        sanitize(company or "", _MAX_COMPANY_LEN, fallback="company"),
        _collision_hash(job_id, url),
    ]) + ".pdf"
