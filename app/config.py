"""Application settings.

Everything is environment-driven so the same image can run locally and in
production without code changes. See `.env.example` for the full list.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from pydantic import field_validator
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
    environment: str = "development"
    log_level: str = "INFO"

    # ----------------------------------------------------------- database
    # Swap for "postgresql+asyncpg://user:pass@host/db" to move off SQLite.
    database_url: str = f"sqlite+aiosqlite:///{(DATA_DIR / 'jobs.db').as_posix()}"

    # ---------------------------------------------------------------- api
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    # When set, mutating endpoints (/scrape/run, /jobs/purge) require
    # `X-Admin-Token`. Leave empty in local dev to disable the check.
    admin_token: str = ""
    cors_origins: CsvList = ["http://localhost:5173", "http://127.0.0.1:5173"]
    # Escape hatch for frontends whose hostname is not fixed - preview deploys
    # get a fresh subdomain per branch, which no static list can cover.
    # Anchored automatically; e.g. https://.*\.my-app\.vercel\.app
    cors_origin_regex: str = ""
    default_page_size: int = 25
    max_page_size: int = 200

    # ---------------------------------------------------------- scheduler
    scheduler_enabled: bool = True
    scrape_interval_hours: float = 6.0
    scrape_on_startup: bool = False
    # How long a run may sit at "running" before another process may declare it
    # dead. Must exceed the longest possible scrape: when the scraper runs
    # elsewhere (GitHub Actions), the API cannot tell a live run from an
    # abandoned one, and clearing a live one also releases its lock.
    orphan_run_after_minutes: int = 90
    # Jobs not seen in a scrape for this long are marked inactive.
    stale_after_days: int = 21
    # Inactive jobs older than this are deleted outright.
    purge_after_days: int = 60

    # ------------------------------------------------------------ scraping
    sources_enabled: CsvList = ["naukri", "linkedin"]
    # Concurrent outbound HTTP requests across the whole pipeline.
    http_concurrency: int = 12
    http_timeout: float = 25.0
    http_retries: int = 3
    # Politeness jitter (seconds) applied per request.
    request_delay_min: float = 0.15
    request_delay_max: float = 0.6

    naukri_pages: int = 3          # 100 results per page
    naukri_page_size: int = 100
    linkedin_pages: int = 4        # 25 results per page
    linkedin_fetch_descriptions: bool = True

    # Browser rendering. Required for Naukri (its JSON API is recaptcha-gated
    # and its search page is client-rendered); optional fallback for LinkedIn.
    playwright_fallback: bool = True
    playwright_headless: bool = True
    # "chromium" selects Chrome's *new* headless mode. The default bundled
    # headless shell is trivially bot-detected and gets served an empty page.
    browser_channel: str = "chromium"
    browser_concurrency: int = 4
    # Try Naukri's JSON API once per run before falling back to rendering.
    naukri_try_api: bool = True

    # --------------------------------------------------------- filtering
    location: str = "India"
    linkedin_geo_id: str = "102713980"     # India
    # Keep jobs whose *minimum* required experience is <= this.
    max_experience_years: int = 3
    include_internships: bool = True
    # Drop postings older than this many days (0 disables the check).
    max_posting_age_days: int = 45
    min_description_chars: int = 60

    @field_validator("cors_origins", "sources_enabled", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> list[str]:
        return _as_list(value)

    @field_validator("database_url", mode="before")
    @classmethod
    def _fix_database_url(cls, value: object) -> object:
        return _normalize_database_url(value) if isinstance(value, str) else value

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
            "ssl": "require",
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
