"""Pydantic request/response models - the contract your frontend codes against."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite discards tzinfo on write. Everything we store is UTC, so tag it
    back on before serialising - otherwise browsers read the timestamp as local
    time and every posting looks hours off."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class UTCModel(BaseModel):
    @field_validator("*", mode="after")
    @classmethod
    def _normalise_datetimes(cls, value: object) -> object:
        return _as_utc(value) if isinstance(value, datetime) else value


class JobOut(UTCModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    title: str
    company: str
    location: str
    apply_link: str
    category: str
    categories: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    seniority: str
    work_mode: str
    employment_type: str = "unknown"
    degree_requirements: list[str] = Field(default_factory=list)
    masters_match: bool = False
    education_requirement: str = "not_stated"
    country: str | None = None
    is_abroad: bool = False
    visa_sponsorship: str = "unknown"
    work_authorization_required: bool = False
    relocation_support: str = "unknown"
    experience_text: str
    experience_min: int | None = None
    experience_max: int | None = None
    salary_text: str
    salary_min: float | None = None
    salary_max: float | None = None
    posted_at: datetime | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    is_active: bool


class JobDetail(JobOut):
    description: str = ""
    external_id: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)
    discovered_profiles: list[str] = Field(default_factory=list)
    discovered_queries: list[str] = Field(default_factory=list)


class PageMeta(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int
    has_next: bool
    has_prev: bool


class JobPage(BaseModel):
    items: list[JobOut]
    meta: PageMeta


class FacetValue(BaseModel):
    value: str
    count: int


class FiltersOut(BaseModel):
    """Everything the frontend needs to build its filter UI in one call."""

    categories: list[FacetValue]
    sources: list[FacetValue]
    seniorities: list[FacetValue]
    work_modes: list[FacetValue]
    locations: list[FacetValue]
    companies: list[FacetValue]
    skills: list[FacetValue]
    countries: list[FacetValue]
    education_requirements: list[FacetValue]
    visa_sponsorships: list[FacetValue]
    relocation_supports: list[FacetValue]
    employment_types: list[FacetValue]


class StatsOut(BaseModel):
    total_jobs: int
    active_jobs: int
    jobs_added_today: int
    by_category: list[FacetValue]
    by_source: list[FacetValue]
    by_seniority: list[FacetValue]
    top_skills: list[FacetValue]
    last_run: ScrapeRunOut | None = None


class ScrapeRequest(BaseModel):
    sources: list[Annotated[str, Field(min_length=1, max_length=32)]] | None = Field(
        default=None, max_length=10,
        description="Subset of enabled sources, e.g. ['naukri']"
    )
    queries: list[Annotated[str, Field(min_length=1, max_length=120)]] | None = Field(
        default=None, max_length=100,
        description="Override the built-in search catalogue"
    )
    query_limit: int | None = Field(
        default=None, ge=1, le=500, description="Cap the number of queries (useful for smoke tests)"
    )
    profile: str | None = Field(
        default=None,
        description=(
            "Run a targeted profile instead of the nationwide sweep, e.g. "
            "'bangalore-fresher-startups' or 'worldwide-masters-tech'. "
            "See GET /scrape/profiles."
        ),
    )


class ScrapeProfileOut(BaseModel):
    key: str
    label: str
    description: str
    location: str
    queries: list[str]
    query_count: int
    max_experience_years: int | None
    require_startup: bool
    allow_any_experience: bool = False
    require_abroad: bool = False
    require_masters: bool = False
    allowed_sources: list[str] = Field(default_factory=list)
    require_tech: bool = True
    freshness_days: int | None = None
    max_pages_by_source: dict[str, int] = Field(default_factory=dict)
    max_results: int | None = None
    deduplication_scope: str = "global"
    deactivate_unseen: bool = False


class ScrapeRunOut(UTCModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: str
    trigger: str
    sources: list[str] = Field(default_factory=list)
    queries_total: int
    queries_done: int
    progress: float
    jobs_seen: int
    jobs_new: int
    jobs_updated: int
    jobs_rejected: int
    started_at: datetime
    finished_at: datetime | None = None
    duration_seconds: float | None = None
    stats: dict = Field(default_factory=dict)
    error: str | None = None


class ScrapeStarted(BaseModel):
    run_id: int
    status: str = "running"
    stream_url: str
    status_url: str


class HealthOut(BaseModel):
    status: str
    database: bool
    scheduler: bool
    version: str
    environment: str


class MessageOut(BaseModel):
    message: str
    detail: dict | None = None


StatsOut.model_rebuild()
