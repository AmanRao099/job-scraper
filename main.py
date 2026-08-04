#!/usr/bin/env python
"""CLI entry point.

    python main.py serve                 # run the API (what production runs)
    python main.py scrape                # one-off scrape into the database
    python main.py scrape --limit 5      # smoke test with 5 search queries
    python main.py scrape --profile bangalore-fresher-startups   # targeted run
    python main.py export jobs.jsonl     # dump the database
    python main.py import output/jobs.jsonl   # backfill from the old format
    python main.py stats                 # quick counts
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from app.config import settings

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
logger = logging.getLogger("job_scraper")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "app.api:app",
        host=args.host or settings.api_host,
        port=args.port or settings.api_port,
        reload=args.reload,
        log_level=settings.log_level.lower(),
    )
    return 0


async def _scrape(args: argparse.Namespace) -> int:
    from app.db import init_db, session_scope
    from app.events import broker
    from app.pipeline import ScrapeAlreadyRunning, run_scrape, resolve_sources, start_run
    from app.profiles import resolve_profile
    from app.repository import reap_orphaned_runs

    try:
        profile = resolve_profile(args.profile)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2

    await init_db()

    # Clear rows left at "running" by a previous crash. Age-limited so this
    # never disturbs a scrape that a live API server is currently running.
    async with session_scope() as session:
        reaped = await reap_orphaned_runs(session, older_than_minutes=args.stale_after)
    if reaped:
        logger.warning("Cleared %s interrupted run(s)", reaped)

    sources = resolve_sources(args.sources)
    try:
        run_id = await start_run(sources, trigger="cli", profile=profile)
    except ScrapeAlreadyRunning as exc:
        print(
            f"{exc}\n"
            f"If that run is dead, clear it with: python main.py scrape --stale-after 0",
            file=sys.stderr,
        )
        return 1

    # Mirror broker events to stdout so the CLI shows the same log the UI does.
    queue, _ = broker.subscribe(run_id)

    async def drain() -> None:
        while True:
            event = await queue.get()
            if event.get("type") == "log":
                print(f"[{event.get('level', 'info').upper():5}] {event['message']}", flush=True)
            elif event.get("type") == "progress":
                print(f"  ... {event['percent']}% ({event['done']}/{event['total']})", flush=True)
            elif event.get("type") == "done":
                return

    printer = asyncio.create_task(drain())
    stats = await run_scrape(
        run_id=run_id, sources=sources, query_limit=args.limit, profile=profile
    )
    await asyncio.wait_for(printer, timeout=5)

    print("\n" + json.dumps(stats.as_dict(), indent=2))
    return 0 if stats.jobs_kept or stats.jobs_updated else 1


async def _export(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from app.db import SessionLocal, init_db
    from app.models import Job

    await init_db()
    target = Path(args.path)
    target.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    async with SessionLocal() as session:
        rows = (await session.execute(select(Job).where(Job.is_active.is_(True)))).scalars()
        with target.open("w", encoding="utf-8") as handle:
            for job in rows:
                record = {
                    "title": job.title,
                    "company": job.company,
                    "location": job.location,
                    "experience": job.experience_text,
                    "salary": job.salary_text,
                    "apply_link": job.apply_link,
                    "description": job.description,
                    "skills": job.skills,
                    "category": job.category,
                    "seniority": job.seniority,
                    "work_mode": job.work_mode,
                    "source": job.source,
                    "posted_at": job.posted_at.isoformat() if job.posted_at else None,
                    "scraped_at": job.first_seen_at.isoformat(),
                }
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                written += 1

    print(f"Exported {written} jobs to {target}")
    return 0


async def _import(args: argparse.Namespace) -> int:
    """Backfill from the pre-2.0 output/jobs.jsonl file."""
    from app.db import init_db, session_scope
    from app.enrich import normalize_many
    from app.sources.base import RawJob
    from app.utils import parse_iso_date

    source_path = Path(args.path)
    if not source_path.exists():
        print(f"No such file: {source_path}", file=sys.stderr)
        return 1

    await init_db()

    raws: list[RawJob] = []
    skipped = 0
    with source_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            raws.append(
                RawJob(
                    source=record.get("source", "legacy"),
                    title=record.get("title", ""),
                    company=record.get("company", ""),
                    location=record.get("location", ""),
                    experience_text=record.get("experience", ""),
                    salary_text=record.get("salary", ""),
                    apply_link=record.get("apply_link", ""),
                    description=record.get("description", ""),
                    posted_at=parse_iso_date(record.get("scraped_at")),
                    declared_skills=record.get("skills") or [],
                )
            )

    normalized, rejections = normalize_many(raws)
    from app.repository import upsert_jobs

    async with session_scope() as session:
        result = await upsert_jobs(session, normalized)

    print(
        f"Read {len(raws)} records ({skipped} malformed), kept {len(normalized)}, "
        f"inserted {result.created}, updated {result.updated}. Rejections: {rejections}"
    )
    return 0


async def _reindex(args: argparse.Namespace) -> int:
    """Re-apply the current taxonomy and search format to every stored job.

    Classification happens at scrape time, so a posting keeps whatever the rules
    said on the day it was collected. When those rules are corrected, existing
    rows stay wrong until the posting happens to be scraped again - and a row
    that no longer qualifies as a tech job never will be, so it would linger
    until it aged out weeks later.

    Everything here is derived from columns we already store, so it is safe to
    re-run and needs no network access.
    """
    from sqlalchemy import select

    from app.db import init_db, session_scope
    from app.models import Job
    from app.config import settings
    from app.taxonomy import (
        categorize,
        detect_seniority,
        detect_work_mode,
        is_tech_job,
        resolve_experience,
        secondary_categories,
    )

    await init_db()

    scanned = reblobbed = recategorised = retired = re_experienced = 0
    async with session_scope() as session:
        rows = (await session.execute(select(Job))).scalars().all()
        for job in rows:
            scanned += 1
            skills = list(job.skills or [])

            # Re-derive experience before anything else - it decides whether the
            # posting belongs here at all. Rows collected before the description
            # was consulted carry no experience, and a row with no experience
            # satisfies every max_experience filter, so a ten-year role sits in
            # a fresher search until this runs.
            exp_min, exp_max = resolve_experience(
                job.experience_text, job.title, job.description
            )
            if (exp_min, exp_max) != (job.experience_min, job.experience_max):
                job.experience_min, job.experience_max = exp_min, exp_max
                re_experienced += 1

            too_senior = exp_min is not None and exp_min > settings.max_experience_years

            # A row that no longer passes the tech filter, or that turns out to
            # demand more experience than this tool targets, was admitted by a
            # rule that has since been corrected. Deactivate rather than delete:
            # the purge job removes it later, and the row stays inspectable.
            if job.is_active and (
                too_senior or not is_tech_job(job.title, job.description, skills)
            ):
                job.is_active = False
                retired += 1

            category = categorize(job.title, job.description, skills)
            categories = secondary_categories(job.title, job.description, skills)
            seniority = detect_seniority(job.title, job.experience_text, exp_min)
            work_mode = detect_work_mode(job.location, job.title, job.description)

            if (category, seniority, work_mode) != (job.category, job.seniority, job.work_mode):
                recategorised += 1
            job.category = category
            job.categories = categories
            job.seniority = seniority
            job.work_mode = work_mode

            rebuilt = job.build_search_blob()
            if rebuilt != job.search_blob:
                job.search_blob = rebuilt
                reblobbed += 1

            if args.verbose and scanned % 500 == 0:
                print(f"  ... {scanned} scanned", flush=True)

    print(
        f"Reindexed {scanned} jobs: {reblobbed} search blobs rebuilt, "
        f"{recategorised} reclassified, {re_experienced} experience re-resolved, "
        f"{retired} retired as non-tech or too senior."
    )
    return 0


async def _stats(_: argparse.Namespace) -> int:
    from app.db import SessionLocal, init_db
    from app.models import Job
    from app.repository import count_jobs, group_counts, latest_run

    await init_db()
    async with SessionLocal() as session:
        total = await count_jobs(session, active_only=False)
        active = await count_jobs(session, active_only=True)
        by_category = await group_counts(session, Job.category)
        by_source = await group_counts(session, Job.source)
        run = await latest_run(session)

    print(f"Jobs: {active} active / {total} total")
    print("\nBy source:")
    for row in by_source:
        print(f"  {row['value']:<12} {row['count']}")
    print("\nBy category:")
    for row in by_category:
        print(f"  {row['value']:<32} {row['count']}")
    if run:
        print(
            f"\nLast run #{run.id}: {run.status}, {run.jobs_new} new, "
            f"{run.jobs_updated} updated, {run.duration_seconds}s"
        )
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="job_scraper", description="Tech job extraction tool"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the API server")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve, is_async=False)

    scrape = sub.add_parser("scrape", help="Run one scrape into the database")
    scrape.add_argument(
        "--sources", nargs="*", default=None, help="e.g. --sources naukri linkedin"
    )
    scrape.add_argument("--limit", type=int, default=None, help="Cap number of search queries")
    scrape.add_argument(
        "--profile",
        default=None,
        metavar="KEY",
        help=(
            "Run a targeted profile instead of the nationwide sweep, e.g. "
            "--profile bangalore-fresher-startups"
        ),
    )
    scrape.add_argument(
        "--stale-after",
        type=int,
        default=120,
        metavar="MINUTES",
        help="Clear interrupted runs older than this before starting (0 clears all)",
    )
    scrape.set_defaults(func=_scrape, is_async=True)

    export = sub.add_parser("export", help="Dump active jobs to JSONL")
    export.add_argument("path", nargs="?", default="output/jobs.jsonl")
    export.set_defaults(func=_export, is_async=True)

    importer = sub.add_parser("import", help="Backfill from a legacy JSONL file")
    importer.add_argument("path", nargs="?", default="output/jobs.jsonl")
    importer.set_defaults(func=_import, is_async=True)

    reindex = sub.add_parser(
        "reindex", help="Rebuild the search index for every stored job"
    )
    reindex.add_argument("--verbose", action="store_true")
    reindex.set_defaults(func=_reindex, is_async=True)

    stats = sub.add_parser("stats", help="Show database counts")
    stats.set_defaults(func=_stats, is_async=True)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.is_async:
        return asyncio.run(args.func(args))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
