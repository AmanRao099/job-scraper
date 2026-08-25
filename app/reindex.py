"""Offline, restartable reclassification of stored jobs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from time import monotonic
from typing import Callable

from sqlalchemy import select

from app.classification import classify_international, parse_qualifications
from app.db import session_scope
from app.models import Job, build_search_blob, make_fingerprint
from app.taxonomy import (
    categorize,
    detect_seniority,
    detect_employment_type,
    detect_work_mode,
    resolve_experience,
    secondary_categories,
)
from app.utils import strip_tracking

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ReindexStats:
    scanned: int = 0
    changed: int = 0
    unchanged: int = 0
    failed: int = 0
    batches_committed: int = 0
    last_id: int = 0
    duration_seconds: float = 0.0

    def as_dict(self) -> dict[str, int | float]:
        return {
            "scanned": self.scanned,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "failed": self.failed,
            "batches_committed": self.batches_committed,
            "last_id": self.last_id,
            "duration_seconds": self.duration_seconds,
        }


def derive_updates(job: Job) -> dict[str, object]:
    """Purely derive current classifications from already stored columns."""
    skills = list(job.skills or [])
    exp_min, exp_max = resolve_experience(
        job.experience_text, job.title, job.description
    )
    qualification = parse_qualifications(job.description)
    international = classify_international(job.location, job.description)
    category = categorize(job.title, job.description, skills)
    canonical_url = strip_tracking(job.apply_link)
    source_id = f"{job.source}:{job.external_id}" if job.external_id else ""

    desired: dict[str, object] = {
        "experience_min": exp_min,
        "experience_max": exp_max,
        "degree_requirements": qualification.degree_requirements,
        "masters_match": qualification.masters_match,
        "education_requirement": qualification.education_requirement,
        "country": international.country,
        "is_abroad": international.is_abroad,
        "visa_sponsorship": international.visa_sponsorship,
        "work_authorization_required": international.work_authorization_required,
        "relocation_support": international.relocation_support,
        "category": category,
        "categories": secondary_categories(job.title, job.description, skills),
        "seniority": detect_seniority(job.title, job.experience_text, exp_min),
        "work_mode": detect_work_mode(job.location, job.title, job.description),
        "employment_type": detect_employment_type(job.title, job.description),
        "canonical_url": canonical_url,
        "dedup_key": make_fingerprint(job.title, job.company, job.location),
        "source_ids": sorted(set(job.source_ids or []) | ({source_id} if source_id else set())),
        "source_urls": sorted(set(job.source_urls or []) | ({canonical_url} if canonical_url else set())),
    }
    desired["search_blob"] = build_search_blob(
        job.title,
        job.company,
        job.location,
        category,
        international.country,
        skills,
    )
    return desired


async def reindex_jobs(
    *,
    batch_size: int,
    start_after_id: int = 0,
    dry_run: bool = False,
    progress: Callable[[ReindexStats], None] | None = None,
) -> ReindexStats:
    """Reindex in stable primary-key batches without network or deactivation.

    Each successful batch commits independently. If the process stops, the
    printed ``last_id`` can be supplied as ``--start-after-id``; rerunning from
    zero is also safe because unchanged rows are skipped.
    """
    stats = ReindexStats(last_id=max(0, start_after_id))
    started = monotonic()

    while True:
        async with session_scope() as session:
            rows = list(
                (
                    await session.execute(
                        select(Job)
                        .where(Job.id > stats.last_id)
                        .order_by(Job.id.asc())
                        .limit(batch_size)
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                break

            for job in rows:
                stats.scanned += 1
                stats.last_id = job.id
                try:
                    desired = derive_updates(job)
                    changes = {
                        key: value
                        for key, value in desired.items()
                        if getattr(job, key) != value
                    }
                    if not changes:
                        stats.unchanged += 1
                        continue
                    stats.changed += 1
                    if not dry_run:
                        for key, value in changes.items():
                            setattr(job, key, value)
                except Exception:
                    stats.failed += 1
                    logger.exception("Reindex classification failed for job_id=%s", job.id)

            if dry_run:
                await session.rollback()
            else:
                # session_scope commits this batch on exit.
                stats.batches_committed += 1

        if progress is not None:
            progress(stats)

    stats.duration_seconds = round(monotonic() - started, 3)
    return stats
