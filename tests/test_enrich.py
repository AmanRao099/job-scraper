"""Tests for normalisation, filtering and dedup."""

from datetime import timedelta

from app.config import settings
from app.enrich import (
    REJECT_DEAD,
    REJECT_EXPERIENCE,
    REJECT_INCOMPLETE,
    REJECT_NOT_TECH,
    looks_dead,
    normalize,
    normalize_many,
)
from app.models import make_fingerprint
from app.sources.base import RawJob


def test_one_malformed_record_does_not_discard_the_batch():
    malformed = RawJob(
        source="linkedin", title=None, company="Broken", apply_link="https://example.com/bad"
    )
    valid = RawJob(
        source="linkedin", external_id="ok-1", title="Python Engineer", company="Acme",
        apply_link="https://example.com/good", description="Build Django REST APIs with Python.",
        declared_skills=["Python"],
    )
    jobs, rejected = normalize_many([malformed, valid])
    assert len(jobs) == 1
    assert rejected["parsing_failure"] == 1


def test_same_title_same_company_distinct_source_ids_are_not_merged():
    common = dict(
        source="linkedin", title="Software Engineer", company="Acme", location="Paris, France",
        description="Build Python cloud services.", declared_skills=["Python"],
    )
    jobs, rejected = normalize_many([
        RawJob(external_id="100", apply_link="https://linkedin.com/jobs/view/100", **common),
        RawJob(external_id="200", apply_link="https://linkedin.com/jobs/view/200", **common),
    ])
    assert not rejected and len(jobs) == 2


def test_cross_source_duplicate_merges_discovery_metadata_and_richer_content():
    jobs, rejected = normalize_many([
        RawJob(
            source="linkedin", external_id="100", title="Software Engineer", company="Acme",
            location="Paris, France", apply_link="https://linkedin.com/jobs/view/100",
            description="Python role.", declared_skills=["Python"], discovered_query="software",
        ),
        RawJob(
            source="other", external_id="abc", title="Software Engineer", company="Acme",
            location="Paris", apply_link="https://jobs.acme.test/apply?id=abc&utm_source=board",
            description="Build production Python and AWS cloud services for customers.",
            declared_skills=["Python", "AWS"], discovered_query="cloud",
        ),
    ], profile_key="worldwide-masters-tech")
    assert not rejected and len(jobs) == 1
    job = jobs[0]
    assert len(job.description) > len("Python role.")
    assert job.source_ids == ["linkedin:100", "other:abc"]
    assert job.discovered_queries == ["cloud", "software"]
    assert job.discovered_profiles == ["worldwide-masters-tech"]
    assert "utm_source" not in job.apply_link and "id=abc" in job.apply_link
from app.utils import utcnow


def make_raw(**overrides) -> RawJob:
    defaults = dict(
        source="naukri",
        title="Python Developer",
        company="Acme Corp",
        apply_link="https://example.com/jobs/1",
        location="Bengaluru",
        experience_text="0-2 Yrs",
        description="Build REST APIs with Django and PostgreSQL. Docker experience a plus.",
        declared_skills=["Python", "Django"],
    )
    defaults.update(overrides)
    return RawJob(**defaults)


class TestNormalize:
    def test_happy_path(self):
        job, reason = normalize(make_raw())
        assert reason is None
        assert job is not None
        assert job.category == "Backend"
        assert job.seniority == "fresher"
        assert {"Python", "Django", "PostgreSQL", "Docker"} <= set(job.skills)
        assert job.experience_min == 0 and job.experience_max == 2

    def test_missing_apply_link_is_incomplete(self):
        _, reason = normalize(make_raw(apply_link=""))
        assert reason == REJECT_INCOMPLETE

    def test_non_web_apply_link_is_incomplete(self):
        _, reason = normalize(make_raw(apply_link="javascript:void(0)"))
        assert reason == REJECT_INCOMPLETE

    def test_non_tech_rejected(self):
        _, reason = normalize(
            make_raw(title="Retail Store Manager", description="Manage the shop floor.",
                     declared_skills=[])
        )
        assert reason == REJECT_NOT_TECH

    def test_experience_above_ceiling_rejected(self):
        _, reason = normalize(make_raw(experience_text="8-12 Yrs"))
        assert reason == REJECT_EXPERIENCE

    def test_experience_at_ceiling_kept(self):
        job, reason = normalize(
            make_raw(experience_text=f"{settings.max_experience_years}-9 Yrs")
        )
        assert reason is None and job is not None

    def test_dead_listing_rejected(self):
        _, reason = normalize(
            make_raw(description="This job is no longer available to applicants.")
        )
        assert reason == REJECT_DEAD

    def test_description_less_posting_survives_when_title_is_tech(self):
        # LinkedIn cards often arrive with no description; they are still useful.
        job, reason = normalize(
            make_raw(description="", declared_skills=[], title="Backend Developer")
        )
        assert reason is None and job is not None

    def test_stale_posting_rejected(self):
        old = utcnow() - timedelta(days=settings.max_posting_age_days + 10)
        _, reason = normalize(make_raw(posted_at=old))
        assert reason == "posting_too_old"

    def test_declared_skills_outside_taxonomy_are_preserved(self):
        job, _ = normalize(make_raw(declared_skills=["Python", "Zephyr Scale"]))
        assert any(s.lower() == "zephyr scale" for s in job.skills)


class TestDeadDetection:
    def test_positive(self):
        assert looks_dead("Sorry, this job is no longer available") is True

    def test_negative(self):
        assert looks_dead("We are hiring backend engineers") is False

    def test_empty(self):
        assert looks_dead("") is False


class TestFingerprintAndDedup:
    def test_same_posting_different_location_suffix(self):
        a = make_fingerprint("Python Developer", "Acme", "Bengaluru")
        b = make_fingerprint("python developer", "ACME", "Bengaluru, Karnataka, India")
        assert a == b

    def test_different_company_differs(self):
        assert make_fingerprint("SDE", "Acme", "Pune") != make_fingerprint("SDE", "Globex", "Pune")

    def test_normalize_many_collapses_duplicates_keeping_richer_record(self):
        thin = make_raw(description="Short.", declared_skills=["Python"])
        rich = make_raw(
            source="linkedin",
            description="A much longer description " * 20,
            declared_skills=["Python"],
        )
        kept, rejected = normalize_many([thin, rich])
        assert len(kept) == 1
        assert kept[0].source == "linkedin"
        assert rejected == {}

    def test_rejections_are_counted(self):
        kept, rejected = normalize_many(
            [make_raw(), make_raw(title="Sales Executive", declared_skills=[],
                                  description="Sell products.")]
        )
        assert len(kept) == 1
        assert rejected.get(REJECT_NOT_TECH) == 1
