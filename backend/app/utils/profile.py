"""Single reader for `master_profile.json`.

Three call sites needed it independently — resume generation (must fail loudly
if it's missing), Smart Job Apply's query builder and form autofill (both must
degrade rather than sink a run). Rather than three `Path(...).read_text()`
copies with three different error behaviours, this module offers one loader in
each flavour and caches the parse.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional

logger = logging.getLogger("profile")


@lru_cache(maxsize=4)
def _read(path: str, mtime: float) -> dict:
    """Parse and cache. `mtime` is part of the key purely so that editing the
    file invalidates the cache without a server restart."""
    del mtime  # only present to key the cache
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_master_profile(path: str) -> dict:
    """Load the profile, raising with an actionable message if unusable.

    Use where the profile is essential (resume generation).
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"master_profile.json not found at '{path}'. "
            f"Set MASTER_PROFILE_PATH in .env to a readable JSON file."
        )
    try:
        return _read(str(p), p.stat().st_mtime)
    except json.JSONDecodeError as exc:
        raise ValueError(f"master_profile.json at '{path}' is not valid JSON: {exc}") from exc


def try_load_master_profile(path: str) -> Optional[dict]:
    """Load the profile, returning None if it is missing or malformed.

    Use where the profile is an enhancement, not a prerequisite — a bad file
    should degrade the feature, never abort the run.
    """
    try:
        return load_master_profile(path)
    except (FileNotFoundError, ValueError) as exc:
        logger.warning("master_profile unavailable (%s) — continuing without it", exc)
        return None


def personal_details(profile: dict) -> dict:
    """The `personal` block, or an empty dict."""
    return profile.get("personal") or {}
