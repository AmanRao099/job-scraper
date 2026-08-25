"""Small shared helpers: HTML -> text, date parsing, salary parsing."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")


def clean_text(value: str | None, limit: int | None = None) -> str:
    if not value:
        return ""
    cleaned = _WS_RE.sub(" ", value).strip()
    return cleaned[:limit] if limit else cleaned


def html_to_text(html: str | None, limit: int = 20000) -> str:
    """Strip markup, keeping paragraph structure readable."""
    if not html:
        return ""
    if "<" not in html:
        return clean_text(html, limit)
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return clean_text(_TAG_RE.sub(" ", html), limit)
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    return clean_text(text, limit)


def absolute_url(href: str | None, base: str) -> str:
    if not href:
        return ""
    href = href.strip()
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return base.rstrip("/") + href
    return f"{base.rstrip('/')}/{href}"


def strip_tracking(url: str) -> str:
    """Remove known tracking parameters while preserving functional query data."""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ""
    tracking_names = {
        "trk", "trackingid", "refid", "ref_id", "lipi", "midtoken",
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "gclid", "fbclid", "mc_cid", "mc_eid",
    }
    query = urlencode(
        [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
         if key.lower() not in tracking_names],
        doseq=True,
    )
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", query, ""))


def from_epoch_ms(value: object) -> datetime | None:
    try:
        millis = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if millis <= 0:
        return None
    if millis > 10_000_000_000:  # milliseconds
        millis //= 1000
    try:
        return datetime.fromtimestamp(millis, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


_REL_RE = re.compile(
    r"(?:(\d+)\s*\+?\s*)?(minute|min|hour|hr|day|week|month|year)s?\s*(?:ago)?",
    re.IGNORECASE,
)
_UNIT_DAYS = {
    "minute": 1 / 1440, "min": 1 / 1440,
    "hour": 1 / 24, "hr": 1 / 24,
    "day": 1, "week": 7, "month": 30, "year": 365,
}


def parse_relative_date(value: str | None, now: datetime | None = None) -> datetime | None:
    """Turn "3 Days Ago" / "Just now" / "30+ days ago" into a timestamp."""
    if not value:
        return None
    now = now or datetime.now(timezone.utc)
    lowered = value.lower().strip()

    if any(token in lowered for token in ("just now", "few hours", "today", "hours ago")):
        return now
    if "yesterday" in lowered:
        return now - timedelta(days=1)

    match = _REL_RE.search(lowered)
    if not match:
        return None
    amount = int(match.group(1)) if match.group(1) else 1
    days = _UNIT_DAYS.get(match.group(2).lower())
    if days is None:
        return None
    return now - timedelta(days=amount * days)


def parse_iso_date(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    for candidate in (text, text[:10]):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    return None


_SALARY_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:-|–|to)?\s*(\d+(?:\.\d+)?)?\s*(lac|lacs|lakh|lakhs|lpa|cr|crore)",
    re.IGNORECASE,
)


def parse_salary(text: str | None) -> tuple[float | None, float | None]:
    """Parse Indian salary strings into annual rupees (lakhs -> absolute)."""
    if not text:
        return (None, None)
    lowered = text.lower()
    if "not disclosed" in lowered or "unpaid" in lowered:
        return (None, None)

    match = _SALARY_RE.search(lowered)
    if not match:
        return (None, None)

    unit = match.group(3).lower()
    multiplier = 10_000_000 if unit.startswith("cr") else 100_000
    low = float(match.group(1)) * multiplier
    high = float(match.group(2)) * multiplier if match.group(2) else low
    return (min(low, high), max(low, high))


def split_csv_field(value: str | None) -> list[str]:
    """Board skill fields are comma or pipe separated strings."""
    if not value:
        return []
    parts = re.split(r"[,|/]", value)
    return [clean_text(p) for p in parts if clean_text(p)]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
