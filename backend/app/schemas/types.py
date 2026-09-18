"""Shared field types for API schemas."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import PlainSerializer


def _to_utc_iso(value: datetime) -> str:
    """Serialise as UTC with an explicit offset.

    Everything written to the database is UTC, but SQLite has no timezone type
    and hands the value back naive. Pydantic then emitted "2026-09-08T07:52:07"
    with no suffix, and JavaScript reads an offset-less timestamp as *local*
    time — which in IST put every date in the app 5.5 hours in the past. An
    account created seconds earlier displayed as "6h ago".

    Naive values are therefore stamped UTC (which is what they already are);
    anything already aware is converted rather than assumed.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


# Use this instead of bare `datetime` on every API schema field. The naked type
# is what caused the bug, so the fix only holds if new fields use this one —
# tests/test_api_timestamps.py checks every route's response for a timezone and
# fails if one slips through.
UtcDatetime = Annotated[datetime, PlainSerializer(_to_utc_iso, return_type=str)]
