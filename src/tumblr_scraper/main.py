#!/usr/bin/env python3
"""Incremental Tumblr public-feed scraper.

What it does:
1. Reads a blog's public Tumblr JSON feed newest-first.
2. Skips individually present posts while seeking older missing material.
3. Converts new public-feed records into the JSON shape expected by
   tumblr-backup 1.0.7, while embedding the original public-feed record.
4. Lets tumblr-backup do the actual HTML/media/archive work.

Selected archive data is stored below ``Archive/<archive-name>/``. The
editable application bundle remains outside every generated archive.
"""

from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import json
import math
import mimetypes
import os
import random
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
import base64
from collections import Counter
from dataclasses import dataclass, field
from html import escape, unescape
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from . import archive, archive_manager, config, coverage, enrichment, graph_projection, graph_slices, network, normalize, presentation, profile as profile_parser, relationships
from . import paths
from .application import Application
from .models import (
    ActionCandidate,
    BlogRenderFailure,
    BlogSourceFailure,
    BlogState,
    CapturePlan,
    CaptureCurvePoint,
    CaptureShapePoint,
    CapturePolicy,
    CoverageStrategy,
    CrawlRequest,
    PolicyError,
    RuntimeControls,
    ScoutOpportunity,
    TumblrRateLimitedError,
)


try:
    import select
    import termios
    import tty
except ImportError:  # pragma: no cover - unavailable on Windows
    select = termios = tty = None
try:
    import msvcrt
except ImportError:  # pragma: no cover - unavailable on POSIX
    msvcrt = None

PROJECT_ROOT = paths.BUNDLE_ROOT
BASE_DIR = PROJECT_ROOT
ARCHIVE_ROOT = paths.ARCHIVE_ROOT
ARCHIVES_ROOT = paths.ARCHIVES_ROOT
ACTIVE_ARCHIVE_NAME = paths.ACTIVE_ARCHIVE_NAME
CONTENT_ROOT = paths.CONTENT_ROOT
NETWORK_ROOT = paths.NETWORK_ROOT
OBSERVATIONS_ROOT = paths.OBSERVATIONS_ROOT
NEIGHBORHOODS_ROOT = paths.NEIGHBORHOODS_ROOT
APP_ROOT = paths.APP_ROOT
GLOBAL_CSS = paths.resource_path("global.css")
SOURCE_ASSET_DIR = paths.ASSET_DIR
SOURCE_ARCHIVE_CSS = SOURCE_ASSET_DIR / "archive.css"
SOURCE_ARCHIVE_JS = SOURCE_ASSET_DIR / "archive.js"
NETWORK_POLICY_FILE = paths.resource_path("network-policy.json")
CONTEXT_POLICY_FILE = paths.resource_path("context-policy.json")
RUNTIME_DIR = paths.RUNTIME_DIR
BLOG = ""
BLOG_HOST = ""
OUT = Path()
JSON_DIR = Path()
PAGE_SIZE = 50
SCOUT_MAX_PROBES = 16
SCOUT_MAX_OFFSET = 5000
PRESENTATION_PAGE_SIZE = 30
PRESENTATION_SCHEMA_VERSION = 3
MAX_POSTS = 300
PROCESS_BATCH = 25
FULL_RES = False
CAPTURE_PLAN = CapturePlan(media_quality="COMPACT")
NETWORK_PROFILE: dict[str, Any] = {}
ACTIVE_RUNTIME: RuntimeControls | None = None
ACTIVE_REQUEST_PRESSURE: network.RequestPressure | None = None
ACTIVE_STATUS: CrawlerStatus | None = None
ACTIVE_LIFECYCLE = "stopped"
ACTIVE_PROGRESS_RENDERER: Any = None
ACTIVE_PRESENTATION_GENERATION = 0
_PRESENTATION_RECOVERY_ROOT: Path | None = None
PENDING_CANCEL_EVENT = threading.Event()
RUNTIME_LOCK = threading.RLock()
HOST_CAPABILITIES = {"compact_terminal": False, "keyboard_controls": True}
VERBOSE = False
USER_AGENT = "PuppetBackup/1.1 (+personal archival copy)"
FOCUS_LABELS = {"deep": "Deep", "balanced": "Balanced", "wide": "Wide", "neighbors": "Neighbors"}
SCORE_COMPONENTS = (
    "source_affinity", "audience_activity", "reciprocal_activity",
    "context_deficit", "representation", "graph_depth",
    "scout_confidence", "failure_penalty",
)

CANONICAL_ACQUISITION_LANES = ("target", "depth1", "depth2", "survey")


@dataclass(frozen=True)
class FeedQuery:
    """Normalized presentation query; independent from crawler state."""

    mode: str = "affinity"
    pov: str = ""
    chronology: str = "newest"
    affinity: str = "any"
    evidence: tuple[str, ...] = ("direct_reblog", "direct_like")
    neighborhood: str = "__all__"
    breadth: str = "all"
    search: str = ""
    include_target: bool = False
    window_size: int = 50

    def fingerprint(self) -> str:
        payload = {
            "mode": self.mode, "pov": self.pov, "chronology": self.chronology,
            "affinity": self.affinity, "evidence": list(self.evidence),
            "neighborhood": self.neighborhood, "breadth": self.breadth,
            "search": self.search, "include_target": self.include_target,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class FeedCursor:
    schema_version: int
    query_fingerprint: str
    index_generation: str
    offset: int
    final_ordering_key: tuple[Any, ...] = ()

    def encode(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "query_fingerprint": self.query_fingerprint,
            "index_generation": self.index_generation,
            "offset": self.offset,
            "final_ordering_key": list(self.final_ordering_key),
        }
        return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, value: str) -> "FeedCursor | None":
        try:
            padded = value + "=" * (-len(value) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
            if not isinstance(payload, dict):
                return None
            return cls(
                int(payload["schema_version"]), str(payload["query_fingerprint"]),
                str(payload["index_generation"]), int(payload["offset"]),
                tuple(payload.get("final_ordering_key") or ()),
            )
        except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeError):
            return None


@dataclass(frozen=True)
class FeedWindow:
    records: tuple[dict[str, Any], ...]
    next_cursor: FeedCursor | None
    selected_count: int | None
    total_archive_posts: int
    metadata_touched: int
    rich_records_loaded: int


FEED_CURSOR_SCHEMA_VERSION = 1
FEED_DEFAULT_WINDOW = 50
_FEED_METADATA_CACHE: dict[tuple[str, str], tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]] = {}
_FEED_SNAPSHOT_CACHE: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {}


def _archive_content_url(relative: str) -> str:
    """Return a live URL into the selected data bundle, never source code."""
    return f"/Archive/{quote(ACTIVE_ARCHIVE_NAME)}/Content/{quote(relative, safe='/')}"


def runtime_identity() -> dict[str, str]:
    """Return a deterministic identity for the loaded source and UI assets."""
    source_files = (
        Path(__file__),
        Path(presentation.__file__),
        Path(paths.__file__),
        Path(graph_projection.__file__),
    )
    asset_files = tuple(
        path for path in (
            SOURCE_ARCHIVE_CSS,
            SOURCE_ARCHIVE_JS,
            SOURCE_ASSET_DIR / "graph-view.css",
            SOURCE_ASSET_DIR / "graph-view.js",
        ) if path.is_file()
    )

    def digest(files: tuple[Path, ...]) -> str:
        hasher = hashlib.sha256()
        for path in files:
            hasher.update(str(path).encode("utf-8"))
            try:
                hasher.update(path.read_bytes())
            except OSError:
                hasher.update(b"<unavailable>")
        return hasher.hexdigest()[:24]

    return {
        "bundle_root": str(PROJECT_ROOT),
        "source_revision": digest(source_files),
        "frontend_revision": digest(asset_files),
        "archive_path": str(ARCHIVE_ROOT),
        "archive_name": ACTIVE_ARCHIVE_NAME,
    }


def _presentation_identity() -> dict[str, str]:
    """Identify the exact source and archive inputs that generate App output."""
    identity = runtime_identity()
    hasher = hashlib.sha256()
    for root in (CONTENT_ROOT, NETWORK_ROOT):
        if not root.is_dir():
            continue
        for path in sorted(
            path for path in root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in {".json", ".html", ".css", ".png", ".jpg", ".jpeg", ".gif", ".webp"}
            and path.name not in {"catalog.json", "tag-index.json", "graph.json", "graph.html", "profile.json"}
            and "graph-slices" not in path.parts
        ):
            hasher.update(str(path.relative_to(root)).encode("utf-8"))
            try:
                hasher.update(path.read_bytes())
            except OSError:
                hasher.update(b"<unavailable>")
    identity["archive_input_revision"] = hasher.hexdigest()[:24]
    identity["generator_revision"] = str(PRESENTATION_SCHEMA_VERSION)
    return identity


class UnknownAcquisitionLane(ValueError):
    """An acquisition lane that cannot be accounted for safely."""

    def __init__(self, raw: object) -> None:
        self.raw = str(raw or "").strip()
        super().__init__(f"unknown acquisition lane: {self.raw or '<empty>'}")


@dataclass(frozen=True)
class RunSummary:
    """Accounting derived from durable acquisition events and source JSON."""

    run_id: str
    budget_used: int
    target_posts: int
    breadth1_posts: int
    breadth2plus_posts: int
    nearby_posts: int
    survey_posts: int
    unclassified_posts: int
    events: tuple[dict[str, Any], ...] = ()
    missing_canonical: tuple[dict[str, Any], ...] = ()
    unknown_lanes: dict[str, int] = field(default_factory=dict)

    @property
    def depth1_posts(self) -> int:
        return self.breadth1_posts

    @property
    def depth2_posts(self) -> int:
        return self.breadth2plus_posts

    @property
    def accounted_saved_posts(self) -> int:
        # Survey is no longer a separate acquisition species.  It remains a
        # historical diagnostic field, but breadth buckets are authoritative.
        return self.target_posts + self.nearby_posts + self.unclassified_posts

    @property
    def saved_by_lane(self) -> dict[str, int]:
        return {
            "target": self.target_posts,
            "depth1": self.breadth1_posts,
            "depth2": self.breadth2plus_posts,
            "survey": self.survey_posts,
        }

    @property
    def saved_by_breadth(self) -> dict[str, int]:
        return {
            "target": self.target_posts,
            "breadth1": self.breadth1_posts,
            "breadth2plus": self.breadth2plus_posts,
            "unclassified": self.unclassified_posts,
        }

    @property
    def reconciliation(self) -> dict[str, Any]:
        return {
            "valid": self.budget_used == self.accounted_saved_posts and not self.missing_canonical and not self.unknown_lanes,
            "ledger_events": len(self.events),
            "durable_events": self.budget_used,
            "missing_canonical": list(self.missing_canonical),
            "unknown_lanes": dict(self.unknown_lanes),
        }


def canonical_acquisition_lane(lane: str | None, graph_depth: int | None = None) -> str:
    """Normalize the single accepted lane vocabulary; reject unknown values."""
    value = str(lane or "").strip().lower().replace("_", "-")
    aliases = {
        "target": "target", "depth0": "target", "distance-0": "target",
        "nearby": "depth1", "depth1": "depth1", "distance-1": "depth1",
        "outer": "depth2", "depth2": "depth2", "distance-2": "depth2",
        "survey": "survey",
    }
    if value in aliases:
        return aliases[value]
    match = re.fullmatch(r"distance-(\d+)", value)
    if match and int(match.group(1)) >= 3:
        return "survey"
    if not value and graph_depth is not None:
        distance = max(0, int(graph_depth))
        return "target" if distance == 0 else "depth1" if distance == 1 else "depth2" if distance == 2 else "survey"
    raise UnknownAcquisitionLane(lane)


def aggregate_lane_counts(values: dict[str, int] | None) -> tuple[dict[str, int], dict[str, int]]:
    """Read-time merge of canonical lanes and historical aliases.

    Unknown keys remain diagnostic data and are never folded into a valid lane.
    """
    totals = {lane: 0 for lane in CANONICAL_ACQUISITION_LANES}
    unknown: dict[str, int] = {}
    for raw_lane, count in (values or {}).items():
        try:
            lane = canonical_acquisition_lane(raw_lane)
        except UnknownAcquisitionLane:
            key = str(raw_lane)
            unknown[key] = unknown.get(key, 0) + int(count or 0)
            continue
        totals[lane] += int(count or 0)
    return totals, unknown


def _factual_breadth(event: dict[str, Any], canonical: dict[str, Any] | None = None) -> int | None:
    """Read the relationship coordinate, accepting only historical aliases."""
    canonical = canonical or {}
    value = event.get("breadth", canonical.get("_puppetbackup_breadth"))
    if value is None:
        value = event.get("network_distance", event.get("graph_depth", canonical.get("_puppetbackup_graph_depth")))
    try:
        return max(0, int(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _factual_is_target(event: dict[str, Any], canonical: dict[str, Any], targets: set[str], breadth: int | None) -> bool:
    value = event.get("is_target", canonical.get("_puppetbackup_is_target"))
    if value is not None:
        if isinstance(value, str):
            return value.strip().casefold() in {"1", "true", "yes", "target"}
        return bool(value)
    blog = canonical_blog_name(str(event.get("blog") or canonical.get("_canonical_blog") or ""))
    return blog in targets or breadth == 0


def derive_run_summary(run_id: str, targets: Iterable[str] = (), ledger: dict[tuple[str, str], dict[str, Any]] | None = None) -> RunSummary:
    """Derive one run's accounting from ledger events and durable JSON files."""
    target_names = {canonical_username(str(value)) for value in targets if str(value).strip()}
    rows = ledger if ledger is not None else _read_acquisition_ledger()
    events: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    unknown: dict[str, int] = {}
    target_posts = depth1_posts = depth2_posts = survey_posts = unclassified_posts = 0
    for item in rows.values():
        if str(item.get("run_id")) != str(run_id) or item.get("state") != "acquired":
            continue
        blog = canonical_blog_name(str(item.get("blog") or ""))
        post_id = str(item.get("post_id") or "")
        path = CONTENT_ROOT / blog / "json" / f"{post_id}.json"
        if not path.is_file():
            missing.append({"blog": blog, "post_id": post_id})
            continue
        event = dict(item)
        event["blog"] = blog
        event["post_id"] = post_id
        try:
            canonical = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            missing.append({"blog": blog, "post_id": post_id})
            continue
        network_distance = _factual_breadth(item, canonical)
        raw_lane = item.get("acquisition_lane", item.get("lane"))
        try:
            normalized_lane = canonical_acquisition_lane(raw_lane, network_distance)
        except UnknownAcquisitionLane:
            normalized_lane = ""
            raw_key = str(raw_lane or "")
            unknown[raw_key] = unknown.get(raw_key, 0) + 1
        event["breadth"] = network_distance
        event["network_distance"] = network_distance  # historical diagnostic alias
        event["raw_lane"] = raw_lane
        event["normalized_lane"] = normalized_lane
        events.append(event)
        is_target = _factual_is_target(item, canonical, target_names, network_distance) or normalized_lane == "target"
        event["is_target"] = is_target
        if raw_lane not in (None, "") and not normalized_lane:
            # A factual coordinate cannot silently launder an unknown legacy
            # lane into a visible bucket. Keep it explicitly unclassified.
            unclassified_posts += 1
            continue
        reason_codes = {str(value).casefold() for value in item.get("reason_codes", [])}
        is_survey = normalized_lane == "survey" or item.get("action_kind") == "survey" or any("survey" in value for value in reason_codes)
        if is_target:
            target_posts += 1
        elif network_distance is not None and network_distance >= 1:
            if network_distance == 1:
                depth1_posts += 1
            else:
                depth2_posts += 1
            # Survey is no longer a distinct acquisition species. Keep the
            # legacy field as a compatibility diagnostic only.
            if is_survey:
                survey_posts += 1
        else:
            unclassified_posts += 1
    summary = RunSummary(
        run_id=str(run_id), budget_used=len(events), target_posts=target_posts,
        breadth1_posts=depth1_posts, breadth2plus_posts=depth2_posts,
        nearby_posts=depth1_posts + depth2_posts, survey_posts=survey_posts,
        unclassified_posts=unclassified_posts, events=tuple(events),
        missing_canonical=tuple(missing), unknown_lanes=unknown,
    )
    return summary


def accounting_fields(status: "CrawlerStatus") -> dict[str, Any]:
    """Return status fields with read-time lane reconciliation applied."""
    if status.acquisition_events:
        target = breadth1 = breadth2plus = unknown_count = 0
        unknown: dict[str, int] = {}
        for event in status.acquisition_events:
            breadth = _factual_breadth(event)
            if bool(event.get("is_target")) or breadth == 0:
                target += 1
            elif breadth == 1:
                breadth1 += 1
            elif breadth is not None and breadth >= 2:
                breadth2plus += 1
            else:
                raw = str(event.get("raw_lane") or "missing breadth")
                unknown[raw] = unknown.get(raw, 0) + 1
        unknown_count = sum(unknown.values())
        accounted = target + breadth1 + breadth2plus + unknown_count
        errors = []
        if unknown:
            errors.append(f"unknown acquisition facts: {', '.join(sorted(unknown))}")
        if accounted != status.budget_used:
            errors.append(f"budget/lane mismatch: {status.budget_used} != {accounted}")
        return {
            "saved_by_lane": {"target": target, "depth1": breadth1, "depth2": breadth2plus, "survey": 0},
            "saved_by_breadth": {"target": target, "breadth1": breadth1, "breadth2plus": breadth2plus, "unclassified": unknown_count},
            "target_posts": target,
            "breadth1_posts": breadth1,
            "breadth2plus_posts": breadth2plus,
            "nearby_posts": breadth1 + breadth2plus,
            "survey_posts": 0,
            "unknown_lanes": unknown,
            "unknown_lane_count": unknown_count,
            "unclassified_posts": unknown_count,
            "accounted_saved_posts": accounted,
            "accounting_valid": not errors,
            "accounting_errors": errors,
        }
    lanes, unknown = aggregate_lane_counts(status.saved_by_lane)
    for raw_lane, count in status.unknown_lanes.items():
        unknown[raw_lane] = unknown.get(raw_lane, 0) + int(count or 0)
    unknown_count = sum(unknown.values())
    accounted = sum(lanes.values()) + unknown_count
    errors = []
    if unknown:
        errors.append(f"unknown acquisition lanes: {', '.join(sorted(unknown))}")
    if accounted != status.budget_used:
        errors.append(f"budget/lane mismatch: {status.budget_used} != {accounted}")
    return {
        "saved_by_lane": lanes,
        "saved_by_breadth": {"target": lanes["target"], "breadth1": lanes["depth1"], "breadth2plus": lanes["depth2"], "unclassified": unknown_count},
        "unknown_lanes": unknown,
        "unknown_lane_count": unknown_count,
        "accounted_saved_posts": accounted,
        "accounting_valid": not errors,
        "accounting_errors": errors,
    }


def summary_fields(status: "CrawlerStatus") -> dict[str, Any]:
    """Expose the durable RunSummary as the sole status accounting source."""
    fallback = accounting_fields(status)
    summary = derive_run_summary(status.run_id, status.target.split(",")) if status.run_id else None
    if summary is None or (not summary.events and status.budget_used):
        return fallback
    fields = {
        "budget_used": summary.budget_used,
        # Read compatibility for existing clients. This is derived output;
        # acquisition events and new canonical metadata contain only breadth.
        "saved_by_lane": summary.saved_by_lane,
        "saved_by_breadth": summary.saved_by_breadth,
        "target_posts": summary.target_posts,
        "breadth1_posts": summary.depth1_posts,
        "breadth2plus_posts": summary.depth2_posts,
        "nearby_posts": summary.nearby_posts,
        "survey_posts": summary.survey_posts,
        "unclassified_posts": summary.unclassified_posts,
        "unknown_lanes": summary.unknown_lanes,
        "unknown_lane_count": summary.unclassified_posts,
        "accounted_saved_posts": summary.accounted_saved_posts,
        "accounting_valid": summary.reconciliation["valid"],
        "accounting_errors": ([] if summary.reconciliation["valid"] else [summary.reconciliation]),
        "reconciliation": summary.reconciliation,
    }
    return fields


def normalize_persisted_run_status(run_status: dict[str, Any] | None) -> dict[str, Any]:
    """Interpret old run_status documents without rewriting them on disk."""
    result = dict(run_status or {})
    run_id = str(result.get("run_id") or "")
    if run_id:
        summary = derive_run_summary(run_id)
        if summary.events:
            result.update({
                "budget_used": summary.budget_used,
                "saved_by_lane": summary.saved_by_lane,
                "saved_by_breadth": summary.saved_by_breadth,
                "target_posts": summary.target_posts,
                "breadth1_posts": summary.depth1_posts,
                "breadth2plus_posts": summary.depth2_posts,
                "nearby_posts": summary.nearby_posts,
                "survey_posts": summary.survey_posts,
                "unclassified_posts": summary.unclassified_posts,
                "unknown_lanes": summary.unknown_lanes,
                "unknown_lane_count": summary.unclassified_posts,
                "accounted_saved_posts": summary.accounted_saved_posts,
                "accounting_valid": summary.reconciliation["valid"],
                "accounting_errors": ([] if summary.reconciliation["valid"] else [summary.reconciliation]),
                "reconciliation": summary.reconciliation,
            })
            return result
    lanes, unknown = aggregate_lane_counts(result.get("saved_by_lane"))
    result["saved_by_lane"] = lanes
    result["saved_by_breadth"] = {
        "target": lanes["target"],
        "breadth1": lanes["depth1"],
        "breadth2plus": lanes["depth2"],
        "unclassified": sum(unknown.values()),
    }
    result["unknown_lanes"] = unknown
    result["unknown_lane_count"] = sum(unknown.values())
    result["target_posts"] = lanes["target"]
    result["breadth1_posts"] = lanes["depth1"]
    result["breadth2plus_posts"] = lanes["depth2"]
    result["nearby_posts"] = lanes["depth1"] + lanes["depth2"]
    result["survey_posts"] = lanes["survey"]
    result["unclassified_posts"] = result["unknown_lane_count"]
    result["accounted_saved_posts"] = sum(lanes.values()) + result["unknown_lane_count"]
    budget = int(result.get("budget_used") or 0)
    result["accounting_valid"] = result["accounted_saved_posts"] == budget and not unknown
    result["accounting_errors"] = ([] if result["accounting_valid"] else [
        *( [f"unknown acquisition lanes: {', '.join(sorted(unknown))}"] if unknown else [] ),
        *( [f"budget/lane mismatch: {budget} != {result['accounted_saved_posts']}"] if result["accounted_saved_posts"] != budget else [] ),
    ])
    return result

MAX_POLICY_DELAY_SECONDS = config.MAX_POLICY_DELAY_SECONDS
MAX_POLICY_WORKERS = config.MAX_POLICY_WORKERS

# Policy locations remain owned by this compatibility surface.  The parsing
# implementation receives an explicit path so tests and tools that override
# these names at call time continue to work without a second path authority.
context_config = config.context_config
resolve_network_profile = config.resolve_network_profile


def load_network_policy(path: Path | None = None) -> dict[str, Any]:
    return config.load_network_policy(NETWORK_POLICY_FILE if path is None else Path(path))


def load_context_policy(path: Path | None = None) -> dict[str, Any]:
    return config.load_context_policy(CONTEXT_POLICY_FILE if path is None else Path(path))


def set_host_capabilities(*, compact_terminal: bool, keyboard_controls: bool) -> None:
    """Set optional host presentation/input capabilities without platform imports."""
    HOST_CAPABILITIES.update({
        "compact_terminal": bool(compact_terminal),
        "keyboard_controls": bool(keyboard_controls),
    })


def build_crawl_request(
    target: str,
    *,
    max_posts: int = 300,
    context: str = "explore",
    context_depth: int | None = 2,
    focus: str | None = None,
    profile_id: str | None = None,
    full_res: bool = False,
    targets: list[str] | tuple[str, ...] | None = None,
    strategy: str | None = None,
    survey_node_limit: int | None = None,
    survey_request_limit: int | None = None,
    survey_max_distance: int | None = None,
    max_breadth: int | None = None,
    max_depth: int | None = None,
    capture_shape: tuple[Any, ...] | list[Any] | None = None,
    capture_curve: tuple[Any, ...] | list[Any] | None = None,
) -> CrawlRequest:
    """Build the one validated configuration used by every entry point."""
    canonical_target = canonical_username(str(target).strip())
    if isinstance(max_posts, bool) or not isinstance(max_posts, int) or max_posts < 0:
        raise PolicyError("max_posts must be a non-negative integer")
    if context not in {"none", "nearby", "explore"}:
        raise PolicyError("context must be none, nearby, or explore")
    if context_depth is not None and (isinstance(context_depth, bool) or not isinstance(context_depth, int) or context_depth < 0):
        raise PolicyError("context_depth must be a non-negative integer")
    if context == "explore" and context_depth is not None and context_depth < 1:
        raise PolicyError("context_depth must be at least 1 for explore")
    policy = load_network_policy()
    selected_profile = resolve_network_profile(policy, profile_id)
    context_policy = load_context_policy()
    if strategy is not None:
        config.coverage_strategy(
            context_policy, strategy, max_distance=survey_max_distance,
            survey_enabled=(strategy == "survey" or survey_max_distance is not None),
        )
        context = {"archive": "none", "neighborhood": "nearby", "explore": "explore", "survey": "explore"}.get(strategy, context)
    context_config(context_policy, context, context_depth, focus)
    if not isinstance(full_res, bool):
        raise PolicyError("full_res must be boolean")
    selected_targets = tuple(canonical_username(str(item).strip()) for item in (targets or (canonical_target,)))
    resolved_shape = None
    if capture_shape is not None:
        try:
            resolved_shape = tuple(
                point if isinstance(point, CaptureShapePoint) else CaptureShapePoint(
                    float(point["breadth"]), float(point["depth"])
                )
                for point in capture_shape
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PolicyError(f"invalid capture_shape: {exc}") from exc
    resolved_curve = None
    if capture_curve is not None:
        try:
            from .models import CaptureCurvePoint
            resolved_curve = tuple(
                point if isinstance(point, CaptureCurvePoint) else CaptureCurvePoint(
                    int(point["breadth"]),
                    None if point.get("history_posts") is None else int(point["history_posts"]),
                )
                for point in capture_curve
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PolicyError(f"invalid capture_curve: {exc}") from exc
    return CrawlRequest(
        target=canonical_target,
        targets=selected_targets,
        max_posts=max_posts,
        context=context,
        context_depth=context_depth,
        focus=focus,
        profile_id=str(selected_profile["id"]),
        full_res=full_res,
        strategy=strategy,
        survey_node_limit=survey_node_limit,
        survey_request_limit=survey_request_limit,
        survey_max_distance=survey_max_distance,
        max_breadth=max_breadth,
        max_depth=max_depth,
        capture_shape=resolved_shape,
        capture_curve=resolved_curve,
    )


def _set_lifecycle(value: str) -> None:
    global ACTIVE_LIFECYCLE
    with RUNTIME_LOCK:
        ACTIVE_LIFECYCLE = value


def _validate_runtime_control(name: str, value: Any) -> Any:
    if name == "focus":
        if value not in {"deep", "balanced", "wide", "neighbors"}:
            raise PolicyError("focus must be deep, balanced, wide, or neighbors")
        return value
    if name == "budget_limit":
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PolicyError("budget_limit must be a non-negative integer")
        return value
    if name in {"context_multiplier", "relationship_multiplier"}:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise PolicyError(f"{name} must be a finite number")
        if value < 0 or value > 4:
            raise PolicyError(f"{name} must be between 0 and 4")
        return float(value)
    if name == "network_profile_id":
        profile = resolve_network_profile(load_network_policy(), str(value))
        return str(profile["id"])
    if name == "cancel_requested":
        if value is not True:
            raise PolicyError("cancel_requested can only be set to true")
        return True
    raise PolicyError(f"unsupported runtime control: {name}")


def apply_runtime_control(
    name: str,
    value: Any,
    *,
    source: str,
    runtime: RuntimeControls | None = None,
    status: "CrawlerStatus | None" = None,
) -> dict[str, Any]:
    """Validate and apply one control through the canonical runtime model."""
    runtime = runtime or ACTIVE_RUNTIME
    status = status or ACTIVE_STATUS
    if runtime is None or status is None:
        raise PolicyError("no crawler is currently running")
    with RUNTIME_LOCK:
        normalized = _validate_runtime_control(name, value)
        if name == "focus":
            bias = {"deep": 0.0, "balanced": 0.5, "wide": 1.0, "neighbors": 1.2}[normalized]
            runtime.update("focus_bias", bias, source)
            status.focus = normalized
        elif name == "budget_limit":
            normalized = max(normalized, status.budget_used)
            runtime.update("budget_limit", normalized, source)
            status.budget_limit = normalized
        elif name in {"context_multiplier", "relationship_multiplier"}:
            runtime.update(name, normalized, source)
        elif name == "network_profile_id":
            profile = resolve_network_profile(load_network_policy(), normalized)
            global NETWORK_PROFILE
            NETWORK_PROFILE = dict(profile)
            runtime.update(name, normalized, source)
            status.network = str(profile.get("label", normalized))
        elif name == "cancel_requested":
            runtime.cancel_count += 1
            runtime.update(name, True, source)
        return status_snapshot(status)


class RuntimeKeyController:
    """Best-effort, action-boundary controls with a non-interactive fallback."""

    def __init__(self, runtime: RuntimeControls, status: "CrawlerStatus", stream: Any = None) -> None:
        self.runtime = runtime
        self.status = status
        self.stream = stream or sys.stdin
        self.enabled = bool(getattr(self.stream, "isatty", lambda: False)()) and HOST_CAPABILITIES["keyboard_controls"]

    def _read_posix(self) -> list[str]:
        if not self.enabled or select is None or termios is None or tty is None:
            return []
        try:
            old = termios.tcgetattr(self.stream.fileno())
            tty.setcbreak(self.stream.fileno())
            keys = []
            while select.select([self.stream], [], [], 0)[0]:
                value = self.stream.read(1)
                if value:
                    keys.append(value)
            return keys
        except (OSError, ValueError, AttributeError):
            self.enabled = False
            return []
        finally:
            try:
                termios.tcsetattr(self.stream.fileno(), termios.TCSADRAIN, old)
            except (OSError, ValueError, UnboundLocalError):
                pass

    def _read_windows(self) -> list[str]:
        if not self.enabled or msvcrt is None:
            return []
        keys = []
        while msvcrt.kbhit():
            value = msvcrt.getwch()
            if value and value not in {"\x00", "\xe0"}:
                keys.append(value)
        return keys

    def poll(self) -> None:
        if not self.enabled:
            return
        keys = self._read_windows() if msvcrt is not None else self._read_posix()
        for key in keys:
            self.apply(key.lower())

    def apply(self, key: str) -> None:
        values = {
            "d": ("focus", "deep"), "b": ("focus", "balanced"),
            "w": ("focus", "wide"), "n": ("focus", "neighbors"),
            "g": ("network_profile_id", "gentle"), "r": ("network_profile_id", "normal"),
            "u": ("network_profile_id", "urgent"), "q": ("cancel_requested", True),
        }
        if key in values:
            try:
                apply_runtime_control(values[key][0], values[key][1], source="keyboard", runtime=self.runtime, status=self.status)
            except PolicyError:
                return
        elif key == "]":
            apply_runtime_control("context_multiplier", min(4.0, self.runtime.context_multiplier + 0.25), source="keyboard", runtime=self.runtime, status=self.status)
        elif key == "[":
            apply_runtime_control("context_multiplier", max(0.0, self.runtime.context_multiplier - 0.25), source="keyboard", runtime=self.runtime, status=self.status)
        elif key == ".":
            apply_runtime_control("relationship_multiplier", min(4.0, self.runtime.relationship_multiplier + 0.25), source="keyboard", runtime=self.runtime, status=self.status)
        elif key == ",":
            apply_runtime_control("relationship_multiplier", max(0.0, self.runtime.relationship_multiplier - 0.25), source="keyboard", runtime=self.runtime, status=self.status)
        elif key == "+":
            apply_runtime_control("budget_limit", max(self.runtime.budget_limit, self.status.budget_used) + 25, source="keyboard", runtime=self.runtime, status=self.status)
        elif key == "-":
            apply_runtime_control("budget_limit", max(self.status.budget_used, self.runtime.budget_limit - 25), source="keyboard", runtime=self.runtime, status=self.status)


def request_graceful_cancel(_signum: int, _frame: Any) -> None:
    with RUNTIME_LOCK:
        if ACTIVE_RUNTIME is not None:
            if ACTIVE_RUNTIME.cancel_requested:
                raise KeyboardInterrupt
            ACTIVE_RUNTIME.cancel_count += 1
            ACTIVE_RUNTIME.update("cancel_requested", True, source="SIGINT")
            _set_lifecycle("finalizing")


def activate_blog_state(state: BlogState) -> None:
    """Activate one state for legacy upstream-facing functions.

    The scheduler guarantees that this is the only active acquisition state.
    State is explicit and restored by the next activation rather than being
    mutated through repeated configure() calls.
    """
    global BLOG, BLOG_HOST, OUT, JSON_DIR, MAX_POSTS
    BLOG = state.blog
    BLOG_HOST = state.host
    OUT = state.out
    JSON_DIR = state.json_dir
    MAX_POSTS = state.max_posts


def canonical_username(value: str) -> str:
    """Validate and canonicalize a Tumblr blog identifier."""
    if not re.fullmatch(r"[A-Za-z0-9-]+", value):
        raise argparse.ArgumentTypeError(
            "username must contain only ASCII letters, digits, and hyphens"
        )
    return value.lower()


def canonical_blog_name(value: str) -> str:
    candidate = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9-]+", candidate) or candidate in {"www", "api", "tumblr"}:
        raise ValueError(f"invalid canonical Tumblr blog: {value}")
    return candidate


def canonical_archive_root(blog: str) -> Path:
    return CONTENT_ROOT / canonical_blog_name(blog)


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("max_posts must be a non-negative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("max_posts must be a non-negative integer")
    return parsed


def configure(
    blog: str,
    max_posts: int,
    full_res: bool = False,
    profile: dict[str, Any] | None = None,
    capture_plan: CapturePlan | None = None,
) -> None:
    global BLOG, BLOG_HOST, OUT, JSON_DIR, MAX_POSTS, FULL_RES, NETWORK_PROFILE, CAPTURE_PLAN
    if profile is None:
        profile = resolve_network_profile(load_network_policy())
    BLOG = blog
    BLOG_HOST = f"{BLOG}.tumblr.com"
    OUT = canonical_archive_root(BLOG)
    JSON_DIR = OUT / "json"
    MAX_POSTS = max_posts
    FULL_RES = full_res
    CAPTURE_PLAN = capture_plan or CapturePlan(media_quality="HIGH" if full_res else "COMPACT")
    NETWORK_PROFILE = dict(profile)


def ensure_blog_stylesheets() -> None:
    """Create the stable global stylesheet hook without overwriting overrides."""
    OUT.mkdir(parents=True, exist_ok=True)
    if not GLOBAL_CSS.is_file():
        raise RuntimeError(f"Missing shared stylesheet: {GLOBAL_CSS}")

    custom = OUT / "custom.css"
    global_css = os.path.relpath(GLOBAL_CSS, OUT).replace(os.sep, "/")
    managed_custom = '@import url("../../App/assets/archive.css");\n@import url("override.css");\n'
    legacy_custom = (
        f'@import url("{global_css}");\n'
        '@import url("../../App/assets/archive.css");\n'
        '@import url("override.css");\n'
    )
    if not custom.exists():
        custom.write_text(managed_custom, encoding="utf-8")
    else:
        try:
            if custom.read_text(encoding="utf-8") == legacy_custom:
                custom.write_text(managed_custom, encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            pass

    override = OUT / "override.css"
    if not override.exists():
        override.write_text(
            "/* Optional per-blog overrides. Leave empty to use the shared style. */\n",
            encoding="utf-8",
        )
        _augment_blog_tags_link(BLOG)


def _retry_after_seconds(value: str | None) -> float | None:
    """Compatibility wrapper for the low-level network helper."""
    return network.retry_after_seconds(value)


def fetch_public_page(start: int, count: int = PAGE_SIZE) -> dict[str, Any]:
    """Compatibility wrapper using the currently active blog state."""
    return network.fetch_public_page(
        blog=BLOG,
        blog_host=BLOG_HOST,
        start=start,
        count=count,
        user_agent=USER_AGENT,
        opener=urlopen,
        report_warning=lambda message: report_terminal_event(message, warning=True),
        pressure=ACTIVE_REQUEST_PRESSURE,
    )


_ProfileMetadataParser = profile_parser.ProfileMetadataParser


def capture_blog_profile_metadata(blog: str) -> None:
    """Capture one public blog-homepage profile snapshot without post work."""
    profile_path = _profile_path(blog)
    existing: dict[str, Any] = {}
    if profile_path.is_file():
        try:
            loaded = json.loads(profile_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = {}
    if existing.get("profile_fetch_attempted"):
        return

    profile = dict(existing)
    profile.update({
        "schema_version": 1,
        "blog": blog,
        "observed_at": time.time(),
        "profile_fetch_attempted": True,
        "profile_fetch_provenance": "public Tumblr blog homepage",
    })
    try:
        if ACTIVE_REQUEST_PRESSURE is not None and not ACTIVE_REQUEST_PRESSURE.reserve("profile"):
            profile["profile_fetch_status"] = "not_checked_request_pressure"
            write_json_atomic(profile_path, profile)
            return
        request = Request(f"https://{canonical_blog_name(blog)}.tumblr.com/", headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=30) as response:
            observation = profile_parser.parse_profile_observation(
                response.read().decode("utf-8", errors="replace")
            )
        if observation["title"]:
            profile["title"] = observation["title"]
        if observation["description"]:
            profile["description"] = observation["description"]
        profile["source"] = "public Tumblr blog homepage metadata"
        profile["profile_fetch_status"] = "captured"
    except (OSError, HTTPError, URLError, ValueError):
        profile["profile_fetch_status"] = "unavailable"
    write_json_atomic(profile_path, profile)


def blog_info(feed: dict[str, Any], total: int) -> dict[str, Any]:
    return normalize.blog_info(feed, total, blog=BLOG, blog_host=BLOG_HOST)


def photo_object(p: dict[str, Any], full_res: bool = False) -> dict[str, Any]:
    return normalize.photo_object(p, full_res=full_res, quality=CAPTURE_PLAN.media_quality)


def normalize_post(p: dict[str, Any], feed: dict[str, Any], total: int) -> dict[str, Any]:
    """Compatibility wrapper using the currently active blog state."""
    return normalize.normalize_post(
        p,
        feed,
        total,
        blog=BLOG,
        blog_host=BLOG_HOST,
        full_res=FULL_RES,
        quality=CAPTURE_PLAN.media_quality,
    )


def post_is_complete(pid: int) -> bool:
    return (
        (JSON_DIR / f"{pid}.json").is_file()
        and (OUT / "posts" / f"{pid}.html").is_file()
    )


def post_source_exists(pid: int) -> bool:
    return archive.source_record_exists(JSON_DIR, pid)


def report_terminal_event(message: str, *, warning: bool = False) -> None:
    renderer = ACTIVE_PROGRESS_RENDERER
    if renderer is not None:
        renderer.event(message, warning=warning)
    elif VERBOSE:
        print(message)


def pending_json_ids() -> list[int]:
    if not JSON_DIR.exists():
        return []
    pending = []
    for path in JSON_DIR.glob("*.json"):
        try:
            pid = int(path.stem)
        except ValueError:
            continue
        if not post_is_complete(pid):
            pending.append(pid)
    pending.sort(reverse=True)
    return pending


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    archive.write_json_atomic(path, data)


def _append_repair_task(post_id: int | str | None, stage: str, error: Exception) -> None:
    """Persist an optional-stage failure without invalidating source JSON."""
    task = {
        "blog": BLOG,
        "post_id": None if post_id is None else str(post_id),
        "stage": stage,
        "kind": "optional_stage_failure",
        "message": str(error),
        "created_at": time.time(),
    }
    try:
        path = OUT / "repair" / "tasks.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(task, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        # The source record is still authoritative even if repair metadata
        # cannot be written. Keep the optional failure non-blocking.
        pass
    detail = f"{BLOG}/{post_id}" if post_id is not None else BLOG
    if ACTIVE_STATUS is not None:
        ACTIVE_STATUS.warning(
            f"{stage} warning",
            key=f"optional-stage:{stage}",
            detail=detail,
        )
    report_terminal_event(f"{stage} warning for {detail}: {str(error)[:140]}", warning=True)


def _record_deferred_presentation(post_id: int | str) -> None:
    """Record non-blocking presentation work without treating it as a crawl failure."""
    task = {
        "blog": BLOG,
        "post_id": str(post_id),
        "stage": "presentation-deferred",
        "kind": "deferred_optional_work",
        "message": "canonical source acquired; rendering deferred",
        "created_at": time.time(),
    }
    try:
        path = OUT / "repair" / "tasks.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(task, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


def _write_fallback_post(pid: int, reason: Exception) -> None:
    """Write a readable, source-faithful page when upstream rendering fails."""
    source_path = JSON_DIR / f"{pid}.json"
    try:
        record = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"preserved source record {pid} could not be read: {exc}") from exc
    if not isinstance(record, dict):
        raise RuntimeError(f"preserved source record {pid} is not an object")

    raw_source = record.get("_puppetbackup_source_record")
    if not isinstance(raw_source, dict):
        raw_source = record
    raw_json = json.dumps(raw_source, ensure_ascii=False, sort_keys=True, indent=2)
    title = str(record.get("title") or f"Post {pid}")
    timestamp = int(record.get("timestamp") or 0)
    date_label = str(record.get("date") or (time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(timestamp)) if timestamp else "undated"))
    source_url = str(record.get("post_url") or record.get("short_url") or "")
    summary = record.get("body") or record.get("answer") or record.get("text") or record.get("description") or ""
    if isinstance(summary, (dict, list)):
        summary = json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2)
    summary_html = f"<p>{escape(str(summary))}</p>" if summary else ""
    source_link = f'<p><a href="{escape(source_url)}" rel="noreferrer noopener">Original public post</a></p>' if source_url else ""
    html = (
        "<!doctype html><meta charset=\"utf-8\">"
        f"<title>{escape(title)}</title>"
        f"<article class=\"fallback-post\" id=\"p-{escape(str(pid))}\">"
        f"<header><h1>{escape(title)}</h1><time datetime=\"{escape(str(timestamp))}\">{escape(date_label)}</time></header>"
        "<p><strong>Minimal fallback rendering.</strong> The source record was preserved; optional rendering failed.</p>"
        + summary_html
        + source_link
        + "<details open><summary>Preserved source record</summary><pre>"
        + escape(raw_json)
        + "</pre></details>"
        + "</article>"
    )
    path = OUT / "posts" / f"{pid}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(html, encoding="utf-8")
    os.replace(temporary, path)
    _append_repair_task(pid, "render-fallback", reason)


def process_batch(ids: list[int]) -> int:
    if not ids:
        return 0
    report_terminal_event(f"Rendering {len(ids)} posts")
    render_error: Exception | None = None
    try:
        render_with_tumblr_backup(ids)
    except Exception as exc:
        render_error = exc
        report_terminal_event(
            f"tumblr-backup rendering failed; preserving source and continuing: {str(exc)[:140]}",
            warning=True,
        )

    for pid in ids:
        if not post_is_complete(pid):
            reason = render_error or RuntimeError("renderer returned without producing post HTML")
            try:
                _write_fallback_post(pid, reason)
            except Exception as exc:
                # Source JSON was already durably written. Leave a repair task
                # and continue to the next source record.
                _append_repair_task(pid, "render-fallback", exc)

    if CAPTURE_PLAN.avatar:
        for pid in ids:
            try:
                capture_participant_avatars([pid])
            except Exception as exc:
                # Avatars and trail enrichment are strictly optional.
                _append_repair_task(pid, "participant-avatar", exc)

    # Source JSON is the durable acquisition boundary. Optional presentation
    # work must not stop later feed records from being acquired.
    return len(ids)


def repair_interrupted_work() -> int:
    pending = pending_json_ids()
    if not pending:
        return 0
    report_terminal_event(f"Repairing {len(pending)} pending posts")
    finished = 0
    for offset in range(0, len(pending), PROCESS_BATCH):
        finished += process_batch(pending[offset:offset + PROCESS_BATCH])
    return finished


def acquire_and_process() -> tuple[int, int, bool]:
    """Fetch newest-first with the configured inspected-record limit.

    Small batches are handed to tumblr-backup immediately instead of waiting
    for the entire crawl to finish.
    """
    OUT.mkdir(parents=True, exist_ok=True)
    JSON_DIR.mkdir(parents=True, exist_ok=True)

    if MAX_POSTS:
        report_terminal_event(f"Checking newest {MAX_POSTS} posts")
    else:
        report_terminal_event("Checking all available posts")

    inspected = 0
    completed = 0
    start = 0
    total: int | None = None
    reached_existing = False

    first_feed_request = True
    profile_captured = False
    while MAX_POSTS == 0 or inspected < MAX_POSTS:
        request_count = PAGE_SIZE if MAX_POSTS == 0 else min(PAGE_SIZE, MAX_POSTS - inspected)
        if not first_feed_request:
            wait_seconds = NETWORK_PROFILE["feed_delay_seconds"] + random.uniform(
                0, NETWORK_PROFILE["feed_jitter_seconds"]
            )
            if wait_seconds > 0:
                time.sleep(wait_seconds)
        feed = fetch_public_page(start, request_count)
        first_feed_request = False
        posts = list(feed.get("posts") or [])
        if posts and not profile_captured:
            capture_blog_profile_metadata(BLOG)
            profile_captured = True

        if total is None:
            total = int(feed.get("posts-total") or len(posts))
        if not posts:
            break

        batch: list[int] = []

        for source_post in posts:
            if MAX_POSTS and inspected >= MAX_POSTS:
                break

            inspected += 1
            pid = int(source_post["id"])

            # Incremental boundary. Any JSON left unfinished by an interruption
            # was repaired before this crawl began.
            if post_is_complete(pid):
                # Presence is per-post evidence, not proof of contiguous
                # historical coverage. Keep scanning past this island.
                reached_existing = True
                continue

            normalized = normalize_post(source_post, feed, total)
            write_json_atomic(JSON_DIR / f"{pid}.json", normalized)
            batch.append(pid)
            report_terminal_event(f"Saved source JSON {pid}")

            if len(batch) >= PROCESS_BATCH:
                completed += process_batch(batch)
                batch = []

        if batch:
            completed += process_batch(batch)

        start += len(posts)
        if total is not None and start >= total:
            break

    hit_depth_limit = bool(MAX_POSTS and inspected >= MAX_POSTS and not reached_existing)
    return inspected, completed, hit_depth_limit


def ensure_tumblr_backup() -> None:
    try:
        import tumblr_backup.main  # noqa: F401
        return
    except ImportError as exc:
        raise RuntimeError(
            "tumblr-backup is not available in this Python environment. "
            "Start Tumblr Scraper through a supported launcher or ./tumblr-scraper."
        ) from exc


def tumblr_backup_cli() -> Path:
    """Find the installed upstream CLI in the current Python environment."""
    candidates: list[Path] = [Path(sys.executable).with_name("tumblr-backup")]
    installed = shutil.which("tumblr-backup")
    if installed:
        candidates.append(Path(installed))

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved

    raise RuntimeError(
        "tumblr-backup was installed but its CLI was not found in the current Python environment"
    )


def run_tumblr_backup(
    extra_args: list[str],
    new_ids: list[int] | None = None,
    allow_no_posts: bool = False,
    index_only: bool = False,
) -> int:
    ensure_tumblr_backup()
    with tempfile.TemporaryDirectory(prefix="puppetbackup-") as td:
        temp = Path(td)
        config_home = temp / "config-home"
        config = config_home / "tumblr-backup" / "config.json"

        # tumblr-backup 1.0.7 insists on reading a config key even when
        # --reuse-json means it never contacts the keyed API. Give it an
        # explicitly dummy value in a temporary directory which disappears
        # at the end of this run.
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('{"oauth_consumer_key":"unused-no-api-key"}\n', encoding="utf-8")

        # Each batch runs in a fresh process, using this same Python interpreter.
        if index_only:
            index_runner = (
                "import tumblr_backup.main as upstream; "
                "upstream.get_avatar = lambda *args, **kwargs: None; "
                "upstream.get_style = lambda *args, **kwargs: None; "
                "raise SystemExit(upstream.main())"
            )
            cmd = [sys.executable, "-c", index_runner]
        else:
            cmd = [
                sys.executable,
                str(tumblr_backup_cli()),
            ]
        cmd.extend([
            "--reuse-json",
            "--no-copy-notes",
            "--threads", str(NETWORK_PROFILE["media_workers"]),
            "--outdir", str(OUT),
        ])
        if new_ids is not None:
            ids_file = temp / "new_ids.txt"
            ids_file.write_text("".join(f"{pid}\n" for pid in new_ids), encoding="utf-8")
            cmd.extend(["--id-file", str(ids_file)])
        cmd.extend(extra_args)
        cmd.append(BLOG)
        env = os.environ.copy()
        env["XDG_CONFIG_HOME"] = str(config_home)
        if VERBOSE:
            result = subprocess.run(cmd, env=env, check=False)
        else:
            result = subprocess.run(
                cmd,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
        stdout = getattr(result, "stdout", "")
        stderr = getattr(result, "stderr", "")
        output = "" if VERBOSE else (stdout if isinstance(stdout, str) else "") + (stderr if isinstance(stderr, str) else "")
        if output:
            for line in output.splitlines():
                message = line.strip()
                if not message:
                    continue
                lower = message.lower()
                if any(token in lower for token in ("error", "failed", "warning")):
                    report_terminal_event(message[:160], warning=True)
                elif any(token in lower for token in (
                    "getting basic information", "getting avatar and style", "building index",
                    "waiting for worker threads", "stopping backup",
                )):
                    report_terminal_event(message[:160])

        accepted = (0, 5) if allow_no_posts else (0,)
        if result.returncode not in accepted:
            report_terminal_event(f"tumblr-backup failed with exit code {result.returncode}", warning=True)
            raise RuntimeError(f"tumblr-backup failed with exit code {result.returncode}")
        return result.returncode


def render_with_tumblr_backup(new_ids: list[int]) -> None:
    if not new_ids:
        return
    run_tumblr_backup([], new_ids=new_ids)


def completed_post_files() -> list[Path]:
    return sorted((OUT / "posts").glob("*.html"))


def finalize_archive_index() -> None:
    """Rebuild the complete local index from every saved post using upstream."""
    if not completed_post_files():
        return
    run_tumblr_backup(
        ["--count", "0", "--no-get", "--ignore-diffopt"],
        allow_no_posts=True,
        index_only=True,
    )
    index = OUT / "index.html"
    if not index.is_file():
        raise RuntimeError("tumblr-backup finished without producing index.html")


def _json_records(directory: Path) -> list[dict[str, Any]]:
    return archive.read_json_records(directory)


def _blog_from_reference(name: Any, url: Any) -> tuple[str, str] | None:
    return relationships.blog_from_reference(name, url)


def interaction_evidence(owner: str, record: dict[str, Any]) -> list[dict[str, Any]]:
    return relationships.interaction_evidence(owner, record)


def _evidence_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return relationships.evidence_key(item)


def _context_root(primary: str) -> Path:
    return OBSERVATIONS_ROOT / canonical_blog_name(primary)


def _presentation_state_path() -> Path:
    return presentation.presentation_state_path(APP_ROOT)


def _presentation_is_dirty() -> bool:
    return presentation.presentation_is_dirty(APP_ROOT, PRESENTATION_SCHEMA_VERSION, _presentation_identity())


def mark_presentation_dirty(reason: str = "canonical data changed") -> None:
    presentation.mark_presentation_dirty(APP_ROOT, PRESENTATION_SCHEMA_VERSION, reason)


def _canonical_blog_directories() -> list[Path]:
    if not CONTENT_ROOT.is_dir():
        return []
    return sorted(
        path for path in CONTENT_ROOT.iterdir()
        if path.is_dir() and re.fullmatch(r"[a-z0-9-]+", path.name) and path.name not in {"assets", "tags"}
    )


def _canonical_local_records() -> list[dict[str, Any]]:
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for blog_dir in _canonical_blog_directories():
        for record in _json_records(blog_dir / "json"):
            pid = str(record.get("id_string") or record.get("id") or "")
            if not pid:
                continue
            record = dict(record)
            record["_canonical_blog"] = blog_dir.name
            records.setdefault((blog_dir.name, pid), record)
    return list(records.values())


def _legacy_presentation_roots(blog: str) -> list[Path]:
    """Find recovered local renderer assets without making them a new source."""
    roots = [BASE_DIR / "Backups" / blog]
    restored = BASE_DIR / "Backups (copy)"
    if restored.is_dir():
        roots.extend(path for path in restored.glob(f"*/context/blogs/{blog}") if path.is_dir())
    return [path for path in roots if path.is_dir()]


def recover_legacy_presentation_assets() -> int:
    """Recover local historical HTML/media/avatar files into generated output.

    Canonical JSON remains the admission test: only post fragments whose IDs
    already exist in the selected bundle are copied. This is a presentation
    repair, not a downloader or a second archive database.
    """
    recovered = 0
    for blog_dir in _canonical_blog_directories():
        blog = blog_dir.name
        json_ids = {
            str(record.get("id_string") or record.get("id") or "")
            for record in _json_records(blog_dir / "json")
        }
        destination_profile = blog_dir / "profile"
        destination_profile.mkdir(parents=True, exist_ok=True)
        destination_media = blog_dir / "media"
        destination_posts = blog_dir / "posts"
        for legacy in _legacy_presentation_roots(blog):
            avatar_sources = sorted((legacy / "profile").glob("avatar.*")) + sorted((legacy / "theme").glob("avatar.*"))
            if avatar_sources and not sorted(destination_profile.glob("avatar.*")):
                source = avatar_sources[0]
                shutil.copy2(source, destination_profile / ("avatar" + source.suffix))
                recovered += 1
            legacy_posts = legacy / "posts"
            for source in sorted(legacy_posts.glob("*.html")):
                if source.stem not in json_ids:
                    continue
                target = destination_posts / source.name
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    recovered += 1
            legacy_media = legacy / "media"
            if legacy_media.is_dir():
                for source in sorted(path for path in legacy_media.rglob("*") if path.is_file()):
                    target = destination_media / source.relative_to(legacy_media)
                    if not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, target)
                        recovered += 1
    return recovered


def _recover_presentation_assets_once() -> None:
    global _PRESENTATION_RECOVERY_ROOT
    if _PRESENTATION_RECOVERY_ROOT == CONTENT_ROOT:
        return
    recover_legacy_presentation_assets()
    _PRESENTATION_RECOVERY_ROOT = CONTENT_ROOT


def _relative_archive_url(blog: str, post_id: str | None = None) -> str:
    return presentation.relative_archive_url(blog, post_id)


def _shared_asset_paths() -> tuple[Path, Path]:
    return presentation.shared_asset_paths(APP_ROOT)


def _archive_shell(
    title: str,
    content: str,
    links: list[tuple[str, str]],
    *,
    active: str = "",
    stylesheet: str = "assets/archive.css",
    script: str = "assets/archive.js",
    extra: str = "",
) -> str:
    return presentation.archive_shell(
        title, content, links, active=active, stylesheet=stylesheet,
        script=script, extra=extra, archive_name=ACTIVE_ARCHIVE_NAME,
    )


def _application_chrome(links: list[tuple[str, str]], active: str = "") -> str:
    return presentation.application_chrome(links, active)


def _application_prefix(links: list[tuple[str, str]]) -> str:
    return presentation.application_prefix(links)


def _application_drawer(prefix: str, active: str, link_map: dict[str, str]) -> str:
    return presentation.application_drawer(prefix, active, link_map)


def _global_nav(prefix: str = "") -> list[tuple[str, str]]:
    return presentation.global_nav(prefix)




def _acquisition_ledger_path() -> Path:
    return CONTENT_ROOT / "acquisition-ledger.jsonl"


def _read_acquisition_ledger() -> dict[tuple[str, str], dict[str, Any]]:
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    path = _acquisition_ledger_path()
    if not path.is_file():
        return entries
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return entries
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict) or not item.get("blog") or not item.get("post_id"):
            continue
        entries[(str(item["blog"]), str(item["post_id"]))] = item
    return entries


def _append_acquisition_ledger(item: dict[str, Any]) -> None:
    path = _acquisition_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, sort_keys=True) + "\n")


def reconcile_acquisition_ledger() -> dict[tuple[str, str], dict[str, Any]]:
    """Reconcile coordination claims from canonical source JSON.

    Source JSON is authoritative. Missing or corrupt journal entries are
    reconstructed; claims without source JSON are ignored for budgeting and
    may be retried. This deliberately is not a transaction boundary.
    """
    ledger = _read_acquisition_ledger()
    canonical = {(str(r["_canonical_blog"]), str(r.get("id_string") or r.get("id"))): r for r in _canonical_local_records()}
    for key in sorted(canonical):
        if key not in ledger:
            blog, post_id = key
            item = {
                "run_id": str(canonical[key].get("_puppetbackup_run_id") or "reconciled"),
                "blog": blog,
                "post_id": post_id,
                "breadth": int(canonical[key].get("_puppetbackup_breadth") or canonical[key].get("_puppetbackup_graph_depth") or 0),
                "is_target": bool(canonical[key].get("_puppetbackup_is_target", False)),
                "action_kind": str(canonical[key].get("_puppetbackup_action_kind") or "reconciled_source_json"),
                "reason_codes": list(canonical[key].get("_puppetbackup_reason_codes") or ["reconciled from canonical source JSON"]),
                "state": "acquired",
                "planned_cost": 0,
                "actual_cost": 1,
                "reconciled": True,
                "updated_at": time.time(),
            }
            _append_acquisition_ledger(item)
            ledger[key] = item
    reconciled: dict[tuple[str, str], dict[str, Any]] = {}
    for key, item in ledger.items():
        if key not in canonical or item.get("state") != "acquired":
            continue
        interpreted = dict(item)
        raw_lane = item.get("acquisition_lane", item.get("lane"))
        interpreted["raw_lane"] = raw_lane
        try:
            normalized = canonical_acquisition_lane(raw_lane, item.get("breadth", item.get("graph_depth")))
        except UnknownAcquisitionLane:
            interpreted["unknown_lane"] = True
        else:
            interpreted["acquisition_lane"] = normalized
            interpreted["lane"] = normalized
        reconciled[key] = interpreted
    return reconciled


def ensure_shared_archive_assets() -> None:
    presentation.copy_shared_archive_assets(
        APP_ROOT,
        SOURCE_ASSET_DIR,
        GLOBAL_CSS,
        SOURCE_ARCHIVE_CSS,
        SOURCE_ARCHIVE_JS,
    )


def _reader_controls(*, page: bool = False) -> str:
    return presentation.reader_controls(page=page)


def _crawler_controls(*, page: bool = False) -> str:
    known_blogs: list[tuple[str, int]] = []
    catalog_path = CONTENT_ROOT / "catalog.json"
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        known_blogs = sorted(
            [(str(item.get("blog")), int(item.get("local_post_count", 0))) for item in catalog.get("blogs", []) if item.get("blog")],
            key=lambda item: (-item[1], item[0].casefold()),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return presentation.crawler_controls(page=page, known_blogs=known_blogs)






_FRAGMENT_DROP_TAGS = {"script", "form", "iframe", "object", "embed"}
_FRAGMENT_REFERENCE_ATTRS = {"href", "src", "poster", "action", "data", "cite"}
_FRAGMENT_ID_ATTRS = {"for", "aria-controls", "aria-describedby", "aria-labelledby", "aria-details"}


def _rebase_local_reference(value: str, source_page: Path, destination_page: Path) -> str:
    """Rebase a local rendered-post URL for an aggregate page.

    External URLs and data URLs remain unchanged because they are provenance or
    media values from the source record. Local paths are resolved from the
    standalone post's directory and made relative to the destination page.
    """
    if value.startswith("#"):
        return value
    if not value or value.startswith(("//", "/", "data:", "mailto:", "tel:")):
        return value
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        return value
    target = (source_page.parent / unquote(parsed.path)).resolve()
    try:
        relative = os.path.relpath(target, destination_page.parent.resolve()).replace(os.sep, "/")
    except ValueError:
        return value
    return urlunsplit(("", "", relative, parsed.query, parsed.fragment))


def _rebase_srcset(value: str, source_page: Path, destination_page: Path) -> str:
    entries = []
    for item in value.split(","):
        bits = item.strip().split()
        if not bits:
            continue
        parsed = urlsplit(bits[0])
        if parsed.scheme or parsed.netloc or bits[0].startswith(("//", "data:")):
            continue
        bits[0] = _rebase_local_reference(bits[0], source_page, destination_page)
        entries.append(" ".join(bits))
    return ", ".join(entries)


class _RenderedPostFragment(HTMLParser):
    """Extract and harden one upstream-rendered article body.

    This is deliberately a presentation adapter. It never creates canonical
    post data and it leaves the upstream body markup intact apart from URL
    rebasing, unsafe active content removal, and fragment-local ID namespacing.
    """

    def __init__(self, source_page: Path, destination_page: Path, namespace: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source_page = source_page
        self.destination_page = destination_page
        self.namespace = namespace
        self.article_depth = 0
        self.skip_depth = 0
        self.skip_header_depth = 0
        self.started = False
        self.finished = False
        self.parts: list[str] = []

    def _in_fragment(self) -> bool:
        return self.started and not self.finished and self.article_depth > 0

    def _attrs(self, attrs: list[tuple[str, str | None]], tag: str = "") -> str:
        rendered = []
        for name, value in attrs:
            lower = name.lower()
            if lower.startswith("on"):
                continue
            if lower in {"autoplay", "data-crt-video", "data-crt-options"}:
                continue
            if value is None:
                rendered.append(lower)
                continue
            if lower in _FRAGMENT_REFERENCE_ATTRS:
                if lower == "srcset":
                    value = _rebase_srcset(value, self.source_page, self.destination_page)
                elif lower in {"src", "poster"} and tag in {"video", "source", "audio"} and (urlsplit(value).scheme or urlsplit(value).netloc or value.startswith("//")):
                    pass
                elif lower in {"src", "poster", "data"} and (urlsplit(value).scheme or urlsplit(value).netloc or value.startswith("//")):
                    continue
                elif lower == "href" and value.strip().lower().startswith("javascript:"):
                    value = "#"
                elif lower == "href" and value.startswith("#"):
                    value = "#" + self.namespace + value[1:]
                else:
                    value = _rebase_local_reference(value, self.source_page, self.destination_page)
            elif lower == "srcset":
                value = _rebase_srcset(value, self.source_page, self.destination_page)
            elif lower == "id":
                value = self.namespace + value
            elif lower in _FRAGMENT_ID_ATTRS:
                value = " ".join(
                    self.namespace + token if token.startswith("#") else self.namespace + token
                    for token in value.split()
                )
            elif lower == "href" and value.startswith("#"):
                value = "#" + self.namespace + value[1:]
            rendered.append(f'{lower}="{escape(value, quote=True)}"')
        return "".join(" " + item for item in rendered)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.lower()
        if not self.started:
            if lower == "article":
                self.started = True
                self.article_depth = 1
            return
        if self.finished:
            return
        if self.skip_depth:
            if lower in _FRAGMENT_DROP_TAGS:
                self.skip_depth += 1
            return
        if self.skip_header_depth:
            self.skip_header_depth += 1
            return
        if self.article_depth == 1 and lower == "header":
            self.skip_header_depth = 1
            return
        if lower in _FRAGMENT_DROP_TAGS:
            self.skip_depth = 1
            return
        if lower == "article":
            self.article_depth += 1
        self.parts.append("<" + lower + self._attrs(attrs, lower) + ">")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._in_fragment() and not self.skip_depth and not self.skip_header_depth:
            self.parts.append("<" + tag.lower() + self._attrs(attrs, tag.lower()) + "/>" )

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        if not self.started or self.finished:
            return
        if self.skip_depth:
            if lower in _FRAGMENT_DROP_TAGS:
                self.skip_depth -= 1
            return
        if self.skip_header_depth:
            if lower == "header":
                self.skip_header_depth = 0
            else:
                self.skip_header_depth = max(0, self.skip_header_depth - 1)
            return
        if lower == "article":
            self.article_depth -= 1
            if self.article_depth == 0:
                self.finished = True
                return
        self.parts.append(f"</{lower}>")

    def handle_data(self, data: str) -> None:
        if self._in_fragment() and not self.skip_depth and not self.skip_header_depth:
            self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if self._in_fragment() and not self.skip_depth and not self.skip_header_depth:
            self.parts.append("&" + name + ";")

    def handle_charref(self, name: str) -> None:
        if self._in_fragment() and not self.skip_depth and not self.skip_header_depth:
            self.parts.append("&#" + name + ";")

    def handle_comment(self, data: str) -> None:
        if self._in_fragment() and not self.skip_depth and not self.skip_header_depth:
            self.parts.append("<!--" + data + "-->")


def _rendered_post_body(source_page: Path, destination_page: Path, namespace: str) -> str:
    if not source_page.is_file():
        return ""
    try:
        parser = _RenderedPostFragment(source_page, destination_page, namespace)
        parser.feed(source_page.read_text(encoding="utf-8"))
        parser.close()
    except (OSError, UnicodeError):
        return ""
    return "".join(parser.parts).strip()


def _live_content_references(html: str) -> str:
    """Make local post-media references addressable by the live bridge."""
    content_root = CONTENT_ROOT.resolve()
    pattern = re.compile(r'(?P<attr>\b(?:src|poster|data)=["\'])(?P<value>[^"\']+)(?P<end>["\'])', re.IGNORECASE)

    def replace(match: re.Match[str]) -> str:
        value = match.group("value")
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc or value.startswith(("/", "#", "data:", "mailto:", "tel:", "//")):
            return match.group(0)
        candidate = (content_root / unquote(parsed.path)).resolve()
        if candidate != content_root and content_root not in candidate.parents:
            return match.group(0)
        relative = candidate.relative_to(content_root).as_posix()
        live_url = _archive_content_url(relative)
        return match.group("attr") + urlunsplit(("", "", live_url, parsed.query, parsed.fragment)) + match.group("end")

    return pattern.sub(replace, html)


@dataclass
class _TrailNode:
    tag: str | None
    attrs: list[tuple[str, str | None]] = field(default_factory=list)
    children: list["_TrailNode"] = field(default_factory=list)
    text: str = ""


class _TrailMarkupParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.root = _TrailNode(None)
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _TrailNode(tag.lower(), attrs=list(attrs))
        self.stack[-1].children.append(node)
        if tag.lower() not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.stack[-1].children.append(_TrailNode(tag.lower(), attrs=list(attrs)))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag.lower():
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(_TrailNode(None, text=data))

    def handle_entityref(self, name: str) -> None:
        self.stack[-1].children.append(_TrailNode(None, text="&" + name + ";"))

    def handle_charref(self, name: str) -> None:
        self.stack[-1].children.append(_TrailNode(None, text="&#" + name + ";"))

    def handle_comment(self, data: str) -> None:
        self.stack[-1].children.append(_TrailNode(None, text="<!--" + data + "-->"))


def _trail_node_text(node: _TrailNode) -> str:
    if node.tag is None:
        return node.text
    return "".join(_trail_node_text(child) for child in node.children)


def _trail_anchor(node: _TrailNode) -> tuple[str, str] | None:
    if node.tag != "p":
        return None
    for child in node.children:
        if child.tag != "a":
            continue
        attrs = {name.lower(): value or "" for name, value in child.attrs}
        classes = set(attrs.get("class", "").split())
        if "tumblr_blog" in classes:
            name = unescape(_trail_node_text(child)).strip()
            if name:
                return name, attrs.get("href", "")
    return None


def _serialize_trail_node(node: _TrailNode) -> str:
    if node.tag is None:
        return node.text
    attrs = "".join(
        f' {name}="{escape(value or "", quote=True)}"' if value is not None else f" {name}"
        for name, value in node.attrs
    )
    void = node.tag in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    if void:
        return f"<{node.tag}{attrs}>"
    return f"<{node.tag}{attrs}>" + "".join(_serialize_trail_node(child) for child in node.children) + f"</{node.tag}>"


def _trail_entries(nodes: list[_TrailNode]) -> list[tuple[str, str, list[_TrailNode]]]:
    entries: list[tuple[str, str, list[_TrailNode]]] = []
    while True:
        meaningful = [node for node in nodes if node.tag is not None or node.text.strip()]
        if len(meaningful) < 2:
            break
        identity = _trail_anchor(meaningful[0])
        wrapper = meaningful[1]
        if identity is None or wrapper.tag != "blockquote":
            break
        wrapper_children = [child for child in wrapper.children if child.tag is not None or child.text.strip()]
        nested_index = next(
            (child_index for child_index, child in enumerate(wrapper_children) if _trail_anchor(child)),
            None,
        )
        if nested_index is None:
            content = wrapper_children
        else:
            nested_wrapper_index = nested_index + 1
            if nested_wrapper_index < len(wrapper_children) and wrapper_children[nested_wrapper_index].tag == "blockquote":
                content = wrapper_children[:nested_index] + wrapper_children[nested_wrapper_index + 1:]
            else:
                content = wrapper_children[:nested_index] + wrapper_children[nested_index + 1:]
        entries.append((identity[0], identity[1], content))
        if nested_index is None:
            break
        nodes = wrapper_children[nested_index:]
    return entries


def _participant_href(name: str, source_href: str, destination_page: Path) -> str | None:
    try:
        local_root = canonical_archive_root(name)
    except ValueError:
        local_root = None
    local_json = local_root / "json" if local_root else Path()
    if local_root is not None and local_json.is_dir() and any(local_json.glob("*.json")):
        return os.path.relpath(local_root / "index.html", destination_page.parent).replace(os.sep, "/")
    parsed = urlsplit(source_href)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return source_href
    return None


def _participant_avatar(name: str, destination_page: Path) -> str | None:
    try:
        local_root = canonical_archive_root(name)
    except ValueError:
        return None
    if (local_root / "json").is_dir():
        local_avatar = _local_profile_avatar(name, destination_page)
        if local_avatar:
            return local_avatar
    assets = sorted((_participant_avatar_asset_dir(name)).glob("avatar.*"))
    if not assets:
        return None
    return os.path.relpath(assets[0], destination_page.parent).replace(os.sep, "/")


def _participant_avatar_asset_dir(name: str) -> Path:
    return CONTENT_ROOT / "participant-assets" / canonical_blog_name(name)


def _participant_avatar_status_path(name: str) -> Path:
    return _participant_avatar_asset_dir(name) / "status.json"


def _participant_avatar_status(name: str) -> dict[str, Any]:
    path = _participant_avatar_status_path(name)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_participant_avatar_status(name: str, status: str, reason: str, **details: Any) -> None:
    payload = {"status": status, "reason": reason, "updated_at": time.time(), **details}
    write_json_atomic(_participant_avatar_status_path(name), payload)


def _participant_avatar_exists(name: str) -> bool:
    canonical = canonical_blog_name(name)
    local_root = canonical_archive_root(canonical)
    return bool(
        (local_root / "json").is_dir() and _local_profile_avatar(canonical, local_root / "index.html")
    ) or bool(list(_participant_avatar_asset_dir(canonical).glob("avatar.*")))


def _save_participant_avatar(name: str, data: bytes, content_type: str, source_url: str = "") -> None:
    media_type = content_type.split(";", 1)[0].strip().lower()
    extension = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(media_type) or mimetypes.guess_extension(media_type)
    if extension not in {".jpg", ".jpeg", ".png", ".gif", ".webp"} and source_url:
        extension = Path(urlsplit(source_url).path).suffix.lower()
    if extension not in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        raise ValueError("participant avatar response was not a supported image")
    target = _participant_avatar_asset_dir(name) / ("avatar" + extension)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, target)


def capture_participant_avatar(name: str, avatar_url: str | None = None) -> bool:
    """Cache one low-resolution participant avatar without creating an archive."""
    try:
        canonical = canonical_blog_name(name)
        if _participant_avatar_exists(canonical):
            return False
        # Tumblr's old blog-host shortcut commonly returns 404. Public feed
        # records already carry the source-faithful 64px URL; prefer it when
        # available and retain the shortcut only for direct legacy callers.
        source_url = str(avatar_url or "").strip()
        if source_url:
            parsed = urlsplit(source_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("participant avatar URL was not an absolute HTTP URL")
        else:
            source_url = f"https://{canonical}.tumblr.com/avatar/64"
        if ACTIVE_REQUEST_PRESSURE is not None and not ACTIVE_REQUEST_PRESSURE.reserve("avatar"):
            _write_participant_avatar_status(canonical, "not_checked_request_pressure", "Avatar skipped by shared request-pressure envelope", source_url=source_url)
            return False
        request = Request(
            source_url,
            headers={"User-Agent": USER_AGENT},
        )
        with urlopen(request, timeout=3) as response:
            content_type = str(response.headers.get("Content-Type", ""))
            data = response.read(256 * 1024 + 1)
        if len(data) > 256 * 1024:
            raise ValueError("participant avatar exceeded 256 KiB")
        _save_participant_avatar(canonical, data, content_type, source_url)
        return True
    except HTTPError as exc:
        if exc.code in {401, 403}:
            _write_participant_avatar_status(
                canonical,
                "restricted",
                "Tumblr denied the profile image to this unauthenticated viewer",
                http_status=exc.code,
                source_url=source_url,
            )
        else:
            _write_participant_avatar_status(canonical, "unavailable", "Tumblr did not provide a profile image", http_status=exc.code, source_url=source_url)
        return False
    except (OSError, URLError, ValueError) as exc:
        _write_participant_avatar_status(canonical, "unavailable", "Profile image capture failed before a local image was saved", error_type=type(exc).__name__, source_url=source_url)
        return False


def _participant_avatar_sources(record: dict[str, Any]) -> dict[str, str]:
    """Return public avatar URLs embedded in one preserved Tumblr record."""
    raw = record.get("_puppetbackup_source_record")
    source = raw if isinstance(raw, dict) else record
    result: dict[str, str] = {}
    for prefix in ("reblogged_from", "reblogged_root"):
        hyphen_prefix = prefix.replace("_", "-")
        name = source.get(f"{prefix}_name") or source.get(f"{hyphen_prefix}-name") or source.get(f"{prefix}-name")
        url = source.get(f"{prefix}_avatar_url_64") or source.get(f"{hyphen_prefix}-avatar-url-64") or source.get(f"{prefix}-avatar-url-64")
        if not name or not url:
            continue
        try:
            canonical = canonical_blog_name(str(name))
            parsed = urlsplit(str(url).strip())
        except (ValueError, TypeError):
            continue
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            result[canonical] = str(url).strip()
    return result


def capture_participant_avatars(ids: list[int]) -> int:
    """Collect at most 20 uncached trail avatars from one render batch."""
    names: list[str] = []
    avatar_sources: dict[str, str] = {}
    seen: set[str] = set()
    for pid in ids:
        source_path = JSON_DIR / f"{pid}.json"
        try:
            record = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            record = {}
        if isinstance(record, dict):
            avatar_sources.update(_participant_avatar_sources(record))
        rendered = OUT / "posts" / f"{pid}.html"
        if not rendered.is_file():
            continue
        try:
            parser = _TrailMarkupParser()
            parser.feed(rendered.read_text(encoding="utf-8"))
            parser.close()
            entries = _trail_entries(parser.root.children)
        except (OSError, UnicodeError, AssertionError, ValueError):
            continue
        for name, _source_href, _content in entries:
            try:
                canonical = canonical_blog_name(name)
            except ValueError:
                continue
            if canonical not in seen:
                seen.add(canonical)
                names.append(canonical)
            if len(names) >= 20:
                break
        if len(names) >= 20:
            break
    return sum(capture_participant_avatar(name, avatar_sources.get(name)) for name in names)


def _avatar_state(name: str, avatar: str | None, *, source_known: bool = False) -> tuple[str, str]:
    if avatar:
        return "available", "Profile image available"
    try:
        canonical = canonical_blog_name(name)
    except ValueError:
        return "unknown", "Profile image status is unknown for this participant"
    status = _participant_avatar_status(canonical)
    if status.get("status") == "restricted":
        return "restricted", str(status.get("reason") or "Profile image is hidden from unauthenticated viewers")
    if status.get("status") == "unavailable":
        return "unavailable", str(status.get("reason") or "Profile image capture was attempted but is unavailable")
    if source_known:
        return "missing", "Profile image source was recorded, but no local copy is available"
    if _profile_path(canonical).is_file() or (canonical_archive_root(canonical) / "json").is_dir():
        return "null", "No profile image was recorded for this archived blog"
    if _participant_avatar_asset_dir(canonical).exists():
        return "unavailable", "Profile image capture was attempted but is unavailable"
    return "unknown", "Profile image status is unknown"


def _avatar_placeholder(class_name: str, state: str, label: str) -> str:
    return f'<span class="{class_name} placeholder avatar-state-{escape(state)}" role="img" aria-label="{escape(label)}" title="{escape(label)}"></span>'


def _avatar_source_known(name: str, avatar_sources: dict[str, str] | None) -> bool:
    try:
        canonical = canonical_blog_name(name)
    except ValueError:
        return False
    return bool((avatar_sources or {}).get(canonical))


def _render_flat_reblog_trail(
    body: str,
    destination_page: Path,
    current_blog: str = "",
    avatar_sources: dict[str, str] | None = None,
) -> str:
    parser = _TrailMarkupParser()
    try:
        parser.feed(body)
        parser.close()
    except (AssertionError, ValueError):
        return ""
    entries = _trail_entries(parser.root.children)
    if not entries:
        return ""
    rendered = []
    # Tumblr's rendered trail is nested newest-first.  Keep archive cards
    # newest-first, but read the utterances from the original post outward.
    for name, source_href, content in reversed(entries):
        avatar = _participant_avatar(name, destination_page)
        state, label = _avatar_state(name, avatar, source_known=_avatar_source_known(name, avatar_sources))
        avatar_html = f'<img class="trail-avatar" src="{escape(avatar)}" alt="" loading="lazy">' if avatar else _avatar_placeholder("trail-avatar", state, label)
        href = _participant_href(name, source_href, destination_page)
        name_html = f'<a href="{escape(href)}"><bdi dir="auto">{escape(name)}</bdi></a>' if href else f'<bdi dir="auto">{escape(name)}</bdi>'
        content_html = "".join(_serialize_trail_node(child) for child in content).strip()
        rendered.append(
            '<section class="reblog-trail-entry">'
            f'<header class="trail-identity">{avatar_html}<span>{name_html}</span></header>'
            f'<div class="trail-content" dir="auto">{content_html}</div>'
            '</section>'
        )
    # Tumblr places the reblogger's added commentary after the outermost
    # blockquote, as a sibling of the trail.  _trail_entries intentionally
    # walks only the nested trail, so preserve that sibling contribution as
    # its own final entry instead of silently dropping it.
    suffix = [
        node for node in parser.root.children[2:]
        if (node.tag is not None or node.text.strip()) and node.tag != "footer"
    ]
    suffix_html = "".join(_serialize_trail_node(child) for child in suffix).strip()
    if suffix_html and current_blog:
        name = current_blog
        avatar = _participant_avatar(name, destination_page)
        state, label = _avatar_state(name, avatar, source_known=_avatar_source_known(name, avatar_sources))
        avatar_html = f'<img class="trail-avatar" src="{escape(avatar)}" alt="" loading="lazy">' if avatar else _avatar_placeholder("trail-avatar", state, label)
        href = _participant_href(name, "", destination_page)
        name_html = f'<a href="{escape(href)}"><bdi dir="auto">{escape(name)}</bdi></a>' if href else f'<bdi dir="auto">{escape(name)}</bdi>'
        rendered.append(
            '<section class="reblog-trail-entry reblog-trail-current">'
            f'<header class="trail-identity">{avatar_html}<span>{name_html}</span></header>'
            f'<div class="trail-content" dir="auto">{suffix_html}</div>'
            '</section>'
        )
    return '<div class="reblog-trail" aria-label="Reblog trail">' + "".join(rendered) + "</div>"


def _profile_path(blog: str) -> Path:
    return canonical_archive_root(blog) / "profile" / "profile.json"


def _write_profile_snapshot(blog: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    path = _profile_path(blog)
    existing: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = {}
    current = max(records, key=lambda record: int(record.get("timestamp") or 0), default={})
    blog_infos = []
    for record in records:
        value = dict(record.get("blog")) if isinstance(record.get("blog"), dict) else {}
        source = record.get("_puppetbackup_source_record")
        source_blog = source.get("tumblelog") if isinstance(source, dict) and isinstance(source.get("tumblelog"), dict) else {}
        value.update(source_blog)
        if value:
            blog_infos.append(value)
    current_info = next((value for value in blog_infos if value.get("title") or value.get("description")), {})
    title_info = next((value for value in blog_infos if value.get("title")), current_info)
    description_info = next((value for value in blog_infos if value.get("description")), current_info)
    description = description_info.get("description") or existing.get("description") or None
    profile_source = "canonical post JSON blog metadata" if description_info.get("description") else existing.get("source") or "profile snapshot metadata"
    profile = {
        "schema_version": 1,
        "blog": blog,
        "title": str(title_info.get("title") or existing.get("title") or current.get("tumblelog") or blog),
        "description": description,
        "url": str(title_info.get("url") or current.get("post_url") or f"https://{blog}.tumblr.com/"),
        "observed_at": time.time(),
        "source": profile_source,
        "avatar_source": "upstream theme/avatar asset when available",
    }
    write_json_atomic(path, profile)
    theme_avatars = sorted((canonical_archive_root(blog) / "theme").glob("avatar.*"))
    if theme_avatars:
        profile_dir = path.parent
        avatar_target = profile_dir / ("avatar" + theme_avatars[0].suffix)
        if not avatar_target.exists() or theme_avatars[0].stat().st_mtime_ns > avatar_target.stat().st_mtime_ns:
            shutil.copy2(theme_avatars[0], avatar_target)
    return profile


def _local_profile_avatar(blog: str, destination_page: Path) -> str:
    profile_dir = _profile_path(blog).parent
    avatars = sorted(profile_dir.glob("avatar.*"))
    if not avatars:
        avatars = sorted((canonical_archive_root(blog) / "theme").glob("avatar.*"))
    if not avatars:
        return ""
    return os.path.relpath(avatars[0], destination_page.parent).replace(os.sep, "/")


def _record_title(record: dict[str, Any]) -> str:
    return str(record.get("title") or record.get("post_url") or f"Post {record.get('id_string') or record.get('id')}")


def _render_post_card(
    record: dict[str, Any],
    destination_page: Path,
    *,
    compact: bool = False,
    data_attributes: dict[str, Any] | None = None,
    live: bool = False,
    hidden: bool = False,
) -> str:
    blog = str(record.get("_canonical_blog") or record.get("blog_name") or record.get("tumblelog") or "unknown")
    post_id = str(record.get("id_string") or record.get("id") or "")
    source_page = canonical_archive_root(blog) / "posts" / f"{post_id}.html"
    namespace = "post-" + re.sub(r"[^a-zA-Z0-9_-]", "-", blog + "-" + post_id) + "-"
    body = _rendered_post_body(source_page, destination_page, namespace)
    if not body:
        # Some development archives contain the canonical feed JSON but no
        # upstream-rendered posts/<id>.html fragments.  Render the preserved
        # canonical body instead of reducing the post to a link or a pending
        # placeholder.  This is presentation-only; the JSON remains the
        # source of truth and the same sanitizer/rebaser is reused.
        raw_body = record.get("body") or record.get("caption") or record.get("description")
        if raw_body:
            try:
                parser = _RenderedPostFragment(source_page, destination_page, namespace)
                parser.feed("<article>" + str(raw_body) + "</article>")
                parser.close()
                body = "".join(parser.parts).strip()
            except (OSError, UnicodeError):
                body = ""
    if live and body:
        body = _live_content_references(body)
    avatar_sources = _participant_avatar_sources(record)
    flat_trail = _render_flat_reblog_trail(body, destination_page, blog, avatar_sources)
    if flat_trail:
        body = flat_trail
    profile_path = _profile_path(blog)
    profile = {}
    if profile_path.is_file():
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            profile = {}
    if not body:
        body = '<p class="render-pending">source preserved; rendering pending. Rendered post HTML is unavailable.</p>'
    avatar = _local_profile_avatar(blog, destination_page)
    avatar_state, avatar_label = _avatar_state(blog, avatar, source_known=bool(avatar_sources.get(blog)))
    if live and avatar:
        avatar = _archive_content_url(avatar)
    avatar_html = f'<img class="post-avatar" src="{escape(avatar)}" alt="" loading="lazy">' if avatar else _avatar_placeholder("post-avatar", avatar_state, avatar_label)
    blog_href = f"/app/blog.html?blog={quote(blog)}" if live else os.path.relpath(canonical_archive_root(blog) / "index.html", destination_page.parent).replace(os.sep, "/")
    post_href = f"/app/post.html?blog={quote(blog)}&post={quote(post_id)}" if live else os.path.relpath(source_page, destination_page.parent).replace(os.sep, "/")
    date_value = str(record.get("date") or "")
    timestamp = int(record.get("timestamp") or 0)
    time_label = date_value or (time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(timestamp)) if timestamp else "undated")
    attribution = []
    if record.get("reblogged_from_name"):
        attribution.append("reblogged from " + str(record["reblogged_from_name"]))
    if record.get("reblogged_root_name") and record.get("reblogged_root_name") != record.get("reblogged_from_name"):
        attribution.append("originally by " + str(record["reblogged_root_name"]))
    attribution_html = f'<p class="post-attribution">{escape("; ".join(attribution))}</p>' if attribution else ""
    tags = []
    for raw_tag in record.get("tags") or []:
        display, _canonical, _search, page_id = _tag_identity(raw_tag)
        tag_path = APP_ROOT / "tags" / f"{page_id}.html"
        tag_href = f"/app/tags.html?tag={quote(page_id)}" if live else os.path.relpath(tag_path, destination_page.parent).replace(os.sep, "/")
        tags.append(f'<a href="{escape(tag_href)}"><bdi dir="auto">#{escape(display)}</bdi></a>')
    tags_html = '<div class="post-tags" aria-label="Tags">' + " ".join(tags) + "</div>" if tags else ""
    source_url = str(record.get("post_url") or record.get("short_url") or "")
    source_action = f'<a href="{escape(source_url)}" rel="noreferrer noopener">Source</a>' if source_url else ""
    notes = record.get("note_count")
    notes_html = f'<span class="post-notes">{int(notes)} notes</span>' if notes is not None else ""
    classes = "post-card" + (" post-card-compact" if compact else "")
    data_html = "".join(
        f' data-feed-{escape(str(key))}="{escape(str(value))}"'
        for key, value in (data_attributes or {}).items()
        if value is not None
    )
    return (
        f'<article class="{classes}" id="{escape(namespace + "card")}"{data_html}{" hidden" if hidden else ""}>'
        f'<header class="post-card-header">{avatar_html}<div class="post-card-identity">'
        f'<a class="post-blog" href="{escape(blog_href)}"><bdi dir="auto">{escape(blog)}</bdi></a>'
        f'<time datetime="{escape(str(timestamp))}">{escape(time_label)}</time></div></header>'
        + attribution_html
        + f'<div class="post-rendered-content">{body}</div>'
        + f'<footer class="post-card-footer">{tags_html}<div class="post-actions">{notes_html}<a href="{escape(post_href)}">Open archived post</a>{source_action}</div></footer>'
        + "</article>"
    )




def build_global_catalog() -> dict[str, Any]:
    blogs = []
    for blog_dir in _canonical_blog_directories():
        records = _json_records(blog_dir / "json")
        timestamps = [int(r.get("timestamp") or 0) for r in records]
        inventory = {}
        inventory_path = blog_dir / "inventory.json"
        if inventory_path.is_file():
            try:
                inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                inventory = {}
        blogs.append({
            "blog": blog_dir.name,
            "archive": f"Archive/{ACTIVE_ARCHIVE_NAME}/Content/{blog_dir.name}/index.html",
            "local_post_count": len(records),
            "complete_post_count": sum(1 for r in records if (blog_dir / "posts" / f"{r.get('id_string') or r.get('id')}.html").is_file()),
            "partial": True,
            "newest_local_timestamp": max(timestamps, default=None),
            "oldest_local_timestamp": min(timestamps, default=None),
            "last_observation": inventory.get("last_observation"),
            "last_checkpoint": inventory.get("last_checkpoint"),
        })
    blogs.sort(key=lambda item: (-int(item["local_post_count"]), str(item["blog"]).casefold()))
    return {"schema_version": 1, "generated_at": time.time(), "blogs": blogs}


def _tag_identity(value: Any) -> tuple[str, str, str, str]:
    display = str(value)
    canonical = unicodedata.normalize("NFC", display)
    search = canonical.casefold()
    page_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return display, canonical, search, page_id


def build_tag_index() -> dict[str, Any]:
    tags: dict[str, dict[str, Any]] = {}
    page_ids: dict[str, str] = {}
    for record in _canonical_local_records():
        blog = str(record["_canonical_blog"])
        post_id = str(record.get("id_string") or record.get("id"))
        seen: set[str] = set()
        for raw_tag in record.get("tags") or []:
            display, canonical, search, page_id = _tag_identity(raw_tag)
            previous = page_ids.get(page_id)
            if previous is not None and previous != canonical:
                raise RuntimeError(f"tag page-id collision between {previous!r} and {canonical!r}")
            page_ids[page_id] = canonical
            item = tags.setdefault(canonical, {
                "display": display,
                "canonical_tag_key": canonical,
                "search_key": search,
                "page_id": page_id,
                "variants": [],
                "posts": [],
            })
            if display not in item["variants"]:
                item["variants"].append(display)
            if canonical in seen:
                continue
            seen.add(canonical)
            rendered = (canonical_archive_root(blog) / "posts" / f"{post_id}.html").is_file()
            post_key = (blog, post_id)
            if not any((entry["blog"], entry["post_id"]) == post_key for entry in item["posts"]):
                item["posts"].append({
                    "blog": blog,
                    "post_id": post_id,
                    "timestamp": int(record.get("timestamp") or 0),
                    "title": str(record.get("title") or record.get("post_url") or f"Post {post_id}"),
                    "rendered": rendered,
                })
    for item in tags.values():
        item["posts"].sort(key=lambda post: (-post["timestamp"], post["blog"], post["post_id"]))
    return {
        "schema_version": 1,
        "generated_at": time.time(),
        "tags": sorted(tags.values(), key=lambda item: (item["search_key"], item["canonical_tag_key"])),
    }


def _tag_post_href(root: Path, post: dict[str, Any]) -> str:
    if root == CONTENT_ROOT / "tags":
        return f"../{post['blog']}/posts/{post['post_id']}.html"
    return f"../posts/{post['post_id']}.html"




def _augment_blog_tags_link(blog: str) -> None:
    index = canonical_archive_root(blog) / "index.html"
    if not index.is_file():
        return
    text = index.read_text(encoding="utf-8")
    marker = "<!-- puppetbackup-tags-link -->"
    link = marker + '<p><a href="tags/index.html">Tags for this blog</a></p>'
    if marker in text:
        text = text[:text.index(marker)] + link + text[text.index(marker) + len(marker):]
    else:
        text = text.replace("</header>", "</header>" + link, 1)
    index.write_text(text, encoding="utf-8")


def _render_blog_page(blog: str, records: list[dict[str, Any]]) -> str:
    output = canonical_archive_root(blog) / "index.html"
    profile = _write_profile_snapshot(blog, records)
    avatar = _local_profile_avatar(blog, output)
    avatar_state, avatar_label = _avatar_state(blog, avatar)
    avatar_html = f'<img class="profile-avatar" src="{escape(avatar)}" alt="">' if avatar else _avatar_placeholder("profile-avatar", avatar_state, avatar_label)
    title = str(profile.get("title") or blog)
    description = profile.get("description")
    bio = f'<p class="profile-bio" dir="auto">{escape(str(description))}</p>' if description else ""
    cards = []
    for record in sorted(records, key=lambda item: (-int(item.get("timestamp") or 0), str(item.get("id_string") or item.get("id")))):
        cards.append(_render_post_card(record, output))
    content = (
        '<section class="profile-header">'
        f'{avatar_html}<div><h1><bdi dir="auto">{escape(title)}</bdi></h1>'
        f'<p class="profile-username">{escape(blog)}</p>{bio}'
        f'<p class="archive-meta">{len(records)} locally preserved posts</p></div></section>'
        '<nav class="context-nav" aria-label="Blog sections">'
        '<a href="index.html" aria-current="page">Posts</a> '
        '<a href="tags/index.html">Tags</a> '
        f'<a href="{escape(str(profile.get("url") or ""))}" rel="noreferrer noopener">Original blog</a>'
        '</nav><div class="post-feed">' + "".join(cards) + '</div>'
    )
    return _archive_shell(
        title,
        content,
        _static_navigation(output, "List"),
        active=blog,
        stylesheet="../assets/archive.css",
        script="../assets/archive.js",
        extra='<link rel="stylesheet" href="custom.css">',
    )




def _feed_target_index(catalog: dict[str, Any], target: str, weights: dict[str, Any], all_records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build derived, directional affinity metadata for one POV target."""
    # Use the shared context loader so generated named archives and recovered
    # legacy observation layouts feed the same affinity projection.
    document = load_context_document(target)
    context = {target: {"neighborhood": target, "distance": 0}}
    for item in document.get("blogs", []):
        if isinstance(item, dict) and item.get("blog"):
            context[str(item["blog"])] = {
                "neighborhood": target,
                "distance": item.get("distance"),
            }
    evidence_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in document.get("interactions", []):
        if isinstance(item, dict):
            evidence_by_key[_evidence_key(item)] = item
    for record in all_records if all_records is not None else _canonical_local_records():
        if str(record.get("_canonical_blog")) != target:
            continue
        for item in interaction_evidence(target, record):
            evidence_by_key[_evidence_key(item)] = item
    timestamps = {}
    for record in _json_records(CONTENT_ROOT / target / "json"):
        post_id = str(record.get("id_string") or record.get("id") or "")
        timestamps[post_id] = int(record.get("timestamp") or 0)
    sources: dict[str, dict[str, Any]] = {}
    for item in evidence_by_key.values():
        if item.get("from_blog") != target or not item.get("to_blog"):
            continue
        kind = str(item.get("kind") or "")
        if kind not in {"direct_reblog", "direct_like", "structured_ask"}:
            continue
        source = str(item["to_blog"])
        entry = sources.setdefault(source, {
            "reblogs": set(), "likes": set(), "asks": set(), "days": set(),
        })
        post_key = (str(item.get("source_blog") or target), str(item.get("source_post_id") or ""), str(item.get("reference_url") or ""))
        entry[{"direct_reblog": "reblogs", "direct_like": "likes", "structured_ask": "asks"}[kind]].add(post_key)
        timestamp = timestamps.get(str(item.get("source_post_id") or ""))
        if timestamp:
            entry["days"].add(time.strftime("%Y-%m-%d", time.gmtime(timestamp)))
    reblog_weight = float(weights.get("target_reblogs_from", 0))
    like_weight = float(weights.get("target_likes_other", 0))
    ask_weight = float(weights.get("ask_answer", 0))
    output_sources = {}
    for source, entry in sources.items():
        counts = {key: len(entry[key]) for key in ("reblogs", "likes", "asks")}
        score = (
            _saturating_signal(counts["reblogs"]) * reblog_weight
            + _saturating_signal(counts["likes"]) * like_weight
        )
        if counts["asks"]:
            score += _saturating_signal(counts["asks"]) * ask_weight
        evidence_scores = {
            "reblogs": _saturating_signal(counts["reblogs"]) * reblog_weight,
            "likes": _saturating_signal(counts["likes"]) * like_weight,
            "asks": _saturating_signal(counts["asks"]) * ask_weight,
        }
        output_sources[source] = {
            "reblogs": counts["reblogs"], "likes": counts["likes"], "asks": counts["asks"],
            "days": len(entry["days"]), "score": score,
            "evidence_scores": evidence_scores,
            "neighborhood": context.get(source, {}).get("neighborhood", ""),
            "distance": context.get(source, {}).get("distance"),
        }
    positive = sorted((item["score"] for item in output_sources.values() if item["score"] > 0), reverse=True)
    def band(index_fraction: float) -> float:
        if not positive:
            return 0.0
        return positive[min(len(positive) - 1, int((len(positive) - 1) * index_fraction))]
    bands = {"closest": band(0.25), "likely": band(0.50), "broad": band(0.75)}
    for item in output_sources.values():
        item["affinity_bands"] = [
            name for name in ("closest", "likely", "broad")
            if item["score"] > 0 and item["score"] >= bands[name]
        ]
    return {
        "sources": output_sources,
        "eligible_blogs": sorted(output_sources),
        "bands": {**bands, "any": 0.0},
        "has_like_evidence": any(item["likes"] for item in output_sources.values()),
    }


def build_feed_index(catalog: dict[str, Any], all_records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    policy = load_context_policy()
    weights = policy.get("relationship_weights", {})
    targets = {}
    for item in catalog.get("blogs", []):
        blog = str(item.get("blog") or "")
        if blog:
            targets[blog] = _feed_target_index(catalog, blog, weights, all_records)
    return {
        "schema_version": 1,
        "weights": {"reblogs": float(weights.get("target_reblogs_from", 0)), "likes": float(weights.get("target_likes_other", 0)), "asks": float(weights.get("ask_answer", 0))},
        "targets": targets,
    }


def _feed_index_generation() -> str:
    """Return a cheap content generation for cursor invalidation."""
    hasher = hashlib.sha256()
    for root in (CONTENT_ROOT, NETWORK_ROOT):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.json")):
            try:
                stat = path.stat()
            except OSError:
                continue
            hasher.update(str(path.relative_to(root)).encode("utf-8"))
            hasher.update(str(stat.st_size).encode("ascii"))
            hasher.update(str(stat.st_mtime_ns).encode("ascii"))
    return hasher.hexdigest()[:24]


def _feed_metadata() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Build one cached metadata index; rich fields are discarded until hydration."""
    generation = _feed_index_generation()
    cache_key = (str(CONTENT_ROOT), generation)
    cached = _FEED_METADATA_CACHE.get(cache_key)
    if cached is not None:
        return cached
    records: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}
    for record in _canonical_local_records():
        blog = str(record.get("_canonical_blog") or "")
        post_id = str(record.get("id_string") or record.get("id") or "")
        if not blog or not post_id:
            continue
        metadata = dict(record)
        metadata.pop("body", None)
        metadata.pop("trail", None)
        metadata.pop("content", None)
        metadata["_canonical_blog"] = blog
        metadata["_feed_path"] = str(canonical_archive_root(blog) / "json" / f"{post_id}.json")
        metadata["_feed_key"] = f"{blog}:{post_id}"
        records.append(metadata)
        by_key[metadata["_feed_key"]] = metadata
    _FEED_METADATA_CACHE.clear()
    _FEED_METADATA_CACHE[cache_key] = (records, by_key)
    return records, by_key


def _feed_hydrate(metadata: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(metadata.get("_feed_path") or ""))
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        record = {}
    if not isinstance(record, dict):
        record = {}
    record["_canonical_blog"] = metadata.get("_canonical_blog", "")
    return record


def _feed_query(params: dict[str, list[str]], selected_pov: str = "") -> FeedQuery:
    raw_pov = selected_pov or (params.get("pov") or [""])[0]
    raw_mode = (params.get("mode") or [""])[0].casefold()
    pov = canonical_blog_name(raw_pov) if raw_pov and raw_pov != "__all__" else raw_pov
    mode = "all" if raw_mode == "all" or pov == "__all__" else "affinity"
    chronology = "oldest" if (params.get("sort") or [""])[0].casefold() == "oldest" else "newest"
    try:
        requested_size = int((params.get("window") or [str(FEED_DEFAULT_WINDOW)])[0])
    except ValueError:
        requested_size = FEED_DEFAULT_WINDOW
    size = max(1, min(requested_size, 100))
    evidence = tuple(sorted(set(params.get("evidence") or ["direct_reblog", "direct_like"])))
    return FeedQuery(
        mode=mode,
        pov=pov,
        chronology=chronology,
        affinity=(params.get("affinity") or ["any"])[0],
        evidence=evidence,
        neighborhood=(params.get("neighborhood") or ["__all__"])[0].casefold(),
        breadth=(params.get("breadth") or ["all"])[0],
        search=(params.get("search") or [""])[0].casefold().strip(),
        include_target=(params.get("include_target") or [""])[0].casefold() in {"1", "true", "yes"},
        window_size=size,
    )


def _feed_query_params(query: FeedQuery, cursor: FeedCursor | None = None) -> str:
    values: list[tuple[str, str]] = [
        ("pov", query.pov or "__all__"), ("mode", query.mode), ("sort", query.chronology),
        ("affinity", query.affinity), ("neighborhood", query.neighborhood),
        ("breadth", query.breadth), ("search", query.search),
        ("include_target", "1" if query.include_target else "0"),
    ]
    values.extend(("evidence", value) for value in query.evidence)
    if cursor is not None:
        values.append(("cursor", cursor.encode()))
    return urlencode(values)


def _feed_matches(metadata: dict[str, Any], query: FeedQuery, target_data: dict[str, Any]) -> bool:
    blog = str(metadata.get("_canonical_blog") or "")
    if query.mode == "affinity":
        if blog == query.pov:
            if not query.include_target:
                return False
        else:
            source = (target_data.get("sources") or {}).get(blog)
            if not source:
                return False
            if query.affinity != "any" and query.affinity not in (source.get("affinity_bands") or []):
                return False
            allowed = set(query.evidence)
            if not any(int(source.get(key, 0) or 0) > 0 for key in ("reblogs" if "direct_reblog" in allowed else "", "likes" if "direct_like" in allowed else "", "asks" if "structured_ask" in allowed else "") if key):
                return False
    if query.search:
        haystack = " ".join(str(metadata.get(key) or "") for key in ("title", "tags", "reblogged_from_name", "_canonical_blog", "post_url")).casefold()
        if query.search not in haystack:
            return False
    if query.neighborhood != "__all__":
        source = (target_data.get("sources") or {}).get(blog, {})
        if str(source.get("neighborhood") or "").casefold() != query.neighborhood:
            return False
    if query.breadth != "all":
        try:
            distance = int((target_data.get("sources") or {}).get(blog, {}).get("distance"))
        except (TypeError, ValueError):
            return False
        if query.breadth == "3+" and distance < 3:
            return False
        if query.breadth != "3+" and str(distance) != query.breadth:
            return False
    return True


def build_feed_window(query: FeedQuery, cursor: FeedCursor | None = None) -> FeedWindow:
    """Select, order, and hydrate only one bounded Feed window."""
    current_generation = _feed_index_generation()
    catalog = build_global_catalog()
    if cursor is not None and cursor.index_generation in _FEED_SNAPSHOT_CACHE:
        metadata, feed_index = _FEED_SNAPSHOT_CACHE[cursor.index_generation]
        generation = cursor.index_generation
    else:
        metadata, _by_key = _feed_metadata()
        feed_index = build_feed_index(catalog, metadata)
        _FEED_SNAPSHOT_CACHE[current_generation] = (metadata, feed_index)
        generation = current_generation
    if not query.pov and query.mode == "affinity":
        query = FeedQuery(**{**query.__dict__, "pov": str((catalog.get("blogs") or [{}])[0].get("blog") or "")})
    target_data = feed_index.get("targets", {}).get(query.pov, {}) if query.mode == "affinity" else {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    eligible_blogs = None if query.mode == "all" else set(str(value) for value in (target_data.get("eligible_blogs") or []))
    if query.mode == "affinity" and query.include_target:
        eligible_blogs = set(eligible_blogs or ()) | {query.pov}
    for item in metadata:
        blog = str(item.get("_canonical_blog") or "")
        if eligible_blogs is not None and blog not in eligible_blogs:
            continue
        if _feed_matches(item, query, target_data):
            grouped.setdefault(blog, []).append(item)
    def ordering_key(item: dict[str, Any]) -> tuple[Any, ...]:
        timestamp = int(item.get("timestamp") or 0)
        post_id = str(item.get("id_string") or item.get("id") or "")
        return ((-timestamp, post_id) if query.chronology != "oldest" else (timestamp, post_id))
    for values in grouped.values():
        values.sort(key=ordering_key)
    import heapq
    heap: list[tuple[tuple[Any, ...], str, int, dict[str, Any]]] = []
    for blog, values in grouped.items():
        if values:
            first = values[0]
            heapq.heappush(heap, (ordering_key(first), blog, 0, first))
    if cursor is not None and (cursor.schema_version != FEED_CURSOR_SCHEMA_VERSION or cursor.query_fingerprint != query.fingerprint() or cursor.index_generation not in _FEED_SNAPSHOT_CACHE):
        cursor = None
    after_key = tuple(cursor.final_ordering_key) if cursor and cursor.final_ordering_key else None
    window_meta: list[dict[str, Any]] = []
    while heap and len(window_meta) < query.window_size:
        key, blog, index, item = heapq.heappop(heap)
        if after_key is None or key > after_key:
            window_meta.append(item)
        next_index = index + 1
        values = grouped[blog]
        if next_index < len(values):
            next_item = values[next_index]
            heapq.heappush(heap, (ordering_key(next_item), blog, next_index, next_item))
    records = tuple(_feed_hydrate(item) for item in window_meta)
    next_cursor = None
    if heap and window_meta:
        last = window_meta[-1] if window_meta else {}
        next_cursor = FeedCursor(
            FEED_CURSOR_SCHEMA_VERSION, query.fingerprint(), generation,
            (cursor.offset if cursor else 0) + len(window_meta),
            ordering_key(last),
        )
    return FeedWindow(records, next_cursor, None, len(metadata), len(metadata), len(records))
def render_global_pages() -> None:
    """Export the source-owned presentation into the disposable Archive tree."""
    return render_static_archive()


def regenerate_global_presentation(force: bool = False) -> None:
    global ACTIVE_PRESENTATION_GENERATION
    state_path = _presentation_state_path()
    required = (
        APP_ROOT / "graph.html",
        APP_ROOT / "list.html",
        APP_ROOT / "feed.html",
        APP_ROOT / "tags.html",
        APP_ROOT / "neighborhoods.html",
        APP_ROOT / "crawler.html",
        APP_ROOT / "assets" / "archive.css",
        APP_ROOT / "assets" / "archive.js",
    )
    identity = _presentation_identity()
    dirty = force or any(not path.is_file() for path in required)
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            dirty = dirty or bool(state.get("dirty"))
            dirty = dirty or state.get("generator_version") != PRESENTATION_SCHEMA_VERSION
            dirty = dirty or state.get("presentation_identity") != identity
        except (OSError, json.JSONDecodeError):
            dirty = True
    else:
        dirty = True
    if not dirty:
        return
    render_global_pages()
    write_json_atomic(
        state_path,
        {
            "generator_version": PRESENTATION_SCHEMA_VERSION,
            "dirty": False,
            "generated_at": time.time(),
            "runtime_identity": runtime_identity(),
            "presentation_identity": _presentation_identity(),
        },
    )
    ACTIVE_PRESENTATION_GENERATION += 1


def load_context_document(primary: str) -> dict[str, Any]:
    path = NEIGHBORHOODS_ROOT / primary / "neighborhood.json"
    if not path.is_file():
        path = _context_root(primary) / "context.json"
    if not path.is_file():
        return {"schema_version": 1, "primary_blog": primary, "blogs": [], "interactions": [], "scout_interactions": [], "adjacency_observations": [], "pending": []}
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "primary_blog": primary, "blogs": [], "interactions": [], "scout_interactions": [], "adjacency_observations": [], "pending": []}
    if not isinstance(data, dict):
        return {"schema_version": 1, "primary_blog": primary, "blogs": [], "interactions": [], "scout_interactions": [], "adjacency_observations": [], "pending": []}
    data.setdefault("blogs", [])
    data.setdefault("interactions", [])
    data.setdefault("scout_interactions", [])
    data.setdefault("adjacency_observations", [])
    data.setdefault("pending", [])
    return data


def save_context_document(primary: str, document: dict[str, Any]) -> None:
    root = NEIGHBORHOODS_ROOT / canonical_blog_name(primary)
    root.mkdir(parents=True, exist_ok=True)
    write_json_atomic(root / "neighborhood.json", document)


def _blog_record(document: dict[str, Any], blog: str) -> dict[str, Any] | None:
    return next((item for item in document.get("blogs", []) if item.get("blog") == blog), None)


def _ensure_blog_record(document: dict[str, Any], blog: str, distance: int, parent: str) -> dict[str, Any]:
    item = _blog_record(document, blog)
    if item is None:
        item = {
            "blog": blog,
            "distance": distance,
            "parents": [],
            "observed_names": [blog],
            "observed_urls": [],
            "sampled_post_ids": [],
            "newest_observed_id": None,
            "oldest_observed_id": None,
            "newest_refresh_cursor": 0,
            "older_expansion_cursor": 0,
            "current_sample_size": 0,
            "configured_cap": 0,
            "partial": True,
            "status": "queued",
            "run_blocked": False,
            "last_attempt": None,
            "last_successful_checkpoint": None,
            "failure": None,
        }
        document.setdefault("blogs", []).append(item)
    if parent and parent not in item.setdefault("parents", []):
        item["parents"].append(parent)
    item["distance"] = min(int(item.get("distance", distance)), distance)
    return item


def _state_records(state: BlogState) -> list[dict[str, Any]]:
    return _json_records(state.json_dir)


def _run_source_count(primary: str, document: dict[str, Any]) -> int:
    total = len(list((canonical_archive_root(primary) / "json").glob("*.json")))
    for item in document.get("blogs", []):
        total += len(list((canonical_archive_root(str(item["blog"])) / "json").glob("*.json")))
    return total


def _state_complete_ids(state: BlogState) -> set[str]:
    return {str(record.get("id_string") or record.get("id")) for record in _state_records(state)}


def _fetch_state_page(state: BlogState) -> bool:
    activate_blog_state(state)
    offset = state.feed_start
    feed, posts = _fetch_feed_page_at(state, offset)
    if not posts:
        state.exhausted = True
        return False
    state.feed_start = offset + len(posts)
    state.feed_buffer.extend(posts)
    return True


def _fetch_feed_page_at(state: BlogState, offset: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fetch one temporary feed coordinate without making it durable state."""
    activate_blog_state(state)
    if not state.first_feed_request:
        wait_seconds = NETWORK_PROFILE["feed_delay_seconds"] + random.uniform(
            0, NETWORK_PROFILE["feed_jitter_seconds"]
        )
        if wait_seconds > 0:
            time.sleep(wait_seconds)
    feed = fetch_public_page(offset, PAGE_SIZE)
    state.first_feed_request = False
    posts = list(feed.get("posts") or [])
    if not state.feed_blog:
        candidate = feed.get("tumblelog") or feed.get("blog") or {}
        if isinstance(candidate, dict):
            state.feed_blog = dict(candidate)
    if state.feed_total is None:
        state.feed_total = int(feed.get("posts-total") or len(posts))
    return feed, posts


@dataclass
class ScoutResult:
    blog: str
    observed: int = 0
    missing: int = 0
    candidates: list[str] = field(default_factory=list)
    opportunities: list[ScoutOpportunity] = field(default_factory=list)
    exhausted: bool = False
    probes: int = 0
    frontier: dict[str, Any] | None = None


def _scout_paths(state: BlogState) -> tuple[Path, Path]:
    root = canonical_archive_root(state.blog) / "scout"
    return root / "observations.jsonl", root / "state.json"


def _source_timestamp(source: dict[str, Any]) -> int:
    return int(source.get("unix-timestamp") or source.get("timestamp") or 0)


def _scout_local_state(blog: str, post_id: str) -> str:
    return "present" if (canonical_archive_root(blog) / "json" / f"{post_id}.json").is_file() else "missing"


def _record_scout_page(
    state: BlogState,
    primary: str,
    document: dict[str, Any],
    page: list[dict[str, Any]],
    source_cursor: int,
    result: ScoutResult,
    output: Any,
) -> bool:
    """Record one observed page; the cursor is provenance, never a resume key."""
    observed_ids = [str(source.get("id") or "") for source in page if source.get("id") is not None]
    adjacency = document.setdefault("adjacency_observations", [])
    adjacency_keys = {
        (item.get("blog"), str(item.get("anchor_post_id")), item.get("direction"), str(item.get("adjacent_post_id")))
        for item in adjacency
    }
    region_id = f"{state.run_id or 'scan'}:{state.blog}:{uuid.uuid4().hex[:8]}"
    for index in range(len(page) - 1):
        newer = page[index]
        older = page[index + 1]
        newer_id = str(newer.get("id") or "")
        older_id = str(older.get("id") or "")
        if not newer_id or not older_id:
            continue
        ordered_timestamps = [_source_timestamp(newer), _source_timestamp(older)]
        new_adjacency = (
            {
                "blog": state.blog,
                "anchor_post_id": newer_id,
                "direction": "before",
                "adjacent_post_id": older_id,
                "region_id": region_id,
                "ordered_post_ids": observed_ids,
                "ordered_timestamps": ordered_timestamps,
                "ephemeral_source_cursor": source_cursor,
                "observed_at": time.time(),
                "confidence": "contiguous_feed_region",
            },
            {
                "blog": state.blog,
                "anchor_post_id": older_id,
                "direction": "after",
                "adjacent_post_id": newer_id,
                "region_id": region_id,
                "ordered_post_ids": observed_ids,
                "ordered_timestamps": ordered_timestamps,
                "ephemeral_source_cursor": source_cursor,
                "observed_at": time.time(),
                "confidence": "contiguous_feed_region",
            },
        )
        for item in new_adjacency:
            key = (item["blog"], str(item["anchor_post_id"]), item["direction"], str(item["adjacent_post_id"]))
            if key not in adjacency_keys:
                adjacency.append(item)
                adjacency_keys.add(key)

    page_has_missing = False
    for order, source in enumerate(page):
        pid = str(source.get("id") or "")
        if not pid:
            continue
        local = _scout_local_state(state.blog, pid)
        page_has_missing = page_has_missing or local == "missing"
        if local == "missing":
            result.missing += 1
        result.observed += 1
        evidence = interaction_evidence(state.blog, source)
        for item in evidence:
            item["evidence_lane"] = "archived" if local == "present" else "scout_only"
            item["observed_at"] = time.time()
            document.setdefault("scout_interactions", []).append(item)
            other = item.get("to_blog") if item.get("from_blog") == state.blog else item.get("from_blog")
            if other and other != primary and other not in result.candidates:
                result.candidates.append(other)
            if other and other != primary:
                result.opportunities.append(ScoutOpportunity(
                    kind="relationship",
                    blog=other,
                    post_id=pid,
                    graph_depth=state.distance + 1,
                    evidence_refs=["evidence:" + ":".join(str(value) for value in _evidence_key(item))],
                    reason_codes=["source affinity" if item.get("direction") == "upstream" else "audience activity"],
                    confidence=1.0 if local == "present" else 0.7,
                ))
        output.write(json.dumps({
            "blog": state.blog,
            "post_id": pid,
            "timestamp": _source_timestamp(source),
            "post_url": source.get("url-with-slug") or source.get("url") or "",
            "observed_order": order,
            "local_state": local,
            "evidence_lane": "archived" if local == "present" else "scout_only",
            "observed_at": time.time(),
            "ephemeral_source_cursor": source_cursor,
        }, sort_keys=True) + "\n")
    return page_has_missing


def _persist_scout_state(state: BlogState, state_path: Path) -> None:
    write_json_atomic(state_path, {
        "blog": state.blog,
        "scouted": state.scouted,
        "last_observation": time.time(),
        "exhausted": state.exhausted,
        "horizon_reached": state.scout_horizon_reached,
        "scan_id": state.run_id or None,
        "observed_order": "newest-first",
        "probe_count": state.scout_probe_count,
        "frontier": state.scout_frontier,
        "verified_complete_anchors": state.scout_complete_anchors,
    })


def scout_blog_page(state: BlogState, primary: str, document: dict[str, Any]) -> ScoutResult:
    """Scout past locally-present islands and leave the newest gap for acquisition."""
    result = ScoutResult(state.blog)
    if state.blocked_for_run:
        result.exhausted = True
        return result

    observations_path, state_path = _scout_paths(state)
    observations_path.parent.mkdir(parents=True, exist_ok=True)
    with observations_path.open("a", encoding="utf-8") as output:
        if state.scout_initialized:
            if state.exhausted:
                result.exhausted = True
                return result
            before = len(state.feed_buffer)
            if not _fetch_state_page(state):
                result.exhausted = True
            else:
                page = list(state.feed_buffer[before:])
                result.probes = 1
                _record_scout_page(state, primary, document, page, state.feed_start - len(page), result, output)
        else:
            state.scout_initialized = True
            observed_pages: dict[int, list[dict[str, Any]]] = {}
            page_missing: dict[int, bool] = {}

            def observe(offset: int) -> bool:
                if offset in observed_pages:
                    return True
                if result.probes >= max(1, state.scout_max_probes):
                    state.scout_horizon_reached = True
                    return False
                feed, page = _fetch_feed_page_at(state, offset)
                result.probes += 1
                if not page:
                    state.exhausted = True
                    return False
                observed_pages[offset] = page
                page_missing[offset] = _record_scout_page(
                    state, primary, document, page, offset, result, output
                )
                if state.feed_total is None:
                    state.feed_total = int(feed.get("posts-total") or len(page))
                return True

            selected_offset: int | None = None
            if observe(0) and page_missing[0]:
                selected_offset = 0
            elif not state.exhausted and not state.scout_horizon_reached:
                complete_offset = 0
                probe_offset = PAGE_SIZE
                while True:
                    if probe_offset > state.scout_max_offset:
                        state.scout_horizon_reached = True
                        break
                    if state.feed_total is not None and probe_offset >= state.feed_total:
                        state.exhausted = True
                        break
                    if not observe(probe_offset):
                        break
                    if page_missing[probe_offset]:
                        # Do not binary-search a false monotone predicate.
                        # Scan every page in this bracket toward the present;
                        # an older complete island does not close the search.
                        missing_offsets = [probe_offset]
                        refine = probe_offset - PAGE_SIZE
                        while refine > complete_offset:
                            if not observe(refine):
                                break
                            if page_missing[refine]:
                                missing_offsets.append(refine)
                            refine -= PAGE_SIZE
                        selected_offset = min(missing_offsets)
                        break
                    complete_offset = probe_offset
                    if state.feed_total is not None and probe_offset + len(observed_pages[probe_offset]) >= state.feed_total:
                        state.exhausted = True
                        break
                    probe_offset *= 2

            if selected_offset is None:
                state.scout_horizon_reached = state.scout_horizon_reached or not state.exhausted
                state.exhausted = True
                state.feed_buffer.clear()
            else:
                selected_page = observed_pages[selected_offset]
                state.feed_buffer = list(selected_page)
                state.feed_start = selected_offset + len(selected_page)
                newest_missing = next(
                    (source for source in selected_page if _scout_local_state(state.blog, str(source.get("id"))) == "missing"),
                    None,
                )
                if newest_missing is not None:
                    state.scout_frontier = {
                        "post_id": str(newest_missing.get("id")),
                        "timestamp": _source_timestamp(newest_missing),
                        "post_url": newest_missing.get("url-with-slug") or newest_missing.get("url") or "",
                        "local_state": "missing",
                        "observed_order": "newest-first",
                        "scan_id": state.run_id or None,
                    }
                    result.frontier = dict(state.scout_frontier)
                state.exhausted = False

            complete_anchors: list[dict[str, Any]] = []
            for offset, page in observed_pages.items():
                if page_missing.get(offset):
                    continue
                for source in (page[0], page[-1]):
                    anchor = {
                        "post_id": str(source.get("id")),
                        "timestamp": _source_timestamp(source),
                        "local_state": "present",
                        "observed_order": "newest-first",
                        "scan_id": state.run_id or None,
                    }
                    if anchor not in complete_anchors:
                        complete_anchors.append(anchor)
            state.scout_complete_anchors = complete_anchors[-12:]

    state.scouted += result.observed
    state.scout_probe_count += result.probes
    result.exhausted = state.exhausted
    _persist_scout_state(state, state_path)
    return result


def _process_source_ids(state: BlogState, sources: list[dict[str, Any]], candidate: ActionCandidate | None = None) -> int:
    if not sources:
        return 0
    candidate = candidate or state.active_candidate
    if candidate is None:
        raise AssertionError("durable acquisition requires an ActionCandidate")
    breadth = candidate.breadth
    is_target = bool(candidate.is_target or breadth == 0)
    assert state.blog == candidate.blog
    assert state.out == canonical_archive_root(candidate.blog or "")
    activate_blog_state(state)
    if CAPTURE_PLAN.profile:
        try:
            capture_blog_profile_metadata(state.blog)
        except Exception as exc:
            _append_repair_task(None, "profile-enrichment", exc)
    total = state.feed_total or len(sources)
    ids = []
    for source in sources:
        pid = int(source["id"])
        event = {
            "run_id": state.run_id,
            "blog": canonical_blog_name(state.blog),
            "post_id": str(pid),
            "breadth": breadth,
            "is_target": is_target,
            "history_position": state.inspected,
            "action_kind": candidate.kind,
            "reason_codes": list(candidate.reason_codes),
            "capture_policy_snapshot": dict(candidate.capture_policy_snapshot),
            "state": "selected",
            "planned_cost": candidate.planned_cost,
            "actual_cost": 0,
            "updated_at": time.time(),
        }
        assert state.blog == candidate.blog
        assert state.out == canonical_archive_root(candidate.blog or "")
        normalized = normalize_post(source, {"tumblelog": state.feed_blog or {"name": state.blog}}, total)
        normalized["_puppetbackup_anchor_role"] = state.lane_role
        normalized["_puppetbackup_run_id"] = state.run_id
        normalized["_puppetbackup_breadth"] = breadth
        normalized["_puppetbackup_is_target"] = is_target
        normalized["_puppetbackup_history_position"] = state.inspected
        normalized["_puppetbackup_capture_policy"] = dict(candidate.capture_policy_snapshot)
        normalized["_puppetbackup_action_kind"] = candidate.kind
        normalized["_puppetbackup_reason_codes"] = list(candidate.reason_codes)
        write_json_atomic(state.json_dir / f"{pid}.json", normalized)
        event["state"] = "acquired"
        event["actual_cost"] = 1
        try:
            _append_acquisition_ledger(event)
        except Exception as exc:
            _append_repair_task(pid, "acquisition-ledger", exc)
        candidate.actual_cost += 1
        ids.append(pid)
        # Durable source JSON is the acquisition boundary. Rendering remains
        # repairable work and must not delay current-run budget accounting.
        state.acquired_this_run += 1
        if state.progress is not None:
            try:
                state.progress.record_acquisition(candidate, state.blog, str(pid))
            except Exception as exc:
                _append_repair_task(pid, "progress-enrichment", exc)
    try:
        mark_presentation_dirty("source JSON acquired")
    except Exception as exc:
        _append_repair_task(None, "presentation-state", exc)
    # The unified scheduler's critical path ends at durable canonical JSON
    # plus its ledger row.  Running tumblr-backup, fallback rendering and
    # avatar downloads here serialized every small acquisition batch and made
    # sparse wide crawls appear much slower than the actual source capture.
    # Presentation stays repairable and is regenerated separately.
    if candidate.lane == "policy":
        for pid in ids:
            _record_deferred_presentation(pid)
        state.completed += len(ids)
        return len(ids)
    try:
        processed = process_batch(ids)
    except Exception as exc:
        for pid in ids:
            _append_repair_task(pid, "post-downstream", exc)
        processed = len(ids)
    state.completed += processed
    return processed


def acquire_one_target_batch(state: BlogState, candidate: ActionCandidate | None = None) -> bool:
    activate_blog_state(state)
    if state.exhausted or state.blocked_for_run:
        return False
    sources = []
    batch_limit = min(PROCESS_BATCH, state.batch_limit or PROCESS_BATCH)
    while len(sources) < batch_limit and not state.exhausted:
        if not state.feed_buffer and not _fetch_state_page(state):
            break
        source = state.feed_buffer.pop(0)
        state.inspected += 1
        pid = int(source["id"])
        if post_source_exists(pid):
            if not post_is_complete(pid):
                state.phase = "repair"
            continue
        sources.append(source)
    return bool(_process_source_ids(state, sources, candidate))


def acquire_one_context_batch(state: BlogState, target_size: int, candidate: ActionCandidate | None = None) -> bool:
    activate_blog_state(state)
    if state.blocked_for_run:
        return False
    state.target_size = target_size
    state.json_dir.mkdir(parents=True, exist_ok=True)
    existing = _state_complete_ids(state)
    if len(existing) >= target_size:
        state.exhausted = True
        return False
    sources = []
    batch_limit = min(PROCESS_BATCH, state.batch_limit or PROCESS_BATCH)
    while len(sources) < batch_limit and not state.exhausted and len(existing) + len(sources) < target_size:
        if not state.feed_buffer and not _fetch_state_page(state):
            break
        source = state.feed_buffer.pop(0)
        pid = str(source["id"])
        state.inspected += 1
        if pid in existing:
            if state.phase == "refresh":
                state.phase = "expand"
            continue
        sources.append(source)
        existing.add(pid)
    return bool(_process_source_ids(state, sources, candidate))


def _all_document_evidence(primary: str, document: dict[str, Any]) -> list[dict[str, Any]]:
    # Existing canonical history is stable for this run.  Parse it once and
    # retain factual relationship evidence in the context document; subsequent
    # frontier updates add only newly scouted observations.  Re-reading every
    # JSON file for every scout action was an avoidable O(actions * archive)
    # startup/acquisition cost.
    canonical = list(document.get("canonical_interactions", []))
    scanned_blogs = set(document.get("canonical_interaction_blogs", []))
    for blog in [primary] + [str(item["blog"]) for item in document.get("blogs", [])]:
        if blog in scanned_blogs:
            continue
        state = BlogState(blog, canonical_archive_root(blog), 0)
        for record in _state_records(state):
            canonical.extend(interaction_evidence(blog, record))
        scanned_blogs.add(blog)
    document["canonical_interactions"] = canonical
    document["canonical_interaction_blogs"] = sorted(scanned_blogs)
    evidence = list(document.get("scout_interactions", [])) + list(canonical)
    unique = {}
    for item in evidence:
        unique[_evidence_key(item)] = item
    return list(unique.values())


def _saturating_signal(count: int) -> float:
    return math.log1p(max(0, int(count)))


def _candidate_scores(primary: str, document: dict[str, Any], policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    scores: dict[str, dict[str, Any]] = {}
    for item in document.get("interactions", []):
        candidate = item.get("to_blog") if item.get("from_blog") == primary else item.get("from_blog") if item.get("to_blog") == primary else None
        if not candidate or candidate == primary:
            continue
        entry = scores.setdefault(candidate, {
            "count": 0, "direct_reblog": 0, "target_reblogs_from": 0,
            "other_reblogs_target": 0, "asks_answers": 0,
            "directions": set(), "evidence": [],
        })
        entry["count"] += 1
        if item.get("kind") == "direct_reblog":
            entry["direct_reblog"] += 1
            if item.get("from_blog") == primary:
                entry["target_reblogs_from"] += 1
                entry["directions"].add("upstream")
            else:
                entry["other_reblogs_target"] += 1
                entry["directions"].add("downstream")
        elif item.get("kind") == "structured_ask":
            entry["asks_answers"] += 1
        entry["evidence"].append(item)
    weights = policy.get("relationship_weights", {})
    for entry in scores.values():
        upstream = _saturating_signal(entry["target_reblogs_from"])
        downstream = _saturating_signal(entry["other_reblogs_target"])
        asks = _saturating_signal(entry["asks_answers"])
        reciprocal = float(weights.get("reciprocal_bonus", 0)) if len(entry["directions"]) > 1 else 0.0
        entry["source_affinity"] = upstream * float(weights.get("target_reblogs_from", 0))
        entry["audience_activity"] = downstream * float(weights.get("other_reblogs_target", 0))
        entry["reciprocal_activity"] = reciprocal
        entry["ask_answer"] = asks * float(weights.get("ask_answer", 0))
        entry["raw_counts"] = {
            "target_reblogs_from": entry["target_reblogs_from"],
            "other_reblogs_target": entry["other_reblogs_target"],
            "asks_answers": entry["asks_answers"],
        }
        entry.pop("directions", None)
    return scores


def recompute_context_document(primary: str, document: dict[str, Any], policy: dict[str, Any], config: dict[str, Any]) -> None:
    recompute_context_deficits(primary, document)
    evidence = _all_document_evidence(primary, document)
    document["interactions"] = evidence
    document["updated_at"] = time.time()
    scores = _candidate_scores(primary, document, policy)
    for blog in document.get("blogs", []):
        if int(blog.get("configured_cap", 0)) <= 0:
            blog["configured_cap"] = config["posts_per_context_blog"]
        score = scores.get(blog["blog"], {})
        blog["observed_interaction_count"] = score.get("count", 0)
        blog["direct_reblog_count"] = score.get("direct_reblog", 0)
        blog["relationship_components"] = {
            key: score.get(key, 0.0)
            for key in ("source_affinity", "audience_activity", "reciprocal_activity", "ask_answer")
        }
        blog["raw_relationship_counts"] = score.get("raw_counts", {})
        blog["evidence"] = score.get("evidence", [])
    known = {item.get("blog") for item in document.get("blogs", [])}
    observation_limit = int(config.get("max_observed_blogs", 0) or 0)
    diagnostics = document.setdefault("frontier_diagnostics", {})
    diagnostics.update({
        "max_breadth": int(config["max_depth"]),
        "observation_limit": observation_limit or None,
        "suppressed_by_observation_limit": 0,
    })
    max_depth = config["max_depth"]
    parents = [primary] + [item["blog"] for item in document.get("blogs", [])]
    for parent in parents:
        parent_item = _blog_record(document, parent)
        parent_distance = 0 if parent == primary else int(parent_item.get("distance", 0))
        if parent_distance >= max_depth:
            continue
        local_scores: dict[str, int] = {}
        for item in evidence:
            if item.get("source_blog") != parent:
                continue
            target = item["to_blog"] if item["from_blog"] == parent else item["from_blog"]
            if target != primary and target != parent:
                local_scores[target] = local_scores.get(target, 0) + 1
        for candidate, _score in sorted(local_scores.items(), key=lambda pair: (-pair[1], pair[0])):
            if candidate in known:
                continue
            # The only frontier population bound is an explicit policy limit.
            # It never changes the post budget or pretends the frontier ended.
            if observation_limit and len(document.get("blogs", [])) >= observation_limit:
                diagnostics["suppressed_by_observation_limit"] += 1
                continue
            candidate_item = _ensure_blog_record(document, candidate, parent_distance + 1, parent)
            candidate_item["configured_cap"] = config["posts_per_context_blog"]
            known.add(candidate)
    for blog in document.get("blogs", []):
        parents = set(blog.get("parents") or [primary])
        relevant = [
            item for item in evidence
            if item.get("source_blog") in parents
            and blog["blog"] in (item.get("from_blog"), item.get("to_blog"))
        ]
        blog["observed_interaction_count"] = len(relevant)
        blog["direct_reblog_count"] = sum(1 for item in relevant if item.get("kind") == "direct_reblog")
        blog["evidence"] = relevant
    document["pending"] = [
        item["blog"] for item in sorted(
            document.get("blogs", []),
            key=lambda item: (int(item.get("distance", 0)), -int(item.get("observed_interaction_count", 0)), item["blog"]),
        ) if item.get("status") in ("queued", "probe", "partial")
    ]


def update_context_blog_record(primary: str, document: dict[str, Any], state: BlogState, cap: int) -> None:
    item = _ensure_blog_record(document, state.blog, state.distance, "")
    records = _state_records(state)
    ids = [str(record.get("id_string") or record.get("id")) for record in records]
    timestamps = [int(record.get("timestamp") or 0) for record in records]
    item.update({
        "sampled_post_ids": ids,
        "current_sample_size": len(ids),
        "configured_cap": cap,
        "partial": True,
        "status": "partial" if len(ids) < cap else "complete",
        "newest_observed_id": ids[0] if ids else None,
        "oldest_observed_id": ids[-1] if ids else None,
        "newest_observed_timestamp": max(timestamps, default=None),
        "oldest_observed_timestamp": min(timestamps, default=None),
        "last_attempt": time.time(),
        "last_successful_checkpoint": time.time(),
        "failure": state.error or None,
    })


def recompute_context_deficits(primary: str, document: dict[str, Any]) -> None:
    """Rebuild best-effort chronological context goals from canonical state.

    Only anchor posts create strong immediate-context obligations. Support posts
    remain useful records but do not recursively close the whole blog.
    """
    observations = {}
    for item in document.get("adjacency_observations", []):
        key = (item.get("blog"), str(item.get("anchor_post_id")), item.get("direction"))
        observations[key] = item
    deficits = {}
    blogs = [primary] + [str(item.get("blog")) for item in document.get("blogs", [])]
    for blog in blogs:
        state = BlogState(blog, canonical_archive_root(blog), 0)
        for record in _state_records(state):
            post_id = str(record.get("id_string") or record.get("id"))
            role = record.get("_puppetbackup_anchor_role", "anchor")
            if role != "anchor":
                continue
            entry = {
                "blog": blog,
                "post_id": post_id,
                "anchor_role": role,
                "immediate_before": {"state": "unknown", "post_id": None, "evidence_ref": None},
                "immediate_after": {"state": "unknown", "post_id": None, "evidence_ref": None},
                "context_radius_left": 0,
                "context_radius_right": 0,
                "last_checked": time.time(),
                "evidence_refs": [],
            }
            for direction in ("before", "after"):
                evidence = observations.get((blog, post_id, direction))
                if evidence:
                    adjacent = str(evidence["adjacent_post_id"])
                    present = (canonical_archive_root(blog) / "json" / f"{adjacent}.json").is_file()
                    state_name = "known" if present else "missing"
                    reference = f"adjacency:{evidence.get('region_id')}:{direction}"
                    entry[f"immediate_{direction}"] = {"state": state_name, "post_id": adjacent, "evidence_ref": reference}
                    entry["evidence_refs"].append(reference)
            deficits[f"{blog}:{post_id}"] = entry
    document["context_deficits"] = deficits


def context_deficit_counts(document: dict[str, Any]) -> tuple[int, int]:
    complete = incomplete = 0
    for entry in document.get("context_deficits", {}).values():
        states = (entry["immediate_before"]["state"], entry["immediate_after"]["state"])
        if all(state in {"known", "unavailable"} for state in states):
            complete += 1
        else:
            incomplete += 1
    return complete, incomplete








def context_state(primary: str, item: dict[str, Any]) -> BlogState:
    return BlogState(
        blog=item["blog"],
        out=canonical_archive_root(item["blog"]),
        max_posts=0,
        role="context",
        distance=int(item.get("distance", 1)),
        observed_names=list(item.get("observed_names", [])),
        observed_urls=list(item.get("observed_urls", [])),
    )


def sync_context_state(primary: str, document: dict[str, Any], state: BlogState, cap: int) -> None:
    update_context_blog_record(primary, document, state, cap)
    document["last_successful_checkpoint"] = time.time()


def mark_blog_source_failure(
    primary: str,
    document: dict[str, Any],
    state: BlogState,
    failure: BlogSourceFailure,
    status: CrawlerStatus,
) -> None:
    """Block one source for this run while preserving all durable work."""
    state.blocked_for_run = True
    state.status = "blocked"
    state.error = str(failure)
    state.failure_kind = failure.kind
    state.failure_code = failure.code
    state.failure_operation = failure.operation
    state.feed_buffer.clear()
    state.active_candidate = None
    state.render_pending = isinstance(failure, BlogRenderFailure)

    item = _blog_record(document, state.blog)
    if item is not None:
        item.update({
            "run_blocked": True,
            "status": "blocked",
            "failure": str(failure),
            "failure_details": {
                "run_id": status.run_id,
                "operation": failure.operation,
                "code": failure.code,
                "kind": failure.kind,
                "retryable": failure.retryable,
                "at": time.time(),
            },
            "last_attempt": time.time(),
        })
        if state.render_pending:
            item["render_pending"] = True

    record = {
        "run_id": status.run_id,
        "blog": failure.blog,
        "operation": failure.operation,
        "code": failure.code,
        "kind": failure.kind,
        "retryable": failure.retryable,
        "message": str(failure),
        "lane": state.lane,
        "graph_depth": state.distance,
        "at": time.time(),
    }
    failures = document.setdefault("source_failures", [])
    if record not in failures:
        failures.append(record)
    status.record_source_failure(failure)
    status.current_action = f"Skipping {state.blog}; redirecting"
    status.current_reason = str(failure)


def context_post_count(primary: str, document: dict[str, Any]) -> int:
    return sum(int(item.get("current_sample_size", 0)) for item in document.get("blogs", []))


def eligible_context_items(document: dict[str, Any], config: dict[str, Any], visited: set[str]) -> list[dict[str, Any]]:
    max_depth = config["max_depth"]
    return [
        item for item in document.get("blogs", [])
        if item.get("blog") not in visited
        and int(item.get("distance", 0)) <= max_depth
        and item.get("status") in ("queued", "partial", "probe")
        and not item.get("run_blocked", False)
        and int(item.get("current_sample_size", 0)) < int(item.get("configured_cap", config["posts_per_context_blog"]))
    ]


def choose_context_item(document: dict[str, Any], config: dict[str, Any], visited: set[str]) -> dict[str, Any] | None:
    candidates = eligible_context_items(document, config, visited)
    if not candidates:
        return None
    return sorted(
        candidates,
        key=lambda item: (
            int(item.get("distance", 0)),
            -int(item.get("observed_interaction_count", 0)),
            -int(item.get("direct_reblog_count", 0)),
            item["blog"],
        ),
    )[0]


def regenerate_presentation(primary: str, document: dict[str, Any], finalize_primary: bool = False) -> None:
    raise RuntimeError(
        "generated presentation is exported under Archive/App; use the source-owned live interface"
    )


@dataclass
class LaneRuntimeState:
    lane: str
    state: str = "DISCOVERING"
    actual: int = 0
    desired: float = 0.0
    debt: float = 0.0
    ready: int = 0
    discovering: int = 0
    attempts: int = 0
    blocked_reason: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "actual": self.actual,
            "desired": self.desired,
            "debt": self.debt,
            "ready": self.ready,
            "discovering": self.discovering,
            "attempts": self.attempts,
            "blocked_reason": self.blocked_reason,
        }


@dataclass
class CrawlerStatus:
    target: str
    focus: str = "balanced"
    max_breadth: int = 2
    network: str = "Gentle"
    budget_limit: int = 0
    budget_used: int = 0
    saved_by_lane: dict[str, int] = field(default_factory=lambda: {"target": 0, "depth1": 0, "depth2": 0, "survey": 0})
    target_posts: int = 0
    breadth1_posts: int = 0
    breadth2plus_posts: int = 0
    queued_by_lane: dict[str, int] = field(default_factory=lambda: {"target": 0, "depth1": 0, "depth2": 0})
    scouted: int = 0
    known_blogs: int = 0
    active_blogs: int = 0
    repairs: int = 0
    current_action: str = "Starting"
    current_blog: str = ""
    last_saved: str = ""
    warnings: list[str] = field(default_factory=list)
    warning_counts: dict[str, int] = field(default_factory=dict)
    warning_details: dict[str, list[str]] = field(default_factory=dict)
    warning_labels: dict[str, str] = field(default_factory=dict)
    run_id: str = ""
    acquisition_events: list[dict[str, Any]] = field(default_factory=list)
    unknown_lanes: dict[str, int] = field(default_factory=dict)
    runtime: RuntimeControls | None = None
    current_reason: str = ""
    completion_reason: str = ""
    current_action_kind: str = ""
    context_bracketed: int = 0
    context_incomplete: int = 0
    deferred_actions: int = 0
    lane_status: dict[str, LaneRuntimeState] = field(default_factory=dict)
    source_failures: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = 0.0
    finished_at: float = 0.0
    strategy: str = "neighborhood"
    survey_requests: int = 0
    survey_node_limit: int = 0
    survey_request_limit: int = 0
    survey_max_distance: int = 0
    candidate_trace: list[dict[str, Any]] = field(default_factory=list)
    candidate_rejections: list[dict[str, Any]] = field(default_factory=list)
    capture_policy_snapshot: dict[str, Any] = field(default_factory=dict)
    frontier_diagnostics: dict[str, Any] = field(default_factory=dict)
    phase_timings: dict[str, dict[str, float | int]] = field(default_factory=dict)

    @property
    def depth(self) -> int:
        """Historical read alias; relationship reach is max_breadth."""
        return self.max_breadth

    def record_acquisition(self, candidate: ActionCandidate, blog: str, post_id: str) -> None:
        if any(event.get("blog") == blog and event.get("post_id") == post_id for event in self.acquisition_events):
            return
        breadth = candidate.breadth
        is_target = bool(candidate.is_target or breadth == 0)
        event = {
            "run_id": self.run_id,
            "blog": blog,
            "post_id": post_id,
            "breadth": breadth,
            "is_target": is_target,
            "state": "acquired",
            "planned_cost": candidate.planned_cost,
            "actual_cost": 1,
            "action_kind": candidate.kind,
            "reason_codes": list(candidate.reason_codes),
        }
        raw_lane = str(candidate.lane or "").strip().lower()
        if raw_lane not in {"", "policy"}:
            try:
                event["acquisition_lane"] = canonical_acquisition_lane(raw_lane, breadth)
            except UnknownAcquisitionLane:
                event["acquisition_lane"] = raw_lane
            event["lane"] = event["acquisition_lane"]
        self.acquisition_events.append(event)
        self.budget_used = len(self.acquisition_events)
        if is_target:
            self.target_posts += 1
            self.saved_by_lane["target"] = self.saved_by_lane.get("target", 0) + 1
        elif breadth == 1:
            self.breadth1_posts += 1
            self.saved_by_lane["depth1"] = self.saved_by_lane.get("depth1", 0) + 1
        elif breadth >= 2:
            self.breadth2plus_posts += 1
            self.saved_by_lane["depth2"] = self.saved_by_lane.get("depth2", 0) + 1
        else:
            self.unknown_lanes["missing breadth"] = self.unknown_lanes.get("missing breadth", 0) + 1
        # Keep the old in-memory lane view coherent for compatibility callers
        # that construct historical candidates directly. Runtime candidates
        # use lane="policy" and are accounted only by factual breadth.
        raw_lane = str(candidate.lane or "").strip().lower()
        if raw_lane not in {"", "policy"}:
            try:
                legacy_lane = canonical_acquisition_lane(raw_lane, breadth)
            except UnknownAcquisitionLane:
                self.unknown_lanes[raw_lane] = self.unknown_lanes.get(raw_lane, 0) + 1
            else:
                factual_lane = "target" if is_target else "depth1" if breadth == 1 else "depth2"
                if legacy_lane != factual_lane:
                    self.saved_by_lane[factual_lane] = max(0, self.saved_by_lane.get(factual_lane, 0) - 1)
                    self.saved_by_lane[legacy_lane] = self.saved_by_lane.get(legacy_lane, 0) + 1
        self.last_saved = f"{blog} / {post_id}"

    def warning(self, message: str, *, key: str | None = None, detail: str | None = None) -> None:
        if not message:
            return
        warning_key = key or message
        self.warning_counts[warning_key] = self.warning_counts.get(warning_key, 0) + 1
        self.warning_labels.setdefault(warning_key, message)
        if detail:
            details = self.warning_details.setdefault(warning_key, [])
            if detail not in details:
                details.append(detail)
        if message not in self.warnings:
            self.warnings.append(message)
            self.warnings = self.warnings[-5:]

    def record_source_failure(self, failure: BlogSourceFailure) -> None:
        record = {
            "blog": failure.blog,
            "operation": failure.operation,
            "code": failure.code,
            "kind": failure.kind,
            "retryable": failure.retryable,
            "message": str(failure),
        }
        if record not in self.source_failures:
            self.source_failures.append(record)
        code = f"HTTP {failure.code}" if failure.code is not None else failure.kind
        key = f"{failure.kind}:{failure.code or 'none'}"
        self.warning(
            f"{failure.kind} sources ({code})",
            key=key,
            detail=failure.blog,
        )

    def update_lane_status(self, lanes: dict[str, LaneRuntimeState]) -> None:
        self.lane_status = lanes

    def set_action(self, action: ActionCandidate | None, fallback: str = "") -> None:
        if action is None:
            self.current_blog = ""
            self.current_action_kind = ""
            self.current_reason = fallback
            self.current_action = fallback
            return
        self.current_action_kind = action.kind
        self.current_blog = action.blog or ""
        self.current_reason = ", ".join(action.reason_codes)
        self.current_action = action.kind.replace("_", " ")
        if action.blog:
            self.current_action += f" {action.blog}"
        if action.target_post_id:
            self.current_action += f" / {action.target_post_id}"


def trace_strategy_candidates(
    status: CrawlerStatus,
    candidates: list[tuple[int, str, BlogState, dict[str, Any] | None, dict[str, Any] | None]],
    selected_blog: str,
    selected_item: dict[str, Any] | None,
    policy: CapturePolicy,
) -> None:
    """Record bounded candidate facts for deterministic diagnosis, not policy."""
    selected = next((value for value in candidates if value[1] == selected_blog), None)
    if selected is None:
        return
    selected_priority = selected[0]
    for position, (_priority, blog, state, _document, item) in enumerate(candidates):
        distance = int(item.get("distance", state.distance)) if item is not None else 0
        capture = policy.media_plan_for_breadth(distance)
        current = int(item.get("current_sample_size", 0)) if item is not None else 0
        desired = policy.history_limit_for_breadth(distance)
        requested = current if desired == 0 else (current + 1 if desired is not None else current + 1)
        status.candidate_trace.append({
            "blog": blog,
            "distance": distance,
            "capture_plan": capture.media_quality,
            "requested_sample_count": requested,
            "desired_history_count": desired,
            "generated": True,
            "priority": _priority,
            "queue_class": "target" if item is None else "outward",
            "queue_position": position,
            "selected": blog == selected_blog,
            "outranked_by": None if blog == selected_blog else selected_blog,
        })
    if len(status.candidate_trace) > 500:
        del status.candidate_trace[:-500]


def status_snapshot(status: CrawlerStatus | None = None) -> dict[str, Any]:
    status = status or ACTIVE_STATUS
    if status is None:
        return {"lifecycle": ACTIVE_LIFECYCLE, "runtime_identity": runtime_identity()}
    with RUNTIME_LOCK:
        runtime = status.runtime
        return {
            "lifecycle": ACTIVE_LIFECYCLE,
            "runtime_identity": runtime_identity(),
            "target": status.target,
            "run_id": status.run_id,
            "focus": status.focus,
            "strategy": status.strategy,
            "capture_policy": dict(status.capture_policy_snapshot),
            "context_multiplier": runtime.context_multiplier if runtime else 1.0,
            "relationship_multiplier": runtime.relationship_multiplier if runtime else 1.0,
            "network": status.network,
            "network_profile_id": runtime.network_profile_id if runtime else "",
            "budget_used": status.budget_used,
            "budget_limit": status.budget_limit,
            "started_at": status.started_at,
            "finished_at": status.finished_at,
            **summary_fields(status),
            "queued_by_lane": dict(status.queued_by_lane),
            "lane_status": {key: value.snapshot() for key, value in status.lane_status.items()},
            "scouted": status.scouted,
            "known_blogs": status.known_blogs,
            "survey_requests": status.survey_requests,
            "survey_node_limit": status.survey_node_limit,
            "survey_request_limit": status.survey_request_limit,
            "survey_max_distance": status.survey_max_distance,
            "active_blogs": status.active_blogs,
            "repairs": status.repairs,
            "current_action": status.current_action,
            "current_blog": status.current_blog,
            "current_reason": status.current_reason,
            "completion_reason": status.completion_reason,
            "last_saved": status.last_saved,
            "context_bracketed": status.context_bracketed,
            "context_incomplete": status.context_incomplete,
            "warnings": list(status.warnings),
            "cancel_requested": bool(runtime.cancel_requested) if runtime else False,
            "presentation_generation": ACTIVE_PRESENTATION_GENERATION,
            "presentation_dirty": _presentation_is_dirty(),
            "candidate_trace": list(status.candidate_trace),
            "candidate_rejections": list(status.candidate_rejections),
            "source_failures": list(status.source_failures),
            "blocked_sources": [failure["blog"] for failure in status.source_failures],
            "frontier_diagnostics": dict(status.frontier_diagnostics),
            "phase_timings": {key: dict(value) for key, value in status.phase_timings.items()},
        }


def _live_interface_links() -> list[tuple[str, str]]:
    """The complete source-owned navigation surface for the loopback UI."""
    return [
        ("Graph", "/app/graph.html"),
        ("List", "/app/list.html"),
        ("Feed", "/app/feed.html"),
        ("Tags", "/app/tags.html"),
        ("Neighborhoods", "/app/neighborhoods.html"),
        ("Crawler", "/app/crawler.html"),
    ]


def _live_post_label(record: dict[str, Any]) -> str:
    return str(record.get("title") or record.get("summary") or record.get("post_url") or f"Post {record.get('id_string') or record.get('id') or ''}").strip()


def _live_filter_toolbar(kind: str, filters: str) -> str:
    labels = {"tag": "Tags", "neighborhood": "Neighborhoods"}
    label = labels.get(kind, kind.title())
    panel = f'<div class="graph-controls {kind}-filter-controls" role="group" aria-label="{escape(label)} filters">{filters}</div>'
    return presentation.explore_toolbar(
        label,
        filter_content=panel,
        extra_class=f"{kind}-explore-toolbar",
    )


def render_list_content() -> str:
    rows = []
    neighborhoods: dict[str, tuple[str, str]] = {}
    targets: set[str] = set()
    for root in sorted(NEIGHBORHOODS_ROOT.iterdir()) if NEIGHBORHOODS_ROOT.is_dir() else []:
        if not root.is_dir():
            continue
        document = load_context_document(root.name)
        primary_value = document.get("primary_blog")
        if root.name.startswith("set-") and (not primary_value or str(primary_value) == root.name):
            continue
        primary = str(primary_value or root.name)
        targets.add(primary)
        for item in document.get("blogs", []):
            blog = str(item.get("blog") or "")
            if blog:
                neighborhoods[blog] = (primary, str(item.get("distance", "-")))
    for item in build_global_catalog().get("blogs", []):
        blog = str(item["blog"])
        href = "/app/blog.html?blog=" + quote(blog)
        neighborhood, distance = neighborhoods.get(blog, ("-", "0" if blog in targets else "-"))
        dates = [value for value in (item.get("oldest_local_timestamp"), item.get("newest_local_timestamp")) if value]
        date_range = " - ".join(time.strftime("%Y-%m-%d", time.gmtime(int(value))) for value in dates) if dates else "-"
        rows.append(
            '<tr data-blog-row data-neighborhood="%s" data-distance="%s"><td data-label="Blog"><a href="%s"><bdi dir="auto">%s</bdi></a></td><td data-label="Posts">%d</td><td data-label="Target">%s</td><td data-label="Neighborhood"><bdi dir="auto">%s</bdi></td><td data-label="Breadth">%s</td><td data-label="Captured range">%s</td><td data-label="Action"><a href="/app/graph.html?blog=%s">Inspect</a></td></tr>'
            % (escape(neighborhood.casefold()), escape(distance), href, escape(blog), int(item.get("local_post_count") or 0), "Yes" if blog in targets else "No", escape(neighborhood), escape(distance), escape(date_range), quote(blog))
        )
    filters = ('<label class="explore-search" for="blog-search">Search blogs<input id="blog-search" type="search" placeholder="Search blogs" autocomplete="off"></label>'
               '<label for="list-neighborhood">Neighborhood<select id="list-neighborhood"><option value="__all__">All neighborhoods</option></select></label>'
               '<label for="list-distance">Breadth<select id="list-distance"><option value="all">All</option><option value="0">0</option><option value="1">1</option><option value="2">2</option><option value="3">3+</option></select></label>')
    body = "".join(rows) or '<tr><td colspan="7">No canonical blogs have been saved yet.</td></tr>'
    script = '<script>(function(){var s=document.getElementById("blog-search"),n=document.getElementById("list-neighborhood"),d=document.getElementById("list-distance");document.querySelectorAll("[data-blog-row]").forEach(function(r){var o=document.createElement("option"),v=r.dataset.neighborhood;if(v&&v!=="-"&&!Array.from(n.options).some(function(x){return x.value===v;})){o.value=v;o.textContent=v;n.appendChild(o);}});function u(){var q=s.value.toLocaleLowerCase(),nv=n.value,dv=d.value;document.querySelectorAll("[data-blog-row]").forEach(function(r){var x=Number(r.dataset.distance),dm=dv==="all"||(dv==="3"&&x>=3)||String(x)===dv;r.hidden=r.textContent.toLocaleLowerCase().indexOf(q)<0||(nv!=="__all__"&&r.dataset.neighborhood!==nv)||!dm;});}s.addEventListener("input",u);n.addEventListener("change",u);d.addEventListener("change",u);u();}());</script>'
    return '<section class="explore-page"><div class="explore-heading"><p class="eyebrow">Explore</p><h1>List</h1><p>Every locally preserved blog, with archive facts and network context.</p></div>' + _live_filter_toolbar("list", filters) + '<div class="collection-table-wrap"><table class="collection-table"><caption class="visually-hidden">Archived blogs</caption><thead><tr><th>Blog</th><th>Posts</th><th>Target</th><th>Neighborhood</th><th>Breadth</th><th>Captured range</th><th>Action</th></tr></thead><tbody>' + body + '</tbody></table></div></section>' + script


def render_feed_content(selected_pov: str = "", params: dict[str, list[str]] | None = None) -> str:
    params = params or {}
    query = _feed_query(params, selected_pov)
    catalog = build_global_catalog()
    if query.mode == "affinity" and not query.pov:
        query = FeedQuery(**{**query.__dict__, "pov": str((catalog.get("blogs") or [{}])[0].get("blog") or "")})
    metadata, _ = _feed_metadata()
    feed_index = build_feed_index(catalog, metadata)
    if query.mode == "affinity" and not (feed_index.get("targets", {}).get(query.pov, {}).get("sources") or {}):
        query = FeedQuery(**{**query.__dict__, "mode": "all", "pov": "__all__"})
    cursor = FeedCursor.decode((params.get("cursor") or [""])[0]) if params.get("cursor") else None
    window = build_feed_window(query, cursor)
    records = list(window.records)
    context_by_blog: dict[str, tuple[str, str]] = {}
    neighborhood_names: set[str] = set()
    for root in sorted(NEIGHBORHOODS_ROOT.iterdir()) if NEIGHBORHOODS_ROOT.is_dir() else []:
        if not root.is_dir():
            continue
        document = load_context_document(root.name)
        primary = str(document.get("primary_blog") or root.name)
        if root.name.startswith("set-") and (not document.get("primary_blog") or primary == root.name):
            continue
        neighborhood_names.add(primary)
        context_by_blog.setdefault(primary, (primary.casefold(), "0"))
        for item in document.get("blogs", []):
            blog = str(item.get("blog") or "")
            if blog:
                context_by_blog.setdefault(blog, (primary.casefold(), str(item.get("distance", "-"))))
    default_pov = query.pov
    card_html = []
    target_data = feed_index.get("targets", {}).get(default_pov, {})
    for record in records:
        blog = str(record.get("_canonical_blog") or "")
        neighborhood, distance = context_by_blog.get(blog, ("", "-"))
        card_html.append(_render_post_card(
            record,
            CONTENT_ROOT / "__live_feed__.html",
            data_attributes={
                "blog": blog,
                "timestamp": int(record.get("timestamp") or 0),
                "neighborhood": neighborhood,
                "distance": distance,
            },
            live=True,
            hidden=False,
        ))
    cards = "".join(card_html)
    target_options = "".join(
        f'<option value="{escape(str(item["blog"]))}"{ " selected" if str(item["blog"]) == default_pov else "" }>{escape(str(item["blog"]))} — {int(item.get("local_post_count") or 0)} posts</option>'
        for item in catalog.get("blogs", [])
    )
    target_options += f'<option value="__all__"{ " selected" if query.mode == "all" else "" }>All archive</option>'
    neighborhood_options = ''.join(
        f'<option value="{escape(name.casefold())}">{escape(name)}</option>'
        for name in sorted(neighborhood_names, key=str.casefold)
    )
    filters = (
        '<label for="feed-pov">POV<select id="feed-pov">' + target_options + '</select></label>'
        f'<label class="explore-search" for="feed-search">Search<input id="feed-search" type="search" value="{escape(query.search)}" placeholder="Search posts or blogs" autocomplete="off"></label>'
        '<label for="feed-neighborhood">Neighborhood<select id="feed-neighborhood"><option value="__all__">All neighborhoods</option>' + neighborhood_options + '</select></label>'
        '<label for="feed-distance">Breadth<select id="feed-distance"><option value="all">All</option><option value="0">0</option><option value="1">1</option><option value="2">2</option><option value="3">3+</option></select></label>'
        '<label for="feed-affinity-level">Affinity<select id="feed-affinity-level"><option value="any">Any observed affinity</option><option value="closest">Closest</option><option value="likely">Likely</option><option value="broad">Broad</option></select></label>'
        f'<fieldset class="feed-evidence-options"><legend>Evidence</legend><label><input id="feed-evidence-reblog" type="checkbox"{" checked" if "direct_reblog" in query.evidence else ""}> Direct target reblogs</label><label><input id="feed-evidence-like" type="checkbox"{" checked" if "direct_like" in query.evidence else ""}> Explicit public likes</label><label><input id="feed-evidence-ask" type="checkbox"{" checked" if "structured_ask" in query.evidence else ""}> Structured asks</label></fieldset>'
        f'<label><input id="feed-include-target" type="checkbox"{" checked" if query.include_target else ""}> Include target posts</label>'
        f'<label for="feed-sort">Sort<select id="feed-sort"><option value="newest"{" selected" if query.chronology == "newest" else ""}>Newest</option><option value="oldest"{" selected" if query.chronology == "oldest" else ""}>Oldest</option></select></label>'
    )
    next_link = ""
    if window.next_cursor:
        next_link = '<p class="feed-pagination"><a class="feed-next" href="/app/feed.html?%s">Next window</a></p>' % escape(_feed_query_params(query, window.next_cursor), quote=True)
    summary = (
        f'<p class="feed-universe-summary" data-feed-universe-size="{escape(str(window.selected_count if window.selected_count is not None else "unknown"))}" '
        f'data-feed-rendered-count="{len(records)}" data-feed-total-archive-posts="{window.total_archive_posts}" '
        f'data-feed-metadata-touched="{window.metadata_touched}" data-feed-rich-records-loaded="{window.rich_records_loaded}">'
        f'Feed window: {len(records)}; ' + (f'selected posts: {window.selected_count}; ' if window.selected_count is not None else 'selected posts: more than this window; ') +
        f'total archive: {window.total_archive_posts}</p>' + next_link
    )
    identity = runtime_identity()
    return '<section class="feed-page explore-page" data-archive-name="%s" data-source-revision="%s" data-frontend-revision="%s" data-feed-mode="%s" data-feed-query="%s"><div class="explore-heading"><p class="eyebrow">Explore</p><h1>Feed</h1><p class="feed-description">Archived posts in chronological and affinity-oriented views.</p></div>' % (
        escape(identity["archive_name"]), escape(identity["source_revision"]), escape(identity["frontend_revision"]), escape(query.mode), escape(query.fingerprint())
    ) + _live_filter_toolbar("feed", filters) + summary + '<details class="feed-evidence"><summary>Why are these sources included?</summary><div id="feed-evidence-list"></div></details><div class="post-feed" id="feed-posts">' + (cards or '<p>No canonical posts have been saved yet.</p>') + '</div><script id="feed-index" type="application/json">' + graph_projection.embedded_json(feed_index) + '</script></section>'


def render_tags_content(selected_tag: str = "") -> str:
    tags = build_tag_index().get("tags", [])
    if selected_tag:
        item = next((value for value in tags if value.get("page_id") == selected_tag), None)
        if item is not None:
            cards = []
            for post in item.get("posts", []):
                path = canonical_archive_root(str(post["blog"])) / "json" / f'{post["post_id"]}.json'
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    record = {}
                if isinstance(record, dict):
                    record["_canonical_blog"] = post["blog"]
                    cards.append(_render_post_card(record, CONTENT_ROOT / "__live_tag__.html", compact=True, live=True))
            return '<section class="explore-page compact-index tags-index"><p class="eyebrow">Explore</p><p><a href="/app/tags.html">All tags</a></p><h1><bdi dir="auto">%s</bdi></h1><p>%d archived posts</p><div class="post-feed">%s</div></section>' % (escape(str(item.get("display") or "")), len(item.get("posts") or []), "".join(cards) or "<p>No rendered local posts for this tag.</p>")
    rows = "".join(f'<li class="tag-card" data-tag-row data-tag-search="{escape(str(item.get("search_key") or ""))}" data-tag-count="{len(item.get("posts") or [])}"><a class="tag-card-link" href="/app/tags.html?tag={quote(str(item.get("page_id") or ""))}"><bdi dir="auto">{escape(str(item.get("display") or ""))}</bdi></a><span class="tag-card-count">{len(item.get("posts") or [])} post{"s" if len(item.get("posts") or []) != 1 else ""}</span></li>' for item in tags)
    filters = '<label class="explore-search" for="tag-search">Search tags<input id="tag-search" type="search" placeholder="Search tags" autocomplete="off"></label><label for="tag-visibility">Show<select id="tag-visibility"><option value="2" selected>2+ posts</option><option value="1">All tags</option></select></label><label for="tag-sort">Sort<select id="tag-sort"><option value="count">Most posts</option><option value="alpha">Name A-Z</option><option value="alpha-desc">Name Z-A</option><option value="count-asc">Fewest posts</option></select></label>'
    script = '<script>(function(){var s=document.getElementById("tag-search"),v=document.getElementById("tag-visibility"),o=document.getElementById("tag-sort");function u(){var q=s.value.toLocaleLowerCase(),minimum=Number(v.value),rows=Array.from(document.querySelectorAll("[data-tag-row]"));rows.sort(function(a,b){return o.value==="alpha"?a.dataset.tagSearch.localeCompare(b.dataset.tagSearch):o.value==="alpha-desc"?b.dataset.tagSearch.localeCompare(a.dataset.tagSearch):o.value==="count-asc"?Number(a.dataset.tagCount)-Number(b.dataset.tagCount):Number(b.dataset.tagCount)-Number(a.dataset.tagCount);});rows.forEach(function(r){document.getElementById("tag-list").appendChild(r);r.hidden=Number(r.dataset.tagCount)<minimum||r.dataset.tagSearch.indexOf(q)<0;});}s.addEventListener("input",u);v.addEventListener("change",u);o.addEventListener("change",u);u();}());</script>'
    return '<section class="explore-page compact-index tags-index"><div class="explore-heading"><p class="eyebrow">Explore</p><h1>Tags</h1><p class="page-intro">Preserved tags across the archive.</p></div>' + _live_filter_toolbar("tag", filters) + '<ul class="index-grid tag-grid" id="tag-list">' + (rows or '<li>No tags have been indexed yet.</li>') + '</ul>' + script + '</section>'


def render_neighborhoods_content(selected_target: str = "") -> str:
    contexts = []
    for root in sorted(NEIGHBORHOODS_ROOT.iterdir()) if NEIGHBORHOODS_ROOT.is_dir() else []:
        if root.is_dir():
            document = load_context_document(root.name)
            primary_value = document.get("primary_blog")
            if root.name.startswith("set-") and (not primary_value or str(primary_value) == root.name):
                continue
            primary = str(primary_value or root.name)
            contexts.append((primary, document))
    if selected_target:
        match = next(((primary, document) for primary, document in contexts if primary == selected_target), None)
        if match:
            primary, document = match
            rows = "".join(f'<li class="neighborhood-card"><a class="neighborhood-card-link" href="/app/blog.html?blog={quote(str(item.get("blog") or ""))}"><bdi dir="auto">{escape(str(item.get("blog") or ""))}</bdi></a><span class="archive-meta">Breadth {escape(str(item.get("distance", "-")))}; {int(item.get("current_sample_size", 0))} sampled posts; {int(item.get("observed_interaction_count", 0))} observed interactions</span></li>' for item in document.get("blogs", []))
            return '<section class="explore-page compact-index neighborhood-index"><p class="eyebrow">Explore</p><p><a href="/app/neighborhoods.html">All neighborhoods</a></p><h1><bdi dir="auto">%s</bdi></h1><p>Observed relationship context around this target.</p><nav class="context-nav" aria-label="Neighborhood views"><a href="/app/graph.html?blog=%s">Graph</a> <a href="/app/list.html">List</a> <a href="/app/feed.html?pov=%s">Feed</a></nav><ul class="index-grid neighborhood-grid">%s</ul></section>' % (escape(primary), quote(primary), quote(primary), rows or '<li>No observed blogs in this neighborhood.</li>')
    rows = "".join(f'<li class="neighborhood-card" data-neighborhood-row data-neighborhood-search="{escape(primary.casefold())}" data-neighborhood-count="{len(document.get("blogs") or [])}"><a class="neighborhood-card-link" href="/app/neighborhoods.html?target={quote(primary)}"><bdi dir="auto">{escape(primary)}</bdi></a><span class="archive-meta">{len(document.get("blogs") or [])} observed blogs; {len(document.get("interactions") or [])} relationships</span><span class="neighborhood-card-action">Open</span></li>' for primary, document in contexts)
    filters = '<label class="explore-search" for="neighborhood-search">Search neighborhoods<input id="neighborhood-search" type="search" placeholder="Search neighborhoods" autocomplete="off"></label><label for="neighborhood-sort">Sort<select id="neighborhood-sort"><option value="name">Name A-Z</option><option value="coverage">Most observed blogs</option></select></label>'
    script = '<script>(function(){var s=document.getElementById("neighborhood-search"),o=document.getElementById("neighborhood-sort");function u(){var q=s.value.toLocaleLowerCase(),rows=Array.from(document.querySelectorAll("[data-neighborhood-row]"));rows.sort(function(a,b){return o.value==="coverage"?Number(b.dataset.neighborhoodCount)-Number(a.dataset.neighborhoodCount):a.dataset.neighborhoodSearch.localeCompare(b.dataset.neighborhoodSearch);});rows.forEach(function(r){document.getElementById("neighborhood-list").appendChild(r);r.hidden=r.dataset.neighborhoodSearch.indexOf(q)<0;});}s.addEventListener("input",u);o.addEventListener("change",u);u();}());</script>'
    return '<section class="explore-page compact-index neighborhood-index"><div class="compact-heading"><p class="eyebrow">Explore</p><h1>Neighborhoods</h1><p class="page-intro">Observed blog relationships and bounded target-centered context.</p></div>' + _live_filter_toolbar("neighborhood", filters) + '<ul class="index-grid neighborhood-grid" id="neighborhood-list">%s</ul><p id="neighborhood-empty" hidden>No matching neighborhoods.</p>%s</section>' % (rows or '<li>No observed neighborhoods have been saved yet.</li>', script)


def render_blog_content(blog: str) -> str:
    blog = canonical_blog_name(blog)
    records = _json_records(canonical_archive_root(blog) / "json")
    if not records and not (canonical_archive_root(blog) / "profile" / "profile.json").is_file():
        return "<section><h1>Blog unavailable</h1><p>No canonical local record exists for <bdi dir=\"auto\">%s</bdi>.</p></section>" % escape(blog)
    records.sort(key=lambda record: (-int(record.get("timestamp") or 0), str(record.get("id_string") or record.get("id") or "")))
    profile = _write_profile_snapshot(blog, records)
    destination = CONTENT_ROOT / "__live_blog__.html"
    avatar = _local_profile_avatar(blog, destination)
    avatar_state, avatar_label = _avatar_state(blog, avatar)
    avatar_html = (
        f'<img class="profile-avatar" src="{escape(_archive_content_url(avatar))}" alt="">'
        if avatar else _avatar_placeholder("profile-avatar", avatar_state, avatar_label)
    )
    title = str(profile.get("title") or blog)
    description = profile.get("description")
    bio = f'<p class="profile-bio" dir="auto">{escape(str(description))}</p>' if description else ""
    cards = "".join(_render_post_card(record, destination, live=True) for record in records)
    source_url = str(profile.get("url") or "")
    source_link = f'<a href="{escape(source_url)}" rel="noreferrer noopener">Original blog</a>' if source_url else ""
    return (
        '<section class="profile-header">'
        f'{avatar_html}<div><h1><bdi dir="auto">{escape(title)}</bdi></h1>'
        f'<p class="profile-username">{escape(blog)}</p>{bio}'
        f'<p class="archive-meta">{len(records)} locally preserved posts</p></div></section>'
        '<nav class="context-nav" aria-label="Blog sections">'
        '<a href="/app/blog.html?blog=%s" aria-current="page">Posts</a> '
        '<a href="/app/tags.html">Tags</a> %s'
        '</nav><div class="post-feed">%s</div>'
        % (quote(blog), source_link, cards or '<p>No canonical posts.</p>')
    )


def render_post_content(blog: str, post_id: str) -> str:
    blog = canonical_blog_name(blog)
    path = canonical_archive_root(blog) / "json" / f"{post_id}.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "<section><h1>Post unavailable</h1><p>No canonical local record exists for this post.</p></section>"
    if not isinstance(record, dict):
        return "<section><h1>Post unavailable</h1><p>The canonical record is not an object.</p></section>"
    record["_canonical_blog"] = blog
    card = _render_post_card(record, CONTENT_ROOT / "__live_post__.html", live=True)
    return '<section class="archive-post"><p>Blog: <a href="/app/blog.html?blog=%s"><bdi dir="auto">%s</bdi></a></p>%s</section>' % (
        quote(blog), escape(blog), card,
    )


def live_interface_page(page: str) -> str | None:
    """Render the loopback UI from source, never from disposable data roots.

    The live bridge asks this function for an in-memory page.  Graph and
    reader projections are derived from Archive facts at request time and are
    not installed into the data folders.
    """
    _recover_presentation_assets_once()
    request = urlsplit(page)
    route = request.path.lstrip("/")
    params = parse_qs(request.query)
    aliases = {"index.html": "list.html", "dashboard.html": "feed.html", "tags/index.html": "tags.html"}
    route = aliases.get(route, route)
    links = _live_interface_links()
    if route == "crawler.html":
        return _archive_shell(
            "Crawler", _crawler_controls(page=True), links, active="Crawler",
            stylesheet="/assets/archive.css", script="/assets/archive.js",
        )
    if route == "graph.html":
        graph_data = graph_projection.build_graph_projection(
            CONTENT_ROOT, NEIGHBORHOODS_ROOT, max_nodes=1000, max_distance=2,
        )
        for node in graph_data.get("nodes", []):
            avatar = str(node.get("avatar_src") or "")
            if avatar:
                node["avatar_src"] = _archive_content_url(avatar)
            blog = str(node.get("id") or "")
            if blog:
                node["archive_href"] = "/app/blog.html?blog=" + quote(blog)
        graph_asset_version = f"{PRESENTATION_SCHEMA_VERSION}-{graph_projection.GRAPH_GENERATOR_VERSION}"
        return _archive_shell(
            "Explore - Graph", graph_projection.render_graph_content(graph_data), links, active="Graph",
            stylesheet="/assets/archive.css", script="/assets/archive.js",
            extra=(
                f'<link rel="stylesheet" href="/assets/graph-view.css?v={graph_asset_version}">'
                f'<script defer src="/assets/vendor/cytoscape.min.js?v={graph_asset_version}"></script>'
                f'<script defer src="/assets/vendor/layout-base.min.js?v={graph_asset_version}"></script>'
                f'<script defer src="/assets/vendor/cose-base.min.js?v={graph_asset_version}"></script>'
                f'<script defer src="/assets/vendor/cytoscape-layout-utilities.min.js?v={graph_asset_version}"></script>'
                f'<script defer src="/assets/vendor/cytoscape-fcose.min.js?v={graph_asset_version}"></script>'
                f'<script defer src="/assets/graph-view.js?v={graph_asset_version}"></script>'
            ),
        )
    pages: dict[str, tuple[str, Callable[[], str], str]] = {
        "list.html": ("Archive list", render_list_content, "List"),
        "feed.html": ("Feed", lambda: render_feed_content((params.get("pov") or [""])[0], params), "Feed"),
    }
    if route in pages:
        title, content, active = pages[route]
        return _archive_shell(title, content(), links, active=active, stylesheet="/assets/archive.css", script="/assets/archive.js")
    if route == "tags.html":
        return _archive_shell("Tags", render_tags_content((params.get("tag") or [""])[0]), links, active="Tags", stylesheet="/assets/archive.css", script="/assets/archive.js")
    if route == "neighborhoods.html":
        return _archive_shell("Neighborhoods", render_neighborhoods_content((params.get("target") or [""])[0]), links, active="Neighborhoods", stylesheet="/assets/archive.css", script="/assets/archive.js")
    if route == "blog.html":
        blog = (params.get("blog") or [""])[0]
        return _archive_shell("Blog", render_blog_content(blog), links, stylesheet="/assets/archive.css", script="/assets/archive.js")
    if route == "post.html":
        blog = (params.get("blog") or [""])[0]
        post_id = (params.get("post") or [""])[0]
        return _archive_shell("Post", render_post_content(blog, post_id), links, stylesheet="/assets/archive.css", script="/assets/archive.js")
    return None


def _static_archive_url(raw: str, destination: Path) -> str:
    """Translate a live source-owned URL into a file-relative archive URL."""
    if not raw or raw.startswith(('#', 'data:', 'mailto:', 'tel:', '//')):
        return raw
    # HTML escaping happens before this delivery adapter runs. Decode only for
    # route parsing; the returned URL is relative and will be escaped again by
    # the existing serializer context.
    raw = unescape(raw)
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc:
        return raw
    target: Path | None = None
    path = parsed.path
    if path.startswith('/assets/'):
        target = APP_ROOT / 'assets' / path.removeprefix('/assets/')
    archive_content_prefix = f'/Archive/{quote(ACTIVE_ARCHIVE_NAME)}/Content/'
    archive_network_prefix = f'/Archive/{quote(ACTIVE_ARCHIVE_NAME)}/Network/'
    if path.startswith(archive_content_prefix):
        target = CONTENT_ROOT / path.removeprefix(archive_content_prefix)
    elif path.startswith(archive_network_prefix):
        target = NETWORK_ROOT / path.removeprefix(archive_network_prefix)
    elif path.startswith('/Archive/Content/'):
        # Compatibility for old generated pages; new exports never emit it.
        target = CONTENT_ROOT / path.removeprefix('/Archive/Content/')
    elif path.startswith('/Archive/Network/'):
        target = NETWORK_ROOT / path.removeprefix('/Archive/Network/')
    elif path.startswith('/app/'):
        route = path.removeprefix('/app/')
        query = parse_qs(parsed.query)
        if route == 'blog.html' and query.get('blog'):
            target = canonical_archive_root(query['blog'][0]) / 'index.html'
        elif route == 'post.html' and query.get('blog') and query.get('post'):
            target = canonical_archive_root(query['blog'][0]) / 'posts' / f"{query['post'][0]}.html"
        elif route == 'tags.html' and query.get('tag'):
            target = APP_ROOT / 'tags' / f"{query['tag'][0]}.html"
        elif route == 'neighborhoods.html' and query.get('target'):
            target = APP_ROOT / 'neighborhoods' / f"{canonical_blog_name(query['target'][0])}.html"
        else:
            target = APP_ROOT / route
    if target is None:
        return raw
    try:
        relative = os.path.relpath(target, destination.parent).replace(os.sep, '/')
    except ValueError:
        return raw
    return urlunsplit(('', '', relative, parsed.query if target == APP_ROOT / path.removeprefix('/app/') else '', parsed.fragment))


def _staticize_html(html: str, destination: Path) -> str:
    """Apply only delivery-specific URL rebasing to source-owned HTML."""
    pattern = re.compile(r'(?P<attr>\b(?:href|src|poster|action)=(["\']))(?P<url>.*?)(?P<quote>["\'])', re.I)

    def replace(match: re.Match[str]) -> str:
        attr = match.group('attr')
        raw = match.group('url')
        return attr + _static_archive_url(raw, destination) + match.group('quote')

    return pattern.sub(replace, html)


def _static_navigation(destination: Path, active: str) -> list[tuple[str, str]]:
    links = {
        'Graph': APP_ROOT / 'graph.html',
        'List': APP_ROOT / 'list.html',
        'Feed': APP_ROOT / 'feed.html',
        'Tags': APP_ROOT / 'tags.html',
        'Neighborhoods': APP_ROOT / 'neighborhoods.html',
        'Crawler': APP_ROOT / 'crawler.html',
    }
    return [(label, os.path.relpath(path, destination.parent).replace(os.sep, '/')) for label, path in links.items()]


def _static_graph_html(destination: Path) -> str:
    graph_data = graph_projection.build_graph_projection(
        CONTENT_ROOT, NEIGHBORHOODS_ROOT, max_nodes=1000, max_distance=2,
    )
    for node in graph_data.get('nodes', []):
        avatar = str(node.get('avatar_src') or '')
        if avatar:
            node['avatar_src'] = os.path.relpath(CONTENT_ROOT / avatar, destination.parent).replace(os.sep, '/')
        blog = str(node.get('id') or '')
        if blog:
            node['archive_href'] = os.path.relpath(canonical_archive_root(blog) / 'index.html', destination.parent).replace(os.sep, '/')
    graph_asset_version = f'{PRESENTATION_SCHEMA_VERSION}-{graph_projection.GRAPH_GENERATOR_VERSION}'
    asset = lambda name: os.path.relpath(APP_ROOT / 'assets' / name, destination.parent).replace(os.sep, '/')
    return _archive_shell(
        'Explore - Graph',
        graph_projection.render_graph_content(graph_data),
        _static_navigation(destination, 'Graph'),
        active='Graph',
        stylesheet=asset('archive.css'),
        script=asset('archive.js'),
        extra=(
            f'<link rel="stylesheet" href="{escape(asset("graph-view.css"))}?v={graph_asset_version}">'
            f'<script defer src="{escape(asset("vendor/cytoscape.min.js"))}?v={graph_asset_version}"></script>'
            f'<script defer src="{escape(asset("vendor/layout-base.min.js"))}?v={graph_asset_version}"></script>'
            f'<script defer src="{escape(asset("vendor/cose-base.min.js"))}?v={graph_asset_version}"></script>'
            f'<script defer src="{escape(asset("vendor/cytoscape-layout-utilities.min.js"))}?v={graph_asset_version}"></script>'
            f'<script defer src="{escape(asset("vendor/cytoscape-fcose.min.js"))}?v={graph_asset_version}"></script>'
            f'<script defer src="{escape(asset("graph-view.js"))}?v={graph_asset_version}"></script>'
        ),
    )


def render_static_archive() -> None:
    """Export the source-owned reader into the disposable Archive tree."""
    _recover_presentation_assets_once()
    if ARCHIVE_ROOT.name == ACTIVE_ARCHIVE_NAME:
        if ARCHIVE_ROOT.is_dir():
            archive_manager.ensure_archive_manifest(ARCHIVE_ROOT, ACTIVE_ARCHIVE_NAME)
        else:
            archive_manager.create_archive(ARCHIVES_ROOT, ACTIVE_ARCHIVE_NAME)
    # App is generated presentation, never a hand-edited source tree.  Clear
    # only that generated subtree so removed pages/assets cannot survive a
    # regeneration; canonical content and network evidence remain untouched.
    if APP_ROOT.exists():
        shutil.rmtree(APP_ROOT)
    graph_slices_root = NETWORK_ROOT / "graph-slices"
    if graph_slices_root.exists():
        shutil.rmtree(graph_slices_root)
    APP_ROOT.mkdir(parents=True, exist_ok=True)
    NETWORK_ROOT.mkdir(parents=True, exist_ok=True)
    CONTENT_ROOT.mkdir(parents=True, exist_ok=True)
    ensure_shared_archive_assets()

    catalog = build_global_catalog()
    write_json_atomic(CONTENT_ROOT / 'catalog.json', catalog)
    tag_index = build_tag_index()
    write_json_atomic(CONTENT_ROOT / 'tag-index.json', tag_index)

    records_by_blog: dict[str, list[dict[str, Any]]] = {}
    for record in _canonical_local_records():
        records_by_blog.setdefault(str(record['_canonical_blog']), []).append(record)
    for blog, records in records_by_blog.items():
        _write_profile_snapshot(blog, records)
        (canonical_archive_root(blog) / 'index.html').write_text(
            _render_blog_page(blog, records), encoding='utf-8'
        )

    # Every reader page gets its content from live_interface_page().  The
    # static adapter only rebases URLs; it does not maintain a second renderer.
    for route in ('list.html', 'feed.html', 'tags.html', 'neighborhoods.html', 'crawler.html'):
        rendered = live_interface_page(route)
        if rendered is not None:
            output = APP_ROOT / route
            output.write_text(_staticize_html(rendered, output), encoding='utf-8')

    # Static readers cannot route query-string cursors through a server.  Keep
    # each generated window bounded and link the disposable window files
    # directly, while the live adapter continues to use opaque cursors.
    feed_window = APP_ROOT / 'feed.html'
    next_pattern = re.compile(r'href="/app/feed\.html\?([^"#]*cursor=[^"#]*)"')
    generated_window = 0
    current_query = ""
    while feed_window.is_file() and generated_window < 10000:
        source = live_interface_page('feed.html' + (('?' + current_query) if current_query else ''))
        if source is None:
            break
        match = next_pattern.search(source)
        output = feed_window if generated_window == 0 else APP_ROOT / f'feed-window-{generated_window:05d}.html'
        rendered = _staticize_html(source, output)
        if match:
            next_query = match.group(1)
            target_href = f'href="feed-window-{generated_window + 1:05d}.html"'
            rendered = rendered.replace('href="feed.html?' + next_query + '"', target_href)
            rendered = rendered.replace('href="feed.html?' + unescape(next_query) + '"', target_href)
        output.write_text(rendered, encoding='utf-8')
        if not match:
            break
        current_query = unescape(match.group(1))
        generated_window += 1

    tags_root = APP_ROOT / 'tags'
    tags_root.mkdir(parents=True, exist_ok=True)
    for item in tag_index.get('tags', []):
        page_id = str(item.get('page_id') or '')
        if not page_id:
            continue
        rendered = live_interface_page(f'tags.html?tag={quote(page_id)}')
        if rendered is not None:
            output = tags_root / f'{page_id}.html'
            output.write_text(_staticize_html(rendered, output), encoding='utf-8')

    neighborhoods_root = APP_ROOT / 'neighborhoods'
    neighborhoods_root.mkdir(parents=True, exist_ok=True)
    if NEIGHBORHOODS_ROOT.is_dir():
        for root in sorted(NEIGHBORHOODS_ROOT.iterdir()):
            if not root.is_dir():
                continue
            document = load_context_document(root.name)
            primary = str(document.get('primary_blog') or root.name)
            if primary.startswith('set-'):
                continue
            rendered = live_interface_page(f'neighborhoods.html?target={quote(primary)}')
            if rendered is not None:
                output = neighborhoods_root / f'{primary}.html'
                output.write_text(_staticize_html(rendered, output), encoding='utf-8')

    graph_data = graph_projection.build_graph_projection(CONTENT_ROOT, NEIGHBORHOODS_ROOT)
    graph_slices.write_graph_slices(graph_data, NETWORK_ROOT / 'graph-slices')
    write_json_atomic(NETWORK_ROOT / 'graph.json', graph_data)
    for output in (APP_ROOT / 'graph.html', NETWORK_ROOT / 'graph.html'):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(_static_graph_html(output), encoding='utf-8')

    archive.write_json_atomic(
        APP_ROOT / "presentation-state.json",
        {
            "generator_version": PRESENTATION_SCHEMA_VERSION,
            "dirty": False,
            "generated_at": time.time(),
            "runtime_identity": runtime_identity(),
            "presentation_identity": _presentation_identity(),
        },
    )



class CrawlerApplication(Application):
    """Compatibility name for the shared application orchestrator."""

    def __init__(self) -> None:
        super().__init__(sys.modules[__name__])


class ProgressRenderer:
    def __init__(self, status: CrawlerStatus, stream: Any = None, verbose: bool = False) -> None:
        self.status = status
        self.stream = stream or sys.stdout
        self.verbose = verbose
        self.terminal = bool(getattr(self.stream, "isatty", lambda: False)())
        self.compact_terminal = self.terminal and HOST_CAPABILITIES["compact_terminal"]
        self.live = self.terminal and not self.compact_terminal and os.environ.get("TERM") not in {None, "dumb"}
        try:
            columns = shutil.get_terminal_size(fallback=(80, 20)).columns
        except OSError:
            columns = 80
        self.width = max(20, columns - 1)
        self.compact_width = min(120, self.width)
        self.compact_line_active = False
        self.seen_warnings: set[str] = set()
        self.previous_lines = 0
        self.last_emit = 0.0

    def _compact_text(self) -> str:
        s = self.status
        limit = s.budget_limit or "-"
        action = s.current_action or "Waiting"
        return (
            f"{FOCUS_LABELS.get(s.focus, s.focus.title())} | "
            f"{s.budget_used}/{limit} saved | "
            f"Targets {s.target_posts} | "
            f"Breadth 1 {s.breadth1_posts} | "
            f"Breadth 2+ {s.breadth2plus_posts} | {action}"
        )

    def _fit(self, value: str, width: int | None = None) -> str:
        width = width or self.width
        if len(value) <= width:
            return value
        if width <= 3:
            return value[:width]
        return value[:width - 3] + "..."

    def _warning_lines(self) -> list[str]:
        lines = []
        for key, count in self.status.warning_counts.items():
            label = self.status.warning_labels.get(key, key)
            details = self.status.warning_details.get(key, [])
            suffix = f" ({', '.join(details[:3])})" if self.verbose and details else ""
            lines.append(f"{count} {label}{suffix}")
        return lines

    def _frame_lines(self) -> list[str]:
        s = self.status
        limit = s.budget_limit or "-"
        reason = s.current_reason
        if reason in {"target macro focus", "target discovery remains active", "target scouting remains active at current focus"}:
            reason = ""
        if self.width < 64:
            lines = [
                "TUMBLR CRAWL",
                self._fit(f"{s.target}  {s.budget_used}/{limit} saved"),
                self._fit(f"Focus {FOCUS_LABELS.get(s.focus, s.focus.title())} | {s.network}"),
                self._fit(f"T {s.target_posts}  B1 {s.breadth1_posts}  B2+ {s.breadth2plus_posts}"),
                self._fit(f"Context {s.context_bracketed} bracketed | {s.known_blogs} blogs"),
                self._fit(f"Now {s.current_action}"),
            ]
            if self.status.warning_counts:
                lines.append(self._fit("Warnings | " + " | ".join(self._warning_lines())))
            lines.append("Ctrl+C stop safely")
            return lines
        lines = [
            "TUMBLR NEIGHBORHOOD CRAWL",
            "",
            self._fit(f"{s.target}                              {s.budget_used} / {limit}"),
            self._fit(f"Focus  {FOCUS_LABELS.get(s.focus, s.focus.title())}        Network  {s.network}"),
            "",
            "HUNTS",
            self._fit(self._lane_line(s, "target", "Targets")),
            self._fit(self._lane_line(s, "depth1", "Breadth 1")),
            self._fit(self._lane_line(s, "depth2", "Breadth 2+")),
            "",
            self._fit(f"CONTEXT  {s.context_bracketed} bracketed | {s.context_incomplete} incomplete | {s.known_blogs} blogs scouted"),
            self._fit(f"NOW      {s.current_action}"),
        ]
        if reason:
            lines.append(self._fit(f"WHY      {reason}"))
        if self.status.warning_counts:
            lines.append("")
            lines.append("WARNINGS")
            lines.extend(self._fit(line) for line in self._warning_lines())
        lines.extend(["", "Ctrl+C  stop safely"])
        return lines

    def _pending_warning_messages(self) -> list[str]:
        return [warning for warning in self.status.warnings if warning not in self.seen_warnings]

    def event(self, message: str, *, warning: bool = False) -> None:
        if warning:
            self.status.warning(message)
        else:
            self.status.current_action = message
        self.render(force=True)

    def _render_ansi(self, lines: list[str]) -> None:
        pending = self._pending_warning_messages()
        if self.previous_lines:
            self.stream.write(f"\033[{self.previous_lines}A")
            for _ in range(self.previous_lines):
                self.stream.write("\r\033[2K\n")
        self.previous_lines = 0
        for warning in pending:
            self.stream.write(f"WARNING: {self._fit(warning)}\n")
            self.seen_warnings.add(warning)
        for line in lines:
            self.stream.write(self._fit(line) + "\n")
        self.previous_lines = len(lines)

    def _render_append_only(self) -> None:
        for warning in self._pending_warning_messages():
            self.stream.write(f"WARNING: {warning}\n")
            self.seen_warnings.add(warning)
        s = self.status
        limit = s.budget_limit or "-"
        self.stream.write(
            self._fit(
                f"{s.target} | {s.budget_used}/{limit} saved | "
                f"Targets: {s.target_posts} | Breadth 1: {s.breadth1_posts} | "
                f"Breadth 2+: {s.breadth2plus_posts} | {s.current_action}"
            ) + "\n"
        )

    def _render_compact(self) -> None:
        pending = self._pending_warning_messages()
        if pending:
            if self.compact_line_active:
                self.stream.write("\r" + (" " * self.compact_width) + "\r")
            for warning in pending:
                self.stream.write(f"WARNING: {warning}\n")
                self.seen_warnings.add(warning)
            self.compact_line_active = False
        text = self._compact_text()[:self.compact_width]
        self.stream.write("\r" + text.ljust(self.compact_width))
        self.compact_line_active = True

    def render(self, force: bool = False) -> None:
        now = time.time()
        if not force and not self.verbose and now - self.last_emit < 0.5:
            return
        try:
            if self.compact_terminal:
                self._render_compact()
                self.stream.flush()
                self.last_emit = now
                return
            if self.live:
                self._render_ansi(self._frame_lines())
            else:
                self._render_append_only()
            self.stream.flush()
        except (OSError, ValueError, AttributeError):
            self.live = False
            self.compact_terminal = False
            self.previous_lines = 0
            self.compact_line_active = False
        self.last_emit = now

    def finish(self) -> None:
        try:
            if self.compact_terminal and self.compact_line_active:
                self.stream.write("\n")
                self.stream.flush()
                self.compact_line_active = False
            elif self.live and self.previous_lines:
                self.stream.write("\n")
                self.stream.flush()
                self.previous_lines = 0
        except (OSError, ValueError, AttributeError):
            self.live = False
            self.compact_terminal = False
            self.previous_lines = 0
            self.compact_line_active = False

    @staticmethod
    def _lane_line(status: CrawlerStatus, lane: str, label: str) -> str:
        info = status.lane_status.get(lane)
        if info is None:
            return f"{label:<10} {status.saved_by_lane[lane]} saved   DISCOVERING"
        detail = info.state
        if info.state == "DISCOVERING" and info.discovering:
            detail += f" ({info.discovering} to scout)"
        elif info.state == "READY":
            detail += f" ({info.ready} ready)"
        elif info.state == "BLOCKED" and info.blocked_reason:
            detail += f" ({info.blocked_reason})"
        return f"{label:<10} {status.saved_by_lane[lane]} saved   {detail}"


def acquisition_budget_exhausted(status: CrawlerStatus) -> bool:
    active_limit = status.runtime.budget_limit if status.runtime else status.budget_limit
    return bool(active_limit and status.budget_used >= active_limit)


def _normalise_shares(values: dict[str, float]) -> dict[str, float]:
    clipped = {key: max(0.0, float(value)) for key, value in values.items()}
    total = sum(clipped.values())
    if total <= 0:
        return {"target": 0.0, "depth1": 100.0, "depth2": 0.0}
    return {key: value * 100.0 / total for key, value in clipped.items()}


def effective_focus_shares(config: dict[str, Any], focus_bias: float) -> dict[str, float]:
    profiles = config.get("focus_profiles", {})
    deep = profiles.get("deep", {"target_share": 80, "depth1_share": 15, "depth2_share": 5})
    balanced = profiles.get("balanced", {"target_share": 60, "depth1_share": 25, "depth2_share": 15})
    wide = profiles.get("wide", {"target_share": 45, "depth1_share": 35, "depth2_share": 20})
    outward = config.get("focus_endpoints", {}).get("outward", {"target_share": 0, "depth1_share": 60, "depth2_share": 40})
    points = ((0.0, deep), (0.5, balanced), (1.0, wide), (1.2, outward))
    bias = max(0.0, float(focus_bias))
    if bias >= points[-1][0]:
        left, right, fraction = points[-2][1], points[-1][1], 1.0
    else:
        left, right, fraction = points[0][1], points[1][1], 0.0
        for (left_bias, left_profile), (right_bias, right_profile) in zip(points, points[1:]):
            if left_bias <= bias <= right_bias:
                left, right = left_profile, right_profile
                fraction = (bias - left_bias) / (right_bias - left_bias) if right_bias != left_bias else 0.0
                break
    fields = {"target": "target_share", "depth1": "depth1_share", "depth2": "depth2_share"}
    return _normalise_shares({
        lane: float(left[field]) + (float(right[field]) - float(left[field])) * fraction
        for lane, field in fields.items()
    })


def _action_for_blog(primary: str, item: dict[str, Any], policy: dict[str, Any], lane: str, runtime: RuntimeControls | None = None) -> ActionCandidate:
    components = {key: 0.0 for key in SCORE_COMPONENTS}
    relationship = item.get("relationship_components", {})
    components["source_affinity"] = float(relationship.get("source_affinity", 0.0))
    components["audience_activity"] = float(relationship.get("audience_activity", 0.0))
    components["reciprocal_activity"] = float(relationship.get("reciprocal_activity", 0.0))
    count = int(item.get("current_sample_size", 0))
    rep = policy.get("representation_weights", {})
    components["representation"] = float(rep.get("underrepresented_blog_bonus", 0.0)) / (1.0 + count * float(rep.get("representation_decay", 1.0)))
    components["graph_depth"] = -float(item.get("distance", 0))
    components["scout_confidence"] = 1.0 if item.get("evidence") else 0.5
    reasons = []
    if components["source_affinity"]:
        reasons.append("source affinity")
    if components["audience_activity"]:
        reasons.append("audience activity")
    if components["reciprocal_activity"]:
        reasons.append("observed both ways")
    if components["representation"] > 0:
        reasons.append("underrepresented")
    if not reasons:
        reasons.append("scouted opportunity")
    multiplier = 1.0
    if item.get("runtime_role") == "support":
        multiplier = float(policy.get("context_weights", {}).get("support_ring_multiplier", 0.35))
    relationship_multiplier = runtime.relationship_multiplier if runtime else 1.0
    context_multiplier = runtime.context_multiplier if runtime else 1.0
    priority = (
        relationship_multiplier * (components["source_affinity"] + components["audience_activity"] + components["reciprocal_activity"])
        + context_multiplier * components["context_deficit"]
        + components["representation"] + components["graph_depth"] + components["scout_confidence"]
        + components["failure_penalty"]
    ) * multiplier
    return ActionCandidate(
        identity=f"acquire:{item['blog']}:next",
        kind="acquire_neighbor_post",
        resource_class="acquisition",
        lane=lane,
        graph_depth=int(item.get("distance", 0)),
        blog=item["blog"],
        anchor_role=item.get("runtime_role", "anchor"),
        reason_codes=reasons,
        score_components=components,
        priority=priority,
        planned_cost=1,
        consumes_budget=True,
    )


def context_action_candidates(document: dict[str, Any], policy: dict[str, Any], lane: str) -> list[ActionCandidate]:
    candidates = []
    context_weight = float(policy.get("context_weights", {}).get("immediate_context_missing", 0.0))
    for entry in document.get("context_deficits", {}).values():
        distance = next((int(item.get("distance", 0)) for item in document.get("blogs", []) if item.get("blog") == entry["blog"]), 0)
        entry_lane = "target" if distance == 0 else "depth1" if distance == 1 else "depth2"
        if entry_lane != lane or entry.get("anchor_role") != "anchor":
            continue
        for direction in ("before", "after"):
            side = entry[f"immediate_{direction}"]
            if side.get("state") != "missing":
                continue
            components = {key: 0.0 for key in SCORE_COMPONENTS}
            components["context_deficit"] = context_weight
            candidates.append(ActionCandidate(
                identity=f"context:{entry['blog']}:{entry['post_id']}:{direction}:1",
                kind="acquire_context_adjacent",
                resource_class="acquisition",
                lane=lane,
                graph_depth=distance,
                blog=entry["blog"],
                target_post_id=str(side["post_id"]),
                anchor_post_id=entry["post_id"],
                anchor_role="support",
                reason_codes=[f"missing immediate {direction} context"],
                evidence_refs=[side["evidence_ref"]] if side.get("evidence_ref") else [],
                score_components=components,
                priority=context_weight,
                planned_cost=1,
                consumes_budget=True,
            ))
    return candidates


def acquire_specific_post(state: BlogState, post_id: str, candidate: ActionCandidate | None = None) -> bool:
    """Acquire one already-observed post without consuming intervening feed records."""
    activate_blog_state(state)
    selected = []
    for source in state.feed_buffer:
        if str(source.get("id")) == str(post_id):
            selected.append(source)
            break
    if not selected or post_source_exists(int(post_id)):
        return False
    state.lane_role = "support"
    state.batch_limit = 1
    return bool(_process_source_ids(state, selected, candidate))


def run_strategy_capture(
    targets: tuple[str, ...], max_posts: int, mode: str, requested_depth: int | None,
    context_policy: dict[str, Any], focus: str | None, strategy_name: str,
    survey_node_limit: int | None, survey_request_limit: int | None,
    survey_max_distance: int | None, install_signal_handlers: bool,
    max_breadth: int | None = None,
    max_depth: int | None = None,
    capture_shape: tuple[CaptureShapePoint, ...] | None = None,
    capture_curve: tuple[CaptureCurvePoint, ...] | None = None,
) -> tuple[BlogState, dict[str, Any]]:
    """Run the single resolved capture-policy scheduler."""
    capture_policy = config.resolve_capture_policy(
        context_policy,
        strategy_name,
        max_posts=max_posts,
        max_breadth=(max_breadth if max_breadth is not None else survey_max_distance),
        max_depth=max_depth,
        capture_shape=capture_shape,
        capture_curve=capture_curve,
    )
    observation_limits = capture_policy.observation_limits
    if survey_node_limit is not None or survey_request_limit is not None:
        observation_limits = type(observation_limits)(
            survey_node_limit if survey_node_limit is not None else observation_limits.max_observed_blogs,
            survey_request_limit if survey_request_limit is not None else observation_limits.max_requests,
            observation_limits.max_breadth,
            observation_limits.continue_after_post_budget,
        )
        capture_policy = CapturePolicy(
            max_breadth=capture_policy.max_breadth,
            max_depth=capture_policy.max_depth,
            capture_shape=capture_policy.capture_shape,
            capture_curve=capture_policy.capture_curve,
            observation_limits=observation_limits,
            media_curve=capture_policy.media_curve,
            enrichment_curve=capture_policy.enrichment_curve,
            global_post_budget=capture_policy.global_post_budget,
            preset_hint=capture_policy.preset_hint,
        )
    envelope = observation_limits
    max_breadth = capture_policy.max_breadth
    strategy = capture_policy  # compatibility alias during policy migration
    global ACTIVE_REQUEST_PRESSURE, ACTIVE_RUNTIME, ACTIVE_STATUS, ACTIVE_PROGRESS_RENDERER, CAPTURE_PLAN
    ACTIVE_REQUEST_PRESSURE = network.RequestPressure(max_requests=envelope.max_requests or None)
    coverage_dir = coverage.coverage_root(NEIGHBORHOODS_ROOT, targets)
    coverage_state = coverage.load_coverage_state(NEIGHBORHOODS_ROOT, targets)
    documents = {target: load_context_document(target) for target in targets}
    states = {target: BlogState(target, canonical_archive_root(target), max_posts, lane="target", distance=0) for target in targets}
    run_id = uuid.uuid4().hex
    status = CrawlerStatus(
        target=", ".join(targets), focus=focus or strategy_name,
        max_breadth=max_breadth,
        network=str(NETWORK_PROFILE.get("label", "")), budget_limit=max_posts, run_id=run_id,
        started_at=time.time(), strategy=strategy_name,
        survey_node_limit=envelope.max_observed_blogs, survey_request_limit=envelope.max_requests,
        survey_max_distance=max_breadth,
    )
    status.capture_policy_snapshot = {
        "max_breadth": capture_policy.max_breadth,
        "max_depth": capture_policy.max_depth,
        "capture_shape": [
            {"breadth": point.breadth, "depth": point.depth}
            for point in capture_policy.capture_shape
        ],
        "capture_curve": [
            {"breadth": point.breadth, "history_posts": point.history_posts}
            for point in capture_policy.capture_curve
        ],
        "global_post_budget": capture_policy.global_post_budget,
    }
    runtime = RuntimeControls(
        focus_bias=0.5, budget_limit=max_posts, network_profile_id=str(NETWORK_PROFILE.get("id", "gentle")),
        strategy=strategy_name, survey_node_limit=envelope.max_observed_blogs,
        survey_request_limit=envelope.max_requests, survey_max_distance=max_breadth,
    )
    status.runtime = runtime
    ACTIVE_RUNTIME, ACTIVE_STATUS = runtime, status
    _set_lifecycle("running")
    renderer = ProgressRenderer(status)
    ACTIVE_PROGRESS_RENDERER = renderer
    primary = targets[0]
    if PENDING_CANCEL_EVENT.is_set():
        runtime.cancel_requested = True

    def timed(phase: str, operation: Callable[[], Any]) -> Any:
        """Small factual profiler retained in status, not a new subsystem."""
        started = time.perf_counter()
        try:
            return operation()
        finally:
            elapsed = time.perf_counter() - started
            sample = status.phase_timings.setdefault(phase, {"seconds": 0.0, "calls": 0})
            sample["seconds"] = float(sample["seconds"]) + elapsed
            sample["calls"] = int(sample["calls"]) + 1

    def checkpoint() -> None:
        coverage_state["counters"] = {
            "observed_blogs": len(known_blogs),
            "survey_requests": ACTIVE_REQUEST_PRESSURE.total if ACTIVE_REQUEST_PRESSURE else 0,
            "new_canonical_posts": status.budget_used,
        }
        coverage_state["targets"] = list(targets)
        coverage.save_coverage_state(NEIGHBORHOODS_ROOT, targets, coverage_state)
        for target, document in documents.items():
            save_context_document(target, document)
        suppressed = sum(int(document.get("frontier_diagnostics", {}).get("suppressed_by_observation_limit", 0)) for document in documents.values())
        status.frontier_diagnostics = {
            "max_breadth": max_breadth,
            "observed_blogs": len(known_blogs),
            "observation_limit": envelope.max_observed_blogs or None,
            "suppressed_by_observation_limit": suppressed,
        }

    def safe_enrichment(blog: str, distance: int) -> None:
        if blog in enriched:
            return
        plan = capture_policy.media_plan_for_breadth(distance)
        if not (plan.public_likes or plan.public_following):
            enriched.add(blog)
            return
        try:
            enrichment.capture_public_relationships(
                blog, coverage_dir, user_agent=USER_AGENT, pressure=ACTIVE_REQUEST_PRESSURE or network.RequestPressure(),
                likes=plan.public_likes, following=plan.public_following,
            )
        except Exception as exc:
            status.warning(f"optional relationship enrichment skipped for {blog}: {exc}", key="relationship-enrichment")
        enriched.add(blog)

    enriched: set[str] = set()
    known_blogs: set[str] = set(targets)
    expanded: set[str] = set()
    no_progress_cycles = 0
    target_turns_since_survey = 0
    for target in targets:
        document = documents[target]
        state = states[target]
        state.run_id = run_id
        state.progress = status
        document.update({
            "schema_version": 2, "primary_blog": target, "targets": list(targets),
            "strategy": strategy_name, "preset_hint": capture_policy.preset_hint,
            "capture_policy": {
                "max_breadth": capture_policy.max_breadth,
                "max_depth": capture_policy.max_depth,
                "capture_shape": [
                    {"breadth": point.breadth, "depth": point.depth}
                    for point in capture_policy.capture_shape
                ],
                "capture_curve": [
                    {"breadth": point.breadth, "history_posts": point.history_posts}
                    for point in capture_policy.capture_curve
                ],
            },
            "survey_enabled": observation_limits.max_breadth > 0,
            "coverage_distance": max_breadth,
            "crawl_status": "partial",
        })
        try:
            activate_blog_state(state)
            timed("repair", repair_interrupted_work)
            ensure_blog_stylesheets()
            # Rendering repairs are intentionally not a startup gate.  The
            # canonical source archive remains readable and the repair queue
            # can be handled independently after acquisition.
            status.repairs = len(pending_json_ids())
            timed("scout", lambda: scout_blog_page(state, target, document))
            # The acquisition adapter normally sets this marker itself. Keep
            # the scheduler invariant here as well so a successful adapter or
            # test double cannot be scouted forever on the next loop.
            state.scout_initialized = True
            reach_distance = max_breadth
            timed("frontier_recompute", lambda: recompute_context_document(target, document, context_policy, {
                "posts_per_context_blog": int(context_policy["defaults"]["posts_per_context_blog"]),
                "max_depth": reach_distance,
                "max_observed_blogs": envelope.max_observed_blogs,
            }))
        except Exception as exc:
            status.warning(f"target {target} scout/preservation setup failed: {exc}", key="target-setup")
        for item in document.get("blogs", []):
            known_blogs.add(str(item.get("blog")))
        timed("checkpoint", checkpoint)

    while True:
        if runtime.cancel_requested:
            break
        progress_before = (
            status.budget_used,
            status.scouted,
            len(known_blogs),
            ACTIVE_REQUEST_PRESSURE.total if ACTIVE_REQUEST_PRESSURE else 0,
        )
        remaining = (
            None
            if capture_policy.global_post_budget is None
            else capture_policy.global_post_budget - status.budget_used
        )
        budget_available = remaining is None or remaining > 0
        candidates: list[tuple[int, str, BlogState, dict[str, Any] | None, dict[str, Any] | None]] = []
        # Target candidates are assembled first, but a productive target must
        # not monopolize the resolved capture policy. Once scouting has
        # established a frontier, periodically yield to the nearest outward
        # candidate. This is one queue and one budget.
        for target in targets:
            state = states[target]
            target_current = len(_state_complete_ids(state))
            target_limit = capture_policy.history_limit_for_breadth(0)
            target_needs_capture = target_limit is None or target_current < target_limit
            if not state.exhausted and not state.blocked_for_run and budget_available and target_needs_capture:
                candidates.append((0, target, state, documents[target], None))
        # Recompute nearest discovery distance across every target document.
        context_by_blog: dict[str, tuple[int, str, dict[str, Any]]] = {}
        for target, document in documents.items():
            for item in document.get("blogs", []):
                blog = str(item.get("blog") or "")
                if not blog or blog in targets:
                    continue
                distance = int(item.get("distance", max_breadth + 1))
                reach_distance = max_breadth
                if distance > reach_distance:
                    continue
                previous = context_by_blog.get(blog)
                if previous is None or distance < previous[0]:
                    context_by_blog[blog] = (distance, target, item)
        for blog, (distance, owner, item) in context_by_blog.items():
            known_blogs.add(blog)
            if len(known_blogs) > envelope.max_observed_blogs and blog not in states:
                status.candidate_rejections.append({"blog": blog, "distance": distance, "reason": "survey node limit"})
                continue
            state = states.setdefault(blog, context_state(owner, item))
            state.run_id, state.progress, state.distance = run_id, status, distance
            if state.blocked_for_run:
                status.candidate_rejections.append({
                    "blog": blog,
                    "distance": distance,
                    "reason": "source blocked for this run",
                })
                continue
            current_sample = int(item.get("current_sample_size", 0))
            desired_limit = capture_policy.history_limit_for_breadth(distance)
            needs_capture = desired_limit is None or current_sample < desired_limit
            # A zero curve value leaves the node observable without creating a
            # canonical acquisition candidate.
            if not state.exhausted and blog not in expanded:
                candidates.append((1 + distance, blog, state, documents[owner], item))
            elif (
                not state.exhausted
                and needs_capture
                and (budget_available or envelope.continue_after_post_budget)
            ):
                candidates.append((1 + distance, blog, state, documents[owner], item))
            elif state.exhausted:
                status.candidate_rejections.append({"blog": blog, "distance": distance, "reason": "source exhausted"})
            elif not needs_capture:
                status.candidate_rejections.append({"blog": blog, "distance": distance, "reason": "capture curve reached"})
            else:
                status.candidate_rejections.append({"blog": blog, "distance": distance, "reason": "post budget unavailable"})
        if not candidates:
            break
        _priority, blog, state, document, item = _select_fair_strategy_candidate(
            candidates, target_turns_since_survey
        )
        trace_strategy_candidates(status, candidates, blog, item, strategy)
        if item is None:
            target_turns_since_survey += 1
        else:
            target_turns_since_survey = 0
        distance = 0 if item is None else int(item.get("distance", state.distance))
        plan = capture_policy.media_plan_for_breadth(distance)
        CAPTURE_PLAN = plan
        state.capture_plan = plan
        desired_limit = capture_policy.history_limit_for_breadth(distance)
        current_history_count = 0 if item is None else int(item.get("current_sample_size", 0))
        candidate = ActionCandidate(
            identity=f"strategy:{run_id}:{blog}", kind="acquire_canonical_post",
            resource_class="canonical",
            lane="policy",
            breadth=distance, blog=blog, priority=float(100 - distance), planned_cost=1 if budget_available else 0,
            consumes_budget=budget_available,
            is_target=(item is None or blog in targets),
            current_history_count=current_history_count,
            desired_history_count=desired_limit,
            global_remaining=remaining,
            capture_policy_snapshot={
                "max_breadth": capture_policy.max_breadth,
                "max_depth": capture_policy.max_depth,
                "capture_shape": [
                    {"breadth": point.breadth, "depth": point.depth}
                    for point in capture_policy.capture_shape
                ],
                "capture_curve": [
                    {"breadth": point.breadth, "history_posts": point.history_posts}
                    for point in capture_policy.capture_curve
                ],
            },
            reason_codes=["capture curve"],
        )
        status.set_action(candidate)
        try:
            activate_blog_state(state)
            timed("repair", repair_interrupted_work)
            if not state.scout_initialized:
                if envelope.max_requests and (ACTIVE_REQUEST_PRESSURE and ACTIVE_REQUEST_PRESSURE.total >= envelope.max_requests):
                    break
                timed("scout", lambda: scout_blog_page(state, blog if item is None else (document.get("primary_blog") or primary), document))
                state.scout_initialized = True
                expanded.add(blog)
                if item is not None:
                    reach_distance = max_breadth
                    timed("frontier_recompute", lambda: recompute_context_document(
                        document.get("primary_blog") or primary,
                        document,
                        context_policy,
                        {
                            "posts_per_context_blog": int(context_policy["defaults"]["posts_per_context_blog"]),
                            "max_depth": reach_distance,
                            "max_observed_blogs": envelope.max_observed_blogs,
                        },
                    ))
            elif budget_available and not state.exhausted:
                state.batch_limit = PROCESS_BATCH if remaining is None else min(PROCESS_BATCH, remaining)
                if item is None:
                    outward_ready = any(value[4] is not None for value in candidates)
                    state.batch_limit = 1 if outward_ready else state.batch_limit
                    target_current = len(_state_complete_ids(state))
                    target_limit = capture_policy.history_limit_for_breadth(0)
                    if target_limit is not None:
                        state.batch_limit = min(state.batch_limit, max(0, target_limit - target_current))
                    timed("canonical_acquisition", lambda: acquire_one_target_batch(state, candidate))
                else:
                    current = int(item.get("current_sample_size", 0))
                    desired_limit = capture_policy.history_limit_for_breadth(distance)
                    desired = current + 1 if desired_limit is None else min(desired_limit, current + 1)
                    timed("canonical_acquisition", lambda: acquire_one_context_batch(state, desired, candidate))
            timed("enrichment", lambda: safe_enrichment(blog, distance))
            if item is not None:
                item["nearest_target_breadth"] = distance
                item["capture_tier"] = plan.media_quality
                item["status"] = "preserved" if state.acquired_this_run else "observed"
                sync_context_state(document.get("primary_blog", primary), document, state, int(item.get("configured_cap") or 0))
            timed("checkpoint", checkpoint)
        except BlogSourceFailure as exc:
            mark_blog_source_failure(
                document.get("primary_blog") or primary,
                document,
                state,
                exc,
                status,
            )
            timed("checkpoint", checkpoint)
        status.scouted = sum(value.scouted for value in states.values())
        status.known_blogs = len(known_blogs)
        progress_after = (
            status.budget_used,
            status.scouted,
            len(known_blogs),
            ACTIVE_REQUEST_PRESSURE.total if ACTIVE_REQUEST_PRESSURE else 0,
        )
        if progress_after == progress_before:
            no_progress_cycles += 1
        else:
            no_progress_cycles = 0
        if no_progress_cycles >= 3:
            status.warning("No eligible crawler progress; finalizing safely")
            status.current_action = "No progress; finalizing"
            break
        if capture_policy.global_post_budget is not None and status.budget_used >= capture_policy.global_post_budget and not envelope.continue_after_post_budget:
            break
    stopped = runtime.cancel_requested or PENDING_CANCEL_EVENT.is_set()
    if stopped:
        completion_reason = "stopped safely before all requested work completed"
    elif capture_policy.global_post_budget is not None and status.budget_used >= capture_policy.global_post_budget:
        completion_reason = "global post budget exhausted"
    elif all(capture_policy.history_limit_for_breadth(0) == 0 for _target in targets):
        completion_reason = "policy requests observation only at breadth 0"
    elif int(status.frontier_diagnostics.get("suppressed_by_observation_limit", 0)) > 0:
        completion_reason = (
            "observation limit reached; "
            f"{status.frontier_diagnostics['suppressed_by_observation_limit']} frontier candidates suppressed"
        )
    elif any(state.blocked_for_run for state in states.values()):
        completion_reason = "source unavailable or fetch failed; other runnable work exhausted"
    elif all(
        capture_policy.history_limit_for_breadth(0) is not None
        and len(_state_complete_ids(states[target])) >= capture_policy.history_limit_for_breadth(0)
        for target in targets
    ):
        completion_reason = "capture curve reached and frontier exhausted"
    elif all(states[target].exhausted for target in targets):
        completion_reason = "target source exhausted"
    else:
        completion_reason = "no remaining runnable work"
    for document in documents.values():
        if stopped:
            crawl_status = "stopped"
        elif status.source_failures or any(state.blocked_for_run for state in states.values()):
            crawl_status = "complete_with_warnings"
        else:
            crawl_status = "complete"
        document["crawl_status"] = crawl_status
        document["run_status"] = {
            "run_id": run_id,
            "started_at": status.started_at,
            "finished_at": time.time(),
            "budget_limit": status.budget_limit,
            "budget_used": status.budget_used,
            **summary_fields(status),
            "scouted": status.scouted,
            "source_failures": list(status.source_failures),
            "blocked_sources": [failure["blog"] for failure in status.source_failures],
            "current_action": "Stopped safely" if stopped else status.current_action,
            "completion_reason": completion_reason,
            "strategy": strategy_name,
            "capture_policy": dict(status.capture_policy_snapshot),
            "candidate_trace": list(status.candidate_trace),
            "candidate_rejections": list(status.candidate_rejections),
            "frontier_diagnostics": dict(status.frontier_diagnostics),
            "phase_timings": {key: dict(value) for key, value in status.phase_timings.items()},
        }
    timed("checkpoint", checkpoint)
    status.finished_at = time.time()
    status.current_action = "Stopped safely" if stopped else "Complete"
    status.completion_reason = completion_reason
    status.current_reason = "Cancellation requested; durable work was preserved." if stopped else completion_reason
    ACTIVE_REQUEST_PRESSURE = None
    renderer.finish()
    ACTIVE_RUNTIME = None
    ACTIVE_PROGRESS_RENDERER = None
    _set_lifecycle("stopped" if stopped else "complete")
    return states[primary], documents[primary]


def _select_fair_strategy_candidate(
    candidates: list[tuple[int, str, BlogState, dict[str, Any] | None, dict[str, Any] | None]],
    target_turns_since_frontier: int,
) -> tuple[int, str, BlogState, dict[str, Any] | None, dict[str, Any] | None]:
    """Select one action from the resolved policy queue.

    The fixed interval is scheduler mechanics, not a preset semantic.  The
    candidate itself carries the resolved curve and breadth facts.
    """
    turns = int(target_turns_since_frontier)
    interval = 3
    outward_candidates = [value for value in candidates if value[4] is not None]
    if outward_candidates and turns >= interval:
        # Give each newly observed blog one turn before deepening an already
        # productive blog. This is generic queue fairness, not a preset ring.
        return min(
            outward_candidates,
            key=lambda value: (bool(getattr(value[2], "acquired_this_run", 0)), value[0], value[1]),
        )
    return min(candidates, key=lambda value: (value[0], value[1]))


def run_incremental_capture(
    primary: str,
    max_posts: int,
    full_res: bool,
    mode: str,
    requested_depth: int | None,
    context_policy: dict[str, Any],
    focus: str | None = None,
    install_signal_handlers: bool = True,
    targets: tuple[str, ...] | None = None,
    strategy_name: str | None = None,
    survey_node_limit: int | None = None,
    survey_request_limit: int | None = None,
    survey_max_distance: int | None = None,
    max_breadth: int | None = None,
    max_depth: int | None = None,
    capture_shape: tuple[CaptureShapePoint, ...] | None = None,
    capture_curve: tuple[CaptureCurvePoint, ...] | None = None,
) -> tuple[BlogState, dict[str, Any]]:
    """Translate legacy request fields once, then enter the sole scheduler."""
    target_set = tuple(dict.fromkeys(targets or (primary,)))
    selected = strategy_name or {"none": "archive", "nearby": "neighborhood", "explore": "explore"}.get(mode, "explore")
    effective_breadth = max_breadth
    if effective_breadth is None and strategy_name is None and requested_depth is not None:
        effective_breadth = requested_depth
    return run_strategy_capture(
        target_set, max_posts, mode, requested_depth, context_policy, focus,
        selected, survey_node_limit, survey_request_limit, survey_max_distance,
        install_signal_handlers, effective_breadth, max_depth, capture_shape, capture_curve,
    )


def main(argv: list[str] | None = None, *, install_signal_handlers: bool = True) -> int:
    global ACTIVE_PROGRESS_RENDERER, ACTIVE_RUNTIME
    parser = argparse.ArgumentParser(
        prog="tumblr-scraper",
        description="Incrementally archive a public Tumblr blog.",
    )
    parser.add_argument("username", type=canonical_username)
    parser.add_argument(
        "max_posts",
        nargs="?",
        type=nonnegative_int,
        default=300,
        help="maximum Tumblr feed records to inspect; 0 means unbounded (default: 300)",
    )
    parser.add_argument(
        "--full-res",
        action="store_true",
        help="download the largest Tumblr-provided photo variant",
    )
    parser.add_argument(
        "--profile",
        metavar="PROFILE_ID",
        help="network aggression profile from network-policy.json",
    )
    parser.add_argument(
        "--context",
        choices=("none", "nearby", "explore"),
        default="explore",
        help="preserve bounded surrounding public context",
    )
    parser.add_argument(
        "--strategy",
        choices=("archive", "neighborhood", "explore", "survey"),
        help="Preserve-to-Survey coverage point; legacy context flags remain compatible",
    )
    parser.add_argument(
        "--targets",
        help="comma-separated additional target blogs for a shared coverage run",
    )
    parser.add_argument("--survey-max-distance", type=nonnegative_int, help=argparse.SUPPRESS)
    parser.add_argument("--survey-node-limit", type=nonnegative_int, help=argparse.SUPPRESS)
    parser.add_argument("--survey-request-limit", type=nonnegative_int, help=argparse.SUPPRESS)
    parser.add_argument("--max-breadth", type=nonnegative_int, help=argparse.SUPPRESS)
    parser.add_argument("--max-depth", type=nonnegative_int, help=argparse.SUPPRESS)
    parser.add_argument("--capture-curve", help=argparse.SUPPRESS)
    parser.add_argument(
        "--focus",
        choices=("deep", "balanced", "wide", "neighbors"),
        help="where to spend crawler effort; default comes from context-policy.json",
    )
    parser.add_argument(
        "--context-depth",
        type=nonnegative_int,
        help="maximum graph distance for --context explore",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show raw tumblr-backup output and diagnostic milestones",
    )
    parser.add_argument(
        "--compact-terminal",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)
    if args.compact_terminal:
        set_host_capabilities(compact_terminal=True, keyboard_controls=False)
    global VERBOSE
    VERBOSE = bool(args.verbose)
    try:
        custom_curve = None
        if args.capture_curve:
            try:
                custom_curve = []
                for raw_point in args.capture_curve.split(","):
                    raw_breadth, separator, raw_posts = raw_point.partition(":")
                    if not separator:
                        raise ValueError("expected breadth:history_posts")
                    custom_curve.append({
                        "breadth": int(raw_breadth),
                        "history_posts": None if raw_posts == "" else int(raw_posts),
                    })
            except (TypeError, ValueError) as exc:
                parser.error(f"crawl configuration: invalid --capture-curve ({exc})")
        request = build_crawl_request(
            args.username,
            max_posts=args.max_posts,
            context=args.context,
            context_depth=args.context_depth,
            focus=args.focus,
            profile_id=args.profile,
            full_res=args.full_res,
            strategy=args.strategy,
            targets=tuple([args.username] + [item.strip() for item in (args.targets or "").split(",") if item.strip()]),
            survey_max_distance=args.survey_max_distance,
            survey_node_limit=args.survey_node_limit,
            survey_request_limit=args.survey_request_limit,
            max_breadth=args.max_breadth,
            max_depth=args.max_depth,
            capture_curve=custom_curve,
        )
    except PolicyError as exc:
        parser.error(f"crawl configuration: {exc}")
    try:
        context_policy = load_context_policy()
        context_config(context_policy, request.context, request.context_depth, request.focus)
    except PolicyError as exc:
        parser.error(f"context policy: {exc}")
    selected_strategy = request.strategy or {"none": "archive", "nearby": "neighborhood", "explore": "explore"}.get(request.context, "explore")
    resolved_policy = config.resolve_capture_policy(
        context_policy,
        selected_strategy,
        max_posts=request.max_posts,
        max_breadth=(request.max_breadth if request.max_breadth is not None else request.survey_max_distance),
        max_depth=request.max_depth,
        capture_shape=request.capture_shape,
        capture_curve=request.capture_curve,
    )
    selected_capture = resolved_policy.media_plan_for_breadth(0)
    configure(request.target, request.max_posts, full_res=request.full_res, capture_plan=selected_capture, profile=resolve_network_profile(load_network_policy(), request.profile_id))

    print("Tumblr Scraper")
    print(f"Folder: {OUT}")
    if MAX_POSTS:
        print(f"Inspection limit: newest {MAX_POSTS} posts maximum per run")
    else:
        print("Inspection limit: unbounded")
    print(f"Network aggression: {NETWORK_PROFILE['label']}")
    print(f"  {NETWORK_PROFILE['description']}")
    print(f"Crawl focus: {FOCUS_LABELS[request.focus or context_policy.get('default_focus', 'balanced')]}")

    try:
        ensure_tumblr_backup()
        primary_state, document = run_incremental_capture(
            request.target,
            request.max_posts,
            request.full_res,
            request.context,
            request.context_depth,
            context_policy,
            request.focus,
            install_signal_handlers=install_signal_handlers,
            targets=request.targets,
            strategy_name=request.strategy,
            survey_node_limit=request.survey_node_limit,
            survey_request_limit=request.survey_request_limit,
            survey_max_distance=request.survey_max_distance,
            max_breadth=request.max_breadth,
            max_depth=request.max_depth,
            capture_shape=request.capture_shape,
            capture_curve=request.capture_curve,
        )
        failures = list(document.get("run_status", {}).get("source_failures", []))
        if failures:
            print("\nBACKUP COMPLETE WITH WARNINGS")
            for failure in failures:
                code = f"HTTP {failure['code']}" if failure.get("code") is not None else failure.get("kind", "failure")
                print(f"Skipped {failure['blog']} ({code}); continuing")
            print("Source JSON already saved is safe; blocked sources will be retried on a later run.")
        else:
            print("\nBACKUP COMPLETE")
        print(f"{document['run_status']['budget_used']} new posts preserved")
        print("Open the live interface to inspect the archive data.")
        return 0

    except KeyboardInterrupt:
        _set_lifecycle("finalizing")
        if ACTIVE_PROGRESS_RENDERER is not None:
            ACTIVE_PROGRESS_RENDERER.finish()
        ACTIVE_PROGRESS_RENDERER = None
        ACTIVE_RUNTIME = None
        sys.stdout.write("\n")
        sys.stdout.flush()
        try:
            document = load_context_document(args.username)
            document["crawl_status"] = "partial"
            save_context_document(args.username, document)
        except Exception:
            pass
        print(
            "\nStopped. Source JSON is written atomically; "
            "unfinished target and context posts will be repaired first next run."
        )
        return 130

    except TumblrRateLimitedError:
        _set_lifecycle("failed")
        if ACTIVE_PROGRESS_RENDERER is not None:
            ACTIVE_PROGRESS_RENDERER.finish()
        ACTIVE_PROGRESS_RENDERER = None
        ACTIVE_RUNTIME = None
        sys.stdout.write("\n")
        sys.stdout.flush()
        print(
            "\nTumblr is limiting requests.\n\n"
            f"Anything already saved is safe in: {OUT}\n"
            "You can run the backup again later.",
            file=sys.stderr,
        )
        return 1

    except Exception as exc:
        _set_lifecycle("failed")
        if ACTIVE_PROGRESS_RENDERER is not None:
            ACTIVE_PROGRESS_RENDERER.finish()
        ACTIVE_PROGRESS_RENDERER = None
        ACTIVE_RUNTIME = None
        sys.stdout.write("\n")
        sys.stdout.flush()
        print(f"\nBACKUP FAILED: {exc}", file=sys.stderr)
        print(
            f"Anything already saved is still in: {OUT}\n"
            "Run the script again; pending JSON will be processed first.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
