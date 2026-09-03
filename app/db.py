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

from sqlalchemy import event, text
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


def _backfill_statement(table_name: str, column_name: str):
    """UPDATE that writes a newly added column's default into existing rows.

    Built with `text()` so SQLAlchemy renders the bind parameter in each
    driver's paramstyle. Writing this as `exec_driver_sql` hands the string to
    the driver untouched, which silently works on sqlite3 (it accepts `:name`)
    and fails on asyncpg (which wants `$1`) - a break no SQLite test can see.

    Both names must already be quoted for the target dialect.
    """
    return text(
        f"UPDATE {table_name} SET {column_name} = :fill WHERE {column_name} IS NULL"
    )


def _sync_added_columns(connection) -> None:
    """Add columns and indexes the models declare but an existing table lacks.

    `create_all` only creates whole tables, so a database written by an earlier
    version keeps its old column set forever and every query naming a new
    column fails. There is no migration tool here on purpose: every schema
    change so far has been additive, and additive changes are expressible as
    plain `ALTER TABLE ADD COLUMN`, which both SQLite and Postgres accept with
    identical syntax. A destructive change (dropping or retyping a column) is
    the point at which this stops being enough and Alembic earns its keep.
    """
    from sqlalchemy import inspect
    from sqlalchemy.schema import CreateIndex

    inspector = inspect(connection)
    existing_tables = set(inspector.get_table_names())
    quote = connection.dialect.identifier_preparer.quote

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # create_all just made it, in full

        present = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue

            table_name = quote(table.name)
            column_name = quote(column.name)
            ddl = column.type.compile(connection.dialect)
            # Deliberately added nullable regardless of what the model says:
            # existing rows have no value to put there, and every backend
            # rejects a NOT NULL column added to a non-empty table.
            connection.exec_driver_sql(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}"
            )
            logger.info("Added column %s.%s", table.name, column.name)

            # A column the model declares non-optional with a scalar default
            # would otherwise read back as NULL on every pre-existing row, and
            # the response models validate those rows against the declared type.
            # Backfilling makes an old row indistinguishable from a new one.
            fill = getattr(column.default, "arg", None)
            if not column.nullable and isinstance(fill, (str, int, float, bool)):
                connection.execute(
                    _backfill_statement(table_name, column_name), {"fill": fill}
                )

        present_indexes = {idx["name"] for idx in inspector.get_indexes(table.name)}
        for index in table.indexes:
            if index.name in present_indexes:
                continue
            connection.execute(CreateIndex(index, if_not_exists=True))
            logger.info("Created index %s", index.name)


async def init_db() -> None:
    """Create tables if they do not exist, and add any columns they are missing."""
    from app import models  # noqa: F401  - register mappers

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_sync_added_columns)
    logger.info("Database ready at %s", settings.database_url)


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
