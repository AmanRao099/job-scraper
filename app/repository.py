"""Data access layer.

All SQL lives here so the API stays declarative and the storage engine stays
swappable. Nothing below uses SQLite-only syntax.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.enrich import NormalizedJob
from app.models import Job, ScrapeRun
from app.utils import utcnow

logger = logging.getLogger(__name__)

SORTABLE = {
    "posted_at": Job.posted_at,
    "first_seen_at": Job.first_seen_at,
    "last_seen_at": Job.last_seen_at,
    "title": Job.title,
    "company": Job.company,
    "experience": Job.experience_min,
}


@dataclass(slots=True)
class JobFilters:
    q: str | None = None
    source: list[str] | None = None
    category: list[str] | None = None
    skill: list[str] | None = None
    seniority: list[str] | None = None
    work_mode: list[str] | None = None
    location: str | None = None
    company: str | None = None
    min_experience: int | None = None
    max_experience: int | None = None
    # Whether postings with no stated experience satisfy an experience filter.
    # True keeps recall (plenty of genuine fresher ads never state a number);
    # False is the strict reading, for a listing that must not show a senior
    # role. Only consulted when an experience bound is actually set.
    include_unknown_experience: bool = True
    posted_within_days: int | None = None
    active_only: bool = True


@dataclass(slots=True)
class UpsertResult:
    created: int = 0
    updated: int = 0

    @property
    def total(self) -> int:
        return self.created + self.updated


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

async def upsert_jobs(session: AsyncSession, jobs: list[NormalizedJob]) -> UpsertResult:
    """Insert new postings, refresh ones we have already seen.

    Done as one bulk fingerprint lookup plus in-memory diffing so a 5,000 job
    batch costs two round-trips rather than 5,000.
    """
    result = UpsertResult()
    if not jobs:
        return result

    fingerprints = [job.fingerprint for job in jobs]
    existing_rows = (
        await session.execute(select(Job).where(Job.fingerprint.in_(fingerprints)))
    ).scalars().all()
    existing = {row.fingerprint: row for row in existing_rows}

    now = utcnow()
    new_models: list[Job] = []

    for job in jobs:
        row = existing.get(job.fingerprint)
        payload = job.as_row()

        if row is None:
            model = Job(**payload, first_seen_at=now, last_seen_at=now, is_active=True)
            model.search_blob = model.build_search_blob()
            new_models.append(model)
            result.created += 1
            continue

        # Refresh everything except the discovery timestamp; a re-listed job
        # should not look brand new to the frontend.
        for key, value in payload.items():
            # Never overwrite a good description with an empty one.
            if key == "description" and not value and row.description:
                continue
            if key == "posted_at" and value is None:
                continue
            setattr(row, key, value)
        row.last_seen_at = now
        row.is_active = True
        # Seeing a posting on the board again overrules whatever retired it.
        # Clearing the failure counter matters as much as clearing the reason:
        # otherwise a job that was unreachable twice during an outage carries
        # those two strikes forever and the next single blip retires it.
        row.expired_at = None
        row.expiry_reason = ""
        row.check_failures = 0
        row.search_blob = row.build_search_blob()
        result.updated += 1

    if new_models:
        session.add_all(new_models)

    await session.flush()
    return result


async def deactivate_stale(session: AsyncSession, days: int | None = None) -> int:
    """Mark postings we have not re-seen in `days` as inactive."""
    days = days if days is not None else settings.stale_after_days
    cutoff = utcnow() - timedelta(days=days)
    stmt = (
        update(Job)
        .where(Job.is_active.is_(True), Job.last_seen_at < cutoff)
        .values(is_active=False)
    )
    outcome = await session.execute(stmt)
    return outcome.rowcount or 0


async def purge_old(session: AsyncSession, days: int | None = None) -> int:
    """Delete inactive postings older than `days`."""
    days = days if days is not None else settings.purge_after_days
    cutoff = utcnow() - timedelta(days=days)
    outcome = await session.execute(
        delete(Job).where(Job.is_active.is_(False), Job.last_seen_at < cutoff)
    )
    return outcome.rowcount or 0


# ---------------------------------------------------------------------------
# Expiry
#
# `deactivate_stale` retires a posting for not having been seen. That is a slow,
# indirect signal - it fires `stale_after_days` after the board took the job
# down, and only if a scrape actually covered the query that would have found
# it. The functions below retire postings on direct evidence instead: the board
# answering 404 for the apply link, or the posting simply being too old to be
# honoured. Everything retired here carries a reason and an `expired_at`, so the
# purge can wait out a grace period rather than deleting on the spot.
# ---------------------------------------------------------------------------

EXPIRY_AGED_OUT = "aged_out"
EXPIRY_GONE = "gone"
EXPIRY_UNREACHABLE = "unreachable"
# Retired by `main.py reindex` because current rules no longer admit it.
EXPIRY_REJECTED = "rejected"


async def expire_jobs(session: AsyncSession, job_ids: list[int], reason: str) -> int:
    """Retire specific postings with a recorded reason."""
    if not job_ids:
        return 0
    outcome = await session.execute(
        update(Job)
        .where(Job.id.in_(job_ids), Job.is_active.is_(True))
        .values(is_active=False, expired_at=utcnow(), expiry_reason=reason)
    )
    return outcome.rowcount or 0


async def expire_aged_out(session: AsyncSession, days: int | None = None) -> int:
    """Retire active postings whose posting date is beyond the honour window.

    Only rows with a known `posted_at` qualify. A posting whose date the board
    never gave us is left to the staleness path - guessing an age from
    `first_seen_at` would retire jobs that were merely discovered late.
    """
    days = days if days is not None else settings.expire_posting_after_days
    if not days:
        return 0
    cutoff = utcnow() - timedelta(days=days)
    outcome = await session.execute(
        update(Job)
        .where(
            Job.is_active.is_(True),
            Job.posted_at.is_not(None),
            Job.posted_at < cutoff,
        )
        .values(is_active=False, expired_at=utcnow(), expiry_reason=EXPIRY_AGED_OUT)
    )
    return outcome.rowcount or 0


async def purge_expired(session: AsyncSession, days: int | None = None) -> int:
    """Delete postings retired by the expiry path more than `days` ago.

    Separate from `purge_old` because the clocks differ: that one waits out
    `last_seen_at` on rows nothing has re-seen, while an expired row has proof
    it is gone and only needs a short grace period in which a scrape could
    contradict us.
    """
    days = days if days is not None else settings.purge_expired_after_days
    cutoff = utcnow() - timedelta(days=days)
    outcome = await session.execute(
        delete(Job).where(
            Job.is_active.is_(False),
            Job.expired_at.is_not(None),
            Job.expired_at < cutoff,
        )
    )
    return outcome.rowcount or 0


async def select_for_expiry_check(
    session: AsyncSession,
    limit: int | None = None,
    recheck_after_hours: float | None = None,
) -> list[Job]:
    """Pick the active postings due for a liveness probe.

    Never-checked rows sort first, then least-recently-checked, so successive
    batches rotate through the whole table instead of re-probing the same head
    of it. Rows checked within the window are skipped entirely - the boards
    throttle by IP and this budget is shared with the scraper.
    """
    limit = limit if limit is not None else settings.expiry_check_batch
    hours = (
        recheck_after_hours
        if recheck_after_hours is not None
        else settings.expiry_recheck_after_hours
    )
    cutoff = utcnow() - timedelta(hours=hours)

    stmt = (
        select(Job)
        .where(
            Job.is_active.is_(True),
            or_(Job.last_checked_at.is_(None), Job.last_checked_at < cutoff),
        )
        .order_by(Job.last_checked_at.asc().nulls_first(), Job.id.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def record_check(
    session: AsyncSession,
    job_id: int,
    *,
    alive: bool | None,
    max_failures: int | None = None,
) -> bool:
    """Record one probe result. Returns True if the posting was retired.

    `alive=None` is the inconclusive case - a timeout, a 429, a block page. It
    is counted rather than acted on, because being blocked is not evidence that
    a job is gone; only `expiry_max_failures` of them in a row is treated as
    one. A single alive result clears the counter.
    """
    max_failures = (
        max_failures if max_failures is not None else settings.expiry_max_failures
    )
    job = await session.get(Job, job_id)
    if job is None:
        return False

    now = utcnow()
    job.last_checked_at = now

    if alive is True:
        job.check_failures = 0
        # A posting the board still serves is one we have effectively re-seen.
        job.last_seen_at = now
        return False

    if alive is False:
        job.check_failures = 0
        job.is_active = False
        job.expired_at = now
        job.expiry_reason = EXPIRY_GONE
        return True

    job.check_failures = (job.check_failures or 0) + 1
    if job.check_failures < max_failures:
        return False

    job.is_active = False
    job.expired_at = now
    job.expiry_reason = EXPIRY_UNREACHABLE
    return True


async def expiry_breakdown(session: AsyncSession) -> list[dict]:
    """Counts of retired postings by reason, for the stats endpoint."""
    stmt = (
        select(Job.expiry_reason, func.count(Job.id))
        .where(Job.is_active.is_(False), Job.expired_at.is_not(None))
        .group_by(Job.expiry_reason)
        .order_by(func.count(Job.id).desc())
    )
    rows = (await session.execute(stmt)).all()
    return [{"value": reason or "unknown", "count": int(count)} for reason, count in rows]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _escape_like(value: str) -> str:
    """Neutralise LIKE wildcards in user input.

    Without this a search for "C_+" would treat the underscore as "any
    character". Skill names such as "C++" and "Node.js" are safe, but the value
    arrives from a query string and cannot be assumed to be one of ours.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _apply_filters(stmt, filters: JobFilters):
    if filters.active_only:
        stmt = stmt.where(Job.is_active.is_(True))

    if filters.q:
        needle = f"%{_escape_like(filters.q.lower().strip())}%"
        stmt = stmt.where(
            or_(
                Job.search_blob.like(needle, escape="\\"),
                func.lower(Job.description).like(needle, escape="\\"),
            )
        )

    if filters.source:
        stmt = stmt.where(Job.source.in_([s.lower() for s in filters.source]))
    if filters.category:
        stmt = stmt.where(Job.category.in_(filters.category))
    if filters.seniority:
        stmt = stmt.where(Job.seniority.in_([s.lower() for s in filters.seniority]))
    if filters.work_mode:
        stmt = stmt.where(Job.work_mode.in_([w.lower() for w in filters.work_mode]))

    if filters.skill:
        # Match the fenced skill token, not a bare substring. '%java%' also
        # matched "javascript" (46% of results were wrong) and '%r%' matched
        # every posting in the database. Fencing keeps this portable across
        # SQLite and Postgres - no JSON operators, still one LIKE per skill.
        for skill in filters.skill:
            token = skill.strip().lower()
            if not token:
                continue
            stmt = stmt.where(
                Job.search_blob.like(f"%|{_escape_like(token)}|%", escape="\\")
            )

    if filters.location:
        needle = f"%{_escape_like(filters.location.lower())}%"
        stmt = stmt.where(func.lower(Job.location).like(needle, escape="\\"))
    if filters.company:
        needle = f"%{_escape_like(filters.company.lower())}%"
        stmt = stmt.where(func.lower(Job.company).like(needle, escape="\\"))

    if filters.min_experience is not None:
        bound = Job.experience_max >= filters.min_experience
        stmt = stmt.where(
            or_(Job.experience_max.is_(None), bound)
            if filters.include_unknown_experience
            else bound
        )
    if filters.max_experience is not None:
        bound = Job.experience_min <= filters.max_experience
        stmt = stmt.where(
            or_(Job.experience_min.is_(None), bound)
            if filters.include_unknown_experience
            else bound
        )

    if filters.posted_within_days:
        cutoff = utcnow() - timedelta(days=filters.posted_within_days)
        stmt = stmt.where(or_(Job.posted_at.is_(None), Job.posted_at >= cutoff))

    return stmt


async def search_jobs(
    session: AsyncSession,
    filters: JobFilters,
    *,
    page: int = 1,
    page_size: int = 25,
    sort: str = "posted_at",
    order: str = "desc",
) -> tuple[list[Job], int]:
    column = SORTABLE.get(sort, Job.posted_at)
    direction = column.desc() if order.lower() == "desc" else column.asc()

    count_stmt = _apply_filters(select(func.count(Job.id)), filters)
    total = (await session.execute(count_stmt)).scalar_one()

    stmt = _apply_filters(select(Job), filters)
    # NULL posted_at rows should not monopolise page 1 on a desc sort.
    stmt = stmt.order_by(direction, Job.id.desc())
    stmt = stmt.offset(max(page - 1, 0) * page_size).limit(page_size)

    rows = (await session.execute(stmt)).scalars().all()
    return list(rows), int(total)


async def get_job(session: AsyncSession, job_id: int) -> Job | None:
    return await session.get(Job, job_id)


async def count_jobs(session: AsyncSession, active_only: bool = True) -> int:
    stmt = select(func.count(Job.id))
    if active_only:
        stmt = stmt.where(Job.is_active.is_(True))
    return int((await session.execute(stmt)).scalar_one())


async def group_counts(session: AsyncSession, column, limit: int = 50) -> list[dict]:
    stmt = (
        select(column, func.count(Job.id))
        .where(Job.is_active.is_(True))
        .group_by(column)
        .order_by(func.count(Job.id).desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    return [{"value": value or "Unknown", "count": int(count)} for value, count in rows]


async def skill_counts(session: AsyncSession, limit: int = 60) -> list[dict]:
    """Aggregate the JSON skills column in Python.

    A relational skills table would be the textbook answer, but this runs once
    per cache window over a single indexed column and keeps the schema simple.
    """
    rows = (
        await session.execute(select(Job.skills).where(Job.is_active.is_(True)))
    ).scalars().all()

    counter: dict[str, int] = {}
    for skills in rows:
        for skill in skills or []:
            counter[skill] = counter.get(skill, 0) + 1

    ranked = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"value": name, "count": count} for name, count in ranked[:limit]]


# ---------------------------------------------------------------------------
# Scrape runs
# ---------------------------------------------------------------------------

async def create_run(
    session: AsyncSession,
    sources: list[str],
    trigger: str,
    stats: dict | None = None,
) -> ScrapeRun:
    """Open a run row. `stats` seeds it with what is already known (e.g. the
    scrape profile), so a *running* run is self-describing in `/scrape/runs`."""
    run = ScrapeRun(
        status="running",
        trigger=trigger,
        sources=sources,
        started_at=utcnow(),
        stats=stats or {},
    )
    session.add(run)
    await session.flush()
    return run


async def get_run(session: AsyncSession, run_id: int) -> ScrapeRun | None:
    return await session.get(ScrapeRun, run_id)


async def list_runs(session: AsyncSession, limit: int = 20) -> list[ScrapeRun]:
    stmt = select(ScrapeRun).order_by(ScrapeRun.id.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def latest_run(session: AsyncSession) -> ScrapeRun | None:
    stmt = select(ScrapeRun).order_by(ScrapeRun.id.desc()).limit(1)
    return (await session.execute(stmt)).scalars().first()


async def reap_orphaned_runs(session: AsyncSession, older_than_minutes: int | None = None) -> int:
    """Fail runs stuck at `running` because their process died.

    Runs live in process memory, so a crash, a kill or a redeploy leaves the row
    at `running` forever - and `has_running_scrape` would then reject every
    future scrape with a 409, permanently.

    Both callers pass an age limit. It is tempting for the API to skip one at
    startup - nothing can still be running in a process that has just booted -
    but that reasoning only holds while a single process owns every scrape.
    Once the scraper runs elsewhere (a CI schedule writing to the same
    database), an unbounded reap on boot will mark a live run failed and hand
    its lock to a second, concurrent scrape.
    """
    stmt = update(ScrapeRun).where(ScrapeRun.status == "running")
    if older_than_minutes is not None:
        stmt = stmt.where(
            ScrapeRun.started_at < utcnow() - timedelta(minutes=older_than_minutes)
        )
    stmt = stmt.values(
        status="failed",
        finished_at=utcnow(),
        error="Interrupted: the process exited before the run finished.",
    )
    outcome = await session.execute(stmt)
    return outcome.rowcount or 0


async def has_running_scrape(session: AsyncSession) -> ScrapeRun | None:
    stmt = select(ScrapeRun).where(ScrapeRun.status == "running").order_by(ScrapeRun.id.desc())
    return (await session.execute(stmt)).scalars().first()


async def finish_run(
    session: AsyncSession,
    run_id: int,
    *,
    status: str,
    stats: dict | None = None,
    error: str | None = None,
) -> None:
    run = await session.get(ScrapeRun, run_id)
    if run is None:
        return
    run.status = status
    run.finished_at = utcnow()
    started = run.started_at
    if started is not None:
        if started.tzinfo is None:
            started = started.replace(tzinfo=run.finished_at.tzinfo)
        run.duration_seconds = round((run.finished_at - started).total_seconds(), 2)
    if stats:
        run.stats = stats
        run.jobs_seen = int(stats.get("jobs_seen", run.jobs_seen))
        run.jobs_new = int(stats.get("jobs_new", run.jobs_new))
        run.jobs_updated = int(stats.get("jobs_updated", run.jobs_updated))
        run.jobs_rejected = int(stats.get("jobs_rejected", run.jobs_rejected))
    if error:
        run.error = error[:4000]


# ---------------------------------------------------------------------------
# Tiny TTL cache for facet endpoints
# ---------------------------------------------------------------------------

class TTLCache:
    def __init__(self, ttl_seconds: float = 120.0) -> None:
        self.ttl = ttl_seconds
        self._store: dict[str, tuple[float, object]] = {}

    def get(self, key: str) -> object | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() > expires_at:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: object) -> None:
        self._store[key] = (time.monotonic() + self.ttl, value)

    def clear(self) -> None:
        self._store.clear()


facet_cache = TTLCache(ttl_seconds=180.0)
