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


async def init_db() -> None:
    """Create tables if they do not exist."""
    from app import models  # noqa: F401  - register mappers

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
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
