"""Additive migration coverage for databases created by the previous schema."""

import pytest

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects import postgresql

from app.db import _migrate_jobs_table, _migrate_scrape_runs_table
from app.models import Job
from app.repository import JobFilters, _apply_filters


def test_previous_jobs_table_is_migrated_without_data_loss(tmp_path):
    database = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{database}")
    previous_columns = """
        id INTEGER PRIMARY KEY,
        fingerprint VARCHAR(40) UNIQUE NOT NULL,
        source VARCHAR(32) NOT NULL,
        external_id VARCHAR(128),
        apply_link TEXT NOT NULL,
        title VARCHAR(300) NOT NULL,
        company VARCHAR(300) NOT NULL,
        location VARCHAR(300) NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        experience_text VARCHAR(120) NOT NULL DEFAULT '',
        experience_min INTEGER,
        experience_max INTEGER,
        salary_text VARCHAR(200) NOT NULL DEFAULT '',
        salary_min FLOAT,
        salary_max FLOAT,
        skills JSON NOT NULL DEFAULT '[]',
        category VARCHAR(64) NOT NULL DEFAULT 'Other',
        categories JSON NOT NULL DEFAULT '[]',
        seniority VARCHAR(24) NOT NULL DEFAULT 'mid',
        work_mode VARCHAR(16) NOT NULL DEFAULT 'onsite',
        posted_at DATETIME,
        first_seen_at DATETIME NOT NULL,
        last_seen_at DATETIME NOT NULL,
        is_active BOOLEAN NOT NULL DEFAULT true,
        search_blob TEXT NOT NULL DEFAULT ''
    """
    with engine.begin() as connection:
        connection.exec_driver_sql(f"CREATE TABLE jobs ({previous_columns})")
        connection.exec_driver_sql(
            "INSERT INTO jobs (fingerprint, source, apply_link, title, company, "
            "first_seen_at, last_seen_at) VALUES "
            "('legacy', 'linkedin', 'https://example.com/1', 'Engineer', 'Acme', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        _migrate_jobs_table(connection)
        _migrate_jobs_table(connection)  # idempotent

    columns = {column["name"] for column in inspect(engine).get_columns("jobs")}
    assert {
        "degree_requirements", "masters_match", "education_requirement", "country",
        "is_abroad", "visa_sponsorship", "work_authorization_required",
        "relocation_support",
        "canonical_url", "dedup_key", "source_ids", "source_urls",
        "discovered_profiles", "discovered_queries", "employment_type",
    } <= columns
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT fingerprint, masters_match, education_requirement, "
                "visa_sponsorship, canonical_url, employment_type FROM jobs"
            )
        ).one()
    assert tuple(row) == ("legacy", 0, "not_stated", "unknown", "", "unknown")


def test_scrape_run_lock_migration_repairs_legacy_race_and_is_idempotent(tmp_path):
    database = tmp_path / "runs.db"
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE scrape_runs (id INTEGER PRIMARY KEY, status VARCHAR(16), "
            "finished_at DATETIME, error TEXT)"
        )
        connection.exec_driver_sql(
            "INSERT INTO scrape_runs (id, status) VALUES (1, 'running'), (2, 'running')"
        )
        _migrate_scrape_runs_table(connection)
        _migrate_scrape_runs_table(connection)

    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, status FROM scrape_runs ORDER BY id")
        ).all()
        assert rows == [(1, "failed"), (2, "running")]
        with pytest.raises(IntegrityError):
            connection.execute(
                text("INSERT INTO scrape_runs (id, status) VALUES (3, 'running')")
            )


def test_combined_job_filter_compiles_for_postgresql():
    from sqlalchemy import select

    statement = _apply_filters(
        select(Job),
        JobFilters(
            is_abroad=True,
            masters_match=True,
            education_requirement=["preferred"],
            visa_sponsorship=["unknown"],
            max_experience=5,
        ),
    ).order_by(Job.posted_at.desc().nullslast(), Job.id.desc())
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "jobs.is_abroad" in sql and "NULLS LAST" in sql
