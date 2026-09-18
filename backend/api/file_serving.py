from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException


def resolve_contained_path(raw_path: str, base_dir: str, description: str) -> Path:
    """
    Resolve a DB-stored file path and verify it lives inside base_dir.

    The screenshot/resume paths are only ever written by the app's own agents
    today, but the serving endpoints must not rely on that staying true —
    without containment, any future bug that lets external data reach those
    DB columns would turn these endpoints into arbitrary-file readers.
    """
    resolved = Path(raw_path).resolve()
    base = Path(base_dir).resolve()
    if not resolved.is_relative_to(base):
        raise HTTPException(
            status_code=403,
            detail=f"{description} path is outside the allowed directory",
        )
    if not resolved.exists():
        raise HTTPException(
            status_code=404,
            detail=f"{description} file missing on disk",
        )
    return resolved
