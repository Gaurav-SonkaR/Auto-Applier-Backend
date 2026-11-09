from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy import and_, or_

from sqlmodel import Session, select

from backend.app.config import get_settings
from backend.app.database import engine, init
from backend.app.models.job import Job, JobStatus
from backend.app.models.run_log import RunLog, RunType
from backend.app.services import resume_library
from backend.app.services.portal_templates import get_template
from backend.app.services.scraper import scrape_jobs, scrape_recommended_jobs
from backend.app.services import profiles
from backend.app.workflows.run_loop import finalize_run, run_job_graph_loop
from backend.app.workflows.state import EventCallback, RunConfig, RunResult

logger = logging.getLogger("flow.portal")

_DEFAULT_TITLE = "Software Engineer"

# How many of the resume's skills to append to the search keyword. Portal search
# boxes match loosely; more than a couple of terms narrows results to nothing.
_SEARCH_SKILL_COUNT = 2


def resolve_search_query(job_title: str, user_id=None) -> tuple[str, str]:
    """Return (query, source) for a portal search.

    A typed title always wins. Otherwise the query is derived from the uploaded
    master resume — the reason for uploading one — using its extracted title
    plus its top couple of skills. `master_profile.json` is the fallback when no
    resume has been uploaded yet, and a generic title the fallback after that,
    so a run never fails just because the search box was left empty.
    """
    typed = (job_title or "").strip()
    if typed:
        return typed, "typed"

    with Session(engine) as session:
        terms = resume_library.search_terms_from_master(session, user_id)
    if terms:
        title, skills = terms
        query = " ".join([title, *skills[:_SEARCH_SKILL_COUNT]]).strip()
        logger.info("Search query from master resume: '%s'", query)
        return query, "master_resume"

    with Session(engine) as session:
        profile = profiles.get_profile(session, user_id)
    experience = profile.get("experience") or []
    skills = [s for s in (profile.get("primary_skills") or []) if isinstance(s, str)]
    if experience or skills:
        title = (experience[0].get("title") if experience else None) or _DEFAULT_TITLE
        query = " ".join([title, *skills[:_SEARCH_SKILL_COUNT]]).strip()
        logger.info("Search query from profile: '%s'", query)
        return query, "master_profile"

    logger.warning(
        "No job title, master resume or profile — falling back to '%s'", _DEFAULT_TITLE
    )
    return _DEFAULT_TITLE, "default"


def _master_resume_path_for(portal: str, user_id=None) -> Optional[str]:
    """Path of the uploaded master resume, if the portal can use it directly
    (upload it to the portal's own profile, scrape its recommended jobs) —
    None if there's no master resume or the portal doesn't support that."""
    if not get_template(portal).supports_resume_driven_search():
        return None
    with Session(engine) as session:
        master = resume_library.get_master(session, user_id)
    return master.file_path if master else None


async def run_portal_apply(
    config: RunConfig,
    on_event: Optional[EventCallback] = None,
    existing_run_id: Optional[int] = None,
) -> RunResult:
    """
    Orchestrate a full portal apply run:
    1. Scrape fresh job URLs
    2. Optionally resume checkpointed jobs (PARSED/RESUME_READY)
    3. Run each job through the shared per-job pipeline (backend.app.workflows.run_loop)
    4. Return run summary
    """
    init()
    t0 = time.monotonic()
    result = RunResult(run_id=None)
    requested_batch_size = config.batch_size
    batch_size = min(requested_batch_size, get_settings().apply_batch_size)
    if batch_size < requested_batch_size:
        logger.warning(
            "Run batch capped from %d to configured apply_batch_size=%d",
            requested_batch_size, batch_size,
        )

    # A blank job_title means "search from my master resume" — resolve it before
    # anything else so the RunLog snapshot records the query actually used.
    query, query_source = resolve_search_query(config.job_title, config.user_id)
    snapshot = {
        "portal": config.portal, "job_title": query,
        "location": config.location, "batch_size": batch_size,
        "query_source": query_source,
    }

    # Use pre-created RunLog ID (from API) or create a new one
    run_log_id: Optional[int] = existing_run_id
    if run_log_id is None:
        with Session(engine) as session:
            run_log = RunLog(
                user_id=config.user_id,
                run_type=RunType.PORTAL_APPLY,
                started_at=datetime.now(timezone.utc),
                config_snapshot=json.dumps(snapshot),
            )
            session.add(run_log)
            session.commit()
            session.refresh(run_log)
            run_log_id = run_log.run_id
    else:
        # The API pre-created the RunLog before the query was resolved — patch
        # the snapshot so run history shows the real search terms, not a blank.
        with Session(engine) as session:
            run_log = session.get(RunLog, run_log_id)
            if run_log:
                run_log.config_snapshot = json.dumps(snapshot)
                session.add(run_log)
                session.commit()
    result.run_id = run_log_id
    logger.info(
        "Run %d started — portal=%s query='%s' (from %s)",
        run_log_id, config.portal, query, query_source,
    )

    # 1. Scrape fresh jobs.
    #
    # A blank title backed by a master resume tries resume-driven discovery
    # first — upload the resume to the portal's own profile, scrape whatever
    # it recommends from it (see scraper.scrape_recommended_jobs). That's a
    # portal automation path with unverified selectors (see the portal
    # template docstrings), so any failure — or a portal with no such
    # feature, e.g. LinkedIn — falls straight through to the keyword search
    # below using the same resume-derived query. A run is never left with
    # nothing just because resume-driven scraping didn't pan out.
    new_jobs: list[Job] = []
    if query_source == "master_resume":
        resume_path = _master_resume_path_for(config.portal, config.user_id)
        if resume_path:
            try:
                new_jobs = await scrape_recommended_jobs(
                    config.portal, resume_path, config.location, batch_size,
                    user_id=config.user_id,
                )
                if new_jobs:
                    logger.info(
                        "Run %d: %d new job(s) from resume-driven matching on %s",
                        run_log_id, len(new_jobs), config.portal,
                    )
            except Exception as exc:
                logger.warning(
                    "Resume-driven scrape failed for %s: %s — falling back to keyword search",
                    config.portal, exc,
                )

    if not new_jobs:
        try:
            new_jobs = await scrape_jobs(
                config.portal, query, config.location, batch_size,
                user_id=config.user_id,
            )
        except Exception as exc:
            logger.error("Scraping failed: %s", exc)
            result.errors.append(f"scrape: {exc}")
            new_jobs = []

    result.jobs_scraped = len(new_jobs)
    logger.info("Run %d: scraped %d new jobs", run_log_id, len(new_jobs))

    # 2. Collect checkpointed jobs (PARSED / RESUME_READY = can skip earlier nodes)
    all_job_ids: list[int] = [j.job_id for j in new_jobs if j.job_id][:batch_size]
    if config.include_checkpointed:
        with Session(engine) as session:
            checkpoint_jobs = session.exec(
                select(Job).where(
                    Job.user_id == config.user_id,
                    Job.portal == config.portal,
                    or_(
                        Job.status.in_([JobStatus.PARSED, JobStatus.RESUME_READY]),
                        and_(Job.status == JobStatus.SCRAPED, Job.retry_count > 0),
                    ),
                )
            ).all()
            for j in checkpoint_jobs:
                if len(all_job_ids) >= batch_size:
                    break
                if j.job_id not in all_job_ids:
                    all_job_ids.append(j.job_id)
            if checkpoint_jobs:
                logger.info("Run %d: +%d checkpointed jobs to process", run_log_id, len(checkpoint_jobs))

    # 3. Process each job through the shared pipeline
    await run_job_graph_loop(all_job_ids, run_log_id, on_event, result,
                             dry_run=config.dry_run)

    # 4. Finalize RunLog
    finalize_run(run_log_id, result, t0, log_prefix="Run")
    return result
