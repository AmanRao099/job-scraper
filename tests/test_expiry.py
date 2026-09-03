"""Expiry: the rules that decide a stored posting is no longer worth serving.

The classification tests carry most of the weight. Retiring a live posting is
worse than keeping a dead one for another few hours - the first loses a job the
consumer could have applied to, the second only wastes a click - so the cases
that must come back "undecidable" are tested as carefully as the ones that must
come back "gone".
"""

import os
import tempfile
from datetime import timedelta
from types import SimpleNamespace

import pytest_asyncio

_TMP_DB = os.path.join(tempfile.mkdtemp(), "test_expiry.db")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DB}"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["ADMIN_TOKEN"] = ""

from app import repository as repo  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.enrich import normalize  # noqa: E402
from app.expiry import _redirected_away, classify_response  # noqa: E402
from app.models import Job  # noqa: E402
from app.sources.base import RawJob  # noqa: E402
from app.utils import utcnow  # noqa: E402

# No module-level asyncio mark: half of these are plain synchronous classifier
# tests, and pytest.ini already runs in asyncio auto mode.


SAMPLES = [
    dict(title="Python Developer", company="Acme", location="Bengaluru",
         experience_text="0-2 Yrs",
         description="Django, PostgreSQL and REST APIs.", declared_skills=["Python"]),
    dict(title="React Developer", company="Globex", location="Pune",
         experience_text="1-3 Yrs",
         description="Build UIs with React and TypeScript.", declared_skills=["React"]),
]


def fake_response(status_code: int, text: str = "", url: str = "https://x.test/jobs/view/1"):
    """Enough of an httpx.Response for the classifier - it reads three fields."""
    return SimpleNamespace(status_code=status_code, text=text, url=url)


LIVE_PAGE = (
    "About the job. We are hiring a Python developer to build and ship backend "
    "services on Django and PostgreSQL. You will own REST APIs end to end, work "
    "with a small team, and help shape our data model. Requirements: two years "
    "of experience, strong SQL, comfort with Linux. Benefits include health "
    "cover and a learning budget. Apply through the link below and our team "
    "will get back to you within a week of your application being received."
)


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
                apply_link=f"https://example.com/job-listings-{sample['title']}",
                posted_at=utcnow() - timedelta(days=1),
                **sample,
            )
        )
        assert reason is None
        normalized.append(job)

    async with SessionLocal() as s:
        await repo.upsert_jobs(s, normalized)
        await s.commit()

    async with SessionLocal() as s:
        yield s


class TestClassification:
    def test_404_means_gone(self):
        verdict, reason = classify_response(fake_response(404), "https://x.test/jobs/view/1")
        assert verdict is False
        assert reason == "http_gone"

    def test_410_means_gone(self):
        verdict, _ = classify_response(fake_response(410), "https://x.test/jobs/view/1")
        assert verdict is False

    def test_403_is_never_treated_as_gone(self):
        # Being blocked says nothing about the posting. Expiring on this would
        # delete the whole table the first time a board rate limits the sweep.
        verdict, reason = classify_response(fake_response(403), "https://x.test/jobs/view/1")
        assert verdict is None
        assert reason == "http_403"

    def test_no_response_is_inconclusive(self):
        verdict, _ = classify_response(None, "https://x.test/jobs/view/1")
        assert verdict is None

    def test_live_page_is_alive(self):
        verdict, reason = classify_response(
            fake_response(200, LIVE_PAGE), "https://x.test/jobs/view/1"
        )
        assert verdict is True
        assert reason == "ok"

    def test_dead_wording_means_gone(self):
        page = LIVE_PAGE + " No longer accepting applications."
        verdict, reason = classify_response(
            fake_response(200, page), "https://x.test/jobs/view/1"
        )
        assert verdict is False
        assert reason == "dead_page"

    def test_thin_page_is_inconclusive(self):
        verdict, reason = classify_response(
            fake_response(200, "Loading..."), "https://x.test/jobs/view/1"
        )
        assert verdict is None
        assert reason == "thin_page"

    def test_redirect_off_the_posting_means_gone(self):
        verdict, reason = classify_response(
            fake_response(200, LIVE_PAGE, url="https://x.test/jobs/search?keywords=python"),
            "https://x.test/jobs/view/1",
        )
        assert verdict is False
        assert reason == "redirected_away"


class TestRedirectDetection:
    def test_canonical_rewrite_is_not_a_redirect_away(self):
        # Boards rewrite a posting URL to a slugged canonical form constantly.
        assert not _redirected_away(
            "https://x.test/jobs/view/python-developer-at-acme-123?trk=guest",
            "https://x.test/jobs/view/123",
        )

    def test_trailing_slash_is_not_a_redirect_away(self):
        assert not _redirected_away("https://x.test/jobs/view/1/", "https://x.test/jobs/view/1")

    def test_bounce_to_search_page_is_a_redirect_away(self):
        assert _redirected_away("https://x.test/jobs", "https://x.test/jobs/view/1")

    def test_non_posting_urls_are_left_alone(self):
        # A source whose links do not look like posting pages must not be
        # judged by this rule at all.
        assert not _redirected_away("https://careers.acme.test/b", "https://careers.acme.test/a")


class TestCheckRecording:
    async def test_gone_verdict_retires_immediately(self, session):
        job = (await session.execute(__import__("sqlalchemy").select(Job))).scalars().first()

        async with SessionLocal() as s:
            assert await repo.record_check(s, job.id, alive=False) is True
            await s.commit()

        async with SessionLocal() as s:
            row = await s.get(Job, job.id)
            assert row.is_active is False
            assert row.expiry_reason == repo.EXPIRY_GONE
            assert row.expired_at is not None

    async def test_single_inconclusive_probe_does_not_retire(self, session):
        job = (await session.execute(__import__("sqlalchemy").select(Job))).scalars().first()

        async with SessionLocal() as s:
            assert await repo.record_check(s, job.id, alive=None, max_failures=3) is False
            await s.commit()

        async with SessionLocal() as s:
            row = await s.get(Job, job.id)
            assert row.is_active is True
            assert row.check_failures == 1

    async def test_retires_only_after_the_failure_streak(self, session):
        job = (await session.execute(__import__("sqlalchemy").select(Job))).scalars().first()

        for expected in (False, False, True):
            async with SessionLocal() as s:
                retired = await repo.record_check(s, job.id, alive=None, max_failures=3)
                await s.commit()
            assert retired is expected

        async with SessionLocal() as s:
            row = await s.get(Job, job.id)
            assert row.is_active is False
            assert row.expiry_reason == repo.EXPIRY_UNREACHABLE

    async def test_one_alive_probe_clears_the_streak(self, session):
        job = (await session.execute(__import__("sqlalchemy").select(Job))).scalars().first()

        async with SessionLocal() as s:
            await repo.record_check(s, job.id, alive=None, max_failures=2)
            await s.commit()
        async with SessionLocal() as s:
            await repo.record_check(s, job.id, alive=True)
            await s.commit()
        async with SessionLocal() as s:
            # Without the reset this second failure would be the second in a
            # row and would retire a posting the board just served us.
            assert await repo.record_check(s, job.id, alive=None, max_failures=2) is False
            await s.commit()

        async with SessionLocal() as s:
            assert (await s.get(Job, job.id)).is_active is True


class TestSelection:
    async def test_never_checked_jobs_come_first(self, session):
        rows = (await session.execute(__import__("sqlalchemy").select(Job))).scalars().all()
        async with SessionLocal() as s:
            checked = await s.get(Job, rows[0].id)
            checked.last_checked_at = utcnow() - timedelta(days=30)
            await s.commit()

        async with SessionLocal() as s:
            due = await repo.select_for_expiry_check(s, limit=10)
        assert [j.id for j in due][0] == rows[1].id

    async def test_recently_checked_jobs_are_skipped(self, session):
        async with SessionLocal() as s:
            for row in (await s.execute(__import__("sqlalchemy").select(Job))).scalars().all():
                row.last_checked_at = utcnow()
            await s.commit()

        async with SessionLocal() as s:
            assert await repo.select_for_expiry_check(s, recheck_after_hours=24) == []

    async def test_inactive_jobs_are_never_probed(self, session):
        async with SessionLocal() as s:
            for row in (await s.execute(__import__("sqlalchemy").select(Job))).scalars().all():
                row.is_active = False
            await s.commit()

        async with SessionLocal() as s:
            assert await repo.select_for_expiry_check(s) == []


class TestAgeExpiry:
    async def test_old_postings_are_retired(self, session):
        async with SessionLocal() as s:
            for row in (await s.execute(__import__("sqlalchemy").select(Job))).scalars().all():
                row.posted_at = utcnow() - timedelta(days=200)
            await s.commit()

        async with SessionLocal() as s:
            assert await repo.expire_aged_out(s, days=60) == len(SAMPLES)
            await s.commit()

        async with SessionLocal() as s:
            assert await repo.count_jobs(s, active_only=True) == 0

    async def test_postings_without_a_date_are_left_to_staleness(self, session):
        # Guessing an age from first_seen_at would retire jobs we merely
        # discovered late, so a NULL posted_at is not an expiry signal.
        async with SessionLocal() as s:
            for row in (await s.execute(__import__("sqlalchemy").select(Job))).scalars().all():
                row.posted_at = None
            await s.commit()

        async with SessionLocal() as s:
            assert await repo.expire_aged_out(s, days=1) == 0


class TestPurge:
    async def test_expired_jobs_are_deleted_after_the_grace_period(self, session):
        rows = (await session.execute(__import__("sqlalchemy").select(Job))).scalars().all()
        ids = [row.id for row in rows]

        async with SessionLocal() as s:
            assert await repo.expire_jobs(s, ids, repo.EXPIRY_GONE) == len(ids)
            await s.commit()

        async with SessionLocal() as s:
            # Still inside the grace period: a scrape could still contradict us.
            assert await repo.purge_expired(s, days=3) == 0
            await s.commit()

        async with SessionLocal() as s:
            for row in (await s.execute(__import__("sqlalchemy").select(Job))).scalars().all():
                row.expired_at = utcnow() - timedelta(days=10)
            await s.commit()

        async with SessionLocal() as s:
            assert await repo.purge_expired(s, days=3) == len(ids)
            await s.commit()

        async with SessionLocal() as s:
            assert await repo.count_jobs(s, active_only=False) == 0

    async def test_active_jobs_are_never_purged(self, session):
        async with SessionLocal() as s:
            assert await repo.purge_expired(s, days=0) == 0
        assert await repo.count_jobs(session, active_only=True) == len(SAMPLES)


class TestRevival:
    async def test_rescraping_an_expired_job_clears_its_expiry(self, session):
        rows = (await session.execute(__import__("sqlalchemy").select(Job))).scalars().all()

        async with SessionLocal() as s:
            await repo.expire_jobs(s, [rows[0].id], repo.EXPIRY_GONE)
            stale = await s.get(Job, rows[0].id)
            stale.check_failures = 2
            await s.commit()

        job, _ = normalize(
            RawJob(
                source="naukri",
                apply_link="https://example.com/job-listings-python",
                posted_at=utcnow(),
                **SAMPLES[0],
            )
        )
        async with SessionLocal() as s:
            await repo.upsert_jobs(s, [job])
            await s.commit()

        async with SessionLocal() as s:
            row = await s.get(Job, rows[0].id)
            assert row.is_active is True
            assert row.expired_at is None
            assert row.expiry_reason == ""
            # Strikes from an outage must not survive a successful re-scrape,
            # or the next single blip retires the posting again.
            assert row.check_failures == 0


class TestMaintenanceCycle:
    async def test_verification_defers_to_a_running_scrape(self, session):
        # The probes and a scrape draw on the same board rate limit from the
        # same address, and the scrape is the more valuable use of it.
        async with SessionLocal() as s:
            await repo.create_run(s, ["naukri"], "test")
            await s.commit()

        from app.pipeline import run_maintenance

        summary = await run_maintenance()
        assert summary["verified"] == {"skipped": "scrape_running"}

    async def test_age_pass_alone_makes_no_requests(self, session):
        # --no-verify must be safe to run anywhere, including offline.
        from app.pipeline import run_maintenance

        summary = await run_maintenance(verify=False)
        assert "verified" not in summary
        assert summary == {"deactivated": 0, "purged": 0}


class TestServing:
    async def test_expired_jobs_are_not_served(self, session):
        rows = (await session.execute(__import__("sqlalchemy").select(Job))).scalars().all()

        async with SessionLocal() as s:
            await repo.expire_jobs(s, [rows[0].id], repo.EXPIRY_GONE)
            await s.commit()

        async with SessionLocal() as s:
            served, total = await repo.search_jobs(s, repo.JobFilters())
        assert total == len(SAMPLES) - 1
        assert rows[0].id not in {job.id for job in served}

        # Still reachable for anyone who explicitly asks for retired rows.
        async with SessionLocal() as s:
            _, all_total = await repo.search_jobs(s, repo.JobFilters(active_only=False))
        assert all_total == len(SAMPLES)
