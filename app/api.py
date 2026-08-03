"""FastAPI application - the only thing your frontend talks to.

Read endpoints serve from the local database, so they are fast and safe to call
on every keystroke. Write endpoints only ever schedule background work.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository as repo
from app import scheduler as sched
from app.config import settings
from app.db import SessionLocal, dispose_db, get_session, healthcheck, init_db
from app.events import broker
from app.models import Job
from app.pipeline import (
    HARD_CANCEL_GRACE_SECONDS,
    ScrapeAlreadyRunning,
    request_cancel,
    trigger_scrape,
)
from app.schemas import (
    FiltersOut,
    HealthOut,
    JobDetail,
    JobOut,
    JobPage,
    MessageOut,
    PageMeta,
    ScrapeRequest,
    ScrapeRunOut,
    ScrapeStarted,
    StatsOut,
)
from app.sources import AVAILABLE_SOURCES
from app.taxonomy import CATEGORIES, SEARCH_QUERIES, SENIORITY_LEVELS, SKILLS
from app.utils import utcnow

logger = logging.getLogger(__name__)

VERSION = "2.0.0"

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    await init_db()

    # Clear runs abandoned by a process that died. Age-limited because this
    # server is no longer the only thing that scrapes: a scheduled run in CI can
    # be perfectly healthy while this instance cold-starts underneath it, and
    # clearing it would both mislabel it failed and release its lock.
    async with SessionLocal() as session:
        orphans = await repo.reap_orphaned_runs(
            session, older_than_minutes=settings.orphan_run_after_minutes
        )
        await session.commit()
    if orphans:
        logger.warning("Marked %s interrupted scrape run(s) as failed", orphans)

    sched.start_scheduler()

    if settings.scrape_on_startup:
        try:
            await trigger_scrape(trigger="startup")
        except Exception:
            logger.exception("Startup scrape failed to launch")

    yield

    sched.shutdown_scheduler()
    await dispose_db()


app = FastAPI(
    title=settings.app_name,
    version=VERSION,
    description=(
        "Tech job aggregation API. Postings are scraped on a schedule into local "
        "storage; every read endpoint below serves from that cache."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins or ["*"],
    # For frontends on rotating hostnames (preview deploys). Ignored when empty.
    allow_origin_regex=settings.cors_origin_regex or None,
    # Credentials cannot be combined with a wildcard origin per the CORS spec;
    # browsers reject that pairing outright.
    allow_credentials=bool(settings.cors_origins),
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    # Cache the preflight so a filter UI firing a request per keystroke does not
    # pay for an OPTIONS round-trip each time.
    max_age=600,
)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    """Protect mutating endpoints when ADMIN_TOKEN is configured."""
    if not settings.admin_token:
        return
    if x_admin_token != settings.admin_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-Admin-Token header",
        )


# ---------------------------------------------------------------------------
# Test console
# ---------------------------------------------------------------------------

# Served from the API itself so it is same-origin: no CORS entry, no build
# step, no separate port. Open http://localhost:8000/ and it just works.

@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/ui")


@app.get("/ui", include_in_schema=False)
async def test_console() -> FileResponse:
    page = STATIC_DIR / "test.html"
    if not page.exists():
        raise HTTPException(status_code=404, detail="Test console is not installed")
    return FileResponse(page, media_type="text/html")


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------

@app.get("/health", response_model=HealthOut, tags=["meta"])
async def health() -> HealthOut:
    db_ok = await healthcheck()
    return HealthOut(
        status="ok" if db_ok else "degraded",
        database=db_ok,
        scheduler=sched.is_running(),
        version=VERSION,
        environment=settings.environment,
    )


@app.get("/meta", tags=["meta"])
async def meta() -> dict:
    """Static reference data: what can be scraped and filtered on."""
    return {
        "version": VERSION,
        "sources": AVAILABLE_SOURCES,
        "sources_enabled": settings.sources_enabled,
        "categories": sorted(CATEGORIES),
        "seniorities": SENIORITY_LEVELS,
        "work_modes": ["onsite", "hybrid", "remote"],
        "known_skills": sorted(SKILLS),
        "search_queries": SEARCH_QUERIES,
        "scheduler": {
            "enabled": settings.scheduler_enabled,
            "running": sched.is_running(),
            "interval_hours": settings.scrape_interval_hours,
            "next_run_at": sched.next_run_time(),
        },
    }


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

@app.get("/jobs", response_model=JobPage, tags=["jobs"])
async def list_jobs(
    session: AsyncSession = Depends(get_session),
    q: str | None = Query(None, description="Free text over title, company, skills, description"),
    source: list[str] | None = Query(None),
    category: list[str] | None = Query(None),
    skill: list[str] | None = Query(None, description="Repeatable; results must match all"),
    seniority: list[str] | None = Query(None),
    work_mode: list[str] | None = Query(None),
    location: str | None = Query(None),
    company: str | None = Query(None),
    min_experience: int | None = Query(None, ge=0, le=30),
    max_experience: int | None = Query(None, ge=0, le=30),
    posted_within_days: int | None = Query(None, ge=1, le=365),
    include_inactive: bool = Query(False),
    sort: str = Query("posted_at", pattern="^(posted_at|first_seen_at|last_seen_at|title|company|experience)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, ge=1),
) -> JobPage:
    size = min(page_size or settings.default_page_size, settings.max_page_size)

    filters = repo.JobFilters(
        q=q,
        source=source,
        category=category,
        skill=skill,
        seniority=seniority,
        work_mode=work_mode,
        location=location,
        company=company,
        min_experience=min_experience,
        max_experience=max_experience,
        posted_within_days=posted_within_days,
        active_only=not include_inactive,
    )

    rows, total = await repo.search_jobs(
        session, filters, page=page, page_size=size, sort=sort, order=order
    )
    total_pages = (total + size - 1) // size if size else 0

    return JobPage(
        items=[JobOut.model_validate(row) for row in rows],
        meta=PageMeta(
            page=page,
            page_size=size,
            total=total,
            total_pages=total_pages,
            has_next=page < total_pages,
            has_prev=page > 1,
        ),
    )


@app.get("/jobs/{job_id}", response_model=JobDetail, tags=["jobs"])
async def get_job(job_id: int, session: AsyncSession = Depends(get_session)) -> JobDetail:
    job = await repo.get_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return JobDetail.model_validate(job)


@app.get("/filters", response_model=FiltersOut, tags=["jobs"])
async def get_filters(session: AsyncSession = Depends(get_session)) -> FiltersOut:
    """Facet counts for building filter dropdowns. Cached for 3 minutes."""
    cached = repo.facet_cache.get("filters")
    if cached is not None:
        return cached  # type: ignore[return-value]

    payload = FiltersOut(
        categories=await repo.group_counts(session, Job.category),
        sources=await repo.group_counts(session, Job.source),
        seniorities=await repo.group_counts(session, Job.seniority),
        work_modes=await repo.group_counts(session, Job.work_mode),
        locations=await repo.group_counts(session, Job.location, limit=40),
        companies=await repo.group_counts(session, Job.company, limit=40),
        skills=await repo.skill_counts(session, limit=60),
    )
    repo.facet_cache.set("filters", payload)
    return payload


@app.get("/stats", response_model=StatsOut, tags=["jobs"])
async def get_stats(session: AsyncSession = Depends(get_session)) -> StatsOut:
    cached = repo.facet_cache.get("stats")
    if cached is not None:
        return cached  # type: ignore[return-value]

    since = utcnow() - timedelta(days=1)
    added_today = int(
        (
            await session.execute(
                select(func.count(Job.id)).where(Job.first_seen_at >= since)
            )
        ).scalar_one()
    )
    last_run = await repo.latest_run(session)

    payload = StatsOut(
        total_jobs=await repo.count_jobs(session, active_only=False),
        active_jobs=await repo.count_jobs(session, active_only=True),
        jobs_added_today=added_today,
        by_category=await repo.group_counts(session, Job.category),
        by_source=await repo.group_counts(session, Job.source),
        by_seniority=await repo.group_counts(session, Job.seniority),
        top_skills=await repo.skill_counts(session, limit=25),
        last_run=ScrapeRunOut.model_validate(last_run) if last_run else None,
    )
    repo.facet_cache.set("stats", payload)
    return payload


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

@app.post(
    "/scrape/run",
    response_model=ScrapeStarted,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["scrape"],
    dependencies=[Depends(require_admin)],
)
async def start_scrape(payload: ScrapeRequest, request: Request) -> ScrapeStarted:
    """Kick off a scrape in the background and return immediately."""
    try:
        run_id = await trigger_scrape(
            sources=payload.sources,
            queries=payload.queries,
            query_limit=payload.query_limit,
            trigger="manual",
        )
    except ScrapeAlreadyRunning as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    base = str(request.base_url).rstrip("/")
    return ScrapeStarted(
        run_id=run_id,
        stream_url=f"{base}/scrape/runs/{run_id}/stream",
        status_url=f"{base}/scrape/runs/{run_id}",
    )


@app.get("/scrape/runs", response_model=list[ScrapeRunOut], tags=["scrape"])
async def list_runs(
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
) -> list[ScrapeRunOut]:
    runs = await repo.list_runs(session, limit=limit)
    return [ScrapeRunOut.model_validate(run) for run in runs]


@app.get("/scrape/runs/{run_id}", response_model=ScrapeRunOut, tags=["scrape"])
async def get_run(run_id: int, session: AsyncSession = Depends(get_session)) -> ScrapeRunOut:
    run = await repo.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return ScrapeRunOut.model_validate(run)


@app.post(
    "/scrape/runs/{run_id}/cancel",
    response_model=MessageOut,
    tags=["scrape"],
    dependencies=[Depends(require_admin)],
)
async def cancel_run(run_id: int, session: AsyncSession = Depends(get_session)) -> MessageOut:
    """Stop a running scrape.

    Cooperative: the run stops at the next page boundary and still saves the
    postings it already collected. If it has not wound down within the grace
    period it is cancelled outright.
    """
    run = await repo.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    if run.status != "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run {run_id} is already {run.status}",
        )

    if not request_cancel(run_id):
        # The row says running but this process owns no such task - it belongs
        # to a previous process that died. Mark it failed so it stops blocking.
        await repo.reap_orphaned_runs(session)
        await session.commit()
        return MessageOut(
            message=f"Run {run_id} was orphaned by a previous process; marked failed.",
            detail={"run_id": run_id, "orphaned": True},
        )

    await broker.log(run_id, "Stop requested - finishing the current page…", level="warning")
    return MessageOut(
        message=f"Stop requested for run {run_id}. Partial results will be saved.",
        detail={"run_id": run_id, "grace_seconds": HARD_CANCEL_GRACE_SECONDS},
    )


@app.get("/scrape/runs/{run_id}/stream", tags=["scrape"])
async def stream_run(run_id: int, session: AsyncSession = Depends(get_session)) -> StreamingResponse:
    """Server-sent events for a single run: logs, progress, completion.

    Each subscriber gets its own queue plus a replay of what it missed, so
    refreshing the page or opening a second tab both work correctly.
    """
    run = await repo.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    async def event_stream() -> AsyncIterator[str]:
        queue, backlog = broker.subscribe(run_id)
        try:
            for event in backlog:
                yield f"data: {json.dumps(event)}\n\n"
                # The replayed backlog can already contain the terminal event -
                # close here rather than falling through and waiting forever.
                if event.get("type") == "done":
                    return

            # A run that finished before this connection (or before a restart,
            # which empties the in-memory buffer) has nothing left to stream.
            if run.status != "running":
                yield f"data: {json.dumps({'type': 'done', 'status': run.status})}\n\n"
                return

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20.0)
                except asyncio.TimeoutError:
                    # Comment frame keeps proxies from closing an idle stream.
                    yield ": keepalive\n\n"
                    continue

                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") == "done":
                    return
        finally:
            broker.unsubscribe(run_id, queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


@app.delete(
    "/jobs/maintenance/stale",
    response_model=MessageOut,
    tags=["scrape"],
    dependencies=[Depends(require_admin)],
)
async def cleanup_stale(
    stale_days: int = Query(None, ge=1, le=365),
    purge_days: int = Query(None, ge=1, le=730),
    session: AsyncSession = Depends(get_session),
) -> MessageOut:
    deactivated = await repo.deactivate_stale(session, stale_days)
    purged = await repo.purge_old(session, purge_days)
    await session.commit()
    repo.facet_cache.clear()
    return MessageOut(
        message="Maintenance complete",
        detail={"deactivated": deactivated, "purged": purged},
    )
