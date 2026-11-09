from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional, TypedDict


# ── Per-job LangGraph state ────────────────────────────────────────────────────

class PipelineState(TypedDict):
    """
    State threaded through every node in the per-job LangGraph.

    All values are plain Python primitives or dicts so LangGraph can
    checkpoint/serialize the state without custom serializers.
    """
    job_id: int
    user_id: Optional[int]
    url: str
    portal: str
    title: str

    # Populated by fetch_jd_node
    jd_text: str

    # Populated by parse_node (serialized ParsedJD.raw dict)
    parsed_jd: dict

    # Populated by resume_node
    resume_path: str

    # SCRAPED / PARSED / RESUME_READY / APPLIED / FAILED / FLAGGED
    status: str

    # Non-empty on FAILED or FLAGGED
    error: str

    # Rehearsal: fill the application but never submit it.
    dry_run: bool


# ── Run-level config / result ─────────────────────────────────────────────────

@dataclass
class RunConfig:
    """Configuration for a portal apply run."""
    portal: str                        # linkedin / naukri / indeed / wellfound / cutshort
    job_title: str
    location: str
    batch_size: int = 10
    include_checkpointed: bool = True  # also retry PARSED/RESUME_READY jobs from previous runs
    user_id: Optional[int] = None   # owner of every row this run creates
    dry_run: bool = False           # fill every application, submit none


@dataclass
class DiscoveryConfig:
    """Configuration for a company-career discovery run (portal='web').

    Mirrors RunConfig's shape so both run types are started the same way —
    a new shared run parameter gets added to a dataclass, not threaded
    through loose kwargs on one call site but not the other.
    """
    job_title: str = ""   # blank → smart search derived from master_profile.json
    location: str = ""
    batch_size: int = 10
    user_id: Optional[int] = None   # owner of every row this run creates


@dataclass
class RunResult:
    """Summary returned by run_portal_apply()."""
    run_id: Optional[int]
    jobs_scraped: int = 0
    jobs_applied: int = 0
    jobs_ready: int = 0        # READY_TO_APPLY — Smart Job Apply, user submits
    jobs_failed: int = 0
    jobs_flagged: int = 0
    jobs_skipped: int = 0
    elapsed_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


# ── Cold email config / result ────────────────────────────────────────────────

# One definition, because the preview endpoint has to promise exactly what a
# run will send. Two literals in two files is how a preview starts lying.
DEFAULT_SUBJECT_TEMPLATE = "Experienced {role} | Open to Opportunities at {company}"


@dataclass
class ColdEmailConfig:
    """Configuration for a cold email campaign run."""
    excel_path: str                # Absolute path to Excel/CSV contact list
    role: str = ""                 # Target role override (e.g. "Backend Developer")
    batch_size: int = 10           # Max emails per run
    subject_template: str = DEFAULT_SUBJECT_TEMPLATE
    resume_per_tech_stack: bool = True  # Generate one tailored resume per tech_stack group
    user_id: Optional[int] = None   # owner of every row this run creates


@dataclass
class ColdEmailResult:
    """Summary returned by run_cold_email()."""
    total_rows: int = 0
    emails_sent: int = 0
    emails_failed: int = 0
    emails_skipped: int = 0        # already sent (DB dedup) or limit reached
    elapsed_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


# ── Event types emitted during runs ──────────────────────────────────────────

@dataclass
class FlowEvent:
    """
    Emitted after each node completes. Consumed by WebSocket handler (Phase 8).
    """
    run_id: Optional[int]
    job_id: Optional[int]
    node: str          # fetch_jd / parse / resume / apply / email
    status: str        # success / failed / flagged / skipped
    message: str = ""
    data: dict = field(default_factory=dict)


# Type alias for the async event callback
EventCallback = Callable[[FlowEvent], Coroutine[Any, Any, None]]

import logging
_event_logger = logging.getLogger("flow.state")


async def emit_event(callback: Optional[EventCallback], event: FlowEvent) -> None:
    """Safely dispatch an event callback, suppressing consumer errors."""
    if callback is not None:
        try:
            await callback(event)
        except Exception as exc:
            _event_logger.debug("Event callback error: %s", exc)

