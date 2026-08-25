import httpx
import pytest

from app.config import settings
from app.http_client import HttpClient, gather_bounded

pytestmark = pytest.mark.asyncio


async def _client(handler, *, retries=3):
    client = HttpClient(retries=retries)
    await client.__aenter__()
    await client._client.aclose()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


async def test_retry_after_rate_limit_then_success(monkeypatch):
    calls = 0
    sleeps = []

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, headers={"Content-Type": "application/json"}, json={"ok": True})

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(settings, "request_delay_min", 0)
    monkeypatch.setattr(settings, "request_delay_max", 0)
    monkeypatch.setattr("app.http_client.asyncio.sleep", fake_sleep)
    client = await _client(handler)
    try:
        result = await client.fetch_json("https://example.test/jobs")
    finally:
        await client.__aexit__(None, None, None)
    assert result.value == {"ok": True}
    assert result.attempts == 2 and calls == 2 and 0 in sleeps


async def test_permanent_4xx_is_not_retried(monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(404, headers={"Content-Type": "text/plain"})

    monkeypatch.setattr(settings, "request_delay_min", 0)
    monkeypatch.setattr(settings, "request_delay_max", 0)
    client = await _client(handler)
    try:
        result = await client.fetch_text("https://example.test/missing")
    finally:
        await client.__aexit__(None, None, None)
    assert result.error == "http_404" and result.attempts == 1 and calls == 1


async def test_temporary_timeout_is_retried(monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("temporary", request=request)
        return httpx.Response(200, headers={"Content-Type": "text/plain"}, text="ok")

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(settings, "request_delay_min", 0)
    monkeypatch.setattr(settings, "request_delay_max", 0)
    monkeypatch.setattr("app.http_client.asyncio.sleep", no_sleep)
    client = await _client(handler)
    try:
        result = await client.fetch_text("https://example.test/jobs")
    finally:
        await client.__aexit__(None, None, None)
    assert result.value == "ok" and result.attempts == 2


async def test_invalid_content_type_is_reported(monkeypatch):
    monkeypatch.setattr(settings, "request_delay_min", 0)
    monkeypatch.setattr(settings, "request_delay_max", 0)
    client = await _client(
        lambda request: httpx.Response(
            200, headers={"Content-Type": "text/html"}, text="<h1>login</h1>"
        )
    )
    try:
        result = await client.fetch_json("https://example.test/api")
    finally:
        await client.__aexit__(None, None, None)
    assert result.error.startswith("invalid_content_type:")


async def test_response_size_is_bounded(monkeypatch):
    monkeypatch.setattr(settings, "request_delay_min", 0)
    monkeypatch.setattr(settings, "request_delay_max", 0)
    monkeypatch.setattr(settings, "http_max_response_bytes", 100_000)
    client = await _client(
        lambda request: httpx.Response(
            200, headers={"Content-Type": "text/plain"}, content=b"x" * 100_001
        )
    )
    try:
        result = await client.fetch_text("https://example.test/large")
    finally:
        await client.__aexit__(None, None, None)
    assert result.error == "response_too_large"


async def test_gather_bounded_enforces_concurrency():
    import asyncio

    active = 0
    peak = 0

    async def work(value):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return value

    results = await gather_bounded([work(value) for value in range(20)], limit=3)
    assert results == list(range(20))
    assert peak <= 3
