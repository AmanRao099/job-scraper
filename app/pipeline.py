"""Scrape orchestration.

One entry point - `run_scrape()` - that every trigger (API, scheduler, CLI)
goes through. Sources run concurrently, results are normalised in one batch,
and persistence happens as a single bulk upsert at the end.

Measured timings:
    4 queries, both sources                ~2.5 min -> 303 raw
    10 queries, LinkedIn only (pure HTTP)  ~5 min   -> 113 raw
    all 95 queries, both sources           ~24 min  -> 7,655 raw / 3,352 new

Naukri dominates the wall clock because it must be rendered in a browser at
concurrency 4. The old implementation was far slower still - it opened a
detail-page tab per posting, at ~4s each, on top of the search pages.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from dataclasses import dataclass, field

from app.config import settings
from app.db import session_scope
from app.enrich import normalize_many
from app.events import broker
from app.http_client import HttpClient
from app.profiles import ScrapeProfile, resolve_profile
from app.expiry import verify_active_jobs
from app.repository import (
    UpsertResult,
    create_run,
    deactivate_stale,
    expire_aged_out,
    facet_cache,
    finish_run,
    has_running_scrape,
    purge_expired,
    purge_old,
    upsert_jobs,
)
from app.sources import SOURCE_REGISTRY
from app.sources.base import SearchScope
from app.sources.browser import BrowserRenderer
from app.taxonomy import SEARCH_QUERIES

logger = logging.getLogger(__name__)

# Guards against two scrapes writing at once, which SQLite would serialise into
# lock contention anyway.
_scrape_lock = asyncio.Lock()

# Run-scoped state for cancellation.
_cancel_events: dict[int, asyncio.Event] = {}
_run_tasks: dict[int, asyncio.Task] = {}

# How long a cancelled run gets to wind down before it is killed outright.
HARD_CANCEL_GRACE_SECONDS = 45


class ScrapeNotRunning(RuntimeError):
    pass


def request_cancel(run_id: int) -> bool:
    """Ask a run to stop. Returns False if that run is not active.

    Cooperative by design: sources check the flag between pages and queries, so
    the run stops at a clean boundary and whatever it already collected is
    still normalised and saved. A watchdog hard-cancels the task if it has not
    wound down within the grace period.
    """
    event = _cancel_events.get(run_id)
    if event is None:
        return False
    if event.is_set():
        return True

    event.set()
    task = _run_tasks.get(run_id)
    if task is not None and not task.done():
        watchdog = asyncio.create_task(_hard_cancel_after_grace(run_id, task))
        _background_tasks.add(watchdog)
        watchdog.add_done_callback(_background_tasks.discard)
    return True


async def _hard_cancel_after_grace(run_id: int, task: asyncio.Task) -> None:
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=HARD_CANCEL_GRACE_SECONDS)
    except asyncio.TimeoutError:
        if not task.done():
            logger.warning(
                "Run %s did not stop within %ss; cancelling it outright",
                run_id, HARD_CANCEL_GRACE_SECONDS,
            )
            task.cancel()
    except Exception:
        pass


def is_cancelled(run_id: int) -> bool:
    event = _cancel_events.get(run_id)
    return event is not None and event.is_set()


@dataclass(slots=True)
class ScrapeStats:
    jobs_seen: int = 0
    jobs_kept: int = 0
    jobs_new: int = 0
    jobs_updated: int = 0
    jobs_rejected: int = 0
    deactivated: int = 0
    purged: int = 0
    cancelled: bool = False
    profile: str | None = None
    per_source: dict[str, int] = field(default_factory=dict)
    rejections: dict[str, int] = field(default_factory=dict)
    duration_seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "profile": self.profile,
            "jobs_seen": self.jobs_seen,
            "jobs_kept": self.jobs_kept,
            "jobs_new": self.jobs_new,
            "jobs_updated": self.jobs_updated,
            "jobs_rejected": self.jobs_rejected,
            "deactivated": self.deactivated,
            "purged": self.purged,
            "cancelled": self.cancelled,
            "per_source": self.per_source,
            "rejections": self.rejections,
            "duration_seconds": self.duration_seconds,
        }


class ScrapeAlreadyRunning(RuntimeError):
    pass


def resolve_sources(requested: list[str] | None) -> list[str]:
    wanted = [s.lower() for s in (requested or settings.sources_enabled)]
    valid = [s for s in wanted if s in SOURCE_REGISTRY]
    if not valid:
        raise ValueError(
            f"No valid sources in {wanted!r}. Available: {sorted(SOURCE_REGISTRY)}"
        )
    return valid


def resolve_queries(
    requested: list[str] | None,
    limit: int | None = None,
    profile: ScrapeProfile | None = None,
) -> list[str]:
    """Explicit queries win, then the profile's catalogue, then the global one."""
    default = list(profile.queries) if profile is not None else SEARCH_QUERIES
    queries = [q.strip() for q in (requested or default) if q and q.strip()]
    if limit:
        queries = queries[:limit]
    return queries


async def start_run(
    sources: list[str], trigger: str, profile: ScrapeProfile | None = None
) -> int:
    """Create the run row up front so the caller gets an id to poll immediately."""
    async with session_scope() as session:
        active = await has_running_scrape(session)
        if active is not None:
            raise ScrapeAlreadyRunning(f"Scrape run {active.id} is already in progress")
        run = await create_run(
            session,
            sources,
            trigger,
            stats={"profile": profile.key} if profile else None,
        )
        return run.id


async def run_scrape(
    *,
    run_id: int,
    sources: list[str] | None = None,
    queries: list[str] | None = None,
    query_limit: int | None = None,
    profile: str | ScrapeProfile | None = None,
) -> ScrapeStats:
    """Execute a full scrape. Never raises - failures are recorded on the run."""
    stats = ScrapeStats()
    loop = asyncio.get_running_loop()
    started = loop.time()

    cancel_event = _cancel_events.setdefault(run_id, asyncio.Event())

    async with _scrape_lock:
        try:
            active_profile = resolve_profile(profile)
            stats.profile = active_profile.key if active_profile else None
            source_names = resolve_sources(sources)
            query_list = resolve_queries(queries, query_limit, active_profile)
            scope = active_profile.scope if active_profile else SearchScope.default()

            await broker.log(
                run_id,
                f"Starting scrape | sources={','.join(source_names)} "
                f"| queries={len(query_list)}"
                + (
                    f" | profile={active_profile.key} ({scope.location})"
                    if active_profile
                    else ""
                ),
            )

            raw_jobs, per_source = await _collect(
                run_id, source_names, query_list, cancel_event, scope
            )
            stats.per_source = per_source
            stats.jobs_seen = len(raw_jobs)
            stats.cancelled = cancel_event.is_set()

            if stats.cancelled:
                await broker.log(
                    run_id,
                    f"Stop requested - keeping the {len(raw_jobs)} postings "
                    f"collected so far",
                    level="warning",
                )
            else:
                await broker.log(run_id, f"Collected {len(raw_jobs)} raw postings")

            # Enrichment is CPU-bound regex work; keep it off the event loop.
            normalized, rejections = await asyncio.to_thread(normalize_many, raw_jobs)

            if active_profile is not None:
                before = len(normalized)
                normalized, profile_rejections = active_profile.apply(normalized)
                rejections.update(profile_rejections)
                await broker.log(
                    run_id,
                    f"Profile {active_profile.key}: kept {len(normalized)} of "
                    f"{before} ({profile_rejections})",
                )

            stats.jobs_kept = len(normalized)
            stats.rejections = rejections
            stats.jobs_rejected = sum(rejections.values())
            await broker.log(
                run_id,
                f"Kept {len(normalized)} after filtering "
                f"({stats.jobs_rejected} rejected: {rejections})",
            )

            upsert = await _persist(normalized)
            stats.jobs_new = upsert.created
            stats.jobs_updated = upsert.updated
            await broker.log(
                run_id, f"Saved {upsert.created} new, refreshed {upsert.updated} existing"
            )

            # A partial run must not age out postings it simply never reached.
            # A profile run is partial by construction - it visited one city and
            # a fraction of the query catalogue - so it skips housekeeping for
            # exactly the same reason a cancelled run does.
            if not stats.cancelled and active_profile is None:
                stats.deactivated, stats.purged = await _housekeeping()
                if stats.deactivated or stats.purged:
                    await broker.log(
                        run_id,
                        f"Housekeeping: {stats.deactivated} deactivated, "
                        f"{stats.purged} purged",
                    )

            stats.duration_seconds = round(loop.time() - started, 2)
            facet_cache.clear()

            status = "cancelled" if stats.cancelled else "completed"
            async with session_scope() as session:
                await finish_run(session, run_id, status=status, stats=stats.as_dict())

            await broker.log(run_id, f"{status.capitalize()} in {stats.duration_seconds}s")
            await broker.finish(run_id, status, stats.as_dict())

        except asyncio.CancelledError:
            # Hard cancel: the cooperative path did not wind down in time.
            stats.duration_seconds = round(loop.time() - started, 2)
            stats.cancelled = True
            logger.warning("Scrape run %s was cancelled outright", run_id)

            async with session_scope() as session:
                await finish_run(
                    session,
                    run_id,
                    status="cancelled",
                    stats=stats.as_dict(),
                    error="Cancelled before the run could stop cleanly.",
                )

            await broker.log(run_id, "Run cancelled", level="warning")
            await broker.finish(run_id, "cancelled", stats.as_dict())
            raise

        except Exception as exc:
            stats.duration_seconds = round(loop.time() - started, 2)
            detail = f"{type(exc).__name__}: {exc}"
            logger.exception("Scrape run %s failed", run_id)

            async with session_scope() as session:
                await finish_run(
                    session,
                    run_id,
                    status="failed",
                    stats=stats.as_dict(),
                    error=f"{detail}\n{traceback.format_exc()}",
                )

            await broker.log(run_id, f"Run failed: {detail}", level="error")
            await broker.finish(run_id, "failed", stats.as_dict())

        finally:
            _cancel_events.pop(run_id, None)
            _run_tasks.pop(run_id, None)
            broker.prune()

    return stats


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

async def _collect(
    run_id: int,
    source_names: list[str],
    queries: list[str],
    cancel_event: asyncio.Event,
    scope: SearchScope | None = None,
) -> tuple[list, dict[str, int]]:
    done = 0
    total = 0
    progress_lock = asyncio.Lock()

    async with HttpClient() as client, BrowserRenderer() as renderer:
        sources = [
            SOURCE_REGISTRY[name](client, None, renderer=renderer, scope=scope)
            for name in source_names
        ]
        total = sum(source.plan(queries) for source in sources)

        async with session_scope() as session:
            from app.models import ScrapeRun

            run = await session.get(ScrapeRun, run_id)
            if run is not None:
                run.queries_total = total

        async def on_progress(delta: int, message: str) -> None:
            nonlocal done
            async with progress_lock:
                if delta:
                    done += delta
                current = done
            await broker.log(run_id, message)
            if delta:
                await broker.progress(run_id, current, total)
                # Persist progress periodically, not on every unit.
                if current % 10 == 0 or current == total:
                    await _save_progress(run_id, current)

        for source in sources:
            source.bind(progress=on_progress, cancel_event=cancel_event)

        results = await asyncio.gather(
            *(source.fetch(queries) for source in sources), return_exceptions=True
        )

    raw_jobs: list = []
    per_source: dict[str, int] = {}
    for source, result in zip(sources, results):
        if isinstance(result, BaseException):
            logger.warning("Source %s failed entirely: %s", source.name, result)
            await broker.log(run_id, f"Source {source.name} failed: {result}", level="error")
            per_source[source.name] = 0
            continue
        per_source[source.name] = len(result)
        raw_jobs.extend(result)

    return raw_jobs, per_source


async def _save_progress(run_id: int, done: int) -> None:
    from app.models import ScrapeRun

    try:
        async with session_scope() as session:
            run = await session.get(ScrapeRun, run_id)
            if run is not None:
                run.queries_done = done
    except Exception:
        logger.debug("Could not persist progress for run %s", run_id, exc_info=True)


async def _persist(normalized: list) -> UpsertResult:
    if not normalized:
        return UpsertResult()
    # Chunked so a very large run never builds one enormous transaction.
    chunk_size = 500
    total = UpsertResult()
    for start in range(0, len(normalized), chunk_size):
        chunk = normalized[start : start + chunk_size]
        async with session_scope() as session:
            result = await upsert_jobs(session, chunk)
        total.created += result.created
        total.updated += result.updated
    return total


async def _housekeeping() -> tuple[int, int]:
    """The age-based half of maintenance: no network, safe after every run."""
    async with session_scope() as session:
        deactivated = await deactivate_stale(session)
        deactivated += await expire_aged_out(session)
        purged = await purge_old(session)
        purged += await purge_expired(session)
    return deactivated, purged


async def run_maintenance(
    *,
    verify: bool | None = None,
    limit: int | None = None,
) -> dict:
    """Retire and delete postings that are no longer worth serving.

    Two independent passes, in this order:

    1. Age. Postings nothing has re-seen, and postings too old for the board to
       still honour, are retired; anything retired long enough ago is deleted.
       Pure SQL, so it always runs.
    2. Liveness. A batch of active postings is re-fetched from the board and the
       ones it no longer serves are retired on the spot. This is the pass that
       catches a job deleted yesterday, which age alone cannot see for weeks.

    Verification runs second so it never spends requests on rows the cheap pass
    was about to retire anyway.
    """
    deactivated, purged = await _housekeeping()
    summary: dict = {"deactivated": deactivated, "purged": purged}

    should_verify = (
        settings.expiry_check_enabled if verify is None else verify
    )

    if should_verify:
        # The probes hit the same boards, from the same address, against the
        # same rate limit as a scrape - and a scrape is the more valuable use
        # of that budget. Overlap is rare at the default cadences, and skipping
        # only defers this pass to the next window.
        async with session_scope() as session:
            running = await has_running_scrape(session)
        if running is not None:
            logger.info("Skipping link verification: scrape run %s is active", running.id)
            summary["verified"] = {"skipped": "scrape_running"}
            should_verify = False

    if should_verify:
        report = await verify_active_jobs(limit=limit)
        summary["verified"] = report.as_dict()
        if report.retired:
            # Newly retired rows only become deletable after the grace period,
            # so this pass does not re-purge - the next one does.
            summary["expired"] = report.retired

    facet_cache.clear()
    return summary


async def trigger_scrape(
    *,
    sources: list[str] | None = None,
    queries: list[str] | None = None,
    query_limit: int | None = None,
    profile: str | ScrapeProfile | None = None,
    trigger: str = "manual",
) -> int:
    """Create a run and kick it off in the background. Returns the run id."""
    source_names = resolve_sources(sources)
    # Resolve here as well as in run_scrape: an unknown profile must fail the
    # caller's request rather than a background task nobody is watching.
    active_profile = resolve_profile(profile)
    run_id = await start_run(source_names, trigger, active_profile)

    # Register before scheduling so a cancel arriving immediately still lands.
    _cancel_events[run_id] = asyncio.Event()

    task = asyncio.create_task(
        run_scrape(
            run_id=run_id,
            sources=source_names,
            queries=queries,
            query_limit=query_limit,
            profile=active_profile,
        )
    )
    _run_tasks[run_id] = task
    # Keep a reference so the task is not garbage collected mid-flight.
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return run_id


_background_tasks: set[asyncio.Task] = set()
