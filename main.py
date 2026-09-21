#!/usr/bin/env python3
"""Incremental Tumblr public-feed scraper.

What it does:
1. Reads a blog's public Tumblr JSON feed newest-first.
2. Stops as soon as it reaches posts already saved locally (incremental).
3. Converts new public-feed records into the JSON shape expected by
   tumblr-backup 1.0.7, while embedding the original public-feed record.
4. Lets tumblr-backup do the actual HTML/media/archive work.

Archives are written below ``Backups/<blog-name>/`` next to this script.
"""

from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
from datetime import timezone
from email.utils import parsedate_to_datetime
import json
import math
import os
import random
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass, field
from html import escape, unescape
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urljoin, urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen

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

BASE_DIR = Path(__file__).resolve().parent
BACKUPS_DIR = BASE_DIR / "Backups"
GLOBAL_CSS = BASE_DIR / "global.css"
SOURCE_ASSET_DIR = BASE_DIR / "assets"
SOURCE_ARCHIVE_CSS = SOURCE_ASSET_DIR / "archive.css"
SOURCE_ARCHIVE_JS = SOURCE_ASSET_DIR / "archive.js"
NETWORK_POLICY_FILE = BASE_DIR / "network-policy.json"
CONTEXT_POLICY_FILE = BASE_DIR / "context-policy.json"
NEIGHBORHOODS_DIR = BASE_DIR / "Neighborhoods"
BLOG = ""
BLOG_HOST = ""
OUT = Path()
JSON_DIR = Path()
PAGE_SIZE = 50
PRESENTATION_PAGE_SIZE = 30
MAX_POSTS = 300
PROCESS_BATCH = 25
FULL_RES = False
NETWORK_PROFILE: dict[str, Any] = {}
ACTIVE_RUNTIME: RuntimeControls | None = None
ACTIVE_PROGRESS_RENDERER: Any = None
VERBOSE = False
USER_AGENT = "PuppetBackup/1.1 (+personal archival copy)"
FOCUS_LABELS = {"deep": "Deep", "balanced": "Balanced", "wide": "Wide", "neighbors": "Neighbors"}
SCORE_COMPONENTS = (
    "source_affinity", "audience_activity", "reciprocal_activity",
    "context_deficit", "representation", "graph_depth",
    "scout_confidence", "failure_penalty",
)

MAX_POLICY_DELAY_SECONDS = 300
MAX_POLICY_WORKERS = 8


def is_android_runtime() -> bool:
    markers = (
        os.environ.get("ANDROID_ARGUMENT", ""),
        os.environ.get("ANDROID_ROOT", ""),
        os.environ.get("ANDROID_DATA", ""),
        sys.executable,
        sys.prefix,
    )
    return sys.platform == "android" or any("pydroid" in value.lower() for value in markers if value)


@dataclass
class BlogState:
    """Durable-in-memory state for one sequentially scheduled blog."""

    blog: str
    out: Path
    max_posts: int
    role: str = "primary"
    distance: int = 0
    observed_names: list[str] = field(default_factory=list)
    observed_urls: list[str] = field(default_factory=list)
    feed_start: int = 0
    feed_total: int | None = None
    feed_blog: dict[str, Any] = field(default_factory=dict)
    feed_buffer: list[dict[str, Any]] = field(default_factory=list)
    first_feed_request: bool = True
    reached_existing: bool = False
    exhausted: bool = False
    inspected: int = 0
    completed: int = 0
    phase: str = "refresh"
    target_size: int = 0
    status: str = "queued"
    error: str = ""
    scouted: int = 0
    queued: int = 0
    lane: str = "target"
    acquired_this_run: int = 0
    run_id: str = ""
    progress: Any = None
    batch_limit: int | None = None
    scout_buffer_recorded: bool = False
    lane_role: str = "anchor"
    active_candidate: ActionCandidate | None = None
    blocked_for_run: bool = False
    failure_kind: str = ""
    failure_code: int | None = None
    failure_operation: str = ""
    render_pending: bool = False

    @property
    def json_dir(self) -> Path:
        return self.out / "json"

    @property
    def host(self) -> str:
        return f"{self.blog}.tumblr.com"


class PolicyError(RuntimeError):
    """The bundled network policy is missing or invalid."""


class TumblrRateLimitedError(RuntimeError):
    """The public feed remains rate limited after the bounded retry."""


class BlogSourceFailure(RuntimeError):
    """A failure that makes one blog unsafe or unavailable for this run."""

    def __init__(
        self,
        blog: str,
        operation: str,
        message: str,
        *,
        code: int | None = None,
        kind: str = "unavailable",
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.blog = blog
        self.operation = operation
        self.code = code
        self.kind = kind
        self.retryable = retryable


class BlogRenderFailure(BlogSourceFailure):
    """Local rendering failed after source work was preserved."""


@dataclass
class ActionCandidate:
    identity: str
    kind: str
    resource_class: str
    lane: str
    graph_depth: int
    blog: str | None = None
    target_post_id: str | None = None
    anchor_post_id: str | None = None
    anchor_role: str = "anchor"
    reason_codes: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    score_components: dict[str, float] = field(default_factory=dict)
    priority: float = 0.0
    planned_cost: int = 0
    actual_cost: int = 0
    consumes_budget: bool = False


@dataclass
class ScoutOpportunity:
    kind: str
    blog: str | None = None
    post_id: str | None = None
    anchor_post_id: str | None = None
    graph_depth: int = 0
    evidence_refs: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    confidence: float = 0.0


@dataclass
class RuntimeControls:
    focus_bias: float
    budget_limit: int
    relationship_multiplier: float = 1.0
    context_multiplier: float = 1.0
    network_profile_id: str = "gentle"
    cancel_requested: bool = False
    cancel_count: int = 0
    changes: list[dict[str, Any]] = field(default_factory=list)

    def update(self, name: str, value: Any, source: str = "runtime") -> None:
        old = getattr(self, name)
        if old == value:
            return
        setattr(self, name, value)
        self.changes.append({
            "name": name,
            "old": old,
            "new": value,
            "source": source,
            "at": time.time(),
        })


class RuntimeKeyController:
    """Best-effort, action-boundary controls with a non-interactive fallback."""

    def __init__(self, runtime: RuntimeControls, status: "CrawlerStatus", stream: Any = None) -> None:
        self.runtime = runtime
        self.status = status
        self.stream = stream or sys.stdin
        self.enabled = bool(getattr(self.stream, "isatty", lambda: False)()) and not is_android_runtime()

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
        if key == "d":
            self.runtime.update("focus_bias", 0.0, "keyboard")
            self.status.focus = "deep"
        elif key == "b":
            self.runtime.update("focus_bias", 0.5, "keyboard")
            self.status.focus = "balanced"
        elif key == "w":
            self.runtime.update("focus_bias", 1.0, "keyboard")
            self.status.focus = "wide"
        elif key == "n":
            self.runtime.update("focus_bias", 1.2, "keyboard")
            self.status.focus = "neighbors"
        elif key == "]":
            self.runtime.update("context_multiplier", min(4.0, self.runtime.context_multiplier + 0.25), "keyboard")
        elif key == "[":
            self.runtime.update("context_multiplier", max(0.0, self.runtime.context_multiplier - 0.25), "keyboard")
        elif key == ".":
            self.runtime.update("relationship_multiplier", min(4.0, self.runtime.relationship_multiplier + 0.25), "keyboard")
        elif key == ",":
            self.runtime.update("relationship_multiplier", max(0.0, self.runtime.relationship_multiplier - 0.25), "keyboard")
        elif key == "+":
            self.runtime.update("budget_limit", max(self.runtime.budget_limit, self.status.budget_used) + 25, "keyboard")
        elif key == "-":
            self.runtime.update("budget_limit", max(self.status.budget_used, self.runtime.budget_limit - 25), "keyboard")
        elif key in {"g", "r", "u"}:
            profile_id = {"g": "gentle", "r": "normal", "u": "urgent"}[key]
            try:
                profile = resolve_network_profile(load_network_policy(), profile_id)
            except PolicyError:
                return
            global NETWORK_PROFILE
            NETWORK_PROFILE = dict(profile)
            self.runtime.update("network_profile_id", profile_id, "keyboard")
            self.status.network = str(profile.get("label", profile_id))
        elif key == "q":
            self.runtime.update("cancel_requested", True, "keyboard")


def request_graceful_cancel(_signum: int, _frame: Any) -> None:
    if ACTIVE_RUNTIME is not None:
        if ACTIVE_RUNTIME.cancel_requested:
            raise KeyboardInterrupt
        ACTIVE_RUNTIME.cancel_count += 1
        ACTIVE_RUNTIME.update("cancel_requested", True, source="SIGINT")


def _require_keys(value: dict[str, Any], expected: set[str], context: str) -> None:
    missing = sorted(expected - set(value))
    unknown = sorted(set(value) - expected)
    if missing or unknown:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise PolicyError(f"{context} has invalid fields: {'; '.join(details)}")


def _finite_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PolicyError(f"{field} must be a finite number")
    number = float(value)
    if number < 0 or number > MAX_POLICY_DELAY_SECONDS:
        raise PolicyError(f"{field} must be between 0 and {MAX_POLICY_DELAY_SECONDS}")
    return number


def load_network_policy(path: Path | None = None) -> dict[str, Any]:
    """Load and strictly validate the bundled version-1 network policy."""
    policy_path = NETWORK_POLICY_FILE if path is None else Path(path)
    try:
        with policy_path.open(encoding="utf-8") as policy_file:
            policy = json.load(policy_file)
    except FileNotFoundError as exc:
        raise PolicyError(f"network policy file not found: {policy_path}") from exc
    except json.JSONDecodeError as exc:
        raise PolicyError(f"network policy is not valid JSON: {exc.msg}") from exc
    except OSError as exc:
        raise PolicyError(f"could not read network policy: {exc}") from exc

    if not isinstance(policy, dict):
        raise PolicyError("network policy root must be an object")
    _require_keys(policy, {"version", "default_profile", "profiles"}, "network policy")
    if type(policy["version"]) is not int or policy["version"] != 1:
        raise PolicyError("network policy version must be integer 1")
    if not isinstance(policy["default_profile"], str) or not policy["default_profile"]:
        raise PolicyError("default_profile must be a non-empty string")
    if not isinstance(policy["profiles"], list) or not policy["profiles"]:
        raise PolicyError("profiles must be a non-empty list")

    profile_ids: set[str] = set()
    normalized_profiles: list[dict[str, Any]] = []
    profile_keys = {
        "id",
        "label",
        "description",
        "feed_delay_seconds",
        "feed_jitter_seconds",
        "media_workers",
    }
    for index, profile in enumerate(policy["profiles"], start=1):
        context = f"profile {index}"
        if not isinstance(profile, dict):
            raise PolicyError(f"{context} must be an object")
        _require_keys(profile, profile_keys, context)
        profile_id = profile["id"]
        if not isinstance(profile_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", profile_id):
            raise PolicyError(f"{context}.id must be a lowercase ASCII slug")
        if profile_id in profile_ids:
            raise PolicyError(f"duplicate profile id: {profile_id}")
        profile_ids.add(profile_id)
        for field in ("label", "description"):
            if not isinstance(profile[field], str) or not profile[field].strip():
                raise PolicyError(f"{context}.{field} must be a non-empty string")
        delay = _finite_number(profile["feed_delay_seconds"], f"{context}.feed_delay_seconds")
        jitter = _finite_number(profile["feed_jitter_seconds"], f"{context}.feed_jitter_seconds")
        workers = profile["media_workers"]
        if type(workers) is not int or not 1 <= workers <= MAX_POLICY_WORKERS:
            raise PolicyError(f"{context}.media_workers must be an integer from 1 to {MAX_POLICY_WORKERS}")
        normalized_profiles.append({
            "id": profile_id,
            "label": profile["label"],
            "description": profile["description"],
            "feed_delay_seconds": delay,
            "feed_jitter_seconds": jitter,
            "media_workers": workers,
        })

    default_profile = policy["default_profile"]
    if default_profile not in profile_ids:
        raise PolicyError(f"default_profile does not name a profile: {default_profile}")
    return {
        "version": 1,
        "default_profile": default_profile,
        "profiles": normalized_profiles,
    }


def resolve_network_profile(policy: dict[str, Any], requested_id: str | None = None) -> dict[str, Any]:
    profile_id = policy["default_profile"] if requested_id is None else requested_id
    for profile in policy["profiles"]:
        if profile["id"] == profile_id:
            return dict(profile)
    available = ", ".join(profile["id"] for profile in policy["profiles"])
    raise PolicyError(f"unknown network profile '{profile_id}' (available: {available})")


def load_context_policy(path: Path | None = None) -> dict[str, Any]:
    policy_path = CONTEXT_POLICY_FILE if path is None else Path(path)
    try:
        with policy_path.open(encoding="utf-8") as f:
            policy = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise PolicyError(f"could not read context policy: {exc}") from exc
    if not isinstance(policy, dict) or policy.get("version") != 1:
        raise PolicyError("context policy version must be integer 1")
    scheduler = policy.get("scheduler")
    defaults = policy.get("defaults")
    modes = policy.get("modes")
    if not isinstance(scheduler, dict) or not isinstance(defaults, dict) or not isinstance(modes, dict):
        raise PolicyError("context policy requires scheduler, defaults, and modes")
    if "cycle_turns" in scheduler and (type(scheduler["cycle_turns"]) is not int or scheduler["cycle_turns"] < 1):
        raise PolicyError("context scheduler cycle_turns must be a positive integer")
    scheduler.setdefault("cycle_turns", 20)
    integer_defaults = (
        "max_neighbors_per_blog", "initial_probe_posts", "posts_per_context_blog",
        "global_context_blog_budget", "global_context_post_budget", "default_explore_depth",
        "scout_initial_pages", "scout_initial_records", "initial_neighborhood_posts",
    )
    for key in integer_defaults:
        if type(defaults.get(key)) is not int or defaults[key] < 0:
            raise PolicyError(f"context default {key} must be a non-negative integer")
    for mode in ("none", "nearby", "explore"):
        if not isinstance(modes.get(mode), dict) or type(modes[mode].get("max_depth")) is not int:
            raise PolicyError(f"context mode {mode} is invalid")
    focus_profiles = policy.get("focus_profiles", {
        "deep": {"target_share": 80, "depth1_share": 15, "depth2_share": 5},
        "balanced": {"target_share": 60, "depth1_share": 25, "depth2_share": 15},
        "wide": {"target_share": 45, "depth1_share": 35, "depth2_share": 20},
    })
    if not isinstance(focus_profiles, dict) or not focus_profiles:
        raise PolicyError("context focus_profiles must be a non-empty object")
    for focus, values in focus_profiles.items():
        if not isinstance(focus, str) or not isinstance(values, dict):
            raise PolicyError("context focus profiles are invalid")
        fields = ("target_share", "depth1_share", "depth2_share")
        if any(type(values.get(field)) is not int or values[field] < 0 for field in fields):
            raise PolicyError(f"context focus profile {focus} has invalid shares")
        if sum(values[field] for field in fields) != 100:
            raise PolicyError(f"context focus profile {focus} shares must total 100")
    default_focus = policy.get("default_focus", "balanced")
    if default_focus not in focus_profiles:
        raise PolicyError(f"unknown default focus: {default_focus}")
    policy["focus_profiles"] = focus_profiles
    policy["default_focus"] = default_focus
    relationship_weights = policy.setdefault("relationship_weights", {
        "target_reblogs_from": 5.0,
        "other_reblogs_target": 1.0,
        "target_likes_other": 5.0,
        "other_likes_target": 1.0,
        "reciprocal_bonus": 2.0,
        "ask_answer": 1.0,
    })
    context_weights = policy.setdefault("context_weights", {
        "immediate_context_missing": 8.0,
        "context_distance_decay": 0.5,
        "support_ring_multiplier": 0.35,
    })
    representation_weights = policy.setdefault("representation_weights", {
        "underrepresented_blog_bonus": 4.0,
        "representation_decay": 1.0,
    })
    for section_name, section in (("relationship_weights", relationship_weights), ("context_weights", context_weights), ("representation_weights", representation_weights)):
        if not isinstance(section, dict) or any(not isinstance(value, (int, float)) or value < 0 for value in section.values()):
            raise PolicyError(f"context {section_name} must contain non-negative numeric weights")
    outward = policy.setdefault("focus_endpoints", {}).setdefault("outward", {
        "target_share": 0, "depth1_share": 60, "depth2_share": 40,
    })
    fields = ("target_share", "depth1_share", "depth2_share")
    if any(not isinstance(outward.get(field), (int, float)) or outward[field] < 0 for field in fields):
        raise PolicyError("context outward focus shares are invalid")
    if sum(outward[field] for field in fields) <= 0:
        raise PolicyError("context outward focus shares must be positive")
    policy.setdefault("action_limits", {"max_context_probe_pages": 2, "max_context_ring": 3, "max_actions_per_cycle": 1})
    policy.setdefault("presentation_refresh_seconds", 30)
    return policy


def context_config(
    policy: dict[str, Any], mode: str, requested_depth: int | None,
    focus: str | None = None,
) -> dict[str, Any]:
    if mode not in ("none", "nearby", "explore"):
        raise PolicyError(f"unknown context mode: {mode}")
    defaults = dict(policy["defaults"])
    mode_depth = int(policy["modes"][mode]["max_depth"])
    if mode == "explore":
        depth = defaults["default_explore_depth"] if requested_depth is None else requested_depth
        if depth < 1:
            raise PolicyError("context depth must be at least 1 for explore mode")
        defaults["max_depth"] = depth
    else:
        defaults["max_depth"] = mode_depth
    if mode == "none":
        defaults["max_depth"] = 0
    defaults["mode"] = mode
    defaults["scheduler"] = dict(policy["scheduler"])
    selected_focus = policy.get("default_focus", "balanced") if focus is None else focus
    if selected_focus not in policy.get("focus_profiles", {}) and selected_focus not in {"neighbors", "outward"}:
        raise PolicyError(f"unknown focus: {selected_focus}")
    defaults["focus"] = selected_focus
    defaults["focus_profile"] = dict(
        policy["focus_profiles"].get(
            selected_focus,
            policy.get("focus_endpoints", {}).get("outward", {}),
        )
    )
    defaults["focus_profiles"] = policy["focus_profiles"]
    defaults["relationship_weights"] = policy.get("relationship_weights", {})
    defaults["context_weights"] = policy.get("context_weights", {})
    defaults["representation_weights"] = policy.get("representation_weights", {})
    defaults["focus_endpoints"] = dict(policy.get("focus_endpoints", {}))
    defaults["action_limits"] = dict(policy.get("action_limits", {}))
    return defaults


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
    return BACKUPS_DIR / canonical_blog_name(blog)


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
) -> None:
    global BLOG, BLOG_HOST, OUT, JSON_DIR, MAX_POSTS, FULL_RES, NETWORK_PROFILE
    if profile is None:
        profile = resolve_network_profile(load_network_policy())
    BLOG = blog
    BLOG_HOST = f"{BLOG}.tumblr.com"
    OUT = canonical_archive_root(BLOG)
    JSON_DIR = OUT / "json"
    MAX_POSTS = max_posts
    FULL_RES = full_res
    NETWORK_PROFILE = dict(profile)


def ensure_blog_stylesheets() -> None:
    """Create the stable global stylesheet hook without overwriting overrides."""
    OUT.mkdir(parents=True, exist_ok=True)
    if not GLOBAL_CSS.is_file():
        raise RuntimeError(f"Missing shared stylesheet: {GLOBAL_CSS}")

    custom = OUT / "custom.css"
    global_css = os.path.relpath(GLOBAL_CSS, OUT).replace(os.sep, "/")
    managed_custom = '@import url("../assets/archive.css");\n@import url("override.css");\n'
    legacy_custom = (
        f'@import url("{global_css}");\n'
        '@import url("../assets/archive.css");\n'
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


def _retry_after_seconds(value: str | None) -> float | None:
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


def fetch_public_page(start: int, count: int = PAGE_SIZE) -> dict[str, Any]:
    """Read Tumblr's public, credential-free blog JSON feed."""
    query = urlencode({"start": start, "num": count})
    url = f"https://{BLOG_HOST}/api/read/json?{query}"
    req = Request(url, headers={"User-Agent": USER_AGENT})
    rate_limit_retry = False
    while True:
        try:
            with urlopen(req, timeout=30) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            if exc.code == 429:
                retry_after = _retry_after_seconds(exc.headers.get("Retry-After"))
                if rate_limit_retry or retry_after is None:
                    raise TumblrRateLimitedError from exc
                report_terminal_event("Tumblr requested slower pacing", warning=True)
                if retry_after:
                    time.sleep(retry_after)
                rate_limit_retry = True
                continue
            kind = "unavailable" if exc.code in {401, 403, 404, 410} else "http_error"
            raise BlogSourceFailure(
                BLOG,
                "feed",
                f"Tumblr returned HTTP {exc.code} while reading {BLOG_HOST}",
                code=exc.code,
                kind=kind,
                retryable=exc.code not in {401, 403, 404, 410},
            ) from exc
        except URLError as exc:
            raise BlogSourceFailure(
                BLOG,
                "feed",
                f"Could not reach Tumblr: {exc.reason}",
                kind="network_error",
                retryable=True,
            ) from exc
        break

    # Tumblr's legacy public feed is JavaScript:
    #     var tumblr_api_read = {...};
    # Strip the wrapper and parse the object as JSON.
    match = re.match(r"\s*var\s+tumblr_api_read\s*=\s*(.*)\s*;\s*$", raw, re.S)
    if not match:
        raise BlogSourceFailure(
            BLOG,
            "feed",
            "Tumblr's public blog feed returned an unexpected format",
            kind="invalid_feed",
            retryable=True,
        )
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise BlogSourceFailure(
            BLOG,
            "feed",
            "Tumblr's public blog feed was not valid JSON",
            kind="invalid_feed",
            retryable=True,
        ) from exc


def blog_info(feed: dict[str, Any], total: int) -> dict[str, Any]:
    t = feed.get("tumblelog") or feed.get("blog") or {}
    return {
        "name": BLOG,
        "title": t.get("title") or feed.get("title") or BLOG,
        "description": t.get("description") or feed.get("description") or "",
        "url": t.get("url") or f"https://{BLOG_HOST}/",
        "posts": total,
        # tumblr-backup does not need a real UUID for this public-feed path.
        "uuid": f"public-feed:{BLOG}",
    }


def photo_object(p: dict[str, Any], full_res: bool = False) -> dict[str, Any]:
    candidates: list[tuple[int, str]] = []
    for size in (1280, 500, 400, 250, 100, 75):
        u = p.get(f"photo-url-{size}")
        if u:
            candidates.append((size, u))
    if not candidates:
        return {"caption": "", "original_size": {"url": ""}, "alt_sizes": []}

    largest_size, largest_url = candidates[0]
    if full_res:
        selected = candidates[0]
    else:
        smaller = [candidate for candidate in candidates if candidate[0] <= 500]
        selected = max(smaller, default=candidates[-1])
    selected_size, selected_url = selected
    alt = [{"width": selected_size, "height": 0, "url": selected_url}]
    alt.extend(
        {"width": size, "height": 0, "url": url}
        for size, url in candidates
        if (size, url) != selected
    )
    return {
        "caption": "",
        "original_size": {"width": largest_size, "height": 0, "url": largest_url},
        "alt_sizes": alt,
    }


def normalize_post(p: dict[str, Any], feed: dict[str, Any], total: int) -> dict[str, Any]:
    """Translate the public feed's legacy record to tumblr-backup's expected shape."""
    legacy_type = str(p.get("type") or "regular")
    post_type = {
        "regular": "text",
        "conversation": "chat",
    }.get(legacy_type, legacy_type)

    post_url = p.get("url-with-slug") or p.get("url") or f"https://{BLOG_HOST}/post/{p['id']}"
    timestamp = int(p.get("unix-timestamp") or 0)

    out: dict[str, Any] = {
        "id": int(p["id"]),
        "id_string": str(p["id"]),
        "blog_name": BLOG,
        "tumblelog": BLOG,
        "blog": blog_info(feed, total),
        "post_url": post_url,
        "short_url": post_url,
        "type": post_type,
        "timestamp": timestamp,
        "date": p.get("date-gmt") or p.get("date") or "",
        "tags": list(p.get("tags") or []),
        "reblog_key": p.get("reblog-key") or "",
        "slug": p.get("slug") or "",
        "note_count": int(p.get("note-count") or 0),
        # Preserve the exact source record inside the normalized record.
        "_puppetbackup_source": "tumblr-public-blog-feed",
        "_puppetbackup_source_record": p,
    }

    # Reblog/source provenance when present in the public feed.
    remap = {
        "reblogged-from-url": "reblogged_from_url",
        "reblogged-from-name": "reblogged_from_name",
        "reblogged-root-url": "reblogged_root_url",
        "reblogged-root-name": "reblogged_root_name",
    }
    for old, new in remap.items():
        if p.get(old):
            out[new] = p[old]

    if post_type == "text":
        out["title"] = p.get("regular-title") or ""
        out["body"] = p.get("regular-body") or ""

    elif post_type == "answer":
        out["question"] = p.get("question") or ""
        out["answer"] = p.get("answer") or ""
        if p.get("asking-name"):
            out["asking_name"] = p["asking-name"]
        if p.get("asking-url"):
            out["asking_url"] = p["asking-url"]

    elif post_type == "photo":
        out["caption"] = p.get("photo-caption") or ""
        out["link_url"] = p.get("photo-link-url") or ""
        out["photos"] = [photo_object(p, full_res=FULL_RES)]

    elif post_type == "quote":
        out["text"] = p.get("quote-text") or ""
        out["source"] = p.get("quote-source") or ""

    elif post_type == "link":
        out["title"] = p.get("link-text") or p.get("link-url") or ""
        out["url"] = p.get("link-url") or post_url
        out["description"] = p.get("link-description") or ""

    elif post_type == "chat":
        out["title"] = p.get("conversation-title") or ""
        dialogue = []
        for row in p.get("conversation") or []:
            dialogue.append({
                "label": row.get("label") or "",
                "name": row.get("name") or "",
                "phrase": row.get("phrase") or "",
            })
        out["dialogue"] = dialogue

    elif post_type == "video":
        embed = p.get("video-player-500") or p.get("video-player") or ""
        out["caption"] = p.get("video-caption") or ""
        out["video_type"] = "tumblr"
        out["video_url"] = p.get("video-source") or ""
        out["player"] = [{"width": 500, "embed_code": embed}] if embed else []

    elif post_type == "audio":
        out["caption"] = p.get("audio-caption") or ""
        out["audio_type"] = "tumblr"
        out["audio_url"] = p.get("audio-source") or p.get("audio-url") or ""
        out["player"] = p.get("audio-player") or ""

    return out


def post_is_complete(pid: int) -> bool:
    return (
        (JSON_DIR / f"{pid}.json").is_file()
        and (OUT / "posts" / f"{pid}.html").is_file()
    )


def post_source_exists(pid: int) -> bool:
    return (JSON_DIR / f"{pid}.json").is_file()


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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, sort_keys=True, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def process_batch(ids: list[int]) -> int:
    if not ids:
        return 0
    report_terminal_event(f"Rendering {len(ids)} posts")
    render_with_tumblr_backup(ids)
    missing = [pid for pid in ids if not post_is_complete(pid)]
    if missing:
        raise RuntimeError(
            "tumblr-backup returned without producing HTML for: "
            + ", ".join(str(pid) for pid in missing)
        )
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
                reached_existing = True
                break

            normalized = normalize_post(source_post, feed, total)
            write_json_atomic(JSON_DIR / f"{pid}.json", normalized)
            batch.append(pid)
            report_terminal_event(f"Saved source JSON {pid}")

            if len(batch) >= PROCESS_BATCH:
                completed += process_batch(batch)
                batch = []

        if batch:
            completed += process_batch(batch)

        if reached_existing:
            break

        start += len(posts)
        if total is not None and start >= total:
            break

    hit_depth_limit = bool(MAX_POSTS and inspected >= MAX_POSTS and not reached_existing)
    return inspected, completed, hit_depth_limit


def ensure_tumblr_backup() -> None:
    try:
        import tumblr_backup.main  # noqa: F401
        return
    except ImportError:
        pass

    print("Installing tumblr-backup 1.0.7 (first run only)...")
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "tumblr-backup==1.0.7",
        "urllib3>=2.2.2,<2.6",
    ]
    try:
        subprocess.check_call(cmd)
    except Exception as exc:
        raise RuntimeError(
            "Could not install tumblr-backup automatically. "
            "This Python app needs working pip support."
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
    records = []
    for path in sorted(directory.glob("*.json")):
        try:
            with path.open(encoding="utf-8") as f:
                record = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _blog_from_reference(name: Any, url: Any) -> tuple[str, str] | None:
    observed_name = str(name or "").strip()
    observed_url = str(url or "").strip()
    candidate = observed_name
    if observed_url:
        parsed = urlparse(observed_url)
        host = (parsed.hostname or "").lower()
        path_parts = [part for part in parsed.path.split("/") if part]
        if host in {"www.tumblr.com", "tumblr.com"} and len(path_parts) >= 2 and path_parts[0] == "blog" and path_parts[1] == "view":
            candidate = path_parts[2] if len(path_parts) >= 3 else ""
        elif host.endswith(".tumblr.com") and host.count(".") == 2 and host.split(".")[0] not in {"www", "api"}:
            candidate = host[:-len(".tumblr.com")]
        elif not candidate and len(path_parts) >= 2 and path_parts[0] == "blog" and path_parts[1] == "view":
            candidate = path_parts[2] if len(path_parts) >= 3 else ""
    if not re.fullmatch(r"[A-Za-z0-9-]+", candidate or ""):
        return None
    if candidate.lower() in {"www", "api", "tumblr"}:
        return None
    return candidate.lower(), observed_url or f"https://{candidate.lower()}.tumblr.com/"


def interaction_evidence(owner: str, record: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = []
    source_id = str(record.get("id_string") or record.get("id") or "")
    source_url = str(record.get("post_url") or record.get("url-with-slug") or record.get("url") or "")
    direct = _blog_from_reference(
        record.get("reblogged_from_name") or record.get("reblogged-from-name"),
        record.get("reblogged_from_url") or record.get("reblogged-from-url"),
    )
    if direct and direct[0] != owner:
        evidence.append({
            "from_blog": owner,
            "to_blog": direct[0],
            "kind": "direct_reblog",
            "direction": "upstream",
            "source_blog": owner,
            "source_post_id": source_id,
            "source_post_url": source_url,
            "reference_url": direct[1],
            "direction_known": True,
        })
    asker = _blog_from_reference(
        record.get("asking_name") or record.get("asking-name"),
        record.get("asking_url") or record.get("asking-url"),
    )
    if asker and asker[0] != owner:
        evidence.append({
            "from_blog": asker[0],
            "to_blog": owner,
            "kind": "structured_ask",
            "direction": "downstream",
            "source_blog": owner,
            "source_post_id": source_id,
            "source_post_url": source_url,
            "reference_url": asker[1],
            "direction_known": True,
        })
    return evidence


def _evidence_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        item.get("from_blog"), item.get("to_blog"), item.get("kind"),
        item.get("source_blog"), item.get("source_post_id"),
        item.get("reference_url"), item.get("referenced_post_id"),
    )


def _context_root(primary: str) -> Path:
    return BACKUPS_DIR / primary / "context"


def _presentation_state_path() -> Path:
    return BACKUPS_DIR / "presentation-state.json"


def mark_presentation_dirty(reason: str = "canonical data changed") -> None:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    write_json_atomic(_presentation_state_path(), {
        "dirty": True,
        "reason": reason,
        "updated_at": time.time(),
    })


def _canonical_blog_directories() -> list[Path]:
    if not BACKUPS_DIR.is_dir():
        return []
    return sorted(
        path for path in BACKUPS_DIR.iterdir()
        if path.is_dir() and re.fullmatch(r"[a-z0-9-]+", path.name) and path.name not in {"assets", "tags"}
    )


def reconcile_legacy_archives() -> list[str]:
    """Non-destructively promote legacy nested context archives.

    Existing files are never removed or overwritten. Canonical files win when
    both layouts contain the same path.
    """
    promoted: list[str] = []
    for target in _canonical_blog_directories():
        legacy_root = target / "context" / "blogs"
        if not legacy_root.is_dir():
            continue
        for source in sorted(legacy_root.iterdir()):
            if not source.is_dir() or source.name in {"www", "api"} or not re.fullmatch(r"[a-z0-9-]+", source.name):
                continue
            destination = BACKUPS_DIR / source.name
            copied = False
            for path in source.rglob("*"):
                if not path.is_file() or path.name == "context.html":
                    continue
                target_path = destination / path.relative_to(source)
                if not target_path.exists():
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target_path)
                    copied = True
            if copied:
                promoted.append(source.name)
    if promoted:
        mark_presentation_dirty("legacy archives promoted to canonical storage")
    return promoted


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


def _relative_archive_url(blog: str, post_id: str | None = None) -> str:
    if post_id is None:
        return f"{blog}/index.html"
    return f"{blog}/posts/{post_id}.html"


def _shared_asset_paths() -> tuple[Path, Path]:
    assets = BACKUPS_DIR / "assets"
    return assets / "archive.css", assets / "archive.js"


def _archive_nav(links: list[tuple[str, str]], active: str = "") -> str:
    items = []
    for label, href in links:
        current = ' aria-current="page"' if label == active else ""
        items.append(f'<a href="{escape(href)}"{current}>{escape(label)}</a>')
    return '<nav class="archive-nav" aria-label="Primary">' + "".join(items) + "</nav>"


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
    return (
        '<!doctype html><html lang="en" dir="auto"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title>"
        f'<link rel="stylesheet" href="{escape(stylesheet)}">'
        f'<script defer src="{escape(script)}"></script>{extra}</head><body>'
        + '<!-- puppetbackup-shared-shell-v3 -->'
        + '<header class="archive-chrome">'
        + _reader_controls()
        + _archive_nav(links, active)
        + '</header>'
        + '<main class="reader-content">'
        + content
        + "</main></body></html>"
    )


def _global_nav(prefix: str = "") -> list[tuple[str, str]]:
    neighborhood_prefix = "../" * (prefix.count("../") + 1)
    return [
        ("Blogs", f"{prefix}index.html"),
        ("Dashboard", f"{prefix}dashboard.html"),
        ("Tags", f"{prefix}tags/index.html"),
        ("Neighborhoods", f"{neighborhood_prefix}Neighborhoods/index.html"),
    ]


def _blog_nav(blog: str, prefix: str = "") -> list[tuple[str, str]]:
    links = _global_nav(prefix) + [(blog, f"{prefix}{blog}/index.html")]
    if (canonical_archive_root(blog) / "tags" / "index.html").is_file():
        links.append(("Blog tags", f"{prefix}{blog}/tags/index.html"))
    return links


def _acquisition_ledger_path() -> Path:
    return BACKUPS_DIR / "acquisition-ledger.jsonl"


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
                "graph_depth": int(canonical[key].get("_puppetbackup_graph_depth") or 0),
                "acquisition_lane": str(canonical[key].get("_puppetbackup_acquisition_lane") or "reconciled"),
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
    return {key: item for key, item in ledger.items() if key in canonical and item.get("state") == "acquired"}


def ensure_shared_archive_assets() -> None:
    css, js = _shared_asset_paths()
    css.parent.mkdir(parents=True, exist_ok=True)
    base_css = ""
    source_css = SOURCE_ARCHIVE_CSS if SOURCE_ARCHIVE_CSS.is_file() else GLOBAL_CSS
    if source_css.is_file():
        try:
            base_css = source_css.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            base_css = ""
    css.write_text(
        base_css
        + "\n/* Generated copy: source authority is assets/archive.css. */\n",
        encoding="utf-8",
    )
    if SOURCE_ARCHIVE_JS.is_file():
        shutil.copy2(SOURCE_ARCHIVE_JS, js)


def _reader_controls() -> str:
    return (
        '<details class="reader-settings"><summary>Reader settings</summary>'
        '<form class="reader-settings-panel" aria-label="Reader settings">'
        '<label for="reader-size">Text size <output id="reader-size-value" for="reader-size">1.08rem</output></label>'
        '<input id="reader-size" type="range" min="0.9" max="1.8" step="0.05" value="1.08" data-reader-setting="size" data-unit="rem"> '
        '<label for="reader-leading">Line spacing <output id="reader-leading-value" for="reader-leading">1.58</output></label>'
        '<input id="reader-leading" type="range" min="1.2" max="2.2" step="0.05" value="1.58" data-reader-setting="leading"> '
        '<label for="reader-width">Content width <output id="reader-width-value" for="reader-width">52rem</output></label>'
        '<input id="reader-width" type="range" min="30" max="80" step="2" value="52" data-reader-setting="width" data-unit="rem"></form></details>'
    )


def _page_chrome(path: Path) -> tuple[list[tuple[str, str]], str]:
    try:
        relative = path.resolve().relative_to(BACKUPS_DIR.resolve())
        parts = relative.parts
        if parts and parts[0] == "tags":
            return _global_nav("../"), "Tags"
        if parts and parts[0] not in {"assets"}:
            blog = parts[0]
            prefix = "../../" if len(parts) > 1 and parts[1] in {"posts", "archive", "tags"} else "../"
            if len(parts) > 1 and parts[1] == "tags":
                return _blog_nav(blog, "../../"), "Blog tags"
            if len(parts) > 1 and parts[1] in {"posts", "archive"}:
                return _blog_nav(blog, "../../"), blog
            return _blog_nav(blog, prefix), blog
    except ValueError:
        pass
    try:
        relative = path.resolve().relative_to(NEIGHBORHOODS_DIR.resolve())
        if relative.parts and relative.parts[0] != "index.html":
            target = relative.parts[0]
            return [
                ("Blogs", "../../Backups/index.html"),
                ("Dashboard", "../../Backups/dashboard.html"),
                ("Tags", "../../Backups/tags/index.html"),
                ("Neighborhoods", "../index.html"),
                (target, f"../../Backups/{target}/index.html"),
            ], "Neighborhoods"
        return [
            ("Blogs", "../Backups/index.html"),
            ("Dashboard", "../Backups/dashboard.html"),
            ("Tags", "../Backups/tags/index.html"),
            ("Neighborhoods", "index.html"),
        ], "Neighborhoods"
    except ValueError:
        pass
    return _global_nav(), ""


def _post_tag_links(path: Path, record: dict[str, Any]) -> str:
    tags = list(record.get("tags") or [])
    if not tags:
        return ""
    blog = path.resolve().relative_to(BACKUPS_DIR.resolve()).parts[0]
    links = []
    for raw_tag in tags:
        display, _canonical, _search, page_id = _tag_identity(raw_tag)
        links.append(f'<a href="../tags/{page_id}.html"><bdi dir="auto">{escape(display)}</bdi></a>')
    return '<!-- puppetbackup-post-tags --><p class="archive-tags"><span>Tags:</span> ' + " ".join(links) + "</p>"


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

    def _attrs(self, attrs: list[tuple[str, str | None]]) -> str:
        rendered = []
        for name, value in attrs:
            lower = name.lower()
            if lower.startswith("on"):
                continue
            if value is None:
                rendered.append(lower)
                continue
            if lower in _FRAGMENT_REFERENCE_ATTRS:
                if lower == "srcset":
                    value = _rebase_srcset(value, self.source_page, self.destination_page)
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
        self.parts.append("<" + lower + self._attrs(attrs) + ">")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._in_fragment() and not self.skip_depth and not self.skip_header_depth:
            self.parts.append("<" + tag.lower() + self._attrs(attrs) + "/>")

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


def _trail_entries(nodes: list[_TrailNode]) -> list[tuple[str, str, list[_TrailNode]]] | None:
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
    return entries if len(entries) >= 1 else None


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
    if not (local_root / "json").is_dir():
        return None
    return _local_profile_avatar(name, destination_page)


def _render_flat_reblog_trail(body: str, destination_page: Path) -> str:
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
    for name, source_href, content in entries:
        avatar = _participant_avatar(name, destination_page)
        avatar_html = (
            f'<img class="trail-avatar" src="{escape(avatar)}" alt="" loading="lazy">'
            if avatar else '<span class="trail-avatar placeholder" aria-hidden="true"></span>'
        )
        href = _participant_href(name, source_href, destination_page)
        name_html = f'<a href="{escape(href)}"><bdi dir="auto">{escape(name)}</bdi></a>' if href else f'<bdi dir="auto">{escape(name)}</bdi>'
        content_html = "".join(_serialize_trail_node(child) for child in content).strip()
        rendered.append(
            '<section class="reblog-trail-entry">'
            f'<header class="trail-identity">{avatar_html}<span>{name_html}</span></header>'
            f'<div class="trail-content" dir="auto">{content_html}</div>'
            '</section>'
        )
    return '<div class="reblog-trail" aria-label="Reblog trail">' + "".join(rendered) + "</div>"


def _profile_path(blog: str) -> Path:
    return canonical_archive_root(blog) / "profile" / "profile.json"


def _write_profile_snapshot(blog: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    current = max(records, key=lambda record: int(record.get("timestamp") or 0), default={})
    blog_infos = [record.get("blog") for record in records if isinstance(record.get("blog"), dict)]
    blog_info = next((value for value in blog_infos if value.get("description")), None) or (current.get("blog") if isinstance(current.get("blog"), dict) else {})
    profile = {
        "schema_version": 1,
        "blog": blog,
        "title": str(blog_info.get("title") or current.get("tumblelog") or blog),
        "description": blog_info.get("description") if "description" in blog_info else None,
        "url": str(blog_info.get("url") or current.get("post_url") or f"https://{blog}.tumblr.com/"),
        "observed_at": time.time(),
        "source": "canonical post JSON blog metadata",
        "avatar_source": "upstream theme/avatar asset when available",
    }
    path = _profile_path(blog)
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


def _render_post_card(record: dict[str, Any], destination_page: Path, *, compact: bool = False) -> str:
    blog = str(record.get("_canonical_blog") or record.get("blog_name") or record.get("tumblelog") or "unknown")
    post_id = str(record.get("id_string") or record.get("id") or "")
    source_page = canonical_archive_root(blog) / "posts" / f"{post_id}.html"
    namespace = "post-" + re.sub(r"[^a-zA-Z0-9_-]", "-", blog + "-" + post_id) + "-"
    body = _rendered_post_body(source_page, destination_page, namespace)
    flat_trail = _render_flat_reblog_trail(body, destination_page)
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
        body = '<p class="render-pending">Source JSON is preserved; rendered post HTML is unavailable.</p>'
    avatar = _local_profile_avatar(blog, destination_page)
    avatar_html = f'<img class="post-avatar" src="{escape(avatar)}" alt="" loading="lazy">' if avatar else '<span class="post-avatar placeholder" aria-hidden="true"></span>'
    blog_href = os.path.relpath(canonical_archive_root(blog) / "index.html", destination_page.parent).replace(os.sep, "/")
    post_href = os.path.relpath(source_page, destination_page.parent).replace(os.sep, "/")
    title = str(profile.get("title") or blog)
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
        tag_path = BACKUPS_DIR / "tags" / f"{page_id}.html"
        tag_href = os.path.relpath(tag_path, destination_page.parent).replace(os.sep, "/")
        tags.append(f'<a href="{escape(tag_href)}"><bdi dir="auto">#{escape(display)}</bdi></a>')
    tags_html = '<div class="post-tags" aria-label="Tags">' + " ".join(tags) + "</div>" if tags else ""
    source_url = str(record.get("post_url") or record.get("short_url") or "")
    source_action = f'<a href="{escape(source_url)}" rel="noreferrer noopener">Source</a>' if source_url else ""
    notes = record.get("note_count")
    notes_html = f'<span class="post-notes">{int(notes)} notes</span>' if notes is not None else ""
    classes = "post-card" + (" post-card-compact" if compact else "")
    return (
        f'<article class="{classes}" id="{escape(namespace + "card")}">'
        f'<header class="post-card-header">{avatar_html}<div class="post-card-identity">'
        f'<a class="post-blog" href="{escape(blog_href)}"><bdi dir="auto">{escape(title)}</bdi></a>'
        f'<span class="post-username">@{escape(blog)}</span>'
        f'<time datetime="{escape(str(timestamp))}">{escape(time_label)}</time></div></header>'
        + attribution_html
        + f'<div class="post-rendered-content">{body}</div>'
        + f'<footer class="post-card-footer">{tags_html}<div class="post-actions">{notes_html}<a href="{escape(post_href)}">Open archived post</a>{source_action}</div></footer>'
        + "</article>"
    )


def _inject_shared_assets(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    rel = os.path.relpath(BACKUPS_DIR / "assets", path.parent).replace(os.sep, "/")
    text = re.sub(r"<!-- puppetbackup-shared-reader -->.*?(?=<!doctype|<html)", "", text, count=1, flags=re.S | re.I)
    if "archive.css" not in text:
        addition = f'<link rel="stylesheet" href="{rel}/archive.css"><script defer src="{rel}/archive.js"></script>'
        if "</head>" in text.lower():
            text = re.sub(r"</head>", addition + "</head>", text, count=1, flags=re.I)
        else:
            text = addition + text
    if "puppetbackup-shared-shell-v3" not in text:
        text = text.replace("<!-- puppetbackup-shared-shell-v2 -->", "")
        text = re.sub(r'<form class="reader-settings".*?</form>', "", text, count=1, flags=re.S)
        text = re.sub(r'<nav class="archive-nav".*?</nav>', "", text, count=1, flags=re.S)
        links, active = _page_chrome(path)
        chrome = '<!-- puppetbackup-shared-shell-v3 --><header class="archive-chrome">' + _reader_controls() + _archive_nav(links, active) + '</header>'
        body_match = re.search(r"<body\b[^>]*>", text, flags=re.I)
        if body_match:
            text = text[:body_match.end()] + chrome + text[body_match.end():]
        else:
            text = chrome + text
    relative = path.resolve().relative_to(BACKUPS_DIR.resolve()) if path.resolve().is_relative_to(BACKUPS_DIR.resolve()) else None
    if relative and len(relative.parts) > 2 and relative.parts[1] == "posts" and "puppetbackup-post-tags" not in text:
        try:
            post_id = Path(relative.parts[2]).stem
            record_path = BACKUPS_DIR / relative.parts[0] / "json" / f"{post_id}.json"
            record = json.loads(record_path.read_text(encoding="utf-8"))
            tags = _post_tag_links(path, record)
            if tags:
                body_match = re.search(r"<body\b[^>]*>", text, flags=re.I)
                if body_match:
                    insert_at = body_match.end()
                    text = text[:insert_at] + tags + text[insert_at:]
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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
            "archive": f"Backups/{blog_dir.name}/index.html",
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
    if root == BACKUPS_DIR / "tags":
        return f"../{post['blog']}/posts/{post['post_id']}.html"
    return f"../posts/{post['post_id']}.html"


def _render_tag_page(
    item: dict[str, Any],
    root: Path,
    scope_blog: str | None = None,
    output_path: Path | None = None,
) -> str:
    posts = [post for post in item["posts"] if scope_blog is None or post["blog"] == scope_blog]
    output_path = output_path or (root / f'{item["page_id"]}.html')
    records = {}
    for post in posts:
        path = canonical_archive_root(post["blog"]) / "json" / f'{post["post_id"]}.json'
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            record["_canonical_blog"] = post["blog"]
            records[(post["blog"], post["post_id"])] = record
    cards = []
    for post in posts:
        record = records.get((post["blog"], post["post_id"]))
        if record is not None and post["rendered"]:
            cards.append(_render_post_card(record, output_path, compact=True))
        else:
            label = f"{post['blog']} / {post['title']}"
            cards.append(
                '<article class="post-card post-card-compact render-pending">'
                f'<p><a href="{escape(_tag_post_href(root, post))}">{escape(label)}</a> '
                f'(source preserved; rendering pending)</p></article>'
            )
    parent = f"index.html" if scope_blog else "index.html"
    if scope_blog:
        links = _blog_nav(scope_blog, "../../")
        active = "Blog tags"
        content = f'<p><a href="{parent}">Blog tags</a></p>'
    else:
        links = _global_nav("../")
        active = "Tags"
        content = f'<p><a href="{parent}">All tags</a></p>'
    content += f'<h1><bdi dir="auto">{escape(item["display"])}</bdi></h1><div class="post-feed">' + "".join(cards) + "</div>"
    return _archive_shell(
        f"Tag - {item['display']}",
        content,
        links,
        active=active,
        stylesheet="../../assets/archive.css" if scope_blog else "../assets/archive.css",
        script="../../assets/archive.js" if scope_blog else "../assets/archive.js",
    )


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
    avatar_html = f'<img class="profile-avatar" src="{escape(avatar)}" alt="">' if avatar else '<span class="profile-avatar placeholder" aria-hidden="true"></span>'
    title = str(profile.get("title") or blog)
    description = profile.get("description")
    bio = f'<p class="profile-bio" dir="auto">{escape(str(description))}</p>' if description else ""
    cards = []
    for record in sorted(records, key=lambda item: (-int(item.get("timestamp") or 0), str(item.get("id_string") or item.get("id")))):
        cards.append(_render_post_card(record, output))
    content = (
        '<section class="profile-header">'
        f'{avatar_html}<div><h1><bdi dir="auto">{escape(title)}</bdi></h1>'
        f'<p class="profile-username">@{escape(blog)}</p>{bio}'
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
        _blog_nav(blog, "../"),
        active=blog,
        stylesheet="../assets/archive.css",
        script="../assets/archive.js",
        extra='<link rel="stylesheet" href="custom.css">',
    )


def render_tag_pages(tag_index: dict[str, Any] | None = None) -> None:
    tag_index = tag_index or build_tag_index()
    write_json_atomic(BACKUPS_DIR / "tag-index.json", tag_index)
    global_root = BACKUPS_DIR / "tags"
    global_root.mkdir(parents=True, exist_ok=True)
    for path in global_root.glob("*.html"):
        path.unlink()
    global_rows = []
    for item in tag_index["tags"]:
        global_rows.append(f'<li><a href="{item["page_id"]}.html"><bdi dir="auto">{escape(item["display"])}</bdi></a> ({len(item["posts"])})</li>')
        output = global_root / f'{item["page_id"]}.html'
        output.write_text(_render_tag_page(item, global_root, output_path=output), encoding="utf-8")
    (global_root / "index.html").write_text(
        _archive_shell(
            "Tags",
            '<h1>Tags</h1><ul>' + "".join(global_rows) + "</ul>",
            _global_nav("../"),
            active="Tags",
            stylesheet="../assets/archive.css",
            script="../assets/archive.js",
        ),
        encoding="utf-8",
    )
    by_blog: dict[str, list[dict[str, Any]]] = {}
    for item in tag_index["tags"]:
        for post in item["posts"]:
            by_blog.setdefault(post["blog"], []).append(item)
    for blog, items in by_blog.items():
        root = canonical_archive_root(blog) / "tags"
        root.mkdir(parents=True, exist_ok=True)
        for path in root.glob("*.html"):
            path.unlink()
        unique = {item["canonical_tag_key"]: item for item in items}
        rows = []
        for item in sorted(unique.values(), key=lambda value: (value["search_key"], value["canonical_tag_key"])):
            rows.append(f'<li><a href="{item["page_id"]}.html"><bdi dir="auto">{escape(item["display"])}</bdi></a></li>')
            output = root / f'{item["page_id"]}.html'
            output.write_text(_render_tag_page(item, root, blog, output_path=output), encoding="utf-8")
        (root / "index.html").write_text(
            _archive_shell(
                f"Tags - {blog}",
                '<h1>Tags</h1><ul>' + "".join(rows) + "</ul>",
                _blog_nav(blog, "../../"),
                active="Blog tags",
                stylesheet="../../assets/archive.css",
                script="../../assets/archive.js",
            ),
            encoding="utf-8",
        )
        _augment_blog_tags_link(blog)


def render_global_pages() -> None:
    ensure_shared_archive_assets()
    catalog = build_global_catalog()
    records_by_blog: dict[str, list[dict[str, Any]]] = {}
    for record in _canonical_local_records():
        records_by_blog.setdefault(str(record["_canonical_blog"]), []).append(record)
    for blog, records in records_by_blog.items():
        _write_profile_snapshot(blog, records)
    tag_index = build_tag_index()
    render_tag_pages(tag_index)
    write_json_atomic(BACKUPS_DIR / "catalog.json", catalog)
    blog_rows = []
    for item in catalog["blogs"]:
        if item["local_post_count"] <= 0:
            continue
        pending = " (render pending)" if item["complete_post_count"] < item["local_post_count"] else ""
        blog_rows.append(
            f'<li><a href="{escape(item["blog"])}/index.html">{escape(item["blog"])}</a> '
            f'- {item["local_post_count"]} locally preserved posts{pending}</li>'
        )
    blogs_content = '<h1>Blogs</h1><p>Locally preserved blog holdings.</p><label for="blog-search">Search blogs</label> <input id="blog-search" placeholder="Search blogs"><ul id="blog-list">' + "".join(blog_rows) + '</ul><script>document.getElementById("blog-search").addEventListener("input",function(){var q=this.value.toLowerCase();document.querySelectorAll("#blog-list li").forEach(function(x){x.hidden=x.textContent.toLowerCase().indexOf(q)<0;});});</script>'
    blogs_html = _archive_shell("Blogs", blogs_content, _global_nav(), active="Blogs")
    (BACKUPS_DIR / "index.html").write_text(blogs_html, encoding="utf-8")

    for blog, records in records_by_blog.items():
        (canonical_archive_root(blog) / "index.html").write_text(
            _render_blog_page(blog, records), encoding="utf-8"
        )

    posts = _canonical_local_records()
    posts.sort(key=lambda r: (-int(r.get("timestamp") or 0), str(r.get("_canonical_blog")), str(r.get("id_string") or r.get("id"))))
    pages = [posts[index:index + PRESENTATION_PAGE_SIZE] for index in range(0, len(posts), PRESENTATION_PAGE_SIZE)] or [[]]
    dashboard_dir = BACKUPS_DIR / "dashboard"
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    for old_page in dashboard_dir.glob("*.html"):
        old_page.unlink()
    for page_number, page_posts in enumerate(pages, start=1):
        output = BACKUPS_DIR / "dashboard.html" if page_number == 1 else dashboard_dir / f"{page_number}.html"
        cards = "".join(_render_post_card(record, output) for record in page_posts)
        pagination = []
        if page_number > 1:
            previous = "../dashboard.html" if page_number == 2 else f"{page_number - 1}.html"
            pagination.append(f'<a href="{previous}">Previous</a>')
        if page_number < len(pages):
            following = f"dashboard/{page_number + 1}.html" if page_number == 1 else f"{page_number + 1}.html"
            pagination.append(f'<a href="{following}">Next</a>')
        pagination_html = '<nav class="pagination" aria-label="Dashboard pages">' + " | ".join(pagination) + "</nav>" if pagination else ""
        content = f'<h1>Global Dashboard</h1><div class="post-feed">{cards}</div>{pagination_html}'
        links = _global_nav() if page_number == 1 else _global_nav("../")
        dashboard = _archive_shell(
            "Global Dashboard",
            content,
            links,
            active="Dashboard",
            stylesheet="assets/archive.css" if page_number == 1 else "../assets/archive.css",
            script="assets/archive.js" if page_number == 1 else "../assets/archive.js",
        )
        output.write_text(dashboard, encoding="utf-8")
    if NEIGHBORHOODS_DIR.is_dir():
        for neighborhood in sorted(NEIGHBORHOODS_DIR.iterdir()):
            if neighborhood.is_dir() and (neighborhood / "neighborhood.json").is_file():
                render_context_pages(neighborhood.name, load_context_document(neighborhood.name))
    render_neighborhood_index()
    for path in BACKUPS_DIR.rglob("*.html"):
        _inject_shared_assets(path)
    for path in NEIGHBORHOODS_DIR.rglob("*.html") if NEIGHBORHOODS_DIR.exists() else []:
        _inject_shared_assets(path)


def regenerate_global_presentation(force: bool = False) -> None:
    state_path = _presentation_state_path()
    dirty = force or not (BACKUPS_DIR / "index.html").is_file() or not (BACKUPS_DIR / "dashboard.html").is_file()
    dirty = dirty or not (BACKUPS_DIR / "tag-index.json").is_file() or not (BACKUPS_DIR / "tags" / "index.html").is_file()
    dirty = dirty or not (BACKUPS_DIR / "assets" / "archive.css").is_file()
    dirty = dirty or not (BACKUPS_DIR / "assets" / "archive.js").is_file()
    dirty = dirty or not (NEIGHBORHOODS_DIR / "index.html").is_file()
    if state_path.is_file():
        try:
            dirty = dirty or bool(json.loads(state_path.read_text(encoding="utf-8")).get("dirty"))
        except (OSError, json.JSONDecodeError):
            dirty = True
    if not dirty:
        return
    render_global_pages()
    write_json_atomic(state_path, {"dirty": False, "generated_at": time.time()})


def load_context_document(primary: str) -> dict[str, Any]:
    path = NEIGHBORHOODS_DIR / primary / "neighborhood.json"
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
    root = NEIGHBORHOODS_DIR / canonical_blog_name(primary)
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
    if not state.first_feed_request:
        wait_seconds = NETWORK_PROFILE["feed_delay_seconds"] + random.uniform(
            0, NETWORK_PROFILE["feed_jitter_seconds"]
        )
        if wait_seconds > 0:
            time.sleep(wait_seconds)
    feed = fetch_public_page(state.feed_start, PAGE_SIZE)
    state.first_feed_request = False
    posts = list(feed.get("posts") or [])
    if not state.feed_blog:
        candidate = feed.get("tumblelog") or feed.get("blog") or {}
        if isinstance(candidate, dict):
            state.feed_blog = dict(candidate)
    if state.feed_total is None:
        state.feed_total = int(feed.get("posts-total") or len(posts))
    if not posts:
        state.exhausted = True
        return False
    state.feed_start += len(posts)
    state.feed_buffer.extend(posts)
    return True


@dataclass
class ScoutResult:
    blog: str
    observed: int = 0
    missing: int = 0
    candidates: list[str] = field(default_factory=list)
    opportunities: list[ScoutOpportunity] = field(default_factory=list)
    exhausted: bool = False


def _scout_paths(state: BlogState) -> tuple[Path, Path]:
    root = canonical_archive_root(state.blog) / "scout"
    return root / "observations.jsonl", root / "state.json"


def scout_blog_page(state: BlogState, primary: str, document: dict[str, Any]) -> ScoutResult:
    """Observe one lightweight feed page and retain raw records in memory."""
    result = ScoutResult(state.blog)
    if state.exhausted or state.blocked_for_run:
        result.exhausted = True
        return result
    before = len(state.feed_buffer)
    if not _fetch_state_page(state):
        result.exhausted = True
        return result
    observations_path, state_path = _scout_paths(state)
    observations_path.parent.mkdir(parents=True, exist_ok=True)
    with observations_path.open("a", encoding="utf-8") as output:
        page = list(state.feed_buffer[before:])
        observed_ids = [str(source.get("id") or "") for source in page if source.get("id") is not None]
        adjacency = document.setdefault("adjacency_observations", [])
        adjacency_keys = {
            (item.get("blog"), str(item.get("anchor_post_id")), item.get("direction"), str(item.get("adjacent_post_id")))
            for item in adjacency
        }
        region_id = f"{state.run_id or 'scan'}:{state.blog}:{state.feed_start - len(page)}"
        for index in range(len(page) - 1):
            newer = page[index]
            older = page[index + 1]
            newer_id = str(newer.get("id") or "")
            older_id = str(older.get("id") or "")
            if not newer_id or not older_id:
                continue
            ordered_timestamps = [
                int(newer.get("unix-timestamp") or newer.get("timestamp") or 0),
                int(older.get("unix-timestamp") or older.get("timestamp") or 0),
            ]
            new_adjacency = (
                {
                    "blog": state.blog,
                    "anchor_post_id": newer_id,
                    "direction": "before",
                    "adjacent_post_id": older_id,
                    "region_id": region_id,
                    "ordered_post_ids": observed_ids,
                    "ordered_timestamps": ordered_timestamps,
                    "source_cursor": state.feed_start - len(page),
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
                    "source_cursor": state.feed_start - len(page),
                    "observed_at": time.time(),
                    "confidence": "contiguous_feed_region",
                },
            )
            for item in new_adjacency:
                key = (item["blog"], str(item["anchor_post_id"]), item["direction"], str(item["adjacent_post_id"]))
                if key not in adjacency_keys:
                    adjacency.append(item)
                    adjacency_keys.add(key)
        for order, source in enumerate(page):
            pid = str(source.get("id") or "")
            if not pid:
                continue
            local = "present" if (canonical_archive_root(state.blog) / "json" / f"{pid}.json").is_file() else "missing"
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
                "timestamp": int(source.get("unix-timestamp") or source.get("timestamp") or 0),
                "post_url": source.get("url-with-slug") or source.get("url") or "",
                "observed_order": order,
                "local_state": local,
                "evidence_lane": "archived" if local == "present" else "scout_only",
                "observed_at": time.time(),
            }, sort_keys=True) + "\n")
    state.scouted += result.observed
    write_json_atomic(state_path, {
        "blog": state.blog,
        "scouted": state.scouted,
        "feed_start": state.feed_start,
        "last_observation": time.time(),
        "exhausted": state.exhausted,
    })
    return result


def _process_source_ids(state: BlogState, sources: list[dict[str, Any]], candidate: ActionCandidate | None = None) -> int:
    if not sources:
        return 0
    candidate = candidate or state.active_candidate
    if candidate is None:
        raise AssertionError("durable acquisition requires an ActionCandidate")
    acquisition_lane = candidate.lane
    assert state.blog == candidate.blog
    assert state.out == canonical_archive_root(candidate.blog or "")
    assert acquisition_lane == candidate.lane
    activate_blog_state(state)
    total = state.feed_total or len(sources)
    ids = []
    for source in sources:
        pid = int(source["id"])
        event = {
            "run_id": state.run_id,
            "blog": canonical_blog_name(state.blog),
            "post_id": str(pid),
            "graph_depth": candidate.graph_depth,
            "acquisition_lane": acquisition_lane,
            "lane": acquisition_lane,
            "action_kind": candidate.kind,
            "reason_codes": list(candidate.reason_codes),
            "state": "selected",
            "planned_cost": candidate.planned_cost,
            "actual_cost": 0,
            "updated_at": time.time(),
        }
        assert state.blog == candidate.blog
        assert state.out == canonical_archive_root(candidate.blog or "")
        assert acquisition_lane == candidate.lane
        normalized = normalize_post(source, {"tumblelog": state.feed_blog or {"name": state.blog}}, total)
        normalized["_puppetbackup_anchor_role"] = state.lane_role
        normalized["_puppetbackup_run_id"] = state.run_id
        normalized["_puppetbackup_graph_depth"] = candidate.graph_depth
        normalized["_puppetbackup_acquisition_lane"] = acquisition_lane
        normalized["_puppetbackup_action_kind"] = candidate.kind
        normalized["_puppetbackup_reason_codes"] = list(candidate.reason_codes)
        write_json_atomic(state.json_dir / f"{pid}.json", normalized)
        event["state"] = "acquired"
        event["actual_cost"] = 1
        _append_acquisition_ledger(event)
        candidate.actual_cost += 1
        ids.append(pid)
        # Durable source JSON is the acquisition boundary. Rendering remains
        # repairable work and must not delay current-run budget accounting.
        state.acquired_this_run += 1
        if state.progress is not None:
            state.progress.record_acquisition(candidate, state.blog, str(pid))
    mark_presentation_dirty("source JSON acquired")
    try:
        processed = process_batch(ids)
    except Exception as exc:
        raise BlogRenderFailure(
            state.blog,
            "render",
            f"Local rendering failed for {state.blog}: {exc}",
            kind="render_pending",
            retryable=True,
        ) from exc
    state.completed += processed
    return processed


def acquire_one_target_batch(state: BlogState, candidate: ActionCandidate | None = None) -> bool:
    activate_blog_state(state)
    if state.exhausted or state.reached_existing or state.blocked_for_run:
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
        if state.batch_limit is not None and len(sources) >= state.batch_limit:
            state.exhausted = True
            break
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
    evidence = list(document.get("scout_interactions", []))
    primary_state = BlogState(primary, canonical_archive_root(primary), 0)
    for record in _state_records(primary_state):
        evidence.extend(interaction_evidence(primary, record))
    for item in document.get("blogs", []):
        state = BlogState(item["blog"], canonical_archive_root(item["blog"]), 0)
        for record in _state_records(state):
            evidence.extend(interaction_evidence(item["blog"], record))
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
        for candidate, _score in sorted(local_scores.items(), key=lambda pair: (-pair[1], pair[0]))[:config["max_neighbors_per_blog"]]:
            if candidate not in known and len(document.get("blogs", [])) < config["global_context_blog_budget"]:
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


def render_context_pages(primary: str, document: dict[str, Any]) -> None:
    root = NEIGHBORHOODS_DIR / canonical_blog_name(primary)
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    for item in sorted(document.get("blogs", []), key=lambda x: (x.get("distance", 0), -x.get("observed_interaction_count", 0), x["blog"])):
        raw = item.get("raw_relationship_counts", {})
        reasons = []
        if raw.get("target_reblogs_from"):
            reasons.append(f"target reblogs from {raw['target_reblogs_from']} times")
        if raw.get("other_reblogs_target"):
            reasons.append(f"reblogs target {raw['other_reblogs_target']} times")
        if item.get("relationship_components", {}).get("reciprocal_activity"):
            reasons.append("observed activity in both directions")
        relationship_text = "; ".join(reasons) or "relationship evidence unavailable"
        rows.append(
            f'<li><a href="../../Backups/{escape(item["blog"])}/index.html">{escape(item["blog"])}</a> '
            f'- {item.get("observed_interaction_count", 0)} observed interactions; '
            f'{item.get("current_sample_size", 0)} sampled posts; distance {item.get("distance", 0)}; '
            f'{escape(relationship_text)}'
            + ("; discovered, 0 posts preserved locally" if int(item.get("current_sample_size", 0)) == 0 else "")
            + "</li>"
        )
        links = []
        for evidence in item.get("evidence", []):
            links.append(f'<li><a href="{escape(str(evidence.get("source_post_url") or "#"))}">{escape(str(evidence.get("kind")))}</a> from captured post {escape(str(evidence.get("source_post_id")))}</li>')
        html = (
            "<!doctype html><meta charset=utf-8><title>Context snapshot - " + escape(item["blog"]) + "</title>"
            '<body><header>'
            f'<h1>{escape(item["blog"])}</h1><p>Context snapshot captured around {escape(primary)}</p>'
            f'<p>Current sample size: {item.get("current_sample_size", 0)}; configured cap: {item.get("configured_cap", 0)}</p>'
            '<p>This is not a complete backup of this blog.</p></header>'
            '<h2>Interactions</h2><ul>' + "".join(links) + '</ul>'
            f'<p><a href="../../Backups/{escape(item["blog"])}/index.html">Canonical blog archive</a> | <a href="index.html">Blog neighborhood</a></p></body>'
        )
        # Neighborhood pages are views; they do not create a second archive.
        item["view_html"] = html
    html = _archive_shell(
        f"Blog neighborhood - {primary}",
        f'<h1>Blog neighborhood around {escape(primary)}</h1>'
        '<p>Frequently interacting blogs observed in captured public data.</p><ul>'
        + "".join(rows) + "</ul>",
        [
            ("Blogs", "../../Backups/index.html"),
            ("Dashboard", "../../Backups/dashboard.html"),
            ("Tags", "../../Backups/tags/index.html"),
            ("Neighborhoods", "../index.html"),
            (primary, f"../../Backups/{primary}/index.html"),
        ],
        active="Neighborhoods",
        stylesheet="../../Backups/assets/archive.css",
        script="../../Backups/assets/archive.js",
    )
    (root / "index.html").write_text(html, encoding="utf-8")


def render_neighborhood_index() -> None:
    root = NEIGHBORHOODS_DIR
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    for path in sorted(root.iterdir()):
        if not path.is_dir() or not (path / "index.html").is_file():
            continue
        rows.append(f'<li><a href="{escape(path.name)}/index.html">{escape(path.name)}</a> <span class="archive-meta">target-centered neighborhood</span></li>')
    (root / "index.html").write_text(
        _archive_shell(
            "Neighborhoods",
            '<h1>Neighborhoods</h1><p>Observed blog relationships and bounded context.</p><ul>' + "".join(rows) + "</ul>",
            [
                ("Blogs", "../Backups/index.html"),
                ("Dashboard", "../Backups/dashboard.html"),
                ("Tags", "../Backups/tags/index.html"),
                ("Neighborhoods", "index.html"),
            ],
            active="Neighborhoods",
            stylesheet="../Backups/assets/archive.css",
            script="../Backups/assets/archive.js",
        ),
        encoding="utf-8",
    )


def augment_primary_index(primary: str, document: dict[str, Any]) -> None:
    index = canonical_archive_root(primary) / "index.html"
    if not index.is_file():
        return
    text = index.read_text(encoding="utf-8")
    marker_start = "<!-- puppetbackup-context-start -->"
    marker_end = "<!-- puppetbackup-context-end -->"
    if not document.get("blogs"):
        return
    rows = []
    for item in sorted(document["blogs"], key=lambda x: (-x.get("observed_interaction_count", 0), x["blog"] ))[:10]:
        rows.append(f'<li><a href="../Neighborhoods/{escape(primary)}/index.html">{escape(item["blog"])}</a> - {item.get("observed_interaction_count", 0)} observed interactions; {item.get("current_sample_size", 0)} sampled posts</li>')
    block = marker_start + '<section class="context"><h2>Frequently interacting blogs</h2><ul>' + "".join(rows) + f'</ul><p><a href="../Neighborhoods/{escape(primary)}/index.html">Explore blog neighborhood</a></p></section>' + marker_end
    if marker_start in text and marker_end in text:
        before = text.split(marker_start, 1)[0]
        after = text.split(marker_end, 1)[1]
        text = before + block + after
    else:
        text = text.replace("</header>", "</header>\n" + block, 1)
    tmp = index.with_suffix(index.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, index)


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
    render_context_pages(primary, document)
    if finalize_primary:
        primary_state = BlogState(primary, canonical_archive_root(primary), 0)
        activate_blog_state(primary_state)
        if completed_post_files():
            finalize_archive_index()
            augment_primary_index(primary, document)
        for item in document.get("blogs", []):
            state = context_state(primary, item)
            activate_blog_state(state)
            ensure_blog_stylesheets()
            if completed_post_files():
                finalize_archive_index()


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
    depth: int = 2
    network: str = "Gentle"
    budget_limit: int = 0
    budget_used: int = 0
    saved_by_lane: dict[str, int] = field(default_factory=lambda: {"target": 0, "depth1": 0, "depth2": 0})
    queued_by_lane: dict[str, int] = field(default_factory=lambda: {"target": 0, "depth1": 0, "depth2": 0})
    scouted: int = 0
    known_blogs: int = 0
    active_blogs: int = 0
    repairs: int = 0
    current_action: str = "Starting"
    last_saved: str = ""
    warnings: list[str] = field(default_factory=list)
    warning_counts: dict[str, int] = field(default_factory=dict)
    warning_details: dict[str, list[str]] = field(default_factory=dict)
    warning_labels: dict[str, str] = field(default_factory=dict)
    run_id: str = ""
    acquisition_events: list[dict[str, Any]] = field(default_factory=list)
    runtime: RuntimeControls | None = None
    current_reason: str = ""
    current_action_kind: str = ""
    context_bracketed: int = 0
    context_incomplete: int = 0
    deferred_actions: int = 0
    lane_status: dict[str, LaneRuntimeState] = field(default_factory=dict)
    source_failures: list[dict[str, Any]] = field(default_factory=list)

    def record_acquisition(self, candidate: ActionCandidate, blog: str, post_id: str) -> None:
        if any(event.get("blog") == blog and event.get("post_id") == post_id for event in self.acquisition_events):
            return
        event = {
            "run_id": self.run_id,
            "blog": blog,
            "post_id": post_id,
            "graph_depth": candidate.graph_depth,
            "acquisition_lane": candidate.lane,
            "lane": candidate.lane,
            "state": "acquired",
            "planned_cost": candidate.planned_cost,
            "actual_cost": 1,
            "action_kind": candidate.kind,
            "reason_codes": list(candidate.reason_codes),
        }
        self.acquisition_events.append(event)
        self.budget_used = len(self.acquisition_events)
        self.saved_by_lane[candidate.lane] = self.saved_by_lane.get(candidate.lane, 0) + 1
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
            self.current_action_kind = ""
            self.current_reason = fallback
            self.current_action = fallback
            return
        self.current_action_kind = action.kind
        self.current_reason = ", ".join(action.reason_codes)
        self.current_action = action.kind.replace("_", " ")
        if action.blog:
            self.current_action += f" {action.blog}"
        if action.target_post_id:
            self.current_action += f" / {action.target_post_id}"


class ProgressRenderer:
    def __init__(self, status: CrawlerStatus, stream: Any = None, verbose: bool = False) -> None:
        self.status = status
        self.stream = stream or sys.stdout
        self.verbose = verbose
        self.terminal = bool(getattr(self.stream, "isatty", lambda: False)())
        self.android_compact = self.terminal and is_android_runtime()
        self.live = self.terminal and not self.android_compact and os.environ.get("TERM") not in {None, "dumb"}
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
            f"Target {s.saved_by_lane['target']} | "
            f"Nearby {s.saved_by_lane['depth1']} | "
            f"Outer {s.saved_by_lane['depth2']} | {action}"
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
                self._fit(f"T {s.saved_by_lane['target']}  N {s.saved_by_lane['depth1']}  O {s.saved_by_lane['depth2']}"),
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
            self._fit(self._lane_line(s, "target", "Target")),
            self._fit(self._lane_line(s, "depth1", "Nearby")),
            self._fit(self._lane_line(s, "depth2", "Outer")),
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
                f"Target: {s.saved_by_lane['target']} | Nearby: {s.saved_by_lane['depth1']} | "
                f"Outer: {s.saved_by_lane['depth2']} | {s.current_action}"
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
            if self.android_compact:
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
            self.android_compact = False
            self.previous_lines = 0
            self.compact_line_active = False
        self.last_emit = now

    def finish(self) -> None:
        try:
            if self.android_compact and self.compact_line_active:
                self.stream.write("\n")
                self.stream.flush()
                self.compact_line_active = False
            elif self.live and self.previous_lines:
                self.stream.write("\n")
                self.stream.flush()
                self.previous_lines = 0
        except (OSError, ValueError, AttributeError):
            self.live = False
            self.android_compact = False
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


def run_incremental_capture(
    primary: str,
    max_posts: int,
    full_res: bool,
    mode: str,
    requested_depth: int | None,
    context_policy: dict[str, Any],
    focus: str | None = None,
) -> tuple[BlogState, dict[str, Any]]:
    config = context_config(context_policy, mode, requested_depth, focus)
    reconcile_acquisition_ledger()
    document = load_context_document(primary)
    document.update({
        "schema_version": 1,
        "primary_blog": primary,
        "mode": mode,
        "requested_depth": requested_depth,
        "primary_max_posts": max_posts,
        "policy_snapshot": config,
        "scheduler": config["scheduler"],
        "crawl_status": "partial",
    })
    for item in document.get("blogs", []):
        item["run_blocked"] = False
    primary_state = BlogState(primary, canonical_archive_root(primary), max_posts, lane="target")
    run_id = uuid.uuid4().hex
    status = CrawlerStatus(
        target=primary,
        focus=config["focus"],
        depth=int(config["max_depth"]),
        network=str(NETWORK_PROFILE.get("label", "")),
        budget_limit=max_posts,
        run_id=run_id,
    )
    runtime = RuntimeControls(
        focus_bias={"deep": 0.0, "balanced": 0.5, "wide": 1.0, "neighbors": 1.2}.get(config["focus"], 0.5),
        budget_limit=max_posts,
        network_profile_id=str(NETWORK_PROFILE.get("id", "gentle")),
    )
    global ACTIVE_RUNTIME, ACTIVE_PROGRESS_RENDERER
    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, request_graceful_cancel)
    ACTIVE_RUNTIME = runtime
    status.runtime = runtime
    primary_state.run_id = run_id
    primary_state.progress = status
    renderer = ProgressRenderer(status)
    ACTIVE_PROGRESS_RENDERER = renderer

    def run_blog_step(state: BlogState, operation: str, callback: Any) -> tuple[bool, Any]:
        try:
            return True, callback()
        except BlogSourceFailure as failure:
            mark_blog_source_failure(primary, document, state, failure, status)
        except Exception as exc:
            if operation not in {"repair", "render"}:
                raise
            failure = BlogRenderFailure(
                state.blog,
                operation,
                f"Local {operation} failed for {state.blog}: {exc}",
                kind="render_pending",
                retryable=True,
            )
            mark_blog_source_failure(primary, document, state, failure, status)
        save_context_document(primary, document)
        renderer.render(force=True)
        return False, None

    activate_blog_state(primary_state)
    startup_ok, _ = run_blog_step(
        primary_state,
        "repair",
        lambda: (ensure_blog_stylesheets(), repair_interrupted_work()),
    )

    context_states: dict[str, BlogState] = {
        item["blog"]: context_state(primary, item) for item in document.get("blogs", [])
    }
    repaired_context: set[str] = set()

    # Scout the target before substantial acquisition. The scout retains raw
    # feed records in memory so the first acquisition can reuse them.
    discovery_records = 0
    discovery_pages = 0
    discovery_found = False
    while discovery_pages < config["scout_initial_pages"] and discovery_records < config["scout_initial_records"]:
        status.current_action = "Scouting target"
        ok, result = run_blog_step(
            primary_state,
            "scout",
            lambda: scout_blog_page(primary_state, primary, document),
        )
        if not ok:
            break
        discovery_pages += 1
        discovery_records += result.observed
        discovery_found = discovery_found or bool(result.candidates)
        if result.exhausted:
            primary_state.exhausted = True
        recompute_context_document(primary, document, context_policy, config)
        save_context_document(primary, document)
        status.scouted = primary_state.scouted
        status.known_blogs = len(document.get("blogs", [])) + 1
        renderer.render()
        if discovery_found or result.exhausted:
            break

    context_states: dict[str, BlogState] = {
        item["blog"]: context_state(primary, item) for item in document.get("blogs", [])
    }
    for state in context_states.values():
        state.run_id = run_id
        state.progress = status
    status.known_blogs = len(document.get("blogs", [])) + 1
    recompute_context_document(primary, document, context_policy, config)
    save_context_document(primary, document)
    status.context_bracketed, status.context_incomplete = context_deficit_counts(document)

    lane_order = {"target": 0, "depth1": 1, "depth2": 2}
    no_progress_cycles = 0
    key_controller = RuntimeKeyController(runtime, status)

    def state_has_ready_sources(state: BlogState) -> bool:
        return any(
            source.get("id") is not None
            and not (state.json_dir / f"{source['id']}.json").is_file()
            for source in state.feed_buffer
        )

    def lane_items(lane: str) -> list[dict[str, Any]]:
        distance = 1 if lane == "depth1" else 2
        return [
            item for item in eligible_context_items(document, config, set())
            if int(item.get("distance", 0)) == distance
        ]

    def lane_runtime() -> dict[str, LaneRuntimeState]:
        shares = effective_focus_shares(config, runtime.focus_bias)
        active_budget = runtime.budget_limit
        lanes = {
            lane: LaneRuntimeState(
                lane=lane,
                actual=status.saved_by_lane.get(lane, 0),
                desired=(active_budget * shares[lane] / 100.0) if active_budget else 0.0,
            )
            for lane in ("target", "depth1", "depth2")
        }
        for info in lanes.values():
            info.debt = max(0.0, info.desired - info.actual)

        target_ready = not primary_state.exhausted and not primary_state.blocked_for_run and state_has_ready_sources(primary_state)
        if primary_state.blocked_for_run:
            lanes["target"].state = "BLOCKED"
            lanes["target"].blocked_reason = primary_state.error or "target source blocked"
        elif target_ready:
            lanes["target"].state = "READY"
            lanes["target"].ready = 1
        elif not primary_state.exhausted:
            lanes["target"].state = "DISCOVERING"
            lanes["target"].discovering = 1
        else:
            lanes["target"].state = "EXHAUSTED"

        for lane in ("depth1", "depth2"):
            distance = 1 if lane == "depth1" else 2
            items = lane_items(lane)
            ready = 0
            discovering = 0
            blocked = 0
            for item in items:
                state = context_states.setdefault(item["blog"], context_state(primary, item))
                if item.get("run_blocked"):
                    blocked += 1
                elif not state.exhausted and state_has_ready_sources(state):
                    ready += 1
                elif not state.exhausted:
                    discovering += 1
            info = lanes[lane]
            info.ready = ready
            info.discovering = discovering
            if distance > int(config["max_depth"]):
                info.state = "EXHAUSTED"
                info.blocked_reason = "outside selected depth"
            elif ready:
                info.state = "READY"
            elif discovering:
                info.state = "DISCOVERING"
            elif items and blocked == len(items):
                info.state = "BLOCKED"
                info.blocked_reason = "all known blogs blocked"
            elif lane == "depth1" and not primary_state.exhausted:
                info.state = "DISCOVERING"
                info.discovering = 1
                info.blocked_reason = "waiting on target discovery"
            elif lane == "depth2" and lanes["depth1"].state not in {"EXHAUSTED", "BLOCKED"}:
                info.state = "DISCOVERING"
                info.discovering = 1
                info.blocked_reason = "waiting on nearby discovery"
            else:
                info.state = "EXHAUSTED"
        if runtime.focus_bias < 1.2:
            active = [lane for lane, info in lanes.items() if info.state not in {"EXHAUSTED", "BLOCKED"}]
            share_total = sum(shares[lane] for lane in active)
            if active and share_total > 0:
                for lane, info in lanes.items():
                    if lane in active:
                        info.desired = active_budget * shares[lane] / share_total if active_budget else 0.0
                        info.debt = max(0.0, info.desired - info.actual)
                    else:
                        info.desired = 0.0
                        info.debt = 0.0
        status.update_lane_status(lanes)
        return lanes

    def lane_has_actionable_work(lane: str, info: LaneRuntimeState, lanes: dict[str, LaneRuntimeState]) -> bool:
        if lane == "target":
            return info.state == "READY" or info.state == "DISCOVERING"
        if info.state == "READY":
            return True
        if info.state != "DISCOVERING":
            return False
        return info.discovering > 0 and not info.blocked_reason.startswith("waiting on ")

    while True:
        key_controller.poll()
        if runtime.cancel_requested:
            status.current_action = "Cancel requested; finalizing"
            status.current_reason = "current atomic work complete"
            for item in document.get("blogs", []):
                if item.get("status") in ("queued", "probe", "partial"):
                    item["run_deferred"] = True
            break
        if acquisition_budget_exhausted(status):
            status.current_action = "Budget exhausted; finalizing"
            for item in document.get("blogs", []):
                if item.get("status") in ("queued", "probe", "partial"):
                    item["run_deferred"] = True
            break

        status.budget_limit = runtime.budget_limit
        lanes = lane_runtime()
        debt_lanes = [
            lane for lane, info in lanes.items()
            if info.debt > 0 and lane_has_actionable_work(lane, info, lanes)
        ]
        if debt_lanes:
            lane = max(
                debt_lanes,
                key=lambda value: (lanes[value].debt, -lane_order[value]),
            )
        elif lanes["target"].state == "DISCOVERING" and any(
            lanes[lane].debt > 0 for lane in ("depth1", "depth2")
        ):
            # Target discovery is the dependency that can create depth-1 work.
            lane = "target"
        else:
            status.current_action = "No eligible work; finalizing"
            break

        remaining = max(0, runtime.budget_limit - status.budget_used) if runtime.budget_limit else PROCESS_BATCH
        allowance = max(1, min(PROCESS_BATCH, math.ceil(lanes[lane].debt), remaining))
        did_work = False
        progress_before = (
            status.budget_used,
            status.scouted,
            len(document.get("blogs", [])),
            tuple((key, value.state, value.ready, value.discovering) for key, value in lanes.items()),
        )

        if lane == "target":
            primary_state.lane = "target"
            primary_state.batch_limit = allowance
            primary_state.run_id = run_id
            primary_state.progress = status
            if lanes["target"].state != "READY" or lanes["target"].debt <= 0:
                status.set_action(ActionCandidate(
                    identity=f"scout:{primary}:run:{run_id}:maintenance",
                    kind="scout_target",
                    resource_class="scout",
                    lane="target",
                    graph_depth=0,
                    blog=primary,
                    reason_codes=["target scouting remains active at current focus"],
                    planned_cost=0,
                    consumes_budget=False,
                ))
                ok, result = run_blog_step(
                    primary_state,
                    "scout",
                    lambda: scout_blog_page(primary_state, primary, document),
                )
                if not ok:
                    continue
                did_work = result.observed > 0
                if result.exhausted:
                    primary_state.exhausted = True
                recompute_context_document(primary, document, context_policy, config)
                save_context_document(primary, document)
                continue
            context_actions = [
                action for action in context_action_candidates(document, context_policy, "target")
                if any(str(source.get("id")) == action.target_post_id for source in primary_state.feed_buffer)
                and not (primary_state.json_dir / f"{action.target_post_id}.json").is_file()
            ]
            if context_actions and allowance > 0:
                action = max(context_actions, key=lambda value: value.priority)
                status.set_action(action)
                before = status.budget_used
                primary_state.lane_role = "support"
                action.planned_cost = allowance
                primary_state.active_candidate = action
                ok, did_work = run_blog_step(
                    primary_state,
                    "acquire",
                    lambda: acquire_specific_post(primary_state, str(action.target_post_id)),
                )
                primary_state.lane_role = "anchor"
                if not ok:
                    continue
                recompute_context_document(primary, document, context_policy, config)
                save_context_document(primary, document)
                continue
            if not primary_state.feed_buffer:
                status.set_action(ActionCandidate(
                    identity=f"scout:{primary}:run:{run_id}:target",
                    kind="scout_target",
                    resource_class="scout",
                    lane="target",
                    graph_depth=0,
                    blog=primary,
                    reason_codes=["target discovery remains active"],
                    planned_cost=0,
                    consumes_budget=False,
                ))
                ok, result = run_blog_step(
                    primary_state,
                    "scout",
                    lambda: scout_blog_page(primary_state, primary, document),
                )
                if not ok:
                    continue
                did_work = result.observed > 0
                if result.exhausted:
                    primary_state.exhausted = True
                recompute_context_document(primary, document, context_policy, config)
                save_context_document(primary, document)
            else:
                action = ActionCandidate(
                    identity=f"acquire:{primary}:next",
                    kind="acquire_target_post",
                    resource_class="acquisition",
                    lane="target",
                    graph_depth=0,
                    blog=primary,
                    reason_codes=["target macro focus"],
                    planned_cost=allowance,
                    consumes_budget=True,
                )
                status.set_action(action)
                status.current_action = "Acquiring target"
                before = status.budget_used
                primary_state.active_candidate = action
                ok, did_work = run_blog_step(
                    primary_state,
                    "acquire",
                    lambda: acquire_one_target_batch(primary_state),
                )
                if not ok:
                    continue
                recompute_context_document(primary, document, context_policy, config)
                save_context_document(primary, document)
        else:
            distance = 1 if lane == "depth1" else 2
            candidates = lane_items(lane)
            if lanes[lane].state == "READY":
                candidates = [
                    item for item in candidates
                    if state_has_ready_sources(context_states.setdefault(item["blog"], context_state(primary, item)))
                ]
            else:
                candidates = [
                    item for item in candidates
                    if item["blog"] not in repaired_context
                    or not context_states.setdefault(item["blog"], context_state(primary, item)).exhausted
                ]
            if candidates:
                actions = [_action_for_blog(primary, value, config, lane, runtime) for value in candidates]
                action = max(actions, key=lambda value: (value.priority, -value.graph_depth, value.blog or ""))
                item = next(value for value in candidates if value["blog"] == action.blog)
                status.set_action(action)
                state = context_states.setdefault(item["blog"], context_state(primary, item))
                state.run_id = run_id
                state.progress = status
                state.lane = lane
                state.batch_limit = allowance
                context_actions = [
                    action for action in context_action_candidates(document, context_policy, lane)
                    if action.blog == state.blog
                    and any(str(source.get("id")) == action.target_post_id for source in state.feed_buffer)
                    and not (state.json_dir / f"{action.target_post_id}.json").is_file()
                ]
                if context_actions and item["blog"] in repaired_context and allowance > 0:
                    action = max(context_actions, key=lambda value: value.priority)
                    status.set_action(action)
                    state.lane_role = "support"
                    action.planned_cost = allowance
                    state.active_candidate = action
                    ok, did_work = run_blog_step(
                        state,
                        "acquire",
                        lambda: acquire_specific_post(state, str(action.target_post_id)),
                    )
                    state.lane_role = "anchor"
                    if not ok:
                        continue
                    sync_context_state(primary, document, state, int(item.get("configured_cap") or config["posts_per_context_blog"]))
                    recompute_context_document(primary, document, context_policy, config)
                    save_context_document(primary, document)
                    continue
                if item["blog"] not in repaired_context:
                    status.set_action(ActionCandidate(
                        identity=f"repair:{item['blog']}:run:{run_id}",
                        kind="repair_local_post",
                        resource_class="repair",
                        lane=lane,
                        graph_depth=distance,
                        blog=item["blog"],
                        reason_codes=["repair durable local state"],
                        planned_cost=0,
                        consumes_budget=False,
                    ))
                    ok, _ = run_blog_step(
                        state,
                        "repair",
                        lambda: (activate_blog_state(state), ensure_blog_stylesheets(), repair_interrupted_work()),
                    )
                    if not ok:
                        continue
                    repaired_context.add(item["blog"])
                    sync_context_state(primary, document, state, int(item.get("configured_cap") or config["posts_per_context_blog"]))
                    recompute_context_document(primary, document, context_policy, config)
                    save_context_document(primary, document)
                    status.repairs = len(pending_json_ids())
                    status.current_action = f"Repairing {item['blog']}"
                    did_work = True
                    renderer.render()
                    continue
                if not state.exhausted and (state.scouted == 0 or not state.feed_buffer):
                    status.set_action(ActionCandidate(
                        identity=f"scout:{item['blog']}:run:{run_id}:initial",
                        kind="scout_promising_blog",
                        resource_class="scout",
                        lane=lane,
                        graph_depth=distance,
                        blog=item["blog"],
                        reason_codes=["discover deeper opportunities"],
                        planned_cost=0,
                        consumes_budget=False,
                    ))
                    status.current_action = f"Scouting {item['blog']}"
                    ok, result = run_blog_step(
                        state,
                        "scout",
                        lambda: scout_blog_page(state, primary, document),
                    )
                    if not ok:
                        continue
                    did_work = result.observed > 0
                    if result.exhausted:
                        state.exhausted = True
                    recompute_context_document(primary, document, context_policy, config)
                    save_context_document(primary, document)
                else:
                    current = int(item.get("current_sample_size", 0))
                    cap = int(item.get("configured_cap") or config["posts_per_context_blog"])
                    state.phase = "refresh" if current == 0 else "expand"
                    item["status"] = "probe" if current == 0 else "partial"
                    status.current_action = f"Acquiring {item['blog']}"
                    if state_has_ready_sources(state):
                        before = status.budget_used
                        action.planned_cost = allowance
                        state.active_candidate = action
                        ok, did_work = run_blog_step(
                            state,
                            "acquire",
                            lambda: acquire_one_context_batch(state, min(cap, current + allowance)),
                        )
                        if not ok:
                            continue
                        acquired = status.budget_used - before
                        sync_context_state(primary, document, state, cap)
                        recompute_context_document(primary, document, context_policy, config)
                        save_context_document(primary, document)
                    else:
                        item["status"] = "complete" if state.exhausted else item.get("status", "partial")
                        did_work = state.exhausted

        status.scouted = primary_state.scouted + sum(state.scouted for state in context_states.values())
        status.known_blogs = len(document.get("blogs", [])) + 1
        status.repairs = len(pending_json_ids())
        status.context_bracketed, status.context_incomplete = context_deficit_counts(document)
        renderer.render()
        progress_after = (status.budget_used, status.scouted, len(document.get("blogs", [])))
        if did_work and progress_after != progress_before:
            no_progress_cycles = 0
        else:
            no_progress_cycles += 1
        if no_progress_cycles >= 3:
            status.warning("No eligible crawler progress; finalizing safely")
            status.current_action = "No progress; finalizing"
            break

    status.set_action(None, "Complete")
    status.current_reason = ""
    document["crawl_status"] = "complete_with_warnings" if status.source_failures else "complete"
    document["run_status"] = {
        "run_id": run_id,
        "budget_limit": status.budget_limit,
        "budget_used": status.budget_used,
        "saved_by_lane": dict(status.saved_by_lane),
        "scouted": status.scouted,
        "source_failures": list(status.source_failures),
        "blocked_sources": [failure["blog"] for failure in status.source_failures],
        "lane_status": {
            lane: info.snapshot() for lane, info in status.lane_status.items()
        },
        "current_action": status.current_action,
        "runtime_controls": {
            "focus_bias": runtime.focus_bias,
            "budget_limit": runtime.budget_limit,
            "relationship_multiplier": runtime.relationship_multiplier,
            "context_multiplier": runtime.context_multiplier,
            "network_profile_id": runtime.network_profile_id,
            "changes": list(runtime.changes),
        },
    }
    recompute_context_document(primary, document, context_policy, config)
    save_context_document(primary, document)
    mark_presentation_dirty("crawl checkpoint complete")
    renderer.render(force=True)
    renderer.finish()
    ACTIVE_RUNTIME = None
    ACTIVE_PROGRESS_RENDERER = None
    signal.signal(signal.SIGINT, previous_sigint)
    return primary_state, document


def main(argv: list[str] | None = None) -> int:
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
    args = parser.parse_args(argv)
    global VERBOSE
    VERBOSE = bool(args.verbose)
    try:
        policy = load_network_policy()
        selected_profile = resolve_network_profile(policy, args.profile)
    except PolicyError as exc:
        parser.error(f"network policy: {exc}")
    configure(args.username, args.max_posts, full_res=args.full_res, profile=selected_profile)
    try:
        context_policy = load_context_policy()
        context_config(context_policy, args.context, args.context_depth, args.focus)
    except PolicyError as exc:
        parser.error(f"context policy: {exc}")

    print("Tumblr Scraper")
    print(f"Folder: {OUT}")
    if MAX_POSTS:
        print(f"Inspection limit: newest {MAX_POSTS} posts maximum per run")
    else:
        print("Inspection limit: unbounded")
    print(f"Network aggression: {NETWORK_PROFILE['label']}")
    print(f"  {NETWORK_PROFILE['description']}")
    print(f"Crawl focus: {FOCUS_LABELS[args.focus or context_policy.get('default_focus', 'balanced')]}")

    try:
        ensure_tumblr_backup()
        primary_state, document = run_incremental_capture(
            args.username,
            args.max_posts,
            args.full_res,
            args.context,
            args.context_depth,
            context_policy,
            args.focus,
        )
        regenerate_presentation(args.username, document, finalize_primary=True)
        regenerate_global_presentation(force=True)

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
        print(f"Archive:\n{BACKUPS_DIR / 'index.html'}")
        return 0

    except KeyboardInterrupt:
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
            regenerate_presentation(args.username, document, finalize_primary=True)
            regenerate_global_presentation(force=True)
        except Exception:
            pass
        print(
            "\nStopped. Source JSON is written atomically; "
            "unfinished target and context posts will be repaired first next run."
        )
        return 130

    except TumblrRateLimitedError:
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
