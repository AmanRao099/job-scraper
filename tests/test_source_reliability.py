import pytest

from app.config import settings
from app.http_client import FetchResult
from app.sources.linkedin import LinkedInSource
from app.sources.naukri import NaukriSource

pytestmark = pytest.mark.asyncio


CARD = """
<div class="base-card" data-entity-urn="urn:li:jobPosting:123">
  <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/dev-123?trk=x"></a>
  <h3 class="base-search-card__title">Python Engineer</h3>
  <h4 class="base-search-card__subtitle">Acme</h4>
  <span class="job-search-card__location">Paris, France</span>
</div>
"""


class TextClient:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)

    async def fetch_text(self, *args, **kwargs):
        return next(self.outcomes)

    @staticmethod
    def random_user_agent():
        return "test-agent"


class JsonClient:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)

    async def fetch_json(self, *args, **kwargs):
        return next(self.outcomes)

    @staticmethod
    def random_user_agent():
        return "test-agent"


async def test_repeated_linkedin_page_stops_loop_and_preserves_first_page(monkeypatch):
    monkeypatch.setattr(settings, "linkedin_fetch_descriptions", False)
    source = LinkedInSource(TextClient([FetchResult(value=CARD), FetchResult(value=CARD)]))
    source.pages = 5
    jobs = await source.fetch(["python"])
    assert len(jobs) == 1
    assert source.stats.pages_attempted == 2
    assert source.stats.repeated_pages == 1
    assert jobs[0].discovered_query == "python"


async def test_block_page_is_not_treated_as_empty_success(monkeypatch):
    monkeypatch.setattr(settings, "linkedin_fetch_descriptions", False)
    source = LinkedInSource(TextClient([FetchResult(value="<html>Access denied: CAPTCHA</html>")]))
    source.pages = 1
    jobs = await source.fetch(["python"])
    assert jobs == []
    assert source.stats.blocked_responses == 1
    assert source.stats.responses_accepted == 0


async def test_explicit_empty_results_stop_cleanly(monkeypatch):
    monkeypatch.setattr(settings, "linkedin_fetch_descriptions", False)
    source = LinkedInSource(
        TextClient([FetchResult(value="<main>No matching jobs found</main>")])
    )
    source.pages = 4
    assert await source.fetch(["python"]) == []
    assert source.stats.pages_attempted == 1
    assert source.stats.responses_accepted == 1
    assert source.stats.errors == {}


async def test_later_page_failure_keeps_partial_progress(monkeypatch):
    monkeypatch.setattr(settings, "linkedin_fetch_descriptions", False)
    source = LinkedInSource(
        TextClient([FetchResult(value=CARD), FetchResult(error="timeoutexception", retryable=True)])
    )
    source.pages = 3
    jobs = await source.fetch(["python"])
    assert len(jobs) == 1
    assert source.stats.network_failures == 1


async def test_naukri_repeated_api_page_stops_and_deduplicates():
    payload = {
        "jobDetails": [
            {
                "jobId": "12345678",
                "title": "Python Engineer",
                "companyName": "Acme",
                "jdURL": "/job-listings-python-engineer-acme-12345678",
                "jobDescription": "Build Python APIs",
                "tagsAndSkills": "Python,REST",
                "placeholders": [{"type": "location", "label": "Bengaluru"}],
            }
        ]
    }
    source = NaukriSource(JsonClient([FetchResult(value=payload), FetchResult(value=payload)]))
    source._api_ok = True
    source.pages = 5
    jobs = await source.fetch(["python"])
    assert len(jobs) == 1 and source.stats.repeated_pages == 1


async def test_naukri_unexpected_api_shape_is_parse_failure():
    source = NaukriSource(JsonClient([FetchResult(value={"unexpected": []})]))
    source._api_ok = True
    source.pages = 1
    jobs = await source.fetch(["python"])
    assert jobs == []
    assert source.stats.parse_failures == 1
    assert source.stats.responses_accepted == 0
