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
from dataclasses import dataclass, field
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.db import session_scope
from app.enrich import normalize_many
from app.events import broker
from app.http_client import HttpClient
from app.profiles import ScrapeProfile, resolve_profile
from app.repository import (
    UpsertResult,
    create_run,
    deactivate_stale,
    facet_cache,
    finish_run,
    has_running_scrape,
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


async def shutdown_runs(timeout: float | None = None) -> None:
    """Cooperatively drain active scrapes before database connections close."""
    tasks = [task for task in _run_tasks.values() if not task.done()]
    if not tasks:
        return
    for event in _cancel_events.values():
        event.set()
    done, pending = await asyncio.wait(
        tasks,
        timeout=timeout if timeout is not None else settings.shutdown_grace_seconds,
    )
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    logger.info("Shutdown drained %s scrape task(s); forced=%s", len(done), len(pending))


@dataclass(slots=True)
class ScrapeStats:
    outcome: str = "running"
    jobs_seen: int = 0
    jobs_kept: int = 0
    jobs_new: int = 0
    jobs_updated: int = 0
    jobs_unchanged: int = 0
    jobs_rejected: int = 0
    deactivated: int = 0
    purged: int = 0
    cancelled: bool = False
    profile: str | None = None
    per_source: dict[str, int] = field(default_factory=dict)
    source_details: dict[str, dict[str, object]] = field(default_factory=dict)
    rejections: dict[str, int] = field(default_factory=dict)
    duration_seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "profile": self.profile,
            "jobs_seen": self.jobs_seen,
            "jobs_kept": self.jobs_kept,
            "jobs_new": self.jobs_new,
            "jobs_updated": self.jobs_updated,
            "jobs_unchanged": self.jobs_unchanged,
            "jobs_rejected": self.jobs_rejected,
            "deactivated": self.deactivated,
            "purged": self.purged,
            "cancelled": self.cancelled,
            "per_source": self.per_source,
            "source_details": self.source_details,
            "rejections": self.rejections,
            "duration_seconds": self.duration_seconds,
        }


class ScrapeAlreadyRunning(RuntimeError):
    pass


def resolve_sources(
    requested: list[str] | None, profile: ScrapeProfile | None = None
) -> list[str]:
    wanted = [s.lower() for s in (requested or settings.sources_enabled)]
    if profile is not None and profile.allowed_sources:
        allowed = set(profile.allowed_sources)
        wanted = [source for source in wanted if source in allowed]
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
    try:
        async with session_scope() as session:
            active = await has_running_scrape(session)
            if active is not None:
                raise ScrapeAlreadyRunning(
                    f"Scrape run {active.id} is already in progress"
                )
            run = await create_run(
                session,
                sources,
                trigger,
                stats={"profile": profile.key} if profile else None,
            )
            return run.id
    except IntegrityError as exc:
        # The partial unique index closes the check-then-insert race across API,
        # CLI and scheduled scraper processes.
        raise ScrapeAlreadyRunning(
            "Another scrape acquired the database run lock"
        ) from exc


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
            source_names = resolve_sources(sources, active_profile)
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

            raw_jobs, per_source, source_details = await _collect(
                run_id, source_names, query_list, cancel_event, scope, active_profile
            )
            stats.per_source = per_source
            stats.source_details = source_details
            stats.jobs_seen = len(raw_jobs)
            stats.cancelled = cancel_event.is_set()

            failed_sources = [
                name
                for name, detail in source_details.items()
                if detail.get("errors")
            ]
            accepted_responses = sum(
                int(detail.get("responses_accepted", 0))
                for detail in source_details.values()
            )
            if not raw_jobs and failed_sources and not accepted_responses:
                raise RuntimeError(
                    "All enabled sources failed before returning a valid response: "
                    + ", ".join(failed_sources)
                )

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
            normalized, rejections = await asyncio.to_thread(
                normalize_many,
                raw_jobs,
                allow_any_experience=bool(
                    active_profile and active_profile.allow_any_experience
                ),
                profile_key=active_profile.key if active_profile else None,
                max_posting_age_days=(
                    active_profile.freshness_days if active_profile else None
                ),
            )

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
            stats.jobs_unchanged = upsert.unchanged
            await broker.log(
                run_id,
                f"Saved {upsert.created} new, updated {upsert.updated}, "
                f"unchanged {upsert.unchanged}",
            )

            # A partial run must not age out postings it simply never reached.
            # A profile run is partial by construction - it visited one city and
            # a fraction of the query catalogue - so it skips housekeeping for
            # exactly the same reason a cancelled run does.
            if not stats.cancelled and not failed_sources and active_profile is None:
                stats.deactivated, stats.purged = await _housekeeping()
                if stats.deactivated or stats.purged:
                    await broker.log(
                        run_id,
                        f"Housekeeping: {stats.deactivated} deactivated, "
                        f"{stats.purged} purged",
                    )

            stats.duration_seconds = round(loop.time() - started, 2)
            facet_cache.clear()

            status = (
                "cancelled"
                if stats.cancelled
                else "partial"
                if failed_sources
                else "completed"
            )
            stats.outcome = status
            async with session_scope() as session:
                await finish_run(session, run_id, status=status, stats=stats.as_dict())

            await broker.log(run_id, f"{status.capitalize()} in {stats.duration_seconds}s")
            await broker.finish(run_id, status, stats.as_dict())

        except asyncio.CancelledError:
            # Hard cancel: the cooperative path did not wind down in time.
            stats.duration_seconds = round(loop.time() - started, 2)
            stats.cancelled = True
            stats.outcome = "cancelled"
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
            stats.outcome = "failed"
            detail = f"{type(exc).__name__}: scrape run failed; see server logs"
            logger.exception("Scrape run %s failed", run_id)

            async with session_scope() as session:
                await finish_run(
                    session,
                    run_id,
                    status="failed",
                    stats=stats.as_dict(),
                    error=detail,
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
    profile: ScrapeProfile | None = None,
) -> tuple[list, dict[str, int], dict[str, dict[str, object]]]:
    done = 0
    total = 0
    progress_lock = asyncio.Lock()

    async with HttpClient() as client, BrowserRenderer() as renderer:
        sources = [
            SOURCE_REGISTRY[name](client, None, renderer=renderer, scope=scope)
            for name in source_names
        ]
        if profile is not None:
            page_limits = dict(profile.max_pages_by_source)
            for source in sources:
                if source.name in page_limits and hasattr(source, "pages"):
                    source.pages = min(source.pages, page_limits[source.name])
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
    source_details: dict[str, dict[str, object]] = {}
    for source, result in zip(sources, results):
        if isinstance(result, BaseException):
            logger.warning("Source %s failed entirely: %s", source.name, result)
            await broker.log(run_id, f"Source {source.name} failed: {result}", level="error")
            per_source[source.name] = 0
            source.stats.fail("source_exception")
            source.finish_stats(0)
            source_details[source.name] = source.stats.as_dict()
            continue
        per_source[source.name] = len(result)
        if profile is not None and profile.max_results is not None:
            result = result[: profile.max_results]
            per_source[source.name] = len(result)
        source_details[source.name] = source.stats.as_dict()
        raw_jobs.extend(result)

    return raw_jobs, per_source, source_details


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
        total.unchanged += result.unchanged
    return total


async def _housekeeping() -> tuple[int, int]:
    async with session_scope() as session:
        deactivated = await deactivate_stale(session)
        purged = await purge_old(session)
    return deactivated, purged


async def trigger_scrape(
    *,
    sources: list[str] | None = None,
    queries: list[str] | None = None,
    query_limit: int | None = None,
    profile: str | ScrapeProfile | None = None,
    trigger: str = "manual",
) -> int:
    """Create a run and kick it off in the background. Returns the run id."""
    # Resolve here as well as in run_scrape: an unknown profile must fail the
    # caller's request rather than a background task nobody is watching.
    active_profile = resolve_profile(profile)
    source_names = resolve_sources(sources, active_profile)
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
