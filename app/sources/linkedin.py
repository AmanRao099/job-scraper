"""LinkedIn adapter (public guest endpoints, no login).

LinkedIn serves its logged-out job search from two small endpoints that return
plain HTML fragments:

  /jobs-guest/jobs/api/seeMoreJobPostings/search  -> 10 cards per call
  /jobs-guest/jobs/api/jobPosting/{id}            -> one description

Neither needs JavaScript, so the browser is only a fallback. Descriptions are
fetched concurrently under the shared semaphore rather than one tab at a time.

LinkedIn rate-limits guest traffic aggressively, so this source deliberately
runs at lower concurrency than Naukri and treats 429 as a retry-with-backoff.
"""

from __future__ import annotations

import asyncio
import logging

from bs4 import BeautifulSoup

from app.config import settings
from app.http_client import HttpClient
from app.sources.base import JobSource, RawJob
from app.sources.browser import BrowserRenderer
from app.utils import (
    absolute_url,
    clean_text,
    html_to_text,
    parse_iso_date,
    parse_relative_date,
    strip_tracking,
)

logger = logging.getLogger(__name__)

BASE = "https://www.linkedin.com"
SEARCH_API = f"{BASE}/jobs-guest/jobs/api/seeMoreJobPostings/search"
POSTING_API = f"{BASE}/jobs-guest/jobs/api/jobPosting"

PAGE_SIZE = 10  # fixed by the endpoint

# f_E: 1=Internship 2=Entry level 3=Associate 4=Mid-Senior 5=Director
EXPERIENCE_FILTER = "1,2,3"

GUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": f"{BASE}/jobs",
    "X-Requested-With": "XMLHttpRequest",
}

# Descriptions are the bulk of the requests and LinkedIn's guest tier throttles
# them hard - measured: 6 in flight produces near-100% HTTP 429. Two in flight
# with a spacing delay sustains ~3 descriptions/sec without tripping the limit.
DESCRIPTION_CONCURRENCY = 2
DESCRIPTION_SPACING_SECONDS = 0.35


class LinkedInSource(JobSource):
    name = "linkedin"

    def __init__(self, client: HttpClient, progress=None,
                 renderer: BrowserRenderer | None = None) -> None:
        super().__init__(client, progress)
        self.renderer = renderer
        self.pages = max(1, settings.linkedin_pages)
        self._desc_semaphore = asyncio.Semaphore(DESCRIPTION_CONCURRENCY)

    def plan(self, queries: list[str]) -> int:
        return len(queries) * self.pages

    # ------------------------------------------------------------------ fetch
    async def fetch(self, queries: list[str]) -> list[RawJob]:
        results = await asyncio.gather(
            *(self._fetch_query(query) for query in queries),
            return_exceptions=True,
        )

        jobs: list[RawJob] = []
        for query, result in zip(queries, results):
            if isinstance(result, BaseException):
                logger.warning("LinkedIn query %r failed: %s", query, result)
                continue
            jobs.extend(result)

        # Cards are deduped before the (expensive) description fetch so we never
        # pay twice for the same posting appearing under two search terms.
        unique: dict[str, RawJob] = {}
        for job in jobs:
            key = job.external_id or job.apply_link
            unique.setdefault(key, job)
        deduped = list(unique.values())

        if settings.linkedin_fetch_descriptions and deduped and not self.cancelled:
            await self._attach_descriptions(deduped)

        return deduped

    async def _fetch_query(self, query: str) -> list[RawJob]:
        jobs: list[RawJob] = []
        for page in range(self.pages):
            if self.cancelled:
                break
            batch = await self._fetch_page(query, page)
            await self.report(1, f"linkedin '{query}' page {page + 1}")
            if not batch:
                break  # no more results for this term
            jobs.extend(batch)

        if not jobs:
            jobs = await self._fetch_via_browser(query)

        await self.report(0, f"linkedin '{query}' -> {len(jobs)} postings")
        return jobs

    async def _fetch_page(self, query: str, page: int) -> list[RawJob]:
        params = {
            "keywords": query,
            "location": settings.location,
            "geoId": settings.linkedin_geo_id,
            "f_E": EXPERIENCE_FILTER,
            "f_TPR": f"r{settings.max_posting_age_days * 86400}"
            if settings.max_posting_age_days
            else "",
            "start": page * PAGE_SIZE,
            "sortBy": "DD",  # most recent
        }
        params = {k: v for k, v in params.items() if v != ""}
        headers = {**GUEST_HEADERS, "User-Agent": self.client.random_user_agent()}

        html = await self.client.get_text(SEARCH_API, params=params, headers=headers)
        if not html or "base-card" not in html:
            return []
        return self._parse_cards(html)

    # ------------------------------------------------------------------ parse
    def _parse_cards(self, html: str) -> list[RawJob]:
        soup = BeautifulSoup(html, "lxml")
        cards = soup.select("div.base-card") or soup.select("li")

        jobs: list[RawJob] = []
        for card in cards:
            title_tag = card.select_one("h3.base-search-card__title")
            company_tag = card.select_one("h4.base-search-card__subtitle")
            location_tag = card.select_one("span.job-search-card__location")

            # The company anchor comes first in the DOM, so target the job link
            # explicitly instead of taking the first <a> like the old code did.
            link_tag = (
                card.select_one("a.base-card__full-link")
                or card.select_one('a[href*="/jobs/view/"]')
            )
            if not (title_tag and link_tag):
                continue

            apply_link = strip_tracking(absolute_url(link_tag.get("href"), BASE))
            if "/jobs/view/" not in apply_link:
                continue

            urn = card.get("data-entity-urn") or ""
            external_id = urn.rsplit(":", 1)[-1] if urn else self._id_from_link(apply_link)

            time_tag = card.select_one("time")
            posted_at = parse_iso_date(
                time_tag.get("datetime") if time_tag else None
            ) or parse_relative_date(time_tag.get_text() if time_tag else None)

            jobs.append(
                RawJob(
                    source=self.name,
                    external_id=external_id,
                    title=clean_text(title_tag.get_text()),
                    company=clean_text(company_tag.get_text()) if company_tag else "",
                    location=clean_text(location_tag.get_text()) if location_tag else "",
                    apply_link=apply_link,
                    posted_at=posted_at,
                )
            )
        return jobs

    @staticmethod
    def _id_from_link(link: str) -> str | None:
        # .../jobs/view/software-engineer-at-acme-3812345678
        tail = link.rstrip("/").rsplit("-", 1)[-1]
        return tail if tail.isdigit() else None

    # ----------------------------------------------------------- descriptions
    async def _attach_descriptions(self, jobs: list[RawJob]) -> None:
        targets = [job for job in jobs if job.external_id]
        await self.report(0, f"linkedin fetching {len(targets)} descriptions")

        await asyncio.gather(
            *(self._fetch_description(job) for job in targets),
            return_exceptions=True,
        )

    async def _fetch_description(self, job: RawJob) -> None:
        # Descriptions are the long tail of a run; drop the queued ones the
        # moment a stop is requested instead of draining thousands of requests.
        if self.cancelled:
            return
        async with self._desc_semaphore:
            if self.cancelled:
                return
            headers = {**GUEST_HEADERS, "User-Agent": self.client.random_user_agent()}
            html = await self.client.get_text(
                f"{POSTING_API}/{job.external_id}", headers=headers
            )
            await asyncio.sleep(DESCRIPTION_SPACING_SECONDS)
        if not html:
            return

        soup = BeautifulSoup(html, "lxml")
        body = (
            soup.select_one("div.show-more-less-html__markup")
            or soup.select_one("div.description__text")
            or soup.select_one("section.description")
        )
        if body is not None:
            job.description = html_to_text(str(body))

        # The criteria list carries the seniority and employment type that the
        # search card omits.
        for item in soup.select("li.description__job-criteria-item"):
            label = clean_text(
                item.select_one("h3.description__job-criteria-subheader").get_text()
                if item.select_one("h3.description__job-criteria-subheader")
                else ""
            ).lower()
            value_tag = item.select_one("span.description__job-criteria-text")
            value = clean_text(value_tag.get_text()) if value_tag else ""
            if not value:
                continue
            if "seniority" in label:
                job.experience_text = value
            elif "employment" in label and "intern" in value.lower():
                job.experience_text = f"{job.experience_text} Internship".strip()

    # --------------------------------------------------------------- fallback
    async def _fetch_via_browser(self, query: str) -> list[RawJob]:
        if self.renderer is None or not self.renderer.enabled:
            return []

        url = (
            f"{BASE}/jobs/search?keywords={query.replace(' ', '%20')}"
            f"&location={settings.location}&geoId={settings.linkedin_geo_id}"
            f"&f_E={EXPERIENCE_FILTER}"
        )
        logger.info("LinkedIn guest API returned nothing for %r; falling back to browser", query)
        html = await self.renderer.render(url, wait_for="div.base-card", scrolls=5)
        if not html:
            return []
        return self._parse_cards(html)
