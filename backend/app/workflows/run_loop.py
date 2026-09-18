"""
Shared per-job pipeline: the LangGraph node chain (fetch_jd → parse → resume →
apply), its status classification, and the run-loop/finalize logic that both
run_portal_apply() (portal_apply_flow.py) and run_discovery_pipeline()
(discovery_flow.py) drive identically once they've each produced their own
list of job_ids to process.

Extracted after job_id fetch/discovery logic diverged (portal scraping vs.
Serper search) but the downstream per-job processing didn't — the two flows'
run loops had drifted out of sync once already (discovery_flow.py was left on
old FLAGGED-only status logic after this module's statuses were made
granular), which is exactly the class of bug a single shared implementation
prevents.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from langgraph.graph import END, StateGraph
from sqlmodel import Session

from backend.app.config import get_settings
from backend.app.database import engine
from backend.app.models.job import Job, JobStatus
from backend.app.models.job_stage_event import JobStageEvent
from backend.app.models.run_log import RunLog, RunStatus
from backend.app.services.browser_pool import browser_semaphore
from backend.app.services.portal_templates import get_template
from backend.app.services.applier import MANUAL_APPLY_PORTAL, apply_to_job
from backend.app.services.jd_parser import parse_jd
from backend.app.services.resume_generator import generate_resume
from backend.app.services import resume_library
from backend.app.workflows.state import EventCallback, FlowEvent, PipelineState, RunResult
from backend.app.workflows.types import ParsedJD

logger = logging.getLogger("flow.run_loop")

# Statuses that stop mid-graph progression — a job in any of these needs a human
# (or was excluded pre-pipeline) and must not silently advance to the next node.
HALTING_STATUSES = (
    JobStatus.FAILED, JobStatus.FLAGGED,
    JobStatus.WAITING_CAPTCHA, JobStatus.WAITING_LOGIN, JobStatus.WAITING_OTP,
    JobStatus.NEED_HUMAN_ACTION,
    JobStatus.DUPLICATE, JobStatus.SKIPPED, JobStatus.BLACKLISTED,
)
HALTING_STATUS_VALUES = tuple(s.value for s in HALTING_STATUSES)

# Coarse "needs human attention" bucket — all roll up into RunLog.jobs_flagged
NEED_HUMAN_STATUSES = (
    JobStatus.FLAGGED, JobStatus.WAITING_CAPTCHA, JobStatus.WAITING_LOGIN,
    JobStatus.WAITING_OTP, JobStatus.NEED_HUMAN_ACTION,
)
NEED_HUMAN_STATUS_VALUES = tuple(s.value for s in NEED_HUMAN_STATUSES)

# Pre-pipeline exclusion statuses — never re-entered, counted as skipped in a run
PRE_EXCLUDED_STATUSES = (JobStatus.DUPLICATE, JobStatus.SKIPPED, JobStatus.BLACKLISTED)
PRE_EXCLUDED_STATUS_VALUES = tuple(s.value for s in PRE_EXCLUDED_STATUSES)

# Jobs from this "portal" are company career pages / third-party ATS forms found
# by Smart Job Apply. The pipeline builds their tailored resume and stops —
# submission stays with the user. See _after_resume.

# Terminal states a job must never be re-processed out of.
TERMINAL_STATUSES = (JobStatus.APPLIED, JobStatus.READY_TO_APPLY)

# ── Node implementations ───────────────────────────────────────────────────────

async def _fetch_jd_node(state: PipelineState) -> dict:
    """Navigate to the job URL and extract the description text."""
    # Checkpoint: already fetched in a previous run — skip and preserve status
    if state.get("jd_text"):
        return {"jd_text": state["jd_text"], "status": state.get("status"), "error": ""}

    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    settings = get_settings()
    template = get_template(state["portal"])
    sel = template.selectors.job_description_container

    jd_text = ""
    try:
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
                await page.goto(state["url"], wait_until="domcontentloaded", timeout=25_000)

                if sel:
                    try:
                        el = await page.wait_for_selector(sel, timeout=8_000)
                        if el:
                            jd_text = (await el.inner_text()).strip()
                    except Exception:
                        # Fall back to body text
                        jd_text = await page.inner_text("body")
                else:
                    jd_text = await page.inner_text("body")
            finally:
                # Guarantee the browser process is closed even if navigation/selector
                # lookup above raises — a bare except-without-close here would leak
                # a Chromium process on every failed JD fetch.
                await browser.close()
    except Exception as exc:
        logger.warning("fetch_jd failed for job %d: %s", state["job_id"], exc)
        return {"jd_text": "", "status": JobStatus.FAILED, "error": f"JD fetch failed: {exc}"}

    # Persist jd_text to DB
    with Session(engine) as session:
        job = session.get(Job, state["job_id"])
        if job:
            job.jd_text = jd_text[:8000]
            job.updated_at = datetime.now(timezone.utc)
            session.add(job)
            session.commit()

    logger.info("Job %d: fetched %d chars of JD text", state["job_id"], len(jd_text))
    return {"jd_text": jd_text[:8000], "status": JobStatus.SCRAPED, "error": ""}


async def _parse_node(state: PipelineState) -> dict:
    """Call LLM to parse the job description."""
    # Checkpoint: already parsed (PARSED or RESUME_READY) — skip and preserve status
    if state.get("parsed_jd") and state.get("status") in (
        JobStatus.PARSED, JobStatus.RESUME_READY,
        JobStatus.PARSED.value, JobStatus.RESUME_READY.value,
    ):
        return {"parsed_jd": state["parsed_jd"], "status": state.get("status"), "error": ""}

    if not state.get("jd_text"):
        return {"status": JobStatus.FAILED, "error": "No JD text to parse"}
    try:
        parsed: ParsedJD = await parse_jd(state["jd_text"], state["job_id"])
        return {"parsed_jd": parsed.raw, "status": JobStatus.PARSED, "error": ""}
    except Exception as exc:
        logger.error("parse_jd failed for job %d: %s", state["job_id"], exc)
        return {"status": JobStatus.FAILED, "error": str(exc)[:300]}


async def _resume_node(state: PipelineState) -> dict:
    """Generate and compile the ATS-tailored PDF resume."""
    # Checkpoint: resume already generated (RESUME_READY) — skip
    if state.get("resume_path") and state.get("status") in (
        JobStatus.RESUME_READY, JobStatus.RESUME_READY.value
    ):
        return {"resume_path": state["resume_path"], "status": state.get("status"), "error": ""}

    raw = state.get("parsed_jd") or {}
    parsed = ParsedJD(
        required_skills=raw.get("required_skills", []),
        nice_to_have=raw.get("nice_to_have", []),
        keywords=raw.get("keywords", []),
        experience_years=raw.get("experience_years"),
        role_level=raw.get("role_level", "mid"),
        raw=raw,
    )
    try:
        pdf_path = await generate_resume(state["job_id"], parsed)
        return {"resume_path": pdf_path, "status": JobStatus.RESUME_READY, "error": ""}
    except Exception as exc:
        logger.error("generate_resume failed for job %d: %s", state["job_id"], exc)

        # Tailoring failed (no LaTeX engine, LLM outage, template drift). If the
        # user has uploaded resumes, apply with the best-matching one rather than
        # losing the posting — a good generic resume beats no application. Only
        # a job with nothing to submit is a genuine failure.
        with Session(engine) as session:
            fallback = _best_uploaded_resume(session, raw)
        if fallback:
            logger.warning(
                "Job %d: falling back to best uploaded resume after generation failure",
                state["job_id"],
            )
            return {
                "resume_path": fallback,
                "status": JobStatus.RESUME_READY,
                "error": f"Tailored resume unavailable ({str(exc)[:150]}); using uploaded resume",
            }
        return {"status": JobStatus.FAILED, "error": str(exc)[:300]}


async def _apply_node(state: PipelineState) -> dict:
    """Submit the application via Playwright.

    `resume_node` normally supplies the path — the tailored resume when
    generation succeeded, or the best-matching uploaded one when it didn't.
    The library lookup repeated here is a safety net for any future path that
    reaches this node without a resume; it must never submit an application
    with no attachment.
    """
    with Session(engine) as session:
        job = session.get(Job, state["job_id"])
        if not job:
            return {"status": JobStatus.FAILED, "error": f"Job {state['job_id']} not found in DB"}

        resume_path = state.get("resume_path", "")
        if not resume_path:
            resume_path = _best_uploaded_resume(session, state.get("parsed_jd") or {})
            if not resume_path:
                return {
                    "status": JobStatus.FAILED,
                    "error": (
                        "No resume to submit — generation failed and no resume "
                        "has been uploaded to the library"
                    ),
                }
            logger.info("Job %d: applying with best uploaded resume", state["job_id"])

        result = await apply_to_job(job, resume_path, session,
                                    dry_run=bool(state.get("dry_run")))

    return {
        "status": result.status,
        "error": result.reason or "",
    }


def _best_uploaded_resume(session: Session, parsed_jd: dict) -> str:
    """Path of the library resume best matching this JD, or "" if none exist."""
    terms = {
        str(t).lower()
        for key in ("required_skills", "nice_to_have", "keywords")
        for t in (parsed_jd.get(key) or [])
        if t
    }
    best = resume_library.best_for_jd(session, sorted(terms))
    return best.file_path if best else ""


# ── Routing ───────────────────────────────────────────────────────────────────

def _is_halting(status) -> bool:
    return status in HALTING_STATUSES or status in HALTING_STATUS_VALUES


def _after_fetch(state: PipelineState) -> str:
    return END if _is_halting(state["status"]) else "parse"


def _after_parse(state: PipelineState) -> str:
    return END if _is_halting(state["status"]) else "fit"


async def _fit_node(state: PipelineState) -> dict:
    """Skip postings the profile clearly doesn't fit, before any resume exists.

    `experience_years` and `required_skills` were extracted for every job and
    then ignored, so a role asking for eight years went on to resume
    generation and, on portals, to an application. Deciding here saves the
    tokens and the daily apply slot, and keeps the resume prompt from being
    handed a seniority target to write towards.
    """
    settings = get_settings()
    status = state.get("status")
    # Already past this point on an earlier run — the decision was made then.
    if not settings.fit_filter_enabled or status in (
        JobStatus.RESUME_READY, JobStatus.RESUME_READY.value
    ):
        return {"status": status, "error": ""}

    from backend.app.services import profiles
    from backend.app.services.job_fit import assess_fit

    with Session(engine) as session:
        job = session.get(Job, state["job_id"])
        profile = profiles.get_profile(session, job.user_id) if job else {}

    fit = assess_fit(
        profile,
        state.get("parsed_jd") or {},
        experience_tolerance_years=settings.fit_experience_tolerance_years,
        min_skill_overlap=settings.fit_min_skill_overlap,
    )
    if fit.fits:
        return {"status": status, "error": ""}

    reason = f"Not a fit: {fit.summary()}"
    logger.info("Job %d skipped — %s", state["job_id"], fit.summary())
    _write_status(state["job_id"], JobStatus.SKIPPED, stage="fit", message=reason)
    return {"status": JobStatus.SKIPPED, "error": reason}


def _after_fit(state: PipelineState) -> str:
    return END if _is_halting(state["status"]) else "resume"


def _after_resume(state: PipelineState) -> str:
    """Route out of the resume node.

    Smart Job Apply (portal="web") stops here by design: company career pages
    and third-party ATS forms are not auto-submitted. The job ends at
    READY_TO_APPLY and the user applies from the apply link with the generated
    resume. Job portals (linkedin/naukri/indeed/wellfound/cutshort/instahyre)
    continue to the apply node as before.
    """
    if _is_halting(state["status"]):
        return END
    if state["portal"] == MANUAL_APPLY_PORTAL:
        return "ready"
    return "apply"


async def _ready_to_apply_node(state: PipelineState) -> dict:
    """Terminal node for manual-apply jobs — records READY_TO_APPLY and stops."""
    _write_status(
        state["job_id"],
        JobStatus.READY_TO_APPLY,
        stage="ready_to_apply",
        message="Resume generated — apply manually using the job link",
    )
    return {"status": JobStatus.READY_TO_APPLY, "error": ""}


# ── Graph builder ─────────────────────────────────────────────────────────────

def _build_graph():
    g = StateGraph(PipelineState)
    g.add_node("fetch_jd", _fetch_jd_node)
    g.add_node("parse", _parse_node)
    g.add_node("fit", _fit_node)
    g.add_node("resume", _resume_node)
    g.add_node("apply", _apply_node)
    g.add_node("ready", _ready_to_apply_node)
    g.set_entry_point("fetch_jd")
    g.add_conditional_edges("fetch_jd", _after_fetch, {"parse": "parse", END: END})
    g.add_conditional_edges("parse", _after_parse, {"fit": "fit", END: END})
    g.add_conditional_edges("fit", _after_fit, {"resume": "resume", END: END})
    g.add_conditional_edges(
        "resume", _after_resume, {"apply": "apply", "ready": "ready", END: END}
    )
    g.add_edge("apply", END)
    g.add_edge("ready", END)
    return g.compile()


JOB_GRAPH = _build_graph()


# ── Checkpoint: determine which node to start from ────────────────────────────

def entry_state_for(job: Job) -> Optional[dict]:
    """
    Build the initial PipelineState for a job, respecting existing progress.
    Returns None if the job should be skipped entirely: a terminal state
    (APPLIED, or READY_TO_APPLY awaiting the user's own submission) or a
    pre-pipeline exclusion state (DUPLICATE/SKIPPED/BLACKLISTED).
    """
    if job.status in TERMINAL_STATUSES or job.status in PRE_EXCLUDED_STATUSES:
        return None
    base: dict = {
        "job_id": job.job_id,
        "url": job.url,
        "portal": job.portal,
        "title": job.title or "",
        "jd_text": job.jd_text or "",
        "parsed_jd": json.loads(job.parsed_jd) if job.parsed_jd else {},
        "resume_path": job.resume_path or "",
        "status": job.status.value,
        "error": "",
    }
    return base


async def emit_event(callback: Optional[EventCallback], event: FlowEvent) -> None:
    if callback is not None:
        try:
            await callback(event)
        except Exception as exc:
            logger.debug("Event callback error: %s", exc)


# ── Shared per-job loop ────────────────────────────────────────────────────────

async def run_job_graph_loop(
    job_ids: list[int],
    run_log_id: int,
    on_event: Optional[EventCallback],
    result: RunResult,
    start_message: str = "Starting pipeline for '{title}'",
    dry_run: bool = False,
) -> None:
    """
    Drive job_ids through JOB_GRAPH one at a time, classifying each job's final
    status into result.jobs_applied/jobs_flagged/jobs_skipped/jobs_failed and
    persisting FAILED + a JobStageEvent on any non-terminal-success outcome.
    Mutates `result` in place — shared by run_portal_apply() and
    run_discovery_pipeline() so their status handling can never drift apart again.
    """
    for job_id in job_ids:
        with Session(engine) as session:
            job = session.get(Job, job_id)
            if not job:
                continue

        initial_state = entry_state_for(job)
        if initial_state is None:
            result.jobs_skipped += 1
            continue
        initial_state["dry_run"] = dry_run or get_settings().apply_dry_run

        await emit_event(on_event, FlowEvent(
            run_id=run_log_id, job_id=job_id, node="start",
            status="running", message=start_message.format(title=job.title, url=job.url),
        ))

        try:
            final_state: PipelineState = await JOB_GRAPH.ainvoke(initial_state)
            final_status = final_state.get("status", "FAILED")

            if final_status in (JobStatus.APPLIED, JobStatus.APPLIED.value):
                result.jobs_applied += 1
            elif final_status in (JobStatus.READY_TO_APPLY, JobStatus.READY_TO_APPLY.value):
                # Smart Job Apply success: resume built, user applies manually.
                # Must be classified before the else-branch, which would
                # otherwise mark every web job FAILED and overwrite its status.
                result.jobs_ready += 1
            elif final_status in NEED_HUMAN_STATUSES or final_status in NEED_HUMAN_STATUS_VALUES:
                result.jobs_flagged += 1
            elif final_status in PRE_EXCLUDED_STATUSES or final_status in PRE_EXCLUDED_STATUS_VALUES:
                result.jobs_skipped += 1
            else:
                result.jobs_failed += 1
                _write_failed(job_id, run_log_id, final_state.get("error", "pipeline failed"))

            status_str = (
                final_status.value
                if isinstance(final_status, JobStatus)
                else str(final_status)
            ).lower()
            await emit_event(on_event, FlowEvent(
                run_id=run_log_id, job_id=job_id, node="done",
                status=status_str,
                message=final_state.get("error", ""),
            ))
        except Exception as exc:
            logger.error("Graph failed for job %d: %s", job_id, exc, exc_info=True)
            result.jobs_failed += 1
            result.errors.append(f"job {job_id}: {exc}")
            _write_failed(job_id, run_log_id, str(exc))


def _write_status(
    job_id: int,
    status: JobStatus,
    run_id: Optional[int] = None,
    stage: str = "pipeline",
    message: str = "",
) -> None:
    """Persist a terminal status plus its timeline event, in one transaction.

    `fail_reason` is only populated for genuine failures — a READY_TO_APPLY job
    is a success, and writing its explanatory message into fail_reason would
    surface it as an error in the UI.
    """
    with Session(engine) as session:
        db_job = session.get(Job, job_id)
        if not db_job:
            return
        db_job.status = status
        db_job.fail_reason = message[:300] if status == JobStatus.FAILED else None
        db_job.updated_at = datetime.now(timezone.utc)
        session.add(db_job)
        session.add(JobStageEvent(
            job_id=job_id, run_id=run_id or db_job.run_id, stage=stage,
            status=status.value, message=message[:300] or None,
        ))
        session.commit()


def _write_failed(job_id: int, run_id: int, reason: str) -> None:
    _write_status(job_id, JobStatus.FAILED, run_id=run_id, stage="pipeline", message=reason)


def finalize_run(run_log_id: int, result: RunResult, t0: float, log_prefix: str = "Run") -> None:
    """Mark the RunLog COMPLETED with final counters. Shared by both flows."""
    result.elapsed_seconds = round(time.monotonic() - t0, 2)
    error_summary = "; ".join(result.errors[:3]) if result.errors else None
    with Session(engine) as session:
        run_log = session.get(RunLog, run_log_id)
        if run_log:
            run_log.status = RunStatus.COMPLETED
            run_log.completed_at = datetime.now(timezone.utc)
            run_log.jobs_scraped = result.jobs_scraped
            run_log.jobs_applied = result.jobs_applied
            run_log.jobs_ready = result.jobs_ready
            run_log.jobs_failed = result.jobs_failed
            run_log.jobs_flagged = result.jobs_flagged
            run_log.jobs_skipped = result.jobs_skipped
            run_log.error_message = error_summary
            session.add(run_log)
            session.commit()
    logger.info(
        "%s %d done in %.1fs — scraped=%d applied=%d ready=%d failed=%d flagged=%d",
        log_prefix, run_log_id, result.elapsed_seconds,
        result.jobs_scraped, result.jobs_applied, result.jobs_ready,
        result.jobs_failed, result.jobs_flagged,
    )
