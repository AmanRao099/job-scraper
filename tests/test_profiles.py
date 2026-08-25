"""Tests for targeted scrape profiles and the startup heuristic."""

import pytest

from app.enrich import normalize
from app.pipeline import resolve_queries, resolve_sources
from app.profiles import (
    BANGALORE_FRESHER_STARTUPS,
    REJECT_NOT_FRESHER,
    REJECT_NOT_STARTUP,
    REJECT_OFF_LOCATION,
    REJECT_NOT_ABROAD,
    REJECT_NO_MASTERS,
    WORLDWIDE_MASTERS_TECH,
    get_profile,
    resolve_profile,
)
from app.sources.base import RawJob, SearchScope
from app.sources.naukri import search_url, seo_key
from app.startups import is_enterprise, is_probable_startup, startup_signal

PROFILE = BANGALORE_FRESHER_STARTUPS

STARTUP_PITCH = (
    "We are an early-stage startup building developer tools. You will join the "
    "founding team and own features end to end. Python, Django and PostgreSQL."
)


def make_job(**overrides):
    """A normalised posting that the profile accepts, before any override."""
    defaults = dict(
        source="naukri",
        title="Software Engineer",
        company="Novastack Labs",
        apply_link="https://example.com/jobs/1",
        location="Bengaluru",
        experience_text="0-1 Yrs",
        description=STARTUP_PITCH,
        declared_skills=["Python", "Django"],
    )
    defaults.update(overrides)
    job, reason = normalize(RawJob(**defaults))
    assert reason is None, f"enrichment rejected the fixture: {reason}"
    return job


class TestStartupHeuristic:
    @pytest.mark.parametrize(
        "company",
        [
            "Tata Consultancy Services",
            "Infosys BPM",
            "Accenture Solutions Pvt Ltd",
            "Deloitte India",
            "JPMorgan Chase & Co.",
            "ABC Staffing Solutions",
            "Talent Solutions Consultancy",
        ],
    )
    def test_enterprises_and_staffing_are_blocked(self, company):
        assert is_enterprise(company) is True
        assert is_probable_startup(company, STARTUP_PITCH) is False

    @pytest.mark.parametrize(
        "company", ["Novastack Labs", "Bytecrate", "Kite Systems Pvt Ltd"]
    )
    def test_unknown_company_is_not_an_enterprise(self, company):
        assert is_enterprise(company) is False

    @pytest.mark.parametrize(
        "text",
        [
            "Join our early-stage startup",
            "Series A funded product company",
            "Backed by Y Combinator",
            "You will be a founding engineer",
            "A bootstrapped team of twelve",
        ],
    )
    def test_positive_signals(self, text):
        assert startup_signal(text) is not None

    @pytest.mark.parametrize(
        "text",
        [
            "Troubleshoot application startup issues in production",
            "Maintain startup scripts and shutdown procedures",
            "Reduce JVM startup time",
        ],
    )
    def test_sysadmin_use_of_startup_is_not_a_signal(self, text):
        assert startup_signal(text) is None

    def test_no_signal_means_not_a_startup(self):
        assert is_probable_startup("Kite Systems", "Maintain our billing service.") is False


class TestProfileFilter:
    def test_accepts_a_bengaluru_fresher_startup_role(self):
        assert PROFILE.reject_reason(make_job()) is None

    @pytest.mark.parametrize(
        "location", ["Bengaluru", "Bangalore/Bengaluru", "Whitefield, Bengaluru", "Koramangala"]
    )
    def test_city_spellings_and_localities(self, location):
        assert PROFILE.reject_reason(make_job(location=location)) is None

    @pytest.mark.parametrize("location", ["Pune", "Hyderabad", "Remote", "India"])
    def test_other_locations_are_rejected(self, location):
        assert PROFILE.reject_reason(make_job(location=location)) == REJECT_OFF_LOCATION

    def test_experience_above_one_year_is_not_fresher(self):
        job = make_job(experience_text="2-4 Yrs")
        assert PROFILE.reject_reason(job) == REJECT_NOT_FRESHER

    def test_intern_counts_as_fresher(self):
        job = make_job(title="Software Engineer Intern", experience_text="")
        assert job.seniority == "intern"
        assert PROFILE.reject_reason(job) is None

    def test_unstated_experience_on_a_plain_title_is_rejected(self):
        # No years, no fresher wording: `detect_seniority` calls it mid-level,
        # and a city+startup listing must not quietly include those.
        job = make_job(
            experience_text="",
            description="We are an early-stage startup. Build REST APIs with Django.",
        )
        assert job.seniority == "mid"
        assert PROFILE.reject_reason(job) == REJECT_NOT_FRESHER

    def test_enterprise_employer_is_rejected(self):
        job = make_job(company="Infosys Limited")
        assert PROFILE.reject_reason(job) == REJECT_NOT_STARTUP

    def test_company_without_a_startup_signal_is_rejected(self):
        job = make_job(
            description="Maintain our internal billing service. Django, PostgreSQL, REST APIs."
        )
        assert PROFILE.reject_reason(job) == REJECT_NOT_STARTUP

    def test_apply_partitions_and_counts(self):
        jobs = [
            make_job(),
            make_job(location="Pune"),
            make_job(company="Wipro Technologies"),
            make_job(experience_text="2-4 Yrs"),
        ]
        kept, rejections = PROFILE.apply(jobs)
        assert len(kept) == 1
        assert rejections == {
            REJECT_OFF_LOCATION: 1,
            REJECT_NOT_STARTUP: 1,
            REJECT_NOT_FRESHER: 1,
        }


class TestProfileWiring:
    def test_lookup_is_forgiving_about_separators(self):
        assert get_profile("Bangalore_Fresher_Startups") is PROFILE

    def test_unknown_profile_raises(self):
        with pytest.raises(ValueError):
            get_profile("mars-office")

    def test_resolve_profile_passes_objects_and_none_through(self):
        assert resolve_profile(None) is None
        assert resolve_profile(PROFILE) is PROFILE
        assert resolve_profile(PROFILE.key) is PROFILE

    def test_profile_supplies_its_own_queries(self):
        assert resolve_queries(None, None, PROFILE) == list(PROFILE.queries)

    def test_explicit_queries_still_win(self):
        assert resolve_queries(["sdet"], None, PROFILE) == ["sdet"]

    def test_query_limit_applies_to_profile_queries(self):
        assert resolve_queries(None, 3, PROFILE) == list(PROFILE.queries[:3])

    def test_scope_is_scoped_to_the_city(self):
        scope = PROFILE.scope
        assert scope.naukri_location_slug == "bangalore"
        assert scope.linkedin_geo_id != SearchScope.default().linkedin_geo_id


class TestWorldwideMastersProfile:
    def make_worldwide_job(self, **overrides):
        defaults = dict(
            source="linkedin",
            title="Senior Machine Learning Engineer",
            company="Global AI Labs",
            apply_link="https://www.linkedin.com/jobs/view/123",
            location="Berlin, Germany",
            experience_text="8+ years",
            description=(
                "Build production ML systems with Python and PyTorch. "
                "A Masters degree in Computer Science is preferred."
            ),
            declared_skills=["Python", "PyTorch"],
        )
        defaults.update(overrides)
        job, reason = normalize(RawJob(**defaults), allow_any_experience=True)
        assert reason is None, f"enrichment rejected fixture: {reason}"
        return job

    def test_retains_senior_international_job(self):
        job = self.make_worldwide_job()
        assert job.experience_min == 8
        assert WORLDWIDE_MASTERS_TECH.reject_reason(job) is None

    def test_ordinary_normalization_still_rejects_senior_job(self):
        raw = RawJob(
            source="linkedin",
            title="Senior Machine Learning Engineer",
            company="Global AI Labs",
            apply_link="https://www.linkedin.com/jobs/view/123",
            location="Berlin, Germany",
            experience_text="8+ years",
            description="Python and PyTorch. Masters degree required.",
        )
        _, reason = normalize(raw)
        assert reason == "experience_too_high"

    def test_rejects_search_hit_without_description_evidence(self):
        job = self.make_worldwide_job(description="Build Python cloud systems.")
        assert WORLDWIDE_MASTERS_TECH.reject_reason(job) == REJECT_NO_MASTERS

    def test_rejects_india_based_job(self):
        job = self.make_worldwide_job(location="Hyderabad, Telangana")
        assert WORLDWIDE_MASTERS_TECH.reject_reason(job) == REJECT_NOT_ABROAD

    def test_non_technical_masters_job_never_reaches_profile(self):
        raw = RawJob(
            source="linkedin",
            title="Marketing Director",
            company="Global Retail",
            apply_link="https://www.linkedin.com/jobs/view/456",
            location="London, United Kingdom",
            description="Masters degree required. Lead brand marketing campaigns.",
        )
        _, reason = normalize(raw, allow_any_experience=True)
        assert reason == "not_tech"

    def test_default_sources_are_international_capable(self):
        assert resolve_sources(None, WORLDWIDE_MASTERS_TECH) == ["linkedin"]

    def test_naukri_cannot_be_forced_into_worldwide_profile(self):
        with pytest.raises(ValueError):
            resolve_sources(["naukri"], WORLDWIDE_MASTERS_TECH)

    def test_linkedin_scope_includes_all_experience_levels(self):
        assert WORLDWIDE_MASTERS_TECH.scope.linkedin_experience_filter == ""


class TestNaukriUrls:
    def test_city_lands_in_the_search_url(self):
        assert seo_key("python developer", "bangalore") == "python-developer-jobs-in-bangalore"
        assert search_url("python developer", 1, "bangalore").endswith(
            "/python-developer-jobs-in-bangalore"
        )
        assert search_url("python developer", 3, "bangalore").endswith(
            "/python-developer-jobs-in-bangalore-3"
        )

    def test_nationwide_urls_are_unchanged(self):
        assert search_url("python developer", 1).endswith("/python-developer-jobs")
        assert search_url("python developer", 2).endswith("/python-developer-jobs-2")
