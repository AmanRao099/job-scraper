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

import logging
import re

from bs4 import BeautifulSoup

from app.config import settings
from app.http_client import HttpClient, gather_bounded
from app.sources.base import (
    JobSource, RawJob, SearchScope, blocked_page_reason, is_no_results_page,
)
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


def seo_key(query: str, location_slug: str = "") -> str:
    """Naukri's slug for a search: `python-developer-jobs-in-bangalore`."""
    slug = f"{slugify(query)}-jobs"
    return f"{slug}-in-{slugify(location_slug)}" if location_slug else slug


def search_url(query: str, page: int, location_slug: str = "") -> str:
    """Naukri paginates by URL suffix: `/python-developer-jobs-2`."""
    slug = seo_key(query, location_slug)
    return f"{BASE}/{slug}" if page <= 1 else f"{BASE}/{slug}-{page}"


class NaukriSource(JobSource):
    name = "naukri"

    def __init__(self, client: HttpClient, progress=None,
                 renderer: BrowserRenderer | None = None,
                 scope: SearchScope | None = None) -> None:
        super().__init__(client, progress, scope)
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
            self.stats.fail("browser_unavailable")
            self.finish_stats(0)
            return []

        results = await gather_bounded(
            [self._fetch_query(query) for query in queries],
            settings.naukri_query_concurrency,
        )

        jobs: list[RawJob] = []
        for query, result in zip(queries, results):
            if isinstance(result, BaseException):
                logger.warning("Naukri query %r failed: %s", query, result)
                self.stats.fail("query_exception")
                continue
            jobs.extend(result)

        unique: dict[str, RawJob] = {}
        for job in jobs:
            key = job.external_id or job.apply_link
            if key in unique:
                self.stats.duplicates_skipped += 1
            unique.setdefault(key, job)
        deduped = list(unique.values())
        self.finish_stats(len(deduped))
        return deduped

    async def _probe_api(self) -> None:
        if self._api_ok is not None:
            return
        if not settings.naukri_try_api:
            self._api_ok = False
            return

        result = await self.client.fetch_json(
            SEARCH_API,
            params=self._api_params("software engineer", 1),
            headers={**API_HEADERS, "User-Agent": self.client.random_user_agent()},
        )
        payload = result.value
        self._api_ok = isinstance(payload, dict) and bool(payload.get("jobDetails"))
        if not result.ok:
            reason = "api_gated" if result.status_code in {401, 403, 406, 429} else (
                result.error or "api_probe_failed"
            )
            self.stats.warn(reason, blocked=reason == "api_gated")
            if reason != "api_gated":
                self.stats.network_failures += 1
        await self.report(
            0,
            "naukri JSON API available - using fast path"
            if self._api_ok
            else "naukri JSON API is gated - rendering search pages instead",
        )

    async def _fetch_query(self, query: str) -> list[RawJob]:
        jobs: list[RawJob] = []
        page_signatures: set[tuple[str, ...]] = set()
        for page in range(1, self.pages + 1):
            if self.cancelled:
                break
            batch = (
                await self._fetch_api_page(query, page)
                if self._api_ok
                else await self._fetch_rendered_page(query, page)
            )
            count = len(batch) if batch is not None else 0
            await self.report(1, f"naukri '{query}' page {page} -> {count}")
            if batch is None:
                break
            if not batch:
                break  # ran out of results for this term
            signature = tuple(job.external_id or job.apply_link for job in batch)
            if signature in page_signatures:
                self.stats.repeated_pages += 1
                self.stats.fail("repeated_page")
                break
            page_signatures.add(signature)
            for job in batch:
                job.discovered_query = query
            jobs.extend(batch)
        return jobs

    # -------------------------------------------------------------- fast path
    def _api_params(self, query: str, page: int) -> dict:
        city = self.scope.naukri_location_slug
        params = {
            "noOfResults": self.page_size,
            "urlType": "search_by_key_loc" if city else "search_by_keyword",
            "searchType": "adv",
            "keyword": query,
            "pageNo": page,
            "k": query,
            "seoKey": seo_key(query, city),
            "src": "jobsearchDesk",
            "sort": "f",  # freshness
        }
        if city:
            params["location"] = city
        return params

    async def _fetch_api_page(self, query: str, page: int) -> list[RawJob] | None:
        self.stats.pages_attempted += 1
        result = await self.client.fetch_json(
            SEARCH_API,
            params=self._api_params(query, page),
            headers={
                **API_HEADERS,
                "User-Agent": self.client.random_user_agent(),
                "Referer": search_url(query, 1, self.scope.naukri_location_slug),
            },
        )
        if not result.ok:
            self.stats.fail(result.error or "network_error", network=True)
            return None
        payload = result.value
        if not isinstance(payload, dict):
            self.stats.parse_failures += 1
            self.stats.fail("unexpected_api_structure")
            return None
        details = payload.get("jobDetails")
        if details is None:
            self.stats.parse_failures += 1
            self.stats.fail("missing_job_details")
            return None
        if not isinstance(details, list):
            self.stats.parse_failures += 1
            self.stats.fail("invalid_job_details")
            return None
        parsed = [
            job
            for job in (self._parse_api_job(item) for item in details)
            if job
        ]
        self.stats.responses_accepted += 1
        return parsed

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
    async def _fetch_rendered_page(self, query: str, page: int) -> list[RawJob] | None:
        self.stats.pages_attempted += 1
        if self.renderer is None or not self.renderer.enabled:
            self.stats.fail("browser_unavailable")
            return None
        html = await self.renderer.render(
            search_url(query, page, self.scope.naukri_location_slug),
            wait_for=CARD_SELECTOR,
            scrolls=3,
        )
        if not html:
            self.stats.fail("render_failed", network=True)
            return None
        blocked = blocked_page_reason(html)
        if blocked:
            self.stats.fail(f"blocked:{blocked}", blocked=True)
            return None
        parsed = self.parse_search_html(html)
        if not parsed and is_no_results_page(html):
            self.stats.responses_accepted += 1
            return []
        if not parsed:
            self.stats.parse_failures += 1
            self.stats.fail("unexpected_search_markup")
            return None
        self.stats.responses_accepted += 1
        return parsed

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
