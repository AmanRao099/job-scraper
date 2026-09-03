"""Background refresh.

The frontend never waits on a scrape: this keeps the database warm so
`GET /jobs` is a millisecond-scale read against local storage.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.pipeline import ScrapeAlreadyRunning, run_maintenance, trigger_scrape

logger = logging.getLogger(__name__)

SCRAPE_JOB_ID = "periodic_scrape"
MAINTENANCE_JOB_ID = "periodic_maintenance"

scheduler = AsyncIOScheduler(timezone="UTC")


async def scheduled_scrape() -> None:
    try:
        run_id = await trigger_scrape(trigger="scheduled")
        logger.info("Scheduled scrape started as run %s", run_id)
    except ScrapeAlreadyRunning as exc:
        logger.info("Skipping scheduled scrape: %s", exc)
    except Exception:
        logger.exception("Scheduled scrape could not be started")


async def scheduled_maintenance() -> None:
    """Retire and delete dead postings on its own clock.

    Deliberately not folded into the scrape. A scrape can fail, be cancelled,
    be throttled to nothing or run a narrow profile, and every one of those
    skips its housekeeping - which is exactly when expired postings pile up. The
    guarantee wanted here is that what we serve is live, so the thing that
    enforces it cannot depend on the thing that keeps breaking.
    """
    try:
        summary = await run_maintenance()
        logger.info("Scheduled maintenance: %s", summary)
    except Exception:
        logger.exception("Scheduled maintenance failed")


def start_scheduler() -> bool:
    if not settings.scheduler_enabled:
        logger.info("Scheduler disabled by configuration")
        return False

    hours = max(settings.scrape_interval_hours, 0.25)
    scheduler.add_job(
        scheduled_scrape,
        trigger=IntervalTrigger(hours=hours),
        id=SCRAPE_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,          # a missed window fires once, not N times
        misfire_grace_time=1800,
    )
    if settings.maintenance_enabled:
        maintenance_hours = max(settings.maintenance_interval_hours, 0.25)
        scheduler.add_job(
            scheduled_maintenance,
            trigger=IntervalTrigger(hours=maintenance_hours),
            id=MAINTENANCE_JOB_ID,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=1800,
        )
        logger.info("Maintenance every %.2f hour(s)", maintenance_hours)

    scheduler.start()
    logger.info("Scheduler started - scraping every %.2f hour(s)", hours)
    return True


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")


def is_running() -> bool:
    return scheduler.running


def next_run_time(job_id: str = SCRAPE_JOB_ID) -> str | None:
    if not scheduler.running:
        return None
    job = scheduler.get_job(job_id)
    return job.next_run_time.isoformat() if job and job.next_run_time else None


def next_maintenance_time() -> str | None:
    return next_run_time(MAINTENANCE_JOB_ID)
