"""End-to-end tests for the query layer and HTTP surface, against a temp DB."""

import os
import tempfile
from datetime import timedelta

import pytest
import pytest_asyncio

# Point at a throwaway database before anything imports app.config.
_TMP_DB = os.path.join(tempfile.mkdtemp(), "test_jobs.db")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DB}"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["ADMIN_TOKEN"] = ""

from app import repository as repo  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.enrich import normalize  # noqa: E402
from app.models import Job  # noqa: E402
from app.sources.base import RawJob  # noqa: E402
from app.utils import utcnow  # noqa: E402

pytestmark = pytest.mark.asyncio


SAMPLES = [
    dict(title="Python Developer", company="Acme", location="Bengaluru",
         experience_text="0-2 Yrs",
         description="Django, PostgreSQL and REST APIs.", declared_skills=["Python", "Django"]),
    dict(title="React Developer", company="Globex", location="Pune",
         experience_text="1-3 Yrs",
         description="Build UIs with React and TypeScript.", declared_skills=["React"]),
    dict(title="DevOps Engineer", company="Initech", location="Remote",
         experience_text="2-3 Yrs",
         description="Kubernetes, Terraform and CI/CD pipelines.", declared_skills=["AWS"]),
    dict(title="Data Analyst", company="Acme", location="Hyderabad",
         experience_text="Fresher",
         description="SQL and Power BI reporting.", declared_skills=["SQL"]),
]


@pytest_asyncio.fixture
async def session():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    normalized = []
    for sample in SAMPLES:
        job, reason = normalize(
            RawJob(
                source="naukri",
                apply_link=f"https://example.com/{sample['title'].replace(' ', '-')}",
                posted_at=utcnow() - timedelta(days=1),
                **sample,
            )
        )
        assert reason is None, f"fixture rejected: {sample['title']} ({reason})"
        normalized.append(job)

    async with SessionLocal() as s:
        await repo.upsert_jobs(s, normalized)
        await s.commit()

    async with SessionLocal() as s:
        yield s


class TestUpsert:
    async def test_inserts_then_updates_rather_than_duplicating(self, session):
        assert await repo.count_jobs(session) == len(SAMPLES)

        job, _ = normalize(
            RawJob(
                source="linkedin",
                apply_link="https://linkedin.com/jobs/view/1",
                posted_at=utcnow(),
                **SAMPLES[0],
            )
        )
        async with SessionLocal() as s:
            result = await repo.upsert_jobs(s, [job])
            await s.commit()

        assert result.created == 0 and result.updated == 1
        async with SessionLocal() as s:
            assert await repo.count_jobs(s) == len(SAMPLES)

    async def test_update_never_blanks_an_existing_description(self, session):
        stripped = dict(SAMPLES[0])
        stripped["description"] = ""
        stripped["declared_skills"] = ["Python"]
        job, _ = normalize(
            RawJob(source="naukri", apply_link="https://example.com/x", **stripped)
        )
        async with SessionLocal() as s:
            await repo.upsert_jobs(s, [job])
            await s.commit()

        async with SessionLocal() as s:
            rows, _ = await repo.search_jobs(s, repo.JobFilters(q="python developer"))
            assert rows[0].description != ""


class TestSearch:
    async def test_unfiltered_returns_everything(self, session):
        rows, total = await repo.search_jobs(session, repo.JobFilters())
        assert total == len(SAMPLES) and len(rows) == len(SAMPLES)

    async def test_free_text(self, session):
        _, total = await repo.search_jobs(session, repo.JobFilters(q="kubernetes"))
        assert total == 1

    async def test_category_filter(self, session):
        _, total = await repo.search_jobs(
            session, repo.JobFilters(category=["DevOps / Cloud / SRE"])
        )
        assert total == 1

    async def test_multiple_skills_are_conjunctive(self, session):
        _, both = await repo.search_jobs(session, repo.JobFilters(skill=["React", "Python"]))
        _, one = await repo.search_jobs(session, repo.JobFilters(skill=["React"]))
        assert both == 0 and one == 1

    async def test_skill_filter_does_not_match_substrings(self, session):
        """Java must not match JavaScript.

        The blob was searched with a bare LIKE '%java%', so on live data 46% of
        skill=Java results listed only JavaScript. Every returned row must
        genuinely carry the requested skill.
        """
        rows, _ = await repo.search_jobs(session, repo.JobFilters(skill=["Java"]))
        for row in rows:
            assert "Java" in row.skills, f"{row.title} matched Java via {row.skills}"

    async def test_single_letter_skill_does_not_match_everything(self, session):
        """skill=R matched 342 of 343 live postings - every "r" in the blob."""
        rows, total = await repo.search_jobs(session, repo.JobFilters(skill=["R"]))
        assert total < len(SAMPLES)
        for row in rows:
            assert "R" in row.skills

    async def test_skill_filter_matches_a_sole_skill(self, session):
        """The fence has to close at both ends or first/last skills are missed."""
        _, total = await repo.search_jobs(session, repo.JobFilters(skill=["SQL"]))
        assert total >= 1

    async def test_like_wildcards_in_input_are_literal(self, session):
        """A query of "%" must not behave as "match everything"."""
        _, total = await repo.search_jobs(session, repo.JobFilters(q="%"))
        assert total == 0

    async def test_unknown_experience_can_be_excluded(self, session):
        """A posting with no stated experience satisfies every bound by default.

        That is right for recall - plenty of genuine fresher ads never state a
        number - but wrong for a listing that must not show senior roles, so it
        has to be switchable rather than implicit.
        """
        async with SessionLocal() as s:
            job = Job(
                fingerprint="unknown-exp",
                source="linkedin",
                apply_link="https://example.com/1",
                title="Software Engineer",
                company="Unknown Co",
                location="Remote",
                description="",
                experience_text="Not Applicable",
                experience_min=None,
                experience_max=None,
                salary_text="",
                skills=["Python"],
                first_seen_at=utcnow(),
                last_seen_at=utcnow(),
            )
            job.search_blob = job.build_search_blob()
            s.add(job)
            await s.commit()

        async with SessionLocal() as s:
            _, lenient = await repo.search_jobs(
                s, repo.JobFilters(max_experience=0, include_unknown_experience=True)
            )
            _, strict = await repo.search_jobs(
                s, repo.JobFilters(max_experience=0, include_unknown_experience=False)
            )

        assert lenient > strict
        async with SessionLocal() as s:
            rows, _ = await repo.search_jobs(
                s, repo.JobFilters(max_experience=0, include_unknown_experience=False)
            )
            assert all(r.experience_min is not None for r in rows)

    async def test_company_filter_is_case_insensitive(self, session):
        _, total = await repo.search_jobs(session, repo.JobFilters(company="acme"))
        assert total == 2

    async def test_work_mode(self, session):
        _, total = await repo.search_jobs(session, repo.JobFilters(work_mode=["remote"]))
        assert total == 1

    async def test_experience_ceiling(self, session):
        _, total = await repo.search_jobs(session, repo.JobFilters(max_experience=0))
        assert total >= 1

    async def test_pagination_does_not_overlap(self, session):
        first, total = await repo.search_jobs(session, repo.JobFilters(), page=1, page_size=2)
        second, _ = await repo.search_jobs(session, repo.JobFilters(), page=2, page_size=2)
        assert total == len(SAMPLES)
        assert {j.id for j in first}.isdisjoint({j.id for j in second})


class TestLifecycle:
    async def test_stale_jobs_are_deactivated_then_purged(self, session):
        async with SessionLocal() as s:
            rows = (await s.execute(__import__("sqlalchemy").select(Job))).scalars().all()
            for row in rows:
                row.last_seen_at = utcnow() - timedelta(days=400)
            await s.commit()

        async with SessionLocal() as s:
            assert await repo.deactivate_stale(s) == len(SAMPLES)
            await s.commit()
        async with SessionLocal() as s:
            assert await repo.count_jobs(s, active_only=True) == 0
            assert await repo.purge_old(s) == len(SAMPLES)
            await s.commit()
        async with SessionLocal() as s:
            assert await repo.count_jobs(s, active_only=False) == 0


class TestRunRecovery:
    async def test_interrupted_run_is_reaped_and_no_longer_blocks(self, session):
        # A run whose process died stays at "running" and would otherwise 409
        # every future scrape forever.
        async with SessionLocal() as s:
            await repo.create_run(s, ["naukri"], "cli")
            await s.commit()

        async with SessionLocal() as s:
            assert await repo.has_running_scrape(s) is not None

        async with SessionLocal() as s:
            assert await repo.reap_orphaned_runs(s) == 1
            await s.commit()

        async with SessionLocal() as s:
            assert await repo.has_running_scrape(s) is None
            run = await repo.latest_run(s)
            assert run.status == "failed" and run.error

    async def test_startup_reap_spares_a_scrape_running_elsewhere(self, session):
        """A cold start must not fail a run owned by another process.

        The scraper runs in CI against this same database, so an unbounded reap
        on boot would mark a healthy run failed and release its lock to a second
        concurrent scrape. The window has to exceed the longest possible scrape.
        """
        from app.config import settings

        assert settings.orphan_run_after_minutes >= 60

        async with SessionLocal() as s:
            await repo.create_run(s, ["naukri"], "scheduled")
            await s.commit()

        async with SessionLocal() as s:
            reaped = await repo.reap_orphaned_runs(
                s, older_than_minutes=settings.orphan_run_after_minutes
            )
            await s.commit()

        assert reaped == 0
        async with SessionLocal() as s:
            assert await repo.has_running_scrape(s) is not None

    async def test_age_limit_spares_a_freshly_started_run(self, session):
        async with SessionLocal() as s:
            await repo.create_run(s, ["naukri"], "manual")
            await s.commit()

        async with SessionLocal() as s:
            # The CLI path must not kill a scrape a live server just started.
            assert await repo.reap_orphaned_runs(s, older_than_minutes=120) == 0
            await s.commit()

        async with SessionLocal() as s:
            assert await repo.has_running_scrape(s) is not None


class TestCancellation:
    async def test_sources_stop_at_the_next_boundary(self):
        import asyncio

        from app.http_client import HttpClient
        from app.sources.base import JobSource

        pages_fetched = []

        class SlowSource(JobSource):
            name = "slow"

            def plan(self, queries):
                return len(queries) * 3

            async def fetch(self, queries):
                for query in queries:
                    for page in range(3):
                        if self.cancelled:
                            return pages_fetched
                        pages_fetched.append((query, page))
                        await asyncio.sleep(0.01)
                return pages_fetched

        event = asyncio.Event()
        source = SlowSource(HttpClient())
        source.bind(cancel_event=event)

        task = asyncio.create_task(source.fetch(["a", "b", "c"]))
        await asyncio.sleep(0.05)
        event.set()
        await task

        # It stopped early but kept everything gathered up to that point.
        assert 0 < len(pages_fetched) < 9

    async def test_request_cancel_on_unknown_run_is_false(self):
        from app.pipeline import request_cancel

        assert request_cancel(999999) is False

    async def test_cancel_endpoint_reports_orphan_when_process_owns_no_task(self, session):
        from httpx import ASGITransport, AsyncClient

        from app.api import app

        async with SessionLocal() as s:
            run = await repo.create_run(s, ["naukri"], "manual")
            run_id = run.id
            await s.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.post(f"/scrape/runs/{run_id}/cancel")
            assert res.status_code == 200
            assert res.json()["detail"]["orphaned"] is True

        # And it no longer blocks the next scrape.
        async with SessionLocal() as s:
            assert await repo.has_running_scrape(s) is None

    async def test_cancelling_a_finished_run_is_409(self, session):
        from httpx import ASGITransport, AsyncClient

        from app.api import app

        async with SessionLocal() as s:
            run = await repo.create_run(s, ["naukri"], "manual")
            run_id = run.id
            await repo.finish_run(s, run_id, status="completed", stats={})
            await s.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.post(f"/scrape/runs/{run_id}/cancel")).status_code == 409

    async def test_cancelling_a_missing_run_is_404(self, session):
        from httpx import ASGITransport, AsyncClient

        from app.api import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.post("/scrape/runs/424242/cancel")).status_code == 404


class TestFacets:
    async def test_group_and_skill_counts(self, session):
        categories = await repo.group_counts(session, Job.category)
        assert sum(c["count"] for c in categories) == len(SAMPLES)

        skills = await repo.skill_counts(session)
        assert any(s["value"] == "Python" for s in skills)


class TestHttp:
    async def test_endpoints_respond(self, session):
        from httpx import ASGITransport, AsyncClient

        from app.api import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/health")).status_code == 200

            listing = await client.get("/jobs", params={"page_size": 2})
            assert listing.status_code == 200
            body = listing.json()
            assert body["meta"]["total"] == len(SAMPLES)
            assert len(body["items"]) == 2

            job_id = body["items"][0]["id"]
            assert (await client.get(f"/jobs/{job_id}")).status_code == 200
            assert (await client.get("/jobs/999999")).status_code == 404

            assert (await client.get("/filters")).status_code == 200
            assert (await client.get("/stats")).status_code == 200
            assert (await client.get("/meta")).status_code == 200

    async def test_admin_token_is_enforced_when_configured(self, session, monkeypatch):
        from httpx import ASGITransport, AsyncClient

        from app.api import app
        from app.config import settings

        monkeypatch.setattr(settings, "admin_token", "s3cret")
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            denied = await client.post("/scrape/run", json={})
            assert denied.status_code == 401
