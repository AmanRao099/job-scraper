"""Application settings.

Everything is environment-driven so the same image can run locally and in
production without code changes. See `.env.example` for the full list.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


def _as_list(value: object) -> list[str]:
    """Accept either a real list or a comma-separated env string."""
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


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
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    default_page_size: int = 25
    max_page_size: int = 200

    # ---------------------------------------------------------- scheduler
    scheduler_enabled: bool = True
    scrape_interval_hours: float = 6.0
    scrape_on_startup: bool = False
    # Jobs not seen in a scrape for this long are marked inactive.
    stale_after_days: int = 21
    # Inactive jobs older than this are deleted outright.
    purge_after_days: int = 60

    # ------------------------------------------------------------ scraping
    sources_enabled: list[str] = ["naukri", "linkedin"]
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

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return Settings()


settings = get_settings()
