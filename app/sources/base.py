"""Source plugin contract.

Adding a board means dropping a new module in here that yields `RawJob`s and
registering it in `app/sources/__init__.py`. Nothing else in the pipeline needs
to change.
"""

from __future__ import annotations

import abc
import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from app.config import settings
from app.http_client import HttpClient

logger = logging.getLogger(__name__)

# Called by sources to report progress: (queries_done_delta, message)
ProgressFn = Callable[[int, str], Awaitable[None] | None]


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

    @abc.abstractmethod
    def plan(self, queries: list[str]) -> int:
        """Return how many discrete units of work this run will perform."""

    @abc.abstractmethod
    async def fetch(self, queries: list[str]) -> list[RawJob]:
        """Fetch and return raw postings for the given search queries."""
