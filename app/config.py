"""Application settings.

Everything is environment-driven so the same image can run locally and in
production without code changes. See `.env.example` for the full list.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# pydantic-settings JSON-decodes env values for list-typed fields *before* any
# validator runs, so CORS_ORIGINS=https://a.com,https://b.com would raise
# SettingsError rather than reaching `_split_csv`. NoDecode hands the validator
# the raw string instead. Comma-separated is the only form a hosting
# dashboard's single-line env var field can reasonably express.
CsvList = Annotated[list[str], NoDecode]

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

# libpq accepts these in the connection string; asyncpg does not and raises
# TypeError on an unexpected keyword. Managed Postgres providers (Neon, Supabase,
# Render) all hand out URLs containing them, so strip and translate instead.
_LIBPQ_ONLY_PARAMS = {"sslmode", "channel_binding", "options", "target_session_attrs"}


def _as_list(value: object) -> list[str]:
    """Accept a real list, a comma-separated env string, or a JSON array."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        # NoDecode turns off pydantic's JSON decoding for these fields, so
        # handle the array form here for anyone whose env already used it.
        if text.startswith("[") and text.endswith("]"):
            try:
                return _as_list(json.loads(text))
            except json.JSONDecodeError:
                pass
        return [item.strip() for item in text.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _normalize_database_url(url: str) -> str:
    """Make a provider-issued Postgres URL usable by SQLAlchemy + asyncpg.

    Hosting dashboards give you a libpq URL (`postgres://…?sslmode=require`).
    Pasting that verbatim is the single most common deploy failure, so rewrite
    it here rather than making every environment get the syntax right.
    """
    if not url.startswith(("postgres://", "postgresql://", "postgresql+")):
        return url

    parts = urlsplit(url)
    scheme = parts.scheme
    if scheme in ("postgres", "postgresql"):
        scheme = "postgresql+asyncpg"

    if scheme == "postgresql+asyncpg":
        kept = [(k, v) for k, v in parse_qsl(parts.query) if k not in _LIBPQ_ONLY_PARAMS]
        parts = parts._replace(query=urlencode(kept))

    return urlunsplit(parts._replace(scheme=scheme))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- app
    app_name: str = "Tech Job Extraction API"
    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # ----------------------------------------------------------- database
    # Swap for "postgresql+asyncpg://user:pass@host/db" to move off SQLite.
    database_url: str = f"sqlite+aiosqlite:///{(DATA_DIR / 'jobs.db').as_posix()}"
    database_ssl_mode: Literal[
        "disable", "prefer", "require", "verify-ca", "verify-full"
    ] = "require"

    # ---------------------------------------------------------------- api
    api_host: str = "0.0.0.0"
    api_port: int = Field(default=8000, ge=1, le=65535)
    # When set, mutating endpoints (/scrape/run, /jobs/purge) require
    # `X-Admin-Token`. Leave empty in local dev to disable the check.
    admin_token: str = ""
    cors_origins: CsvList = ["http://localhost:5173", "http://127.0.0.1:5173"]
    # Escape hatch for frontends whose hostname is not fixed - preview deploys
    # get a fresh subdomain per branch, which no static list can cover.
    # Anchored automatically; e.g. https://.*\.my-app\.vercel\.app
    cors_origin_regex: str = ""
    allowed_hosts: CsvList = ["localhost", "127.0.0.1", "test", "testserver"]
    default_page_size: int = Field(default=25, ge=1, le=500)
    max_page_size: int = Field(default=200, ge=1, le=500)

    # ---------------------------------------------------------- scheduler
    scheduler_enabled: bool = True
    scrape_interval_hours: float = Field(default=6.0, gt=0, le=168)
    scrape_on_startup: bool = False
    shutdown_grace_seconds: float = Field(default=30.0, ge=1, le=60)
    # How long a run may sit at "running" before another process may declare it
    # dead. Must exceed the longest possible scrape: when the scraper runs
    # elsewhere (GitHub Actions), the API cannot tell a live run from an
    # abandoned one, and clearing a live one also releases its lock.
    orphan_run_after_minutes: int = Field(default=90, ge=1, le=1440)
    # Jobs not seen in a scrape for this long are marked inactive.
    stale_after_days: int = Field(default=21, ge=1, le=3650)
    # Inactive jobs older than this are deleted outright.
    purge_after_days: int = Field(default=60, ge=2, le=3650)

    # ------------------------------------------------------------ scraping
    sources_enabled: CsvList = ["naukri", "linkedin"]
    # Concurrent outbound HTTP requests across the whole pipeline.
    http_concurrency: int = Field(default=12, ge=1, le=100)
    http_connect_timeout: float = Field(default=10.0, gt=0, le=120)
    http_read_timeout: float = Field(default=25.0, gt=0, le=300)
    # Backward-compatible alias for deployments that already set HTTP_TIMEOUT.
    # When present it overrides HTTP_READ_TIMEOUT.
    http_timeout: float | None = Field(default=None, gt=0, le=300)
    http_write_timeout: float = Field(default=10.0, gt=0, le=120)
    http_pool_timeout: float = Field(default=10.0, gt=0, le=120)
    http_overall_timeout: float = Field(default=60.0, gt=0, le=600)
    http_retries: int = Field(default=3, ge=1, le=10)
    http_backoff_max: float = Field(default=20.0, gt=0, le=300)
    http_max_response_bytes: int = Field(default=5_000_000, ge=100_000, le=50_000_000)
    http_user_agent: str = (
        "Mozilla/5.0 (compatible; TechJobExtractor/2.0; public-job-board-client)"
    )
    # Politeness jitter (seconds) applied per request.
    request_delay_min: float = Field(default=0.15, ge=0, le=30)
    request_delay_max: float = Field(default=0.6, ge=0, le=60)

    naukri_pages: int = Field(default=3, ge=1, le=25)
    naukri_page_size: int = Field(default=100, ge=20, le=100)
    naukri_query_concurrency: int = Field(default=4, ge=1, le=20)
    linkedin_pages: int = Field(default=4, ge=1, le=25)
    linkedin_query_concurrency: int = Field(default=4, ge=1, le=20)
    linkedin_fetch_descriptions: bool = True

    # Browser rendering. Required for Naukri (its JSON API is recaptcha-gated
    # and its search page is client-rendered); optional fallback for LinkedIn.
    playwright_fallback: bool = True
    playwright_headless: bool = True
    # "chromium" selects Chrome's *new* headless mode. The default bundled
    # headless shell is trivially bot-detected and gets served an empty page.
    browser_channel: str = "chromium"
    browser_concurrency: int = Field(default=4, ge=1, le=12)
    # Try Naukri's JSON API once per run before falling back to rendering.
    naukri_try_api: bool = True

    # --------------------------------------------------------- filtering
    location: str = "India"
    linkedin_geo_id: str = "102713980"     # India
    # Keep jobs whose *minimum* required experience is <= this.
    max_experience_years: int = Field(default=3, ge=0, le=50)
    include_internships: bool = True
    # Drop postings older than this many days (0 disables the check).
    max_posting_age_days: int = Field(default=45, ge=0, le=3650)
    min_description_chars: int = Field(default=60, ge=0, le=5000)
    reindex_batch_size: int = Field(default=500, ge=10, le=5000)

    @field_validator("cors_origins", "sources_enabled", "allowed_hosts", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> list[str]:
        return _as_list(value)

    @field_validator("database_url", mode="before")
    @classmethod
    def _fix_database_url(cls, value: object) -> object:
        return _normalize_database_url(value) if isinstance(value, str) else value

    @field_validator("environment", mode="before")
    @classmethod
    def _lower_environment(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("cors_origin_regex")
    @classmethod
    def _anchor_cors_regex(cls, value: str) -> str:
        pattern = value.strip()
        if not pattern:
            return ""
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"Invalid CORS_ORIGIN_REGEX: {exc}") from exc
        if not pattern.startswith("^"):
            pattern = f"^(?:{pattern})"
        if not pattern.endswith("$"):
            pattern = f"{pattern}$"
        return pattern

    @model_validator(mode="after")
    def _validate_ranges(self) -> "Settings":
        if self.request_delay_min > self.request_delay_max:
            raise ValueError("REQUEST_DELAY_MIN cannot exceed REQUEST_DELAY_MAX")
        if self.stale_after_days >= self.purge_after_days:
            raise ValueError("PURGE_AFTER_DAYS must be greater than STALE_AFTER_DAYS")
        if not self.sources_enabled:
            raise ValueError("SOURCES_ENABLED must contain at least one source")
        if self.default_page_size > self.max_page_size:
            raise ValueError("DEFAULT_PAGE_SIZE cannot exceed MAX_PAGE_SIZE")
        return self

    @property
    def effective_http_read_timeout(self) -> float:
        return self.http_timeout or self.http_read_timeout

    def validate_api_startup(self) -> None:
        """Fail closed for unsafe production API configuration.

        CLI scrapers may use ``ENVIRONMENT=production`` without exposing an HTTP
        server, so these checks intentionally run from the API lifespan rather
        than during module import.
        """
        if self.environment.strip().lower() != "production":
            return
        if len(self.admin_token) < 24:
            raise RuntimeError(
                "ADMIN_TOKEN must contain at least 24 characters in production"
            )
        if not self.cors_origins and not self.cors_origin_regex:
            raise RuntimeError(
                "CORS_ORIGINS or CORS_ORIGIN_REGEX must be configured in production"
            )
        if "*" in self.cors_origins:
            raise RuntimeError("Wildcard CORS origins are not allowed in production")
        if not self.allowed_hosts or "*" in self.allowed_hosts:
            raise RuntimeError("Explicit ALLOWED_HOSTS are required in production")
        if self.is_postgres and self.database_ssl_mode != "verify-full":
            raise RuntimeError(
                "DATABASE_SSL_MODE=verify-full is required for PostgreSQL in production"
            )

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_postgres(self) -> bool:
        return self.database_url.startswith("postgresql")

    @property
    def is_pooled_postgres(self) -> bool:
        """True for a PgBouncer endpoint (Neon and Supabase both mark it '-pooler')."""
        return self.is_postgres and "-pooler." in self.database_url

    @property
    def db_connect_args(self) -> dict:
        if self.is_sqlite:
            return {"timeout": 30}
        if not self.is_postgres:
            return {}

        # Every free managed Postgres requires TLS. asyncpg spells it `ssl`,
        # and "require" encrypts without verifying the CA - which is what
        # `sslmode=require` in the provider's own URL already meant.
        args: dict = {
            "ssl": self.database_ssl_mode,
            "server_settings": {"application_name": self.app_name},
        }

        if self.is_pooled_postgres:
            # PgBouncer in transaction mode hands successive queries to
            # different backends, so asyncpg's numerically-named prepared
            # statements collide across clients. The failure is intermittent -
            # DuplicatePreparedStatementError under load, nothing at all on a
            # quiet service - so make it structurally impossible rather than
            # relying on the direct endpoint being configured.
            args["prepared_statement_cache_size"] = 0
            args["prepared_statement_name_func"] = lambda: f"__asyncpg_{uuid4()}__"

        return args


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return Settings()


settings = get_settings()
