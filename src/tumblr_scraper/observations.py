"""Append-only, local-first relationship observation registry."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time
from typing import Any, Iterable


OBSERVATION_KINDS = {
    "explicit_public_like",
    "explicit_public_follow",
    "direct_reblog",
    "structured_ask",
    "derived_affinity",
    "scout_adjacency",
}
OBSERVATION_STATUSES = {
    "NOT_CHECKED", "AVAILABLE_EMPTY", "AVAILABLE_NONEMPTY",
    "PRIVATE_OR_NOT_SHARED", "UNKNOWN_OR_UNAVAILABLE", "FETCH_FAILED", "MALFORMED_RESPONSE",
}


def observation_key(item: dict[str, Any]) -> str:
    """Stable identity; timestamps are deliberately excluded."""
    kind = str(item.get("kind") or "")
    if kind == "explicit_public_like":
        parts = (item.get("liker_blog"), item.get("liked_post_id"), item.get("source_blog"))
    elif kind == "explicit_public_follow":
        parts = (item.get("follower_blog"), item.get("followed_blog"))
    else:
        parts = (item.get("from_blog"), item.get("to_blog"), kind,
                 item.get("source_blog"), item.get("source_post_id"), item.get("referenced_post_id"))
    return hashlib.sha256("\0".join(str(part or "") for part in parts).encode()).hexdigest()


@dataclass
class ObservationRegistry:
    root: Path
    shard_count: int = 32

    def _path(self, key: str) -> Path:
        shard = key[:2] if self.shard_count >= 16 else "all"
        return self.root / "observations" / f"{shard}.jsonl"

    def append(self, item: dict[str, Any]) -> bool:
        normalized = dict(item)
        normalized.setdefault("observed_at", time.time())
        kind = str(normalized.get("kind") or "")
        if kind not in OBSERVATION_KINDS:
            raise ValueError(f"unsupported observation kind: {kind}")
        key = observation_key(normalized)
        normalized["observation_key"] = key
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.contains(key):
            return False
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(normalized, ensure_ascii=False, sort_keys=True) + "\n")
        return True

    def append_status(self, *, blog: str, surface: str, status: str,
                      source_url: str, acquisition_mode: str = "public_web") -> bool:
        if status not in OBSERVATION_STATUSES:
            raise ValueError(f"unsupported observation status: {status}")
        return self.append({
            "kind": f"{surface}_status",
            "blog": blog,
            "surface": surface,
            "status": status,
            "source_url": source_url,
            "acquisition_mode": acquisition_mode,
        }) if f"{surface}_status" in OBSERVATION_KINDS else self._append_status_record(
            blog, surface, status, source_url, acquisition_mode
        )

    def _append_status_record(self, blog: str, surface: str, status: str,
                              source_url: str, acquisition_mode: str) -> bool:
        item = {"kind": "scout_adjacency", "status_record": True, "blog": blog,
                "surface": surface, "status": status, "source_url": source_url,
                "acquisition_mode": acquisition_mode}
        item["observation_key"] = hashlib.sha256(
            f"status\0{blog}\0{surface}\0{status}".encode()).hexdigest()
        path = self._path(item["observation_key"])
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.contains(item["observation_key"]):
            return False
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, sort_keys=True) + "\n")
        return True

    def contains(self, key: str) -> bool:
        path = self._path(key)
        if not path.is_file():
            return False
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                if json.loads(line).get("observation_key") == key:
                    return True
            except json.JSONDecodeError:
                continue
        return False

    def iter_records(self) -> Iterable[dict[str, Any]]:
        for path in sorted((self.root / "observations").glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    yield item
