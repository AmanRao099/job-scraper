"""Naukri adapter.

Transport reality, verified against production:

* `GET /jobapi/v3/search` (the endpoint Naukri's own SPA calls) answers
  `406 {"message":"recaptcha required"}` for any client that cannot mint a
  reCAPTCHA token - plain HTTP, warmed cookies and in-page `fetch()` all fail.
* The search *page* renders fine in Chrome's new headless mode and each card
  carries title, company, location, experience, salary, a description snippet,
  the recruiter's skill tags and the posting age.

So this source renders search pages and parses cards. It still probes the JSON
API **once** per run - if Naukri ever relaxes the gate the fast path lights up
automatically - but it never burns a request per page discovering the block.

Crucially it never opens a detail page per posting, which is what made the old
implementation take hours: everything needed is already on the card.
"""

from __future__ import annotations

import asyncio
import logging
import re

from bs4 import BeautifulSoup

from app.config import settings
from app.http_client import HttpClient
from app.sources.base import JobSource, RawJob
from app.sources.browser import BrowserRenderer
from app.utils import (
    absolute_url,
    clean_text,
    from_epoch_ms,
    html_to_text,
    parse_relative_date,
    split_csv_field,
    strip_tracking,
)

logger = logging.getLogger(__name__)

BASE = "https://www.naukri.com"
SEARCH_API = f"{BASE}/jobapi/v3/search"
CARD_SELECTOR = "div.cust-job-tuple"

API_HEADERS = {
    "appid": "109",
    "systemid": "109",
    "clientid": "d3skt0p",
    "Accept": "application/json",
    "Referer": f"{BASE}/",
    "gid": "LOCATION,INDUSTRY,EDUCATION,FAREA_ROLE",
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(query: str) -> str:
    return _SLUG_RE.sub("-", query.lower()).strip("-")


def search_url(query: str, page: int) -> str:
    """Naukri paginates by URL suffix: `/python-developer-jobs-2`."""
    slug = f"{slugify(query)}-jobs"
    return f"{BASE}/{slug}" if page <= 1 else f"{BASE}/{slug}-{page}"


class NaukriSource(JobSource):
    name = "naukri"

    def __init__(self, client: HttpClient, progress=None,
                 renderer: BrowserRenderer | None = None) -> None:
        super().__init__(client, progress)
        self.renderer = renderer
        self.pages = max(1, settings.naukri_pages)
        self.page_size = max(20, min(settings.naukri_page_size, 100))
        # None = not probed yet, True/False = decided for this run.
        self._api_ok: bool | None = None

    def plan(self, queries: list[str]) -> int:
        return len(queries) * self.pages

    # ------------------------------------------------------------------ fetch
    async def fetch(self, queries: list[str]) -> list[RawJob]:
        await self._probe_api()

        if not self._api_ok and (self.renderer is None or not self.renderer.enabled):
            await self.report(
                0,
                "naukri unavailable: JSON API is gated and no browser is running "
                "(install playwright and run `playwright install chromium`)",
            )
            return []

        results = await asyncio.gather(
            *(self._fetch_query(query) for query in queries), return_exceptions=True
        )

        jobs: list[RawJob] = []
        for query, result in zip(queries, results):
            if isinstance(result, BaseException):
                logger.warning("Naukri query %r failed: %s", query, result)
                continue
            jobs.extend(result)
        return jobs

    async def _probe_api(self) -> None:
        if self._api_ok is not None:
            return
        if not settings.naukri_try_api:
            self._api_ok = False
            return

        payload = await self.client.get_json(
            SEARCH_API,
            params=self._api_params("software engineer", 1),
            headers={**API_HEADERS, "User-Agent": self.client.random_user_agent()},
        )
        self._api_ok = isinstance(payload, dict) and bool(payload.get("jobDetails"))
        await self.report(
            0,
            "naukri JSON API available - using fast path"
            if self._api_ok
            else "naukri JSON API is gated - rendering search pages instead",
        )

    async def _fetch_query(self, query: str) -> list[RawJob]:
        jobs: list[RawJob] = []
        for page in range(1, self.pages + 1):
            if self.cancelled:
                break
            batch = (
                await self._fetch_api_page(query, page)
                if self._api_ok
                else await self._fetch_rendered_page(query, page)
            )
            await self.report(1, f"naukri '{query}' page {page} -> {len(batch)}")
            if not batch:
                break  # ran out of results for this term
            jobs.extend(batch)
        return jobs

    # -------------------------------------------------------------- fast path
    def _api_params(self, query: str, page: int) -> dict:
        return {
            "noOfResults": self.page_size,
            "urlType": "search_by_keyword",
            "searchType": "adv",
            "keyword": query,
            "pageNo": page,
            "k": query,
            "seoKey": f"{slugify(query)}-jobs",
            "src": "jobsearchDesk",
            "sort": "f",  # freshness
        }

    async def _fetch_api_page(self, query: str, page: int) -> list[RawJob]:
        payload = await self.client.get_json(
            SEARCH_API,
            params=self._api_params(query, page),
            headers={
                **API_HEADERS,
                "User-Agent": self.client.random_user_agent(),
                "Referer": search_url(query, 1),
            },
        )
        if not isinstance(payload, dict):
            return []
        return [
            job
            for job in (self._parse_api_job(item) for item in payload.get("jobDetails") or [])
            if job
        ]

    def _parse_api_job(self, item: dict) -> RawJob | None:
        if not isinstance(item, dict):
            return None

        title = clean_text(item.get("title"))
        company = clean_text(item.get("companyName"))
        apply_link = strip_tracking(
            absolute_url(item.get("jdURL") or item.get("staticUrl") or "", BASE)
        )
        if not (title and company and apply_link):
            return None

        placeholders = {
            str(p.get("type", "")).lower(): clean_text(p.get("label"))
            for p in item.get("placeholders") or []
            if isinstance(p, dict)
        }

        posted_at = from_epoch_ms(item.get("createdDate")) or parse_relative_date(
            item.get("footerPlaceholderLabel")
        )

        return RawJob(
            source=self.name,
            external_id=str(item.get("jobId") or "") or None,
            title=title,
            company=company,
            location=placeholders.get("location", ""),
            experience_text=placeholders.get("experience", ""),
            salary_text=placeholders.get("salary", ""),
            apply_link=apply_link,
            description=html_to_text(item.get("jobDescription")),
            posted_at=posted_at,
            declared_skills=split_csv_field(item.get("tagsAndSkills")),
        )

    # ---------------------------------------------------------- rendered path
    async def _fetch_rendered_page(self, query: str, page: int) -> list[RawJob]:
        if self.renderer is None or not self.renderer.enabled:
            return []
        html = await self.renderer.render(
            search_url(query, page), wait_for=CARD_SELECTOR, scrolls=3
        )
        if not html:
            return []
        return self.parse_search_html(html)

    def parse_search_html(self, html: str) -> list[RawJob]:
        soup = BeautifulSoup(html, "lxml")
        cards = soup.select(CARD_SELECTOR) or soup.select("article.jobTuple")

        jobs: list[RawJob] = []
        for card in cards:
            title_tag = card.select_one("a.title")
            if not title_tag:
                continue

            apply_link = strip_tracking(absolute_url(title_tag.get("href"), BASE))
            if not apply_link:
                continue

            company_tag = card.select_one("a.comp-name, span.comp-name")
            location_tag = card.select_one("span.locWdth, span.loc-wrap span")
            exp_tag = card.select_one("span.expwdth, span.exp-wrap span")
            salary_tag = card.select_one("span.sal-wrap span, span.sal")
            desc_tag = card.select_one("span.job-desc")
            date_tag = card.select_one("span.job-post-day")

            skills = [
                clean_text(li.get_text())
                for li in card.select("ul.tags-gt li.dot-gt, ul.tags-gt li")
            ]

            jobs.append(
                RawJob(
                    source=self.name,
                    external_id=self._id_from_link(apply_link),
                    title=clean_text(title_tag.get_text()),
                    company=clean_text(company_tag.get_text()) if company_tag else "",
                    location=clean_text(location_tag.get_text()) if location_tag else "",
                    experience_text=clean_text(exp_tag.get_text()) if exp_tag else "",
                    salary_text=clean_text(salary_tag.get_text()) if salary_tag else "",
                    apply_link=apply_link,
                    description=clean_text(desc_tag.get_text()) if desc_tag else "",
                    posted_at=parse_relative_date(date_tag.get_text() if date_tag else None),
                    declared_skills=[s for s in skills if s],
                )
            )
        return jobs

    @staticmethod
    def _id_from_link(link: str) -> str | None:
        # .../job-listings-python-developer-acme-pune-2-to-5-years-030826501234
        tail = link.rstrip("/").rsplit("-", 1)[-1]
        return tail if tail.isdigit() and len(tail) >= 8 else None
