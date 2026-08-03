"""Pydantic request/response models - the contract your frontend codes against."""

from __future__ import annotations

from datetime import datetime, timezone

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
    sources: list[str] | None = Field(
        default=None, description="Subset of enabled sources, e.g. ['naukri']"
    )
    queries: list[str] | None = Field(
        default=None, description="Override the built-in search catalogue"
    )
    query_limit: int | None = Field(
        default=None, ge=1, le=500, description="Cap the number of queries (useful for smoke tests)"
    )


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
