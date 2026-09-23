"""Durable monotonic media metadata, independent of the crawl run."""

from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import json
from pathlib import Path
from typing import Any


QUALITY_RANK = {"NONE": 0, "THUMBNAIL": 1, "COMPACT": 2, "STANDARD": 3, "HIGH": 4}


def logical_media_id(url: str, *, source_id: str = "") -> str:
    """Stable identity for one logical Tumblr media asset, not one URL variant."""
    value = f"{source_id}\0{url.strip()}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass
class MediaManifestEntry:
    logical_media_id: str
    known_source_variants: list[dict[str, Any]]
    best_known_source_quality: str = "NONE"
    best_acquired_source_quality: str = "NONE"
    current_local_representation: dict[str, Any] | None = None
    optimizer_result: dict[str, Any] | None = None


def _rank(value: str) -> int:
    return QUALITY_RANK.get(value, -1)


def merge_media_observation(existing: dict[str, Any] | None, *, media_id: str,
                            variants: list[dict[str, Any]], best_known_quality: str) -> dict[str, Any]:
    """Merge source knowledge without changing the local working representation."""
    current = dict(existing or {})
    current["logical_media_id"] = media_id
    old_variants = current.get("known_source_variants") or []
    by_url = {str(item.get("url")): dict(item) for item in old_variants if item.get("url")}
    by_url.update({str(item.get("url")): dict(item) for item in variants if item.get("url")})
    current["known_source_variants"] = [by_url[key] for key in sorted(by_url)]
    old_quality = str(current.get("best_known_source_quality") or "NONE")
    current["best_known_source_quality"] = max((old_quality, best_known_quality), key=_rank)
    current.setdefault("best_acquired_source_quality", "NONE")
    current.setdefault("current_local_representation", None)
    current.setdefault("optimizer_result", None)
    return current


def record_acquisition(existing: dict[str, Any], *, acquired_quality: str,
                       local_representation: dict[str, Any]) -> dict[str, Any]:
    """Promote only successful non-downgrading acquisitions."""
    if _rank(acquired_quality) < _rank(str(existing.get("best_acquired_source_quality") or "NONE")):
        return dict(existing)
    result = dict(existing)
    result["best_acquired_source_quality"] = acquired_quality
    result["current_local_representation"] = dict(local_representation)
    return result


def record_optimizer_result(existing: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Record downstream optimization separately; callers decide atomic file promotion."""
    updated = dict(existing)
    updated["optimizer_result"] = dict(result)
    return updated


class MediaManifest:
    """Small per-blog JSONL event log; callers may compact it later."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, entry: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

    def latest(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        if not self.path.is_file():
            return result
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("logical_media_id"):
                result[str(item["logical_media_id"])] = item
        return result
