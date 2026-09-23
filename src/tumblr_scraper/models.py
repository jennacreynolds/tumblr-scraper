"""Shared application models and domain errors.

This module defines data passed between the hosts and crawler. It does not
perform network, archive, or presentation work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Any


SCOUT_MAX_PROBES = 16
SCOUT_MAX_OFFSET = 5000

QUALITY_LEVELS = ("NONE", "THUMBNAIL", "COMPACT", "STANDARD", "HIGH")


@dataclass(frozen=True)
class CapturePlan:
    """Downstream acquisition policy; never changes canonical source truth."""

    media_quality: str = "STANDARD"
    profile: bool = True
    avatar: bool = True
    public_likes: bool = False
    public_following: bool = False

    def __post_init__(self) -> None:
        if self.media_quality not in QUALITY_LEVELS:
            raise ValueError(f"unknown media quality: {self.media_quality}")


@dataclass(frozen=True, init=False)
class ObservationLimits:
    """Independent hard limits for relationship observation."""

    max_observed_blogs: int = 0
    max_requests: int = 0
    max_breadth: int = 0
    continue_after_post_budget: bool = False

    def __init__(
        self,
        max_observed_blogs: int = 0,
        max_requests: int = 0,
        max_breadth: int = 0,
        continue_after_post_budget: bool = False,
        *,
        max_distance: int | None = None,
        allow_after_post_budget: bool | None = None,
    ) -> None:
        if max_distance is not None:
            max_breadth = max_distance
        if allow_after_post_budget is not None:
            continue_after_post_budget = allow_after_post_budget
        object.__setattr__(self, "max_observed_blogs", max_observed_blogs)
        object.__setattr__(self, "max_requests", max_requests)
        object.__setattr__(self, "max_breadth", max_breadth)
        object.__setattr__(self, "continue_after_post_budget", continue_after_post_budget)
        self.__post_init__()

    def __post_init__(self) -> None:
        if min(self.max_observed_blogs, self.max_requests, self.max_breadth) < 0:
            raise ValueError("observation limits must be non-negative")

    @property
    def max_distance(self) -> int:
        """Compatibility read alias for historical callers."""
        return self.max_breadth

    @property
    def allow_after_post_budget(self) -> bool:
        """Compatibility read alias for historical callers."""
        return self.continue_after_post_budget


SurveyEnvelope = ObservationLimits


@dataclass(frozen=True)
class CaptureCurvePoint:
    """Desired chronological sample size at one relationship breadth."""

    breadth: int
    history_posts: int | None

    def __post_init__(self) -> None:
        if self.breadth < 0:
            raise ValueError("capture curve breadth must be non-negative")
        if self.history_posts is not None and self.history_posts < 0:
            raise ValueError("capture curve history_posts must be non-negative or null")


@dataclass(frozen=True)
class CaptureShapePoint:
    """One normalized point in a capture shape.

    ``breadth`` and ``depth`` are deliberately unitless fractions.  The
    resolved CapturePolicy applies the user's integer maximum breadth and
    maximum depth afterwards.  This keeps a preset's character stable when
    either scale changes.
    """

    breadth: float
    depth: float

    def __post_init__(self) -> None:
        if not 0.0 <= float(self.breadth) <= 1.0:
            raise ValueError("capture shape breadth must be between 0 and 1")
        if not 0.0 <= float(self.depth) <= 1.0:
            raise ValueError("capture shape depth must be between 0 and 1")


@dataclass(frozen=True)
class CapturePolicy:
    """Resolved, preset-independent crawler policy."""

    max_breadth: int = 1
    max_depth: int = 50
    capture_shape: tuple[CaptureShapePoint, ...] = (
        CaptureShapePoint(0.0, 1.0), CaptureShapePoint(1.0, 1.0),
    )
    # Derived integer facts consumed by the scheduler.  This is retained as a
    # convenience for the existing scheduler, never a second editable policy.
    capture_curve: tuple[CaptureCurvePoint, ...] = ()
    observation_limits: ObservationLimits = ObservationLimits()
    media_curve: tuple[CapturePlan, ...] = (CapturePlan(),)
    enrichment_curve: tuple[CapturePlan, ...] = (CapturePlan(),)
    global_post_budget: int | None = 300
    preset_hint: str | None = None

    def __post_init__(self) -> None:
        if self.max_breadth < 0:
            raise ValueError("max_breadth must be non-negative")
        if self.max_depth < 0:
            raise ValueError("max_depth must be non-negative")
        if self.global_post_budget is not None and self.global_post_budget < 0:
            raise ValueError("global_post_budget must be non-negative or null")
        shape = tuple(sorted(self.capture_shape, key=lambda point: point.breadth))
        if not shape:
            raise ValueError("capture_shape must not be empty")
        if len({point.breadth for point in shape}) != len(shape):
            raise ValueError("capture_shape breadth values must be unique")
        object.__setattr__(self, "capture_shape", shape)
        # A discrete curve is accepted only as a compatibility construction
        # boundary.  New policies always derive it from the normalized shape.
        if self.capture_curve:
            points = tuple(sorted(self.capture_curve, key=lambda point: point.breadth))
            if len({point.breadth for point in points}) != len(points):
                raise ValueError("capture_curve breadth values must be unique")
            object.__setattr__(self, "capture_curve", points)
        else:
            object.__setattr__(self, "capture_curve", tuple(
                CaptureCurvePoint(breadth, self.quantize_depth(self.shape_value(
                    0.0 if self.max_breadth == 0 else breadth / self.max_breadth
                )))
                for breadth in range(self.max_breadth + 1)
            ))

    def shape_value(self, normalized_breadth: float) -> float:
        """Piecewise-linear normalized depth; endpoints extend flat."""
        x = min(1.0, max(0.0, float(normalized_breadth)))
        points = self.capture_shape
        if x <= points[0].breadth:
            return points[0].depth
        for left, right in zip(points, points[1:]):
            if x <= right.breadth:
                span = right.breadth - left.breadth
                if span == 0:
                    return right.depth
                return left.depth + (right.depth - left.depth) * ((x - left.breadth) / span)
        return points[-1].depth

    def quantize_depth(self, normalized_depth: float) -> int:
        """Resolve normalized depth: zero stays observation-only; positive
        values round half-up and never vanish while max_depth is positive.
        """
        y = min(1.0, max(0.0, float(normalized_depth)))
        if y <= 0.0 or self.max_depth == 0:
            return 0
        return min(self.max_depth, max(1, int(self.max_depth * y + 0.5)))

    def history_limit_for_breadth(self, breadth: int) -> int | None:
        """Return the resolved chronological depth at this breadth."""
        selected = self.capture_curve[0].history_posts
        for point in self.capture_curve:
            if point.breadth > breadth:
                break
            selected = point.history_posts
        return selected

    def media_plan_for_breadth(self, breadth: int) -> CapturePlan:
        return self.media_curve[min(max(0, breadth), len(self.media_curve) - 1)]

    def enrichment_plan_for_breadth(self, breadth: int) -> CapturePlan:
        return self.enrichment_curve[min(max(0, breadth), len(self.enrichment_curve) - 1)]

    # Compatibility properties for callers still being migrated. Runtime
    # policy decisions should use max_breadth and the curve methods above.
    @property
    def max_distance(self) -> int:
        return self.max_breadth

    @property
    def survey(self) -> ObservationLimits:
        return self.observation_limits

    @property
    def survey_enabled(self) -> bool:
        return self.observation_limits.max_breadth > 0

    @property
    def preservation_name(self) -> str:
        return self.preset_hint or "custom"

    def plan_for_distance(self, distance: int) -> CapturePlan:
        return self.media_plan_for_breadth(distance)


@dataclass(frozen=True)
class CoverageStrategy:
    """Preservation policy with an optional, independent survey layer."""

    name: str = "neighborhood"
    max_distance: int = 1
    capture_by_distance: tuple[CapturePlan, ...] = (CapturePlan(),)
    survey: SurveyEnvelope = SurveyEnvelope()
    survey_enabled: bool = False
    preservation_name: str = "neighborhood"

    def plan_for_distance(self, distance: int) -> CapturePlan:
        if distance < 0:
            distance = 0
        return self.capture_by_distance[min(distance, len(self.capture_by_distance) - 1)]


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
    scout_initialized: bool = False
    scout_horizon_reached: bool = False
    scout_frontier: dict[str, Any] | None = None
    scout_complete_anchors: list[dict[str, Any]] = field(default_factory=list)
    scout_probe_count: int = 0
    scout_max_probes: int = SCOUT_MAX_PROBES
    scout_max_offset: int = SCOUT_MAX_OFFSET
    discovery_distance: int = 0
    nearest_target_distance: int = 0
    capture_plan: CapturePlan = field(default_factory=CapturePlan)

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
    graph_depth: int = 0
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
    breadth: int | None = None
    # Factual policy inputs.  ``lane`` and ``graph_depth`` remain only as
    # constructor/read compatibility for older callers; new candidates use
    # these fields and the integer relationship coordinate.
    is_target: bool = False
    current_history_count: int = 0
    desired_history_count: int | None = None
    global_remaining: int | None = None
    capture_policy_snapshot: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.breadth is None:
            self.breadth = self.graph_depth
        self.breadth = max(0, int(self.breadth))


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
    breadth: int | None = None

    def __post_init__(self) -> None:
        if self.breadth is None:
            self.breadth = self.graph_depth
        self.breadth = max(0, int(self.breadth))


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
    strategy: str = "neighborhood"
    survey_node_limit: int = 0
    survey_request_limit: int = 0
    survey_max_distance: int = 0

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


@dataclass(frozen=True)
class CrawlRequest:
    """Validated, transport-neutral request for one canonical crawl."""

    target: str = ""
    targets: tuple[str, ...] = ()
    max_posts: int = 300
    context: str = "explore"
    context_depth: int | None = 2
    focus: str | None = None
    profile_id: str | None = None
    full_res: bool = False
    strategy: str | None = None
    survey_node_limit: int | None = None
    survey_request_limit: int | None = None
    survey_max_distance: int | None = None
    max_breadth: int | None = None
    max_depth: int | None = None
    capture_shape: tuple[CaptureShapePoint, ...] | None = None
    capture_curve: tuple[CaptureCurvePoint, ...] | None = None

    def __post_init__(self) -> None:
        values = tuple(dict.fromkeys(str(item).strip().lower() for item in self.targets if str(item).strip()))
        if self.target and self.target not in values:
            values = (self.target.lower(),) + values
        if not values:
            raise ValueError("at least one target is required")
        object.__setattr__(self, "targets", values)
        object.__setattr__(self, "target", values[0])

    def argv(self) -> list[str]:
        args = [self.target, str(self.max_posts), "--profile", str(self.profile_id)]
        if self.targets and tuple(self.targets) != (self.target,):
            args.extend(["--targets", ",".join(self.targets)])
        if self.strategy:
            args.extend(["--strategy", self.strategy])
        if self.survey_max_distance is not None:
            args.extend(["--survey-max-distance", str(self.survey_max_distance)])
        if self.survey_node_limit is not None:
            args.extend(["--survey-node-limit", str(self.survey_node_limit)])
        if self.survey_request_limit is not None:
            args.extend(["--survey-request-limit", str(self.survey_request_limit)])
        if self.max_breadth is not None:
            args.extend(["--max-breadth", str(self.max_breadth)])
        if self.max_depth is not None:
            args.extend(["--max-depth", str(self.max_depth)])
        if self.capture_curve is not None:
            args.extend(["--capture-curve", ",".join(
                f"{point.breadth}:{'' if point.history_posts is None else point.history_posts}"
                for point in self.capture_curve
            )])
        if self.full_res:
            args.append("--full-res")
        args.extend(["--context", self.context])
        if self.context_depth is not None:
            args.extend(["--context-depth", str(self.context_depth)])
        if self.focus is not None:
            args.extend(["--focus", self.focus])
        return args
