"""Liveness verification: ask the board whether a stored posting still exists.

The scraper's own signals only ever say "we saw this job". Nothing says "this
job is gone" - a posting the board deleted the day after we stored it looks
exactly like one our queries happened not to cover, and both sit in the database
until `stale_after_days` elapses. For a consumer that applies to what we serve,
three weeks of dead apply links is the whole problem.

So this module re-fetches the apply link and classifies the answer:

    True   the posting is still live      -> keep, reset the failure counter
    False  the board says it is gone      -> retire it now
    None   we could not tell              -> count it, retire only on a streak

The last case carries the design. Both boards throttle aggressively and serve
block pages under load, and a blocked request looks nothing like a deleted job.
Treating "no answer" as "gone" would delete the most live listings precisely
when the sweep is working hardest, so it never expires on a single failure.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import httpx

from app.config import settings
from app.db import session_scope
from app.enrich import looks_dead
from app.http_client import HttpClient
from app.repository import record_check, select_for_expiry_check
from app.utils import html_to_text

logger = logging.getLogger(__name__)

# Statuses that are proof the posting is gone, as opposed to proof we were
# blocked. 403 is deliberately absent: LinkedIn serves it to guests it does not
# like, on live and dead postings alike.
GONE_STATUSES = {404, 410}

LINKEDIN_POSTING_API = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting"

PROBE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# A live posting page is a substantial document. A board that redirects an
# expired listing to its search page hands back something much smaller, and the
# dead-listing phrases live in the first few KB either way.
MIN_LIVE_PAGE_CHARS = 400


@dataclass(slots=True)
class ExpiryReport:
    checked: int = 0
    alive: int = 0
    # The board answered that the posting is gone.
    gone: int = 0
    # No usable answer. A subset of these are retired anyway, once a posting has
    # failed `expiry_max_failures` probes in a row.
    inconclusive: int = 0
    retired: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "checked": self.checked,
            "alive": self.alive,
            "gone": self.gone,
            "inconclusive": self.inconclusive,
            "retired": self.retired,
            "reasons": self.reasons,
        }


@dataclass(slots=True)
class _Target:
    """The fields a probe needs, detached from the session.

    Read before the requests start so no database connection is held open for
    the length of a network round-trip.
    """

    id: int
    source: str
    external_id: str | None
    apply_link: str


def _probe_url(job: _Target) -> str:
    """Where to ask about this posting.

    LinkedIn's guest posting endpoint is the same one the scraper already uses
    for descriptions, and it answers 404 for a removed job - an unambiguous
    signal the public /jobs/view/ page does not give, since that one renders a
    200 "no longer accepting applications" shell instead.
    """
    if job.source == "linkedin" and job.external_id:
        return f"{LINKEDIN_POSTING_API}/{job.external_id}"
    return job.apply_link


def classify_response(
    response: httpx.Response | None, probe_url: str
) -> tuple[bool | None, str]:
    """Decide whether a probe response means the posting is still live.

    Returns (verdict, reason) where verdict follows the module contract:
    True live, False gone, None undecidable.
    """
    if response is None:
        # Retries exhausted: timeout, transport error, 429 or 5xx throughout.
        return None, "no_response"

    if response.status_code in GONE_STATUSES:
        return False, "http_gone"

    if response.status_code >= 400:
        # 401/403 and the rest: we were refused, which says nothing about the
        # posting. Never expire on this.
        return None, f"http_{response.status_code}"

    # A board that retires a listing by bouncing it to a search or landing page
    # answers 200 from a URL that is no longer the posting's own.
    if _redirected_away(str(response.url), probe_url):
        return False, "redirected_away"

    text = html_to_text(response.text, limit=8000)
    if looks_dead(text):
        return False, "dead_page"

    if len(text) < MIN_LIVE_PAGE_CHARS:
        # Too little content to read either way - usually an interstitial.
        return None, "thin_page"

    return True, "ok"


def _redirected_away(final_url: str, probe_url: str) -> bool:
    """True when a redirect landed somewhere that is not a posting page.

    Compared by path shape rather than by string equality: boards routinely
    rewrite a posting URL to a canonical slug or append tracking parameters,
    and treating that as a redirect away would expire every live job.
    """
    if final_url.rstrip("/") == probe_url.rstrip("/"):
        return False

    posting_markers = ("/jobs/view/", "/job-listings-", "/jobs-guest/", "/job/")
    was_posting = any(marker in probe_url for marker in posting_markers)
    still_posting = any(marker in final_url for marker in posting_markers)
    return was_posting and not still_posting


async def _probe(client: HttpClient, job: _Target) -> tuple[bool | None, str]:
    url = _probe_url(job)
    if not url:
        # Nothing to check against; leave it to the staleness path.
        return None, "no_link"

    headers = {**PROBE_HEADERS, "User-Agent": client.random_user_agent()}
    try:
        response = await client.request(
            "GET", url, headers=headers, allow_error_status=True
        )
    except Exception as exc:  # a probe must never take the sweep down
        logger.debug("Probe of %s failed: %s", url, exc)
        return None, "error"

    return classify_response(response, url)


async def verify_active_jobs(
    *,
    limit: int | None = None,
    recheck_after_hours: float | None = None,
    concurrency: int | None = None,
) -> ExpiryReport:
    """Probe a batch of active postings and retire the ones that are gone.

    One batch per call rather than the whole table: the request budget is shared
    with the scraper, and a sweep that runs every few hours reaches every
    posting inside its recheck window anyway.
    """
    report = ExpiryReport()

    async with session_scope() as session:
        due = await select_for_expiry_check(session, limit, recheck_after_hours)
        targets = [
            _Target(job.id, job.source, job.external_id, job.apply_link) for job in due
        ]

    if not targets:
        return report

    logger.info("Expiry sweep: probing %s posting(s)", len(targets))

    # The client's own semaphore is the only gate needed; it already bounds
    # in-flight requests and applies the politeness jitter between them.
    parallel = (
        concurrency if concurrency is not None else settings.expiry_check_concurrency
    )

    async with HttpClient(concurrency=max(parallel, 1)) as client:
        results = await asyncio.gather(
            *(_probe_one(client, job) for job in targets), return_exceptions=True
        )

    async with session_scope() as session:
        for result in results:
            if isinstance(result, BaseException):
                logger.debug("Probe task failed: %s", result)
                continue

            job_id, verdict, reason = result
            report.checked += 1
            report.reasons[reason] = report.reasons.get(reason, 0) + 1

            if await record_check(session, job_id, alive=verdict):
                report.retired += 1
            if verdict is True:
                report.alive += 1
            elif verdict is False:
                report.gone += 1
            else:
                report.inconclusive += 1

    logger.info(
        "Expiry sweep: %s checked, %s alive, %s gone, %s inconclusive, %s retired",
        report.checked, report.alive, report.gone, report.inconclusive, report.retired,
    )
    return report


async def _probe_one(client: HttpClient, job: _Target) -> tuple[int, bool | None, str]:
    verdict, reason = await _probe(client, job)
    return job.id, verdict, reason
