from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from backend.db.database import engine
from backend.db.init_db import init
from backend.models.job import Job, JobStatus
from backend.models.run_log import RunLog, RunType
from workflows.agents.scraper_agent import scrape_jobs
from workflows.run_loop import finalize_run, run_job_graph_loop
from workflows.state import EventCallback, RunConfig, RunResult

logger = logging.getLogger("flow.portal")


async def run_portal_apply(
    config: RunConfig,
    on_event: Optional[EventCallback] = None,
    existing_run_id: Optional[int] = None,
) -> RunResult:
    """
    Orchestrate a full portal apply run:
    1. Scrape fresh job URLs
    2. Optionally resume checkpointed jobs (PARSED/RESUME_READY)
    3. Run each job through the shared per-job pipeline (workflows.run_loop)
    4. Return run summary
    """
    init()
    t0 = time.monotonic()
    result = RunResult(run_id=None)

    # Use pre-created RunLog ID (from API) or create a new one
    run_log_id: Optional[int] = existing_run_id
    if run_log_id is None:
        with Session(engine) as session:
            run_log = RunLog(
                run_type=RunType.PORTAL_APPLY,
                started_at=datetime.now(timezone.utc),
                config_snapshot=json.dumps(
                    {"portal": config.portal, "job_title": config.job_title,
                     "location": config.location, "batch_size": config.batch_size}
                ),
            )
            session.add(run_log)
            session.commit()
            session.refresh(run_log)
            run_log_id = run_log.run_id
    result.run_id = run_log_id
    logger.info("Run %d started — portal=%s query='%s'", run_log_id, config.portal, config.job_title)

    # 1. Scrape fresh jobs
    try:
        new_jobs = await scrape_jobs(
            config.portal, config.job_title, config.location, config.batch_size
        )
        result.jobs_scraped = len(new_jobs)
        logger.info("Run %d: scraped %d new jobs", run_log_id, len(new_jobs))
    except Exception as exc:
        logger.error("Scraping failed: %s", exc)
        result.errors.append(f"scrape: {exc}")
        new_jobs = []

    # 2. Collect checkpointed jobs (PARSED / RESUME_READY = can skip earlier nodes)
    all_job_ids: list[int] = [j.job_id for j in new_jobs if j.job_id]
    if config.include_checkpointed:
        with Session(engine) as session:
            checkpoint_jobs = session.exec(
                select(Job).where(
                    Job.portal == config.portal,
                    Job.status.in_([JobStatus.PARSED, JobStatus.RESUME_READY]),  # type: ignore[attr-defined]
                )
            ).all()
            for j in checkpoint_jobs:
                if j.job_id not in all_job_ids:
                    all_job_ids.append(j.job_id)
            if checkpoint_jobs:
                logger.info("Run %d: +%d checkpointed jobs to process", run_log_id, len(checkpoint_jobs))

    # 3. Process each job through the shared pipeline
    await run_job_graph_loop(all_job_ids, run_log_id, on_event, result)

    # 4. Finalize RunLog
    finalize_run(run_log_id, result, t0, log_prefix="Run")
    return result
