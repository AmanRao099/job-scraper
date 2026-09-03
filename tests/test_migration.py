"""The additive migration, checked against every dialect it has to run on.

These exist because the first version of the backfill shipped green: it used
`exec_driver_sql`, which hands the statement to the driver verbatim, so the
`:fill` placeholder was accepted by sqlite3 and rejected by asyncpg with
"syntax error at or near :". The whole test suite passed and the deploy died on
startup, because the suite only ever ran SQLite.

Compiling against the Postgres dialect needs no Postgres server, so there is no
excuse for a dialect-specific break to pass again.
"""

import os
import tempfile

from sqlalchemy.dialects.postgresql import asyncpg as pg_dialect
from sqlalchemy.dialects.sqlite import aiosqlite as sqlite_dialect
from sqlalchemy.schema import CreateIndex

_TMP_DB = os.path.join(tempfile.mkdtemp(), "test_migration.db")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TMP_DB}")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from app.db import Base, _backfill_statement  # noqa: E402
from app.models import Job  # noqa: E402

DIALECTS = {"asyncpg": pg_dialect.dialect(), "aiosqlite": sqlite_dialect.dialect()}

# The columns the expiry work added. An older database has the table without
# them, so these are exactly what the migration has to emit.
ADDED_COLUMNS = ["expired_at", "expiry_reason", "last_checked_at", "check_failures"]


class TestBackfillStatement:
    def test_placeholder_is_rendered_per_driver(self):
        stmt = _backfill_statement("jobs", "expiry_reason")

        rendered = {
            name: str(stmt.compile(dialect=dialect))
            for name, dialect in DIALECTS.items()
        }

        # The exact bug that broke the deploy: asyncpg needs $1, not :fill.
        assert "$1" in rendered["asyncpg"]
        assert "?" in rendered["aiosqlite"]
        for sql in rendered.values():
            assert ":fill" not in sql

    def test_only_null_rows_are_touched(self):
        # A backfill that dropped the guard would overwrite values that later
        # runs had legitimately written.
        sql = str(_backfill_statement("jobs", "expiry_reason"))
        assert "IS NULL" in sql


class TestAddedColumnDDL:
    def test_every_added_column_compiles_on_both_dialects(self):
        columns = {c.name: c for c in Job.__table__.columns}

        for name in ADDED_COLUMNS:
            column = columns[name]
            for dialect_name, dialect in DIALECTS.items():
                ddl = column.type.compile(dialect)
                assert ddl, f"{name} produced no type on {dialect_name}"

    def test_added_columns_are_nullable_or_carry_a_scalar_default(self):
        # ALTER TABLE ADD COLUMN cannot add a NOT NULL column to a table that
        # already has rows, so any non-nullable addition must be backfillable.
        columns = {c.name: c for c in Job.__table__.columns}

        for name in ADDED_COLUMNS:
            column = columns[name]
            if column.nullable:
                continue
            fill = getattr(column.default, "arg", None)
            assert isinstance(fill, (str, int, float, bool)), (
                f"{name} is NOT NULL with no scalar default, so existing rows "
                f"would keep reading back as NULL"
            )


class TestAddedIndexDDL:
    def test_indexes_compile_with_if_not_exists_on_both_dialects(self):
        for index in Job.__table__.indexes:
            for dialect_name, dialect in DIALECTS.items():
                sql = str(CreateIndex(index, if_not_exists=True).compile(dialect=dialect))
                assert "IF NOT EXISTS" in sql, f"{index.name} on {dialect_name}"


class TestModelCoverage:
    def test_the_expiry_columns_are_actually_on_the_model(self):
        # Guards the test list above against silently drifting from the model.
        names = {c.name for c in Job.__table__.columns}
        assert set(ADDED_COLUMNS) <= names

    def test_every_table_is_reachable_from_the_metadata(self):
        # The migration walks Base.metadata, so a table registered nowhere in it
        # would never be migrated.
        assert {"jobs", "scrape_runs"} <= set(Base.metadata.tables)
