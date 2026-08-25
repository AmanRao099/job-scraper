"""Source plugin contract.

Adding a board means dropping a new module in here that yields `RawJob`s and
registering it in `app/sources/__init__.py`. Nothing else in the pipeline needs
to change.
"""

from __future__ import annotations

import abc
import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic

from app.config import settings
from app.http_client import HttpClient

logger = logging.getLogger(__name__)

# Called by sources to report progress: (queries_done_delta, message)
ProgressFn = Callable[[int, str], Awaitable[None] | None]

_BLOCK_PAGE_RE = re.compile(
    r"(?:captcha|access\s+denied|too\s+many\s+requests|unusual\s+traffic|"
    r"verify\s+(?:you are|that you are|your identity)|sign\s+in\s+to\s+continue|"
    r"consent\s+required)",
    re.IGNORECASE,
)
_NO_RESULTS_RE = re.compile(
    r"(?:no\s+(?:matching\s+)?jobs?\s+(?:found|available)|no\s+results|"
    r"we\s+couldn['’]?t\s+find\s+any\s+jobs)",
    re.IGNORECASE,
)


def blocked_page_reason(content: str) -> str | None:
    """Return a non-evasive diagnostic when a provider serves a gate page."""
    match = _BLOCK_PAGE_RE.search(content[:100_000])
    return match.group(0).lower().replace("\n", " ") if match else None


def is_no_results_page(content: str) -> bool:
    return bool(_NO_RESULTS_RE.search(content[:100_000]))


@dataclass(slots=True)
class SourceStats:
    pages_attempted: int = 0
    responses_accepted: int = 0
    jobs_fetched: int = 0
    duplicates_skipped: int = 0
    repeated_pages: int = 0
    network_failures: int = 0
    parse_failures: int = 0
    blocked_responses: int = 0
    errors: dict[str, int] = field(default_factory=dict)
    warnings: dict[str, int] = field(default_factory=dict)
    duration_seconds: float = 0.0

    def fail(self, reason: str, *, network: bool = False, blocked: bool = False) -> None:
        self.errors[reason] = self.errors.get(reason, 0) + 1
        self.network_failures += int(network)
        self.blocked_responses += int(blocked)

    def warn(self, reason: str, *, blocked: bool = False) -> None:
        self.warnings[reason] = self.warnings.get(reason, 0) + 1
        self.blocked_responses += int(blocked)

    def as_dict(self) -> dict[str, object]:
        return {
            "pages_attempted": self.pages_attempted,
            "responses_accepted": self.responses_accepted,
            "jobs_fetched": self.jobs_fetched,
            "duplicates_skipped": self.duplicates_skipped,
            "repeated_pages": self.repeated_pages,
            "network_failures": self.network_failures,
            "parse_failures": self.parse_failures,
            "blocked_responses": self.blocked_responses,
            "errors": dict(self.errors),
            "warnings": dict(self.warnings),
            "duration_seconds": self.duration_seconds,
        }


@dataclass(frozen=True, slots=True)
class SearchScope:
    """Where a run searches.

    Every board spells a city differently - LinkedIn wants a numeric geoId,
    Naukri wants a URL slug - so the translation lives here rather than in the
    caller. The default mirrors the global settings, which is what an ordinary
    nationwide run uses; a scrape profile supplies a narrower one.
    """

    location: str
    linkedin_geo_id: str
    naukri_location_slug: str = ""
    # None uses the source's normal entry-level filter; an empty string means
    # all experience levels (used only by an explicit profile).
    linkedin_experience_filter: str | None = None

    @classmethod
    def default(cls) -> "SearchScope":
        return cls(
            location=settings.location,
            linkedin_geo_id=settings.linkedin_geo_id,
        )


@dataclass(slots=True)
class RawJob:
    """A posting exactly as the board reported it, before enrichment."""

    source: str
    title: str
    company: str
    apply_link: str
    external_id: str | None = None
    location: str = ""
    experience_text: str = ""
    salary_text: str = ""
    description: str = ""
    posted_at: datetime | None = None
    # Skills the board itself tagged; merged with ones we parse out.
    declared_skills: list[str] = field(default_factory=list)
    discovered_query: str = ""

    def is_usable(self) -> bool:
        return bool(self.title.strip() and self.company.strip() and self.apply_link.strip())


class JobSource(abc.ABC):
    """Base class for a job board adapter."""

    name: str = "base"

    def __init__(
        self,
        client: HttpClient,
        progress: ProgressFn | None = None,
        scope: SearchScope | None = None,
    ) -> None:
        self.client = client
        self._progress = progress
        self.scope = scope or SearchScope.default()
        self._cancel_event: asyncio.Event | None = None
        self.stats = SourceStats()
        self._started_at = monotonic()

    def bind(
        self,
        progress: ProgressFn | None = None,
        cancel_event: "asyncio.Event | None" = None,
    ) -> None:
        """Attach run-scoped callbacks after construction."""
        if progress is not None:
            self._progress = progress
        if cancel_event is not None:
            self._cancel_event = cancel_event

    @property
    def cancelled(self) -> bool:
        """True once the run has been asked to stop.

        Sources check this between pages and between queries so a cancelled run
        winds down cleanly and whatever it already collected still gets saved.
        """
        return self._cancel_event is not None and self._cancel_event.is_set()

    async def report(self, done_delta: int, message: str) -> None:
        logger.info("[%s] %s", self.name, message)
        if self._progress is None:
            return
        result = self._progress(done_delta, message)
        if hasattr(result, "__await__"):
            await result  # type: ignore[misc]

    def finish_stats(self, jobs: int) -> None:
        self.stats.jobs_fetched = jobs
        self.stats.duration_seconds = round(monotonic() - self._started_at, 3)

    @abc.abstractmethod
    def plan(self, queries: list[str]) -> int:
        """Return how many discrete units of work this run will perform."""

    @abc.abstractmethod
    async def fetch(self, queries: list[str]) -> list[RawJob]:
        """Fetch and return raw postings for the given search queries."""
