"""ORM models."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


_NORMALISE_RE = re.compile(r"[^a-z0-9]+")

# Wraps every skill so each one is delimited on both sides: |python|java|.
SKILL_FENCE = "|"


def skills_fence(skills: list | None) -> str:
    """Render a skill list as `|a|b|c|`, or "" when there are none.

    Both ends are closed so the first and last entries are delimited exactly
    like the middle ones - without that, '%|java|%' would miss a posting whose
    only skill is Java.
    """
    cleaned = [str(s).strip().lower() for s in (skills or []) if str(s).strip()]
    if not cleaned:
        return ""
    return SKILL_FENCE + SKILL_FENCE.join(cleaned) + SKILL_FENCE


def make_fingerprint(title: str, company: str, location: str = "") -> str:
    """Stable dedup key.

    Only the first location token is used because the same posting is listed as
    "Bengaluru", "Bengaluru, Karnataka" and "Bangalore/Bengaluru" across boards.
    """
    def norm(value: str) -> str:
        return _NORMALISE_RE.sub(" ", (value or "").lower()).strip()

    city = norm(location).split(" ")[0] if location else ""
    raw = f"{norm(title)}|{norm(company)}|{city}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def make_identity_fingerprint(
    source: str,
    external_id: str | None,
    canonical_url: str,
    dedup_key: str,
) -> str:
    """Prefer provider identity over the deliberately fuzzy cross-board key."""
    if external_id:
        raw = f"source:{source.strip().lower()}:{external_id.strip().lower()}"
    elif canonical_url:
        raw = f"url:{canonical_url.strip().lower()}"
    else:
        raw = f"composite:{dedup_key}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def build_search_blob(
    title: str,
    company: str,
    location: str,
    category: str,
    country: str | None,
    skills: list | None,
) -> str:
    """Build the portable lowercase search document from explicit values."""
    parts = [
        title or "",
        company or "",
        location or "",
        category or "",
        country or "",
        skills_fence(skills),
    ]
    return " ".join(part for part in parts if part).lower()


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(40), unique=True, index=True)

    # provenance
    source: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str | None] = mapped_column(String(128), index=True)
    apply_link: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text, default="", index=True)
    dedup_key: Mapped[str] = mapped_column(String(40), default="", index=True)
    source_ids: Mapped[list] = mapped_column(JSON, default=list)
    source_urls: Mapped[list] = mapped_column(JSON, default=list)
    discovered_profiles: Mapped[list] = mapped_column(JSON, default=list)
    discovered_queries: Mapped[list] = mapped_column(JSON, default=list)

    # core fields
    title: Mapped[str] = mapped_column(String(300), index=True)
    company: Mapped[str] = mapped_column(String(300), index=True)
    location: Mapped[str] = mapped_column(String(300), default="")
    description: Mapped[str] = mapped_column(Text, default="")

    # normalised experience
    experience_text: Mapped[str] = mapped_column(String(120), default="")
    experience_min: Mapped[int | None] = mapped_column(Integer, index=True)
    experience_max: Mapped[int | None] = mapped_column(Integer)

    # compensation (best effort - most Indian listings hide it)
    salary_text: Mapped[str] = mapped_column(String(200), default="")
    salary_min: Mapped[float | None] = mapped_column(Float)
    salary_max: Mapped[float | None] = mapped_column(Float)

    # enrichment
    skills: Mapped[list] = mapped_column(JSON, default=list)
    category: Mapped[str] = mapped_column(String(64), index=True, default="Other")
    categories: Mapped[list] = mapped_column(JSON, default=list)
    seniority: Mapped[str] = mapped_column(String(24), index=True, default="mid")
    work_mode: Mapped[str] = mapped_column(String(16), index=True, default="onsite")
    employment_type: Mapped[str] = mapped_column(String(16), index=True, default="unknown")
    degree_requirements: Mapped[list] = mapped_column(JSON, default=list)
    masters_match: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    education_requirement: Mapped[str] = mapped_column(
        String(16), default="not_stated", index=True
    )
    country: Mapped[str | None] = mapped_column(String(128), index=True)
    is_abroad: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    visa_sponsorship: Mapped[str] = mapped_column(
        String(16), default="unknown", index=True
    )
    work_authorization_required: Mapped[bool] = mapped_column(
        Boolean, default=False, index=True
    )
    relocation_support: Mapped[str] = mapped_column(
        String(16), default="unknown", index=True
    )

    # lifecycle
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    # Lowercase haystack for LIKE search. Free text spans the whole string;
    # skills are additionally pipe-delimited so they can be matched exactly.
    search_blob: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (
        Index("ix_jobs_active_posted", "is_active", "posted_at"),
        Index("ix_jobs_category_active", "category", "is_active"),
        Index("ix_jobs_source_active", "source", "is_active"),
        Index("ix_jobs_source_external", "source", "external_id"),
        Index(
            "ix_jobs_international_masters_education_posted",
            "is_active",
            "is_abroad",
            "masters_match",
            "education_requirement",
            "posted_at",
        ),
    )

    def build_search_blob(self) -> str:
        """Free-text haystack, with skills fenced for exact matching.

        Skills are emitted as `|python|java|` rather than plain words because a
        bare LIKE '%java%' also matches "javascript", and LIKE '%r%' matches
        essentially every posting. Fencing lets the skill filter ask for
        '%|java|%' and get only real Java roles, while free-text search still
        works over the whole string.
        """
        return build_search_blob(
            self.title,
            self.company,
            self.location,
            self.category,
            self.country,
            self.skills,
        )


class ScrapeRun(Base):
    __tablename__ = "scrape_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), index=True, default="pending")
    trigger: Mapped[str] = mapped_column(String(16), default="manual")

    sources: Mapped[list] = mapped_column(JSON, default=list)
    queries_total: Mapped[int] = mapped_column(Integer, default=0)
    queries_done: Mapped[int] = mapped_column(Integer, default=0)

    jobs_seen: Mapped[int] = mapped_column(Integer, default=0)
    jobs_new: Mapped[int] = mapped_column(Integer, default=0)
    jobs_updated: Mapped[int] = mapped_column(Integer, default=0)
    jobs_rejected: Mapped[int] = mapped_column(Integer, default=0)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float | None] = mapped_column(Float)

    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index(
            "ux_scrape_runs_single_running",
            "status",
            unique=True,
            sqlite_where=text("status = 'running'"),
            postgresql_where=text("status = 'running'"),
        ),
    )

    @property
    def progress(self) -> float:
        if not self.queries_total:
            return 0.0
        return round(min(self.queries_done / self.queries_total, 1.0) * 100, 1)
