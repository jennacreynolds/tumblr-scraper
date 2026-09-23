"""Optional public relationship enrichment, isolated from canonical acquisition."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .network import RequestPressure, fetch_public_surface, public_surface_url
from .observations import ObservationRegistry
from .relationships import public_follow_evidence, public_like_evidence


def capture_public_relationships(blog: str, root: Path, *, user_agent: str,
                                  pressure: RequestPressure,
                                  likes: bool = False, following: bool = False) -> dict[str, Any]:
    """Capture only reliable public surfaces; every result is optional."""
    registry = ObservationRegistry(root)
    result: dict[str, Any] = {"blog": blog, "surfaces": {}}
    for surface, enabled in (("likes", likes), ("following", following)):
        if not enabled:
            continue
        url = public_surface_url(blog, surface)
        if not pressure.reserve(f"public_{surface}"):
            result["surfaces"][surface] = {"status": "UNKNOWN_OR_UNAVAILABLE", "source_url": url, "skipped": "request_pressure"}
            continue
        status, records, source_url = fetch_public_surface(blog=blog, surface=surface, user_agent=user_agent)
        saved = 0
        for record in records:
            item = (public_like_evidence(blog, record, source_url=source_url)
                    if surface == "likes" else public_follow_evidence(blog, record, source_url=source_url))
            if item is not None and registry.append(item):
                saved += 1
        registry.append_status(blog=blog, surface=surface, status=status, source_url=source_url)
        result["surfaces"][surface] = {"status": status, "records": len(records), "saved": saved, "source_url": source_url}
    return result
