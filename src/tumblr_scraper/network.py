"""Low-level public Tumblr transport helpers.

This module performs retrieval only.  It does not know about crawler state,
archive paths, presentation, or application hosts.
"""

from __future__ import annotations

from datetime import timezone
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import json
import re
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import BlogSourceFailure, TumblrRateLimitedError


PUBLIC_SURFACE_STATUS = {
    "AVAILABLE_EMPTY", "AVAILABLE_NONEMPTY", "PRIVATE_OR_NOT_SHARED",
    "UNKNOWN_OR_UNAVAILABLE", "FETCH_FAILED", "MALFORMED_RESPONSE",
}


@dataclass
class RequestPressure:
    """Shared logical envelope for feeds, enrichment, media, and survey work."""

    max_requests: int | None = None
    total: int = 0
    by_class: dict[str, int] = field(default_factory=dict)

    def reserve(self, request_class: str) -> bool:
        if self.max_requests is not None and self.total >= self.max_requests:
            return False
        self.total += 1
        self.by_class[request_class] = self.by_class.get(request_class, 0) + 1
        return True


def public_surface_url(blog: str, surface: str) -> str:
    if surface == "likes":
        return f"https://www.tumblr.com/liked/by/{blog}"
    if surface == "following":
        return f"https://www.tumblr.com/{blog}/following"
    raise ValueError(f"unknown public surface: {surface}")


def parse_public_surface(raw: bytes | str, surface: str) -> tuple[str, list[dict[str, Any]]]:
    """Parse only explicit JSON payloads; HTML/client-rendered pages fail closed."""
    try:
        value = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "UNKNOWN_OR_UNAVAILABLE", []
    if not isinstance(value, dict):
        return "MALFORMED_RESPONSE", []
    key = "liked_posts" if surface == "likes" else "blogs"
    records = value.get(key)
    if not isinstance(records, list):
        return "MALFORMED_RESPONSE", []
    return ("AVAILABLE_NONEMPTY" if records else "AVAILABLE_EMPTY"), [item for item in records if isinstance(item, dict)]


def fetch_public_surface(*, blog: str, surface: str, user_agent: str,
                         opener: Callable[..., Any] | None = None,
                         pressure: RequestPressure | None = None) -> tuple[str, list[dict[str, Any]], str]:
    """Best-effort credential-free enrichment; never raises crawler failures."""
    url = public_surface_url(blog, surface)
    if pressure is not None and not pressure.reserve(f"public_{surface}"):
        return "UNKNOWN_OR_UNAVAILABLE", [], url
    request_opener = urlopen if opener is None else opener
    try:
        with request_opener(Request(url, headers={"User-Agent": user_agent}), timeout=30) as response:
            raw = response.read()
        status, records = parse_public_surface(raw, surface)
        return status, records, url
    except HTTPError as exc:
        if exc.code in {401, 403, 404, 410}:
            return "PRIVATE_OR_NOT_SHARED", [], url
        return "FETCH_FAILED", [], url
    except (URLError, OSError, TimeoutError):
        return "FETCH_FAILED", [], url


def retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = int(value.strip())
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        seconds = max(0, int(parsed.timestamp() - time.time()))
    if seconds < 0:
        return None
    return float(seconds)


def fetch_public_page(
    *,
    blog: str,
    blog_host: str,
    start: int,
    count: int,
    user_agent: str,
    opener: Callable[..., Any] | None = None,
    report_warning: Callable[[str], None] | None = None,
    pressure: RequestPressure | None = None,
) -> dict[str, Any]:
    """Read one public, credential-free Tumblr legacy feed page."""
    if pressure is not None and not pressure.reserve("feed"):
        raise BlogSourceFailure(blog, "feed", "shared request-pressure envelope exhausted", kind="request_pressure", retryable=False)
    query = urlencode({"start": start, "num": count})
    url = f"https://{blog_host}/api/read/json?{query}"
    req = Request(url, headers={"User-Agent": user_agent})
    request_opener = urlopen if opener is None else opener
    rate_limit_retry = False
    while True:
        try:
            with request_opener(req, timeout=30) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            if exc.code == 429:
                retry_after = retry_after_seconds(exc.headers.get("Retry-After"))
                if rate_limit_retry or retry_after is None:
                    raise TumblrRateLimitedError from exc
                if report_warning is not None:
                    report_warning("Tumblr requested slower pacing")
                if retry_after:
                    time.sleep(retry_after)
                rate_limit_retry = True
                continue
            kind = "unavailable" if exc.code in {401, 403, 404, 410} else "http_error"
            raise BlogSourceFailure(
                blog,
                "feed",
                f"Tumblr returned HTTP {exc.code} while reading {blog_host}",
                code=exc.code,
                kind=kind,
                retryable=exc.code not in {401, 403, 404, 410},
            ) from exc
        except URLError as exc:
            raise BlogSourceFailure(
                blog,
                "feed",
                f"Could not reach Tumblr: {exc.reason}",
                kind="network_error",
                retryable=True,
            ) from exc
        break

    # Tumblr's legacy public feed is JavaScript:
    #     var tumblr_api_read = {...};
    match = re.match(r"\s*var\s+tumblr_api_read\s*=\s*(.*)\s*;\s*$", raw, re.S)
    if not match:
        raise BlogSourceFailure(
            blog,
            "feed",
            "Tumblr's public blog feed returned an unexpected format",
            kind="invalid_feed",
            retryable=True,
        )
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise BlogSourceFailure(
            blog,
            "feed",
            "Tumblr's public blog feed was not valid JSON",
            kind="invalid_feed",
            retryable=True,
        ) from exc
