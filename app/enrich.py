"""Turn a `RawJob` into the row we store, applying quality filters.

Kept separate from both the sources and the persistence layer so it can be unit
tested against fixtures without touching the network or the database.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from app.config import settings
from app.models import make_fingerprint
from app.sources.base import RawJob
from app.taxonomy import (
    categorize,
    detect_seniority,
    detect_work_mode,
    extract_skills,
    has_strong_tech_title,
    is_tech_job,
    resolve_experience,
    secondary_categories,
)
from app.utils import clean_text, parse_salary, utcnow

logger = logging.getLogger(__name__)

# Reasons a posting is dropped - surfaced in run stats so coverage regressions
# are visible instead of silent.
REJECT_INCOMPLETE = "incomplete"
REJECT_NOT_TECH = "not_tech"
REJECT_EXPERIENCE = "experience_too_high"
REJECT_STALE = "posting_too_old"
REJECT_EMPTY = "no_content"
REJECT_DEAD = "dead_listing"

DEAD_SIGNALS = (
    "no longer accepting applications",
    "this job is no longer available",
    "job not found",
    "this job does not exist",
    "the job you are looking for is not available",
    "position has been filled",
    "job expired",
    "job has expired",
)


@dataclass(slots=True)
class NormalizedJob:
    fingerprint: str
    source: str
    external_id: str | None
    apply_link: str
    title: str
    company: str
    location: str
    description: str
    experience_text: str
    experience_min: int | None
    experience_max: int | None
    salary_text: str
    salary_min: float | None
    salary_max: float | None
    skills: list[str] = field(default_factory=list)
    category: str = "Other"
    categories: list[str] = field(default_factory=list)
    seniority: str = "mid"
    work_mode: str = "onsite"
    posted_at: object | None = None

    def as_row(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "source": self.source,
            "external_id": self.external_id,
            "apply_link": self.apply_link,
            "title": self.title,
            "company": self.company,
            "location": self.location,
            "description": self.description,
            "experience_text": self.experience_text,
            "experience_min": self.experience_min,
            "experience_max": self.experience_max,
            "salary_text": self.salary_text,
            "salary_min": self.salary_min,
            "salary_max": self.salary_max,
            "skills": self.skills,
            "category": self.category,
            "categories": self.categories,
            "seniority": self.seniority,
            "work_mode": self.work_mode,
            "posted_at": self.posted_at,
        }


def looks_dead(text: str) -> bool:
    """Cheap dead-listing check against text we already have.

    The old version fetched every job page a second time just to run this. We
    now run it over the description the source already returned - same signal,
    zero extra requests.
    """
    if not text:
        return False
    lowered = text[:4000].lower()
    return any(signal in lowered for signal in DEAD_SIGNALS)


def normalize(raw: RawJob) -> tuple[NormalizedJob | None, str | None]:
    """Return (job, None) if the posting is kept, else (None, reject_reason)."""
    if not raw.is_usable():
        return None, REJECT_INCOMPLETE

    title = clean_text(raw.title, 300)
    company = clean_text(raw.company, 300)
    location = clean_text(raw.location, 300)
    description = clean_text(raw.description, 20000)

    if looks_dead(description):
        return None, REJECT_DEAD

    declared = [clean_text(s) for s in raw.declared_skills if clean_text(s)]
    skills = extract_skills(title, description, " ".join(declared))

    # Recruiter-supplied tags that our taxonomy does not know are still useful
    # signal for the frontend's skill filter, so keep a few verbatim.
    known_lower = {s.lower() for s in skills}
    for tag in declared[:12]:
        if len(skills) >= 40:
            break
        if tag.lower() not in known_lower and 1 < len(tag) <= 30:
            skills.append(tag.title())
            known_lower.add(tag.lower())

    if not is_tech_job(title, description, skills):
        return None, REJECT_NOT_TECH

    # A posting with no description is still worth serving when its title alone
    # identifies the role - the frontend links out to the board for the full
    # text. Only drop the genuinely contentless ones. Running this *after* the
    # tech filter is deliberate: running it first discarded every posting whose
    # description fetch had been rate limited.
    if (
        len(description) < settings.min_description_chars
        and not declared
        and not skills
        and not has_strong_tech_title(title)
    ):
        return None, REJECT_EMPTY

    experience_text = clean_text(raw.experience_text, 120)
    exp_min, exp_max = resolve_experience(experience_text, title, description)

    if exp_min is not None and exp_min > settings.max_experience_years:
        return None, REJECT_EXPERIENCE

    seniority = detect_seniority(title, experience_text, exp_min)
    if seniority == "intern" and not settings.include_internships:
        return None, REJECT_EXPERIENCE
    if seniority == "lead" and (exp_min is None or exp_min > settings.max_experience_years):
        return None, REJECT_EXPERIENCE

    if settings.max_posting_age_days and raw.posted_at is not None:
        cutoff = utcnow() - timedelta(days=settings.max_posting_age_days)
        if raw.posted_at < cutoff:
            return None, REJECT_STALE

    salary_text = clean_text(raw.salary_text, 200)
    salary_min, salary_max = parse_salary(salary_text)

    return (
        NormalizedJob(
            fingerprint=make_fingerprint(title, company, location),
            source=raw.source,
            external_id=raw.external_id,
            apply_link=raw.apply_link,
            title=title,
            company=company,
            location=location,
            description=description,
            experience_text=experience_text,
            experience_min=exp_min,
            experience_max=exp_max,
            salary_text=salary_text,
            salary_min=salary_min,
            salary_max=salary_max,
            skills=skills,
            category=categorize(title, description, skills),
            categories=secondary_categories(title, description, skills),
            seniority=seniority,
            work_mode=detect_work_mode(location, title, description),
            posted_at=raw.posted_at,
        ),
        None,
    )


def normalize_many(raws: list[RawJob]) -> tuple[list[NormalizedJob], dict[str, int]]:
    """Normalise a batch, collapsing duplicates and counting rejections."""
    kept: dict[str, NormalizedJob] = {}
    rejected: dict[str, int] = {}

    for raw in raws:
        job, reason = normalize(raw)
        if job is None:
            rejected[reason or "unknown"] = rejected.get(reason or "unknown", 0) + 1
            continue

        existing = kept.get(job.fingerprint)
        if existing is None:
            kept[job.fingerprint] = job
            continue

        # Same posting from two boards / two queries: keep the richer record.
        if len(job.description) > len(existing.description):
            kept[job.fingerprint] = job

    return list(kept.values()), rejected
