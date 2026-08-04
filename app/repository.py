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
