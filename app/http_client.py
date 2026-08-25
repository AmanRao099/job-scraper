"""Shared bounded HTTP transport with explicit failure outcomes."""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Generic, TypeVar

import httpx

from app.config import settings

logger = logging.getLogger(__name__)
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class FetchResult(Generic[T]):
    value: T | None = None
    error: str | None = None
    status_code: int | None = None
    attempts: int = 0
    retryable: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


class ResponseTooLarge(Exception):
    pass


class InvalidContentType(Exception):
    pass


class HttpClient:
    """Reusable httpx client with bounded retries and concurrency."""

    def __init__(self, *, concurrency: int | None = None, timeout: float | None = None,
                 retries: int | None = None, headers: dict[str, str] | None = None) -> None:
        self.concurrency = concurrency or settings.http_concurrency
        self.read_timeout = timeout or settings.effective_http_read_timeout
        self.retries = retries if retries is not None else settings.http_retries
        self._semaphore = asyncio.Semaphore(self.concurrency)
        self._base_headers = headers or {}
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "HttpClient":
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.http_connect_timeout,
                read=self.read_timeout,
                write=settings.http_write_timeout,
                pool=settings.http_pool_timeout,
            ),
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=self.concurrency,
                max_keepalive_connections=self.concurrency,
            ),
            headers={
                "User-Agent": settings.http_user_agent,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                **self._base_headers,
            },
        )
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None,
                        exc: BaseException | None, tb: TracebackType | None) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def random_user_agent() -> str:
        """Compatibility shim returning the configured, stable user agent."""
        return settings.http_user_agent

    async def _sleep_jitter(self) -> None:
        delay = random.uniform(settings.request_delay_min, settings.request_delay_max)
        if delay:
            await asyncio.sleep(delay)

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("retry-after", "").strip()
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                return max(0.0, (when - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    async def _bounded_response(self, method: str, url: str, *, params: dict | None,
                                headers: dict | None,
                                expected_content: str | None) -> httpx.Response:
        assert self._client is not None
        async with self._client.stream(method, url, params=params, headers=headers) as response:
            if response.status_code in RETRY_STATUS or response.status_code >= 400:
                return response
            content_type = response.headers.get("content-type", "").lower()
            if expected_content and expected_content not in content_type:
                raise InvalidContentType(content_type or "missing")
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > settings.http_max_response_bytes:
                raise ResponseTooLarge(f"declared={declared}")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > settings.http_max_response_bytes:
                    raise ResponseTooLarge(f"received>{settings.http_max_response_bytes}")
                chunks.append(chunk)
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                content=b"".join(chunks),
                request=response.request,
                extensions=response.extensions,
            )

    async def request_result(self, method: str, url: str, *, params: dict | None = None,
                             headers: dict | None = None,
                             expected_content: str | None = None) -> FetchResult[httpx.Response]:
        if self._client is None:
            raise RuntimeError("HttpClient must be used as an async context manager")
        last_error = "unknown"
        last_status: int | None = None
        retryable = True
        retry_allowed = method.upper() in {"GET", "HEAD", "OPTIONS"}
        for attempt in range(1, self.retries + 1):
            retry_after: float | None = None
            async with self._semaphore:
                await self._sleep_jitter()
                try:
                    async with asyncio.timeout(settings.http_overall_timeout):
                        response = await self._bounded_response(
                            method, url, params=params, headers=headers,
                            expected_content=expected_content,
                        )
                except asyncio.CancelledError:
                    raise
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = type(exc).__name__.lower()
                except TimeoutError:
                    last_error = "overall_timeout"
                except ResponseTooLarge:
                    return FetchResult(error="response_too_large", attempts=attempt)
                except InvalidContentType as exc:
                    return FetchResult(error=f"invalid_content_type:{exc}", attempts=attempt)
                else:
                    last_status = response.status_code
                    if response.status_code < 400:
                        return FetchResult(value=response, attempts=attempt)
                    last_error = f"http_{response.status_code}"
                    retryable = response.status_code in RETRY_STATUS and retry_allowed
                    if not retryable:
                        return FetchResult(error=last_error, status_code=last_status,
                                           attempts=attempt, retryable=False)
                    retry_after = self._retry_after(response)
            if not retry_allowed:
                break
            if attempt < self.retries:
                backoff = min(
                    retry_after if retry_after is not None else 2 ** (attempt - 1) + random.random(),
                    settings.http_backoff_max,
                )
                logger.info("Retrying %s %s after %s (attempt %s/%s, %.2fs)",
                            method, url, last_error, attempt, self.retries, backoff)
                await asyncio.sleep(backoff)
        logger.warning("Request failed method=%s url=%s attempts=%s error=%s",
                       method, url, self.retries, last_error)
        return FetchResult(error=last_error, status_code=last_status,
            attempts=attempt, retryable=retryable)

    async def request(self, method: str, url: str, *, params: dict | None = None,
                      headers: dict | None = None,
                      expect_json: bool = False) -> httpx.Response | None:
        return (await self.request_result(
            method, url, params=params, headers=headers,
            expected_content="json" if expect_json else None,
        )).value

    async def fetch_json(self, url: str, *, params: dict | None = None,
                         headers: dict | None = None) -> FetchResult[dict | list]:
        result = await self.request_result(
            "GET", url, params=params, headers=headers, expected_content="json"
        )
        if not result.ok:
            return FetchResult(error=result.error, status_code=result.status_code,
                               attempts=result.attempts, retryable=result.retryable)
        try:
            return FetchResult(value=result.value.json(), attempts=result.attempts)
        except (ValueError, UnicodeDecodeError):
            return FetchResult(error="malformed_json", attempts=result.attempts)

    async def get_json(self, url: str, *, params: dict | None = None,
                       headers: dict | None = None) -> dict | list | None:
        return (await self.fetch_json(url, params=params, headers=headers)).value

    async def fetch_text(self, url: str, *, params: dict | None = None,
                         headers: dict | None = None) -> FetchResult[str]:
        result = await self.request_result(
            "GET", url, params=params, headers=headers, expected_content="text/"
        )
        if not result.ok:
            return FetchResult(error=result.error, status_code=result.status_code,
                               attempts=result.attempts, retryable=result.retryable)
        return FetchResult(value=result.value.text, attempts=result.attempts)

    async def get_text(self, url: str, *, params: dict | None = None,
                       headers: dict | None = None) -> str | None:
        return (await self.fetch_text(url, params=params, headers=headers)).value


async def gather_bounded(coros: list, limit: int) -> list[Any]:
    semaphore = asyncio.Semaphore(limit)

    async def _run(coro):
        async with semaphore:
            return await coro

    return await asyncio.gather(*(_run(c) for c in coros), return_exceptions=True)
