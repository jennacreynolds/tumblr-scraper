"""Compact target-set coverage state and durable frontier helpers."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from typing import Any
import uuid


def target_set_id(targets: list[str] | tuple[str, ...]) -> str:
    canonical = sorted({str(item).strip().lower() for item in targets if str(item).strip()})
    if not canonical:
        raise ValueError("target set cannot be empty")
    return "set-" + hashlib.sha256("\0".join(canonical).encode()).hexdigest()[:16]


def coverage_root(root: Path, targets: list[str] | tuple[str, ...]) -> Path:
    return Path(root) / target_set_id(targets)


def load_coverage_state(root: Path, targets: list[str] | tuple[str, ...]) -> dict[str, Any]:
    path = coverage_root(root, targets) / "coverage.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    value.setdefault("schema_version", 2)
    value.setdefault("targets", sorted(set(targets)))
    value.setdefault("blogs", {})
    value.setdefault("counters", {"observed_blogs": 0, "survey_requests": 0})
    value.setdefault("observation_shards", "observations/*.jsonl")
    value.setdefault("frontier_file", "frontier.jsonl")
    return value


def save_coverage_state(root: Path, targets: list[str] | tuple[str, ...], state: dict[str, Any]) -> Path:
    destination = coverage_root(root, targets)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "coverage.json"
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        temporary.write_text(payload, encoding="utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.replace(path)
    except FileNotFoundError as exc:
        # A disposable archive may be removed while a run is checkpointing.
        # Recreate the selected bundle path once, then fail with an explicit
        # path if another actor removes it during the retry.
        destination.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.retry.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(path)
        except FileNotFoundError as retry_exc:
            raise RuntimeError(
                f"coverage checkpoint target disappeared during atomic save: {path}"
            ) from retry_exc
    return path


def append_frontier(root: Path, targets: list[str] | tuple[str, ...], item: dict[str, Any]) -> Path:
    destination = coverage_root(root, targets)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "frontier.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    return path
