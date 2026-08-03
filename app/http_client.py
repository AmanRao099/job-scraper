"""Shared async HTTP client: bounded concurrency, retries, polite jitter.

Replaces the old approach of driving a real browser and `time.sleep(8)` for
every single page. Both target boards expose JSON/HTML endpoints that need no
JavaScript, so a plain client is ~50x faster and survives in a small container.
"""

from __future__ import annotations

import asyncio
import logging
import random
from types import TracebackType

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
]

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class RetryableError(Exception):
    """Raised when a response is worth retrying."""


class HttpClient:
    """Thin wrapper around httpx.AsyncClient with a global concurrency gate."""

    def __init__(
        self,
        *,
        concurrency: int | None = None,
        timeout: float | None = None,
        retries: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.concurrency = concurrency or settings.http_concurrency
        self.timeout = timeout or settings.http_timeout
        self.retries = retries if retries is not None else settings.http_retries
        self._semaphore = asyncio.Semaphore(self.concurrency)
        self._base_headers = headers or {}
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> HttpClient:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=self.concurrency * 2,
                max_keepalive_connections=self.concurrency,
            ),
            headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                **self._base_headers,
            },
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def random_user_agent() -> str:
        return random.choice(DEFAULT_USER_AGENTS)

    async def _sleep_jitter(self) -> None:
        await asyncio.sleep(
            random.uniform(settings.request_delay_min, settings.request_delay_max)
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        expect_json: bool = False,
    ) -> httpx.Response | None:
        """Perform a request with retry/backoff. Returns None if it never succeeded."""
        if self._client is None:
            raise RuntimeError("HttpClient must be used as an async context manager")

        last_error: str = "unknown"
        for attempt in range(1, self.retries + 1):
            async with self._semaphore:
                await self._sleep_jitter()
                try:
                    response = await self._client.request(
                        method, url, params=params, headers=headers
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                else:
                    if response.status_code in RETRY_STATUS:
                        last_error = f"HTTP {response.status_code}"
                    elif response.status_code >= 400:
                        logger.debug("%s %s -> HTTP %s", method, url, response.status_code)
                        return None
                    elif expect_json and "json" not in response.headers.get(
                        "content-type", ""
                    ):
                        last_error = "non-JSON response (likely a block page)"
                    else:
                        return response

            if attempt < self.retries:
                backoff = min(2 ** attempt + random.uniform(0, 1), 20)
                logger.debug(
                    "Retry %s/%s for %s after %s (sleeping %.1fs)",
                    attempt, self.retries, url, last_error, backoff,
                )
                await asyncio.sleep(backoff)

        logger.warning("Giving up on %s after %s attempts (%s)", url, self.retries, last_error)
        return None

    async def get_json(
        self, url: str, *, params: dict | None = None, headers: dict | None = None
    ) -> dict | list | None:
        response = await self.request("GET", url, params=params, headers=headers, expect_json=True)
        if response is None:
            return None
        try:
            return response.json()
        except ValueError:
            logger.debug("Malformed JSON from %s", url)
            return None

    async def get_text(
        self, url: str, *, params: dict | None = None, headers: dict | None = None
    ) -> str | None:
        response = await self.request("GET", url, params=params, headers=headers)
        return response.text if response is not None else None


async def gather_bounded(coros: list, limit: int) -> list:
    """Run coroutines with a cap, returning results in order (exceptions included)."""
    semaphore = asyncio.Semaphore(limit)

    async def _run(coro):
        async with semaphore:
            return await coro

    return await asyncio.gather(*(_run(c) for c in coros), return_exceptions=True)
