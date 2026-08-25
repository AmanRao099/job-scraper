"""Reproducible synthetic production-path benchmark (no network access)."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def raw_jobs(size: int):
    from app.sources.base import RawJob

    description = (
        "Build production Python, PostgreSQL and AWS services. "
        "A Masters degree in Computer Science is preferred. "
        "Candidates must already be authorized to work."
    )
    return [
        RawJob(
            source="linkedin",
            external_id=str(index),
            title=f"Software Engineer {index}",
            company=f"Company {index}",
            location="Berlin, Germany",
            apply_link=f"https://www.linkedin.com/jobs/view/{index}",
            description=description,
            declared_skills=["Python", "AWS", "PostgreSQL"],
            discovered_query="software engineer masters",
        )
        for index in range(size)
    ]


def measure(function):
    tracemalloc.start()
    started = time.perf_counter()
    result = function()
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return result, round(elapsed, 4), round(peak / 1024 / 1024, 2)


async def run(size: int) -> dict:
    from app import repository as repo
    from app.db import SessionLocal, engine, init_db
    from app.enrich import deduplicate_jobs, normalize
    from app.models import Job
    from app.reindex import reindex_jobs

    await init_db()
    from sqlalchemy import event
    from sqlalchemy import text

    query_counter = {"count": 0}

    def count_query(*_args):
        query_counter["count"] += 1

    event.listen(engine.sync_engine, "before_cursor_execute", count_query)
    raws = raw_jobs(size)
    normalized, classify_time, classify_peak = measure(
        lambda: [
            normalize(
                raw,
                allow_any_experience=True,
                profile_key="worldwide-masters-tech",
            )[0]
            for raw in raws
        ]
    )
    normalized = [job for job in normalized if job is not None]
    half = max(1, size // 2)
    dedup_input = [
        copy.deepcopy(normalized[index % half]) for index in range(size)
    ]
    deduped, dedup_time, dedup_peak = measure(
        lambda: deduplicate_jobs(dedup_input)
    )

    started = time.perf_counter()
    query_counter["count"] = 0
    async with SessionLocal() as session:
        persisted = await repo.upsert_jobs(session, normalized)
        await session.commit()
    persist_time = time.perf_counter() - started
    persist_queries = query_counter["count"]

    started = time.perf_counter()
    query_counter["count"] = 0
    stats = await reindex_jobs(batch_size=500)
    reindex_time = time.perf_counter() - started
    reindex_queries = query_counter["count"]

    filters = repo.JobFilters(
        is_abroad=True,
        masters_match=True,
        education_requirement=["preferred"],
        max_experience=10,
    )
    started = time.perf_counter()
    query_counter["count"] = 0
    async with SessionLocal() as session:
        rows, total = await repo.search_jobs(session, filters, page=1, page_size=50)
    filter_time = time.perf_counter() - started
    filter_queries = query_counter["count"]

    started = time.perf_counter()
    query_counter["count"] = 0
    async with SessionLocal() as session:
        countries = await repo.group_counts(session, Job.country, limit=100)
        skills = await repo.skill_counts(session, limit=60)
    facet_time = time.perf_counter() - started
    facet_queries = query_counter["count"]
    async with SessionLocal() as session:
        plan_rows = (
            await session.execute(
                text(
                    "EXPLAIN QUERY PLAN SELECT id FROM jobs "
                    "WHERE is_active = 1 AND is_abroad = 1 AND masters_match = 1 "
                    "AND education_requirement = 'preferred' "
                    "ORDER BY posted_at DESC, id DESC LIMIT 50"
                )
            )
        ).all()
    event.remove(engine.sync_engine, "before_cursor_execute", count_query)

    return {
        "dataset_size": size,
        "classification": {
            "seconds": classify_time,
            "peak_memory_mb": classify_peak,
            "kept": len(normalized),
        },
        "deduplication": {
            "seconds": dedup_time,
            "peak_memory_mb": dedup_peak,
            "input": len(dedup_input),
            "output": len(deduped),
        },
        "batch_upsert": {
            "seconds": round(persist_time, 4),
            "created": persisted.created,
            "updated": persisted.updated,
            "unchanged": persisted.unchanged,
            "database_queries": persist_queries,
        },
        "reindex": {
            "seconds": round(reindex_time, 4),
            "database_queries": reindex_queries,
            **stats.as_dict(),
        },
        "combined_filter": {
            "seconds": round(filter_time, 4), "database_queries": filter_queries,
            "total": total, "page_rows": len(rows)
        },
        "facets": {
            "seconds": round(facet_time, 4),
            "database_queries": facet_queries,
            "country_values": len(countries),
            "skill_values": len(skills),
        },
        "sqlite_query_plan": [str(row[-1]) for row in plan_rows],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=int, default=10_000)
    args = parser.parse_args()
    if args.size < 1 or args.size > 100_000:
        parser.error("--size must be between 1 and 100000")
    database = Path(tempfile.mkdtemp(prefix="job-scraper-bench-")) / "bench.db"
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{database.as_posix()}"
    os.environ["SCHEDULER_ENABLED"] = "false"
    print(json.dumps(asyncio.run(run(args.size)), indent=2))


if __name__ == "__main__":
    main()
