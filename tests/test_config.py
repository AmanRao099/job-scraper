"""Connection-string handling.

Every deploy pastes a URL straight out of a hosting dashboard, and libpq syntax
that asyncpg rejects is the failure mode that only shows up in production.
"""

import pytest

from app.config import Settings, _normalize_database_url


@pytest.mark.parametrize(
    "given, expected",
    [
        # Heroku-style scheme, still handed out by several providers.
        ("postgres://u:p@host/db", "postgresql+asyncpg://u:p@host/db"),
        ("postgresql://u:p@host:5432/db", "postgresql+asyncpg://u:p@host:5432/db"),
        # An explicit driver is left alone.
        ("postgresql+asyncpg://u:p@host/db", "postgresql+asyncpg://u:p@host/db"),
        # asyncpg raises TypeError on these; they must not survive.
        (
            "postgresql://u:p@ep-a.neon.tech/neondb?sslmode=require&channel_binding=require",
            "postgresql+asyncpg://u:p@ep-a.neon.tech/neondb",
        ),
        # SQLite is untouched.
        ("sqlite+aiosqlite:///./data/jobs.db", "sqlite+aiosqlite:///./data/jobs.db"),
    ],
)
def test_normalize_database_url(given, expected):
    assert _normalize_database_url(given) == expected


def test_password_special_characters_survive():
    url = _normalize_database_url("postgres://u:p%40ss$w@host/db?sslmode=require")
    assert "p%40ss$w" in url


def test_settings_normalizes_and_reports_backend(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@host/db?sslmode=require")
    settings = Settings()

    assert settings.database_url == "postgresql+asyncpg://u:p@host/db"
    assert settings.is_postgres and not settings.is_sqlite
    # asyncpg spells TLS `ssl`, and it must be requested explicitly once
    # sslmode has been stripped out of the URL.
    assert settings.db_connect_args["ssl"] == "require"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("linkedin", ["linkedin"]),
        ("naukri,linkedin", ["naukri", "linkedin"]),
        ("naukri, linkedin ", ["naukri", "linkedin"]),
        ('["naukri", "linkedin"]', ["naukri", "linkedin"]),
    ],
)
def test_list_env_vars_accept_comma_separated_values(monkeypatch, raw, expected):
    """A hosting dashboard gives you one text box; commas have to work in it.

    pydantic-settings JSON-decodes list fields before validators run, so without
    the NoDecode annotation this raises SettingsError at import time - in
    production only, since local dev uses the defaults.
    """
    monkeypatch.setenv("SOURCES_ENABLED", raw)
    assert Settings().sources_enabled == expected


def test_cors_origins_accept_comma_separated_values(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://a.vercel.app, https://b.vercel.app")
    assert Settings().cors_origins == ["https://a.vercel.app", "https://b.vercel.app"]


def test_direct_postgres_uses_prepared_statement_caching(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@ep-abc.ap-southeast-1.aws.neon.tech/db")
    settings = Settings()

    assert not settings.is_pooled_postgres
    assert "prepared_statement_cache_size" not in settings.db_connect_args


def test_pooled_postgres_disables_prepared_statements(monkeypatch):
    """PgBouncer transaction mode breaks asyncpg's numbered statement names.

    The symptom is an intermittent DuplicatePreparedStatementError under
    concurrency, so this has to be handled by configuration rather than caught.
    """
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgres://u:p@ep-abc-pooler.c-3.ap-southeast-1.aws.neon.tech/db?sslmode=require",
    )
    settings = Settings()
    args = settings.db_connect_args

    assert settings.is_pooled_postgres
    assert args["prepared_statement_cache_size"] == 0
    # Unique per call, so two clients on one backend cannot collide.
    name_func = args["prepared_statement_name_func"]
    assert name_func() != name_func()


def test_sqlite_connect_args_keep_the_busy_timeout(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./data/jobs.db")
    settings = Settings()

    assert settings.is_sqlite
    assert settings.db_connect_args == {"timeout": 30}
