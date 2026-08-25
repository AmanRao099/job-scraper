"""Turn a `RawJob` into the row we store, applying quality filters.

Kept separate from both the sources and the persistence layer so it can be unit
tested against fixtures without touching the network or the database.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlsplit

from app.classification import classify_international, parse_qualifications
from app.config import settings
from app.models import make_fingerprint, make_identity_fingerprint
from app.sources.base import RawJob
from app.taxonomy import (
    categorize,
    detect_seniority,
    detect_employment_type,
    detect_work_mode,
    extract_skills,
    has_strong_tech_title,
    is_tech_job,
    resolve_experience,
    secondary_categories,
)
from app.utils import clean_text, parse_salary, strip_tracking, utcnow

logger = logging.getLogger(__name__)

# Reasons a posting is dropped - surfaced in run stats so coverage regressions
# are visible instead of silent.
REJECT_INCOMPLETE = "incomplete"
REJECT_NOT_TECH = "not_tech"
REJECT_EXPERIENCE = "experience_too_high"
REJECT_STALE = "posting_too_old"
REJECT_EMPTY = "no_content"
REJECT_DEAD = "dead_listing"
REJECT_PARSING = "parsing_failure"

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
    canonical_url: str
    dedup_key: str
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
    employment_type: str = "unknown"
    source_ids: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    discovered_profiles: list[str] = field(default_factory=list)
    discovered_queries: list[str] = field(default_factory=list)
    degree_requirements: list[str] = field(default_factory=list)
    masters_match: bool = False
    education_requirement: str = "not_stated"
    country: str | None = None
    is_abroad: bool = False
    visa_sponsorship: str = "unknown"
    work_authorization_required: bool = False
    relocation_support: str = "unknown"
    posted_at: object | None = None

    def as_row(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "source": self.source,
            "external_id": self.external_id,
            "apply_link": self.apply_link,
            "canonical_url": self.canonical_url,
            "dedup_key": self.dedup_key,
            "source_ids": self.source_ids,
            "source_urls": self.source_urls,
            "discovered_profiles": self.discovered_profiles,
            "discovered_queries": self.discovered_queries,
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
            "employment_type": self.employment_type,
            "degree_requirements": self.degree_requirements,
            "masters_match": self.masters_match,
            "education_requirement": self.education_requirement,
            "country": self.country,
            "is_abroad": self.is_abroad,
            "visa_sponsorship": self.visa_sponsorship,
            "work_authorization_required": self.work_authorization_required,
            "relocation_support": self.relocation_support,
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


def normalize(
    raw: RawJob, *, allow_any_experience: bool = False, profile_key: str | None = None,
    max_posting_age_days: int | None = None,
) -> tuple[NormalizedJob | None, str | None]:
    """Return (job, None) if the posting is kept, else (None, reject_reason)."""
    if not raw.is_usable():
        return None, REJECT_INCOMPLETE
    canonical_url = strip_tracking(raw.apply_link)
    apply_url = urlsplit(canonical_url)
    if apply_url.scheme not in {"http", "https"} or not apply_url.netloc:
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

    if (
        not allow_any_experience
        and exp_min is not None
        and exp_min > settings.max_experience_years
    ):
        return None, REJECT_EXPERIENCE

    seniority = detect_seniority(title, experience_text, exp_min)
    if seniority == "intern" and not settings.include_internships:
        return None, REJECT_EXPERIENCE
    if (
        not allow_any_experience
        and seniority == "lead"
        and (exp_min is None or exp_min > settings.max_experience_years)
    ):
        return None, REJECT_EXPERIENCE

    freshness_days = (
        settings.max_posting_age_days
        if max_posting_age_days is None
        else max_posting_age_days
    )
    if freshness_days and raw.posted_at is not None:
        cutoff = utcnow() - timedelta(days=freshness_days)
        if raw.posted_at < cutoff:
            return None, REJECT_STALE

    salary_text = clean_text(raw.salary_text, 200)
    salary_min, salary_max = parse_salary(salary_text)
    qualification = parse_qualifications(description)
    international = classify_international(location, description)

    dedup_key = make_fingerprint(title, company, location)
    source_id = f"{raw.source}:{raw.external_id}" if raw.external_id else ""
    return (
        NormalizedJob(
            fingerprint=make_identity_fingerprint(
                raw.source, raw.external_id, canonical_url, dedup_key
            ),
            source=raw.source,
            external_id=raw.external_id,
            apply_link=canonical_url,
            canonical_url=canonical_url,
            dedup_key=dedup_key,
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
            employment_type=detect_employment_type(title, description),
            source_ids=[source_id] if source_id else [],
            source_urls=[canonical_url] if canonical_url else [],
            discovered_profiles=[profile_key] if profile_key else [],
            discovered_queries=[raw.discovered_query] if raw.discovered_query else [],
            degree_requirements=qualification.degree_requirements,
            masters_match=qualification.masters_match,
            education_requirement=qualification.education_requirement,
            country=international.country,
            is_abroad=international.is_abroad,
            visa_sponsorship=international.visa_sponsorship,
            work_authorization_required=international.work_authorization_required,
            relocation_support=international.relocation_support,
            posted_at=raw.posted_at,
        ),
        None,
    )


def _richness(job: NormalizedJob) -> tuple[int, int, int, int, int]:
    """Prefer records that are more useful after deduplication."""
    return (
        bool(job.description),
        len(job.description),
        len(job.skills),
        bool(job.external_id),
        bool(job.posted_at),
    )


def deduplicate_jobs(jobs: list[NormalizedJob]) -> list[NormalizedJob]:
    """Collapse source and cross-source duplicates in O(n) expected time."""
    kept: list[NormalizedJob] = []
    by_identity: dict[str, int] = {}
    by_composite: dict[str, int] = {}

    for job in jobs:
        index = by_identity.get(job.fingerprint)
        if index is None:
            candidate = by_composite.get(job.dedup_key)
            if candidate is not None:
                other = kept[candidate]
                distinct_same_source_ids = (
                    other.source == job.source
                    and other.external_id
                    and job.external_id
                    and other.external_id != job.external_id
                )
                if not distinct_same_source_ids:
                    index = candidate

        if index is None:
            index = len(kept)
            kept.append(job)
            by_identity[job.fingerprint] = index
            by_composite.setdefault(job.dedup_key, index)
            continue

        existing = kept[index]
        preferred, secondary = (
            (job, existing) if _richness(job) > _richness(existing) else (existing, job)
        )
        preferred.skills = sorted(set(preferred.skills) | set(secondary.skills))
        preferred.categories = sorted(set(preferred.categories) | set(secondary.categories))
        preferred.source_ids = sorted(set(preferred.source_ids) | set(secondary.source_ids))
        preferred.source_urls = sorted(set(preferred.source_urls) | set(secondary.source_urls))
        preferred.discovered_profiles = sorted(
            set(preferred.discovered_profiles) | set(secondary.discovered_profiles)
        )
        preferred.discovered_queries = sorted(
            set(preferred.discovered_queries) | set(secondary.discovered_queries)
        )
        if preferred.posted_at is None or (
            secondary.posted_at is not None and secondary.posted_at > preferred.posted_at
        ):
            preferred.posted_at = secondary.posted_at
        if preferred.source != secondary.source:
            preferred.fingerprint = preferred.dedup_key
        kept[index] = preferred
        by_identity[preferred.fingerprint] = index

    return kept


def normalize_many(
    raws: list[RawJob], *, allow_any_experience: bool = False,
    profile_key: str | None = None,
    max_posting_age_days: int | None = None,
) -> tuple[list[NormalizedJob], dict[str, int]]:
    """Normalise a batch with O(n) identity maps and isolated bad records."""
    candidates: list[NormalizedJob] = []
    rejected: dict[str, int] = {}

    for raw in raws:
        try:
            job, reason = normalize(
                raw,
                allow_any_experience=allow_any_experience,
                profile_key=profile_key,
                max_posting_age_days=max_posting_age_days,
            )
        except Exception:
            logger.exception("Could not normalize source=%s external_id=%s", raw.source, raw.external_id)
            rejected[REJECT_PARSING] = rejected.get(REJECT_PARSING, 0) + 1
            continue
        if job is None:
            rejected[reason or "unknown"] = rejected.get(reason or "unknown", 0) + 1
            continue

        candidates.append(job)

    return deduplicate_jobs(candidates), rejected
