"""Policy parsing and pure crawl-configuration calculations.

This module owns policy meaning and validation, but deliberately does not own
where policy files live.  Callers provide policy paths explicitly.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from .models import (
    CaptureCurvePoint,
    CaptureShapePoint,
    CapturePlan,
    CapturePolicy,
    CoverageStrategy,
    ObservationLimits,
    PolicyError,
    SurveyEnvelope,
)


MAX_POLICY_DELAY_SECONDS = 300
MAX_POLICY_WORKERS = 8


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


def load_network_policy(path: Path) -> dict[str, Any]:
    """Load and strictly validate the bundled version-1 network policy."""
    policy_path = Path(path)
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
        "id", "label", "description", "feed_delay_seconds",
        "feed_jitter_seconds", "media_workers",
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
            raise PolicyError(
                f"{context}.media_workers must be an integer from 1 to {MAX_POLICY_WORKERS}"
            )
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


def load_context_policy(path: Path) -> dict[str, Any]:
    policy_path = Path(path)
    try:
        with policy_path.open(encoding="utf-8") as f:
            policy = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise PolicyError(f"could not read context policy: {exc}") from exc
    if not isinstance(policy, dict) or policy.get("version") != 1:
        raise PolicyError("context policy version must be integer 1")
    if "coverage_strategies" in policy:
        if not isinstance(policy["coverage_strategies"], dict) or not policy["coverage_strategies"]:
            raise PolicyError("coverage_strategies must be a non-empty object")
        for name, strategy in policy["coverage_strategies"].items():
            if not isinstance(strategy, dict) or type(strategy.get("max_distance")) is not int or strategy["max_distance"] < 0:
                raise PolicyError(f"coverage strategy {name} has invalid max_distance")
            if not isinstance(strategy.get("capture_by_distance", []), list):
                raise PolicyError(f"coverage strategy {name} capture tiers are invalid")
            for tier in strategy.get("capture_by_distance", []):
                if not isinstance(tier, dict) or tier.get("media_quality", "STANDARD") not in {"NONE", "THUMBNAIL", "COMPACT", "STANDARD", "HIGH"}:
                    raise PolicyError(f"coverage strategy {name} has invalid capture tier")
        if policy.get("default_strategy", "neighborhood") not in policy["coverage_strategies"]:
            raise PolicyError("default_strategy does not name a coverage strategy")
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
        "global_context_post_budget", "default_explore_depth",
        "scout_initial_pages", "scout_initial_records", "initial_neighborhood_posts",
        "scout_gap_max_probes", "scout_gap_max_offset",
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
        "target_reblogs_from": 5.0, "other_reblogs_target": 1.0,
        "target_likes_other": 5.0, "other_likes_target": 1.0,
        "reciprocal_bonus": 2.0, "ask_answer": 1.0,
    })
    context_weights = policy.setdefault("context_weights", {
        "immediate_context_missing": 8.0, "context_distance_decay": 0.5,
        "support_ring_multiplier": 0.35,
    })
    representation_weights = policy.setdefault("representation_weights", {
        "underrepresented_blog_bonus": 4.0, "representation_decay": 1.0,
    })
    for section_name, section in (
        ("relationship_weights", relationship_weights),
        ("context_weights", context_weights),
        ("representation_weights", representation_weights),
    ):
        if not isinstance(section, dict) or any(
            not isinstance(value, (int, float)) or value < 0 for value in section.values()
        ):
            raise PolicyError(f"context {section_name} must contain non-negative numeric weights")
    outward = policy.setdefault("focus_endpoints", {}).setdefault("outward", {
        "target_share": 0, "depth1_share": 60, "depth2_share": 40,
    })
    fields = ("target_share", "depth1_share", "depth2_share")
    if any(not isinstance(outward.get(field), (int, float)) or outward[field] < 0 for field in fields):
        raise PolicyError("context outward focus shares are invalid")
    if sum(outward[field] for field in fields) <= 0:
        raise PolicyError("context outward focus shares must be positive")
    policy.setdefault("action_limits", {
        "max_context_probe_pages": 2, "max_context_ring": 3, "max_actions_per_cycle": 1,
    })
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


def coverage_strategy(policy: dict[str, Any], name: str | None = None,
                      *, max_distance: int | None = None,
                      survey_enabled: bool | None = None) -> CoverageStrategy:
    """Resolve preservation policy plus an optional independent survey layer."""
    strategies = policy.get("coverage_strategies", {})
    selected = name or policy.get("default_strategy", "neighborhood")
    if selected not in strategies:
        raise PolicyError(f"unknown coverage strategy: {selected}")
    enabled = (selected == "survey") if survey_enabled is None else bool(survey_enabled)
    preservation_name = selected
    if selected == "survey":
        preservation_name = str(policy.get("survey_preservation", "explore"))
        if preservation_name not in strategies:
            raise PolicyError("survey_preservation does not name a coverage strategy")
        enabled = True
    raw = strategies[preservation_name]
    preservation_distance = int(raw["max_distance"])
    survey_raw = policy.get("survey_policy", strategies[selected].get("survey", {}))
    envelope = SurveyEnvelope(
        max_observed_blogs=int(survey_raw.get("max_observed_blogs", 0)),
        max_requests=int(survey_raw.get("max_requests", 0)),
        max_breadth=int(survey_raw.get("max_distance", survey_raw.get("max_breadth", 0))),
        continue_after_post_budget=bool(survey_raw.get("allow_after_post_budget", survey_raw.get("continue_after_post_budget", False))),
    )
    if max_distance is not None:
        envelope = SurveyEnvelope(
            envelope.max_observed_blogs, envelope.max_requests,
            max_breadth=int(max_distance),
            continue_after_post_budget=envelope.continue_after_post_budget,
        )
    if envelope.max_distance < 0:
        raise PolicyError("coverage max_distance must be non-negative")
    plans = tuple(CapturePlan(**item) for item in raw.get("capture_by_distance", [{"media_quality": "STANDARD"}]))
    return CoverageStrategy(
        selected, preservation_distance, plans, envelope, enabled, preservation_name,
    )


def _preset_shape(name: str, policy: dict[str, Any] | None = None) -> tuple[CaptureShapePoint, ...]:
    """Preset shapes are normalized, inspectable nozzle settings.

    Their coordinates never encode a particular absolute breadth or depth.
    ``CapturePolicy`` applies the two user-controlled integer scales.
    """
    configured = (policy or {}).get("capture_presets", {}).get(name, {})
    if configured.get("capture_shape"):
        return tuple(
            CaptureShapePoint(float(item["breadth"]), float(item["depth"]))
            for item in configured["capture_shape"]
        )
    shapes = {
        "archive": ((0.0, 1.0), (1.0, 1.0)),
        "neighborhood": ((0.0, 1.0), (1.0, 0.5)),
        "explore": ((0.0, 1.0), (1 / 3, 0.5), (2 / 3, 0.1), (1.0, 0.01)),
        "survey": ((0.0, 1.0), (1 / 3, 1.0), (0.5, 0.5), (1.0, 0.5)),
    }
    try:
        return tuple(CaptureShapePoint(breadth, depth) for breadth, depth in shapes[name])
    except KeyError as exc:
        raise PolicyError(f"unknown capture preset: {name}") from exc


def resolve_capture_policy(
    policy: dict[str, Any],
    preset: str = "neighborhood",
    *,
    max_posts: int = 300,
    max_breadth: int | None = None,
    max_depth: int | None = None,
    capture_shape: tuple[CaptureShapePoint, ...] | None = None,
    capture_curve: tuple[CaptureCurvePoint, ...] | None = None,
    preset_hint: str | None = None,
) -> CapturePolicy:
    """Resolve a named nozzle preset into ordinary policy values.

    Runtime code must consume the returned CapturePolicy and never branch on
    ``preset`` or ``preset_hint``.
    """
    if preset not in {"archive", "neighborhood", "explore", "survey"}:
        raise PolicyError(f"unknown capture preset: {preset}")
    if type(max_posts) is not int or max_posts < 0:
        raise PolicyError("max_posts must be a non-negative integer")
    defaults = policy.get("defaults", {})
    configured_max = {
        "archive": 0,
        "neighborhood": 1,
        "explore": int(defaults.get("default_explore_depth", 3)),
        "survey": 6,
    }.get(preset)
    configured_max = int(
        policy.get("capture_presets", {}).get(preset, {}).get("max_breadth", configured_max)
    )
    breadth_limit = configured_max if max_breadth is None else int(max_breadth)
    if breadth_limit < 0:
        raise PolicyError("max_breadth must be non-negative")
    configured = policy.get("capture_presets", {}).get(preset, {})
    default_depths = {"archive": 300, "neighborhood": 100, "explore": 100, "survey": 2}
    depth_limit = int(configured.get("max_depth", default_depths[preset])) if max_depth is None else int(max_depth)
    if depth_limit < 0:
        raise PolicyError("max_depth must be non-negative")
    shape = capture_shape or _preset_shape(preset, policy)
    raw_limits = policy.get("coverage_strategies", {}).get(preset, {}).get("survey", {})
    if preset == "survey":
        raw_limits = policy.get("survey_policy", raw_limits)
    observed_default = 10000 if breadth_limit > 0 else 0
    request_default = 10000 if breadth_limit > 0 else 0
    observation = ObservationLimits(
        max_observed_blogs=int(raw_limits.get("max_observed_blogs", observed_default) or observed_default),
        max_requests=int(raw_limits.get("max_requests", request_default) or request_default),
        max_breadth=breadth_limit,
        continue_after_post_budget=bool(raw_limits.get("allow_after_post_budget", False)),
    )
    raw_plans = policy.get("coverage_strategies", {}).get(preset, {}).get("capture_by_distance", [])
    if not raw_plans:
        raw_plans = [{"media_quality": "STANDARD"}]
    plans = tuple(CapturePlan(**item) for item in raw_plans)
    return CapturePolicy(
        max_breadth=breadth_limit,
        max_depth=depth_limit,
        capture_shape=tuple(shape),
        capture_curve=tuple(capture_curve or ()),
        observation_limits=observation,
        media_curve=plans,
        enrichment_curve=plans,
        global_post_budget=None if max_posts == 0 else max_posts,
        preset_hint=preset_hint or preset,
    )
