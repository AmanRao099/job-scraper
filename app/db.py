"""Async SQLAlchemy engine + session factory.

SQLite is the default because at this scale (low hundreds of thousands of rows,
single writer) it is faster than a network round-trip to Postgres and needs no
setup. Moving to Postgres is a `DATABASE_URL` change - nothing here is
SQLite-specific except the PRAGMA tuning, which is guarded.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, inspect, text
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_engine_options: dict = {}
if settings.is_pooled_postgres:
    # PgBouncer already pools on the server side; a second pool in front of it
    # just holds connections open against the free tier's cap for no gain.
    _engine_options = {"poolclass": NullPool}
elif not settings.is_sqlite:
    # Free Postgres tiers (Neon, Supabase) suspend an idle compute and drop the
    # TCP connection with it. Pre-ping plus a short recycle means the next
    # request transparently reconnects instead of raising ConnectionDoesNotExist.
    # The pool stays small because free tiers cap total connections low and the
    # API server and the scraper both draw from it.
    _engine_options = {"pool_recycle": 280, "pool_size": 5, "max_overflow": 5}

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    # SQLite's default single connection serialises writers; that is what we
    # want, but reads should not block behind the scraper's write batches.
    connect_args=settings.db_connect_args,
    **_engine_options,
)

SessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


if settings.is_sqlite:

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        # WAL lets the API keep reading while a scrape writes.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


async def init_db() -> None:
    """Create tables and add backward-compatible columns to existing jobs."""
    from app import models  # noqa: F401  - register mappers

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_jobs_table)
        await conn.run_sync(_migrate_scrape_runs_table)
    logger.info(
        "Database ready at %s",
        engine.url.render_as_string(hide_password=True),
    )


# ``create_all`` intentionally does not alter existing tables. These additions
# are nullable or carry a server default, so both SQLite and PostgreSQL can add
# them in place without rebuilding the table or losing rows.
_JOB_COLUMN_ADDITIONS: tuple[tuple[str, str], ...] = (
    ("canonical_url", "TEXT NOT NULL DEFAULT ''"),
    ("dedup_key", "VARCHAR(40) NOT NULL DEFAULT ''"),
    ("source_ids", "JSON NOT NULL DEFAULT '[]'"),
    ("source_urls", "JSON NOT NULL DEFAULT '[]'"),
    ("discovered_profiles", "JSON NOT NULL DEFAULT '[]'"),
    ("discovered_queries", "JSON NOT NULL DEFAULT '[]'"),
    ("employment_type", "VARCHAR(16) NOT NULL DEFAULT 'unknown'"),
    ("degree_requirements", "JSON NOT NULL DEFAULT '[]'"),
    ("masters_match", "BOOLEAN NOT NULL DEFAULT false"),
    ("education_requirement", "VARCHAR(16) NOT NULL DEFAULT 'not_stated'"),
    ("country", "VARCHAR(128)"),
    ("is_abroad", "BOOLEAN NOT NULL DEFAULT false"),
    ("visa_sponsorship", "VARCHAR(16) NOT NULL DEFAULT 'unknown'"),
    ("work_authorization_required", "BOOLEAN NOT NULL DEFAULT false"),
    ("relocation_support", "VARCHAR(16) NOT NULL DEFAULT 'unknown'"),
)

_JOB_INDEX_ADDITIONS: tuple[tuple[str, str], ...] = (
    ("ix_jobs_canonical_url", "canonical_url"),
    ("ix_jobs_dedup_key", "dedup_key"),
    ("ix_jobs_employment_type", "employment_type"),
    ("ix_jobs_masters_match", "masters_match"),
    ("ix_jobs_education_requirement", "education_requirement"),
    ("ix_jobs_country", "country"),
    ("ix_jobs_is_abroad", "is_abroad"),
    ("ix_jobs_visa_sponsorship", "visa_sponsorship"),
    ("ix_jobs_work_authorization_required", "work_authorization_required"),
    ("ix_jobs_relocation_support", "relocation_support"),
)

_JOB_COMPOSITE_INDEX_ADDITIONS: tuple[tuple[str, str], ...] = (
    ("ix_jobs_source_external", "source, external_id"),
    (
        "ix_jobs_international_masters_education_posted",
        "is_active, is_abroad, masters_match, education_requirement, posted_at",
    ),
)


def _migrate_jobs_table(sync_conn) -> None:
    """Apply the small additive schema migration, idempotently."""
    inspector = inspect(sync_conn)
    if "jobs" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("jobs")}
    for name, definition in _JOB_COLUMN_ADDITIONS:
        if name not in existing:
            sync_conn.exec_driver_sql(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
            logger.info("Added jobs.%s", name)
    for index_name, column_name in _JOB_INDEX_ADDITIONS:
        sync_conn.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS {index_name} ON jobs ({column_name})"
        )
    for index_name, columns in _JOB_COMPOSITE_INDEX_ADDITIONS:
        sync_conn.exec_driver_sql(
            f"CREATE INDEX IF NOT EXISTS {index_name} ON jobs ({columns})"
        )


def _migrate_scrape_runs_table(sync_conn) -> None:
    """Enforce one database-wide running scrape, including legacy databases."""
    inspector = inspect(sync_conn)
    if "scrape_runs" not in inspector.get_table_names():
        return
    # A pre-index deployment could race and leave several rows running. Keep
    # the newest as the lock owner and retain older rows as failed history.
    sync_conn.exec_driver_sql(
        "UPDATE scrape_runs SET status = 'failed', finished_at = CURRENT_TIMESTAMP, "
        "error = 'Superseded while installing the single-run constraint' "
        "WHERE status = 'running' AND id <> ("
        "SELECT newest_id FROM (SELECT MAX(id) AS newest_id FROM scrape_runs "
        "WHERE status = 'running') AS newest)"
    )
    sync_conn.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_scrape_runs_single_running "
        "ON scrape_runs (status) WHERE status = 'running'"
    )


async def dispose_db() -> None:
    await engine.dispose()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope for background work."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def healthcheck() -> bool:
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception:
        logger.exception("Database healthcheck failed")
        return False
