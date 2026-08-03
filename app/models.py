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
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


_NORMALISE_RE = re.compile(r"[^a-z0-9]+")


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


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(40), unique=True, index=True)

    # provenance
    source: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str | None] = mapped_column(String(128), index=True)
    apply_link: Mapped[str] = mapped_column(Text)

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

    # lifecycle
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    # lowercase haystack for LIKE search (title + company + location + skills)
    search_blob: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (
        Index("ix_jobs_active_posted", "is_active", "posted_at"),
        Index("ix_jobs_category_active", "category", "is_active"),
        Index("ix_jobs_source_active", "source", "is_active"),
    )

    def build_search_blob(self) -> str:
        parts = [
            self.title or "",
            self.company or "",
            self.location or "",
            " ".join(self.skills or []),
            self.category or "",
        ]
        return " ".join(parts).lower()


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

    @property
    def progress(self) -> float:
        if not self.queries_total:
            return 0.0
        return round(min(self.queries_done / self.queries_total, 1.0) * 100, 1)
