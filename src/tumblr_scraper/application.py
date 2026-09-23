"""Application lifecycle orchestration shared by all hosts.

This layer intentionally knows nothing about where a request originated. A
browser, CLI, Pydroid host, or future messaging adapter must translate its
environment-specific event into ordinary crawl values before calling this
controller. Request origin and Tumblr acquisition identity are separate
concepts and are not represented by this lifecycle object.
"""

from __future__ import annotations

import threading
from types import ModuleType
from typing import Any


class Application:
    """Own one crawl lifecycle while delegating policy and work to the core."""

    def __init__(self, core: ModuleType) -> None:
        self.core = core
        self.lock = threading.RLock()
        self.state = "idle"
        self.worker: threading.Thread | None = None
        self.last_request: Any = None
        self.error = ""
        # Browser polls are asynchronous.  A terminal response must never be
        # overwritten by an older in-flight "running" response, so every
        # lifecycle transition carries a monotonic controller revision.
        self.status_revision = 0
        self.cancel_requested = threading.Event()
        self.core._set_lifecycle("idle")

    def _advance_status_revision(self) -> None:
        """Mark a lifecycle transition while ``self.lock`` is held."""
        self.status_revision += 1

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            snapshot = self.core.status_snapshot()
            # A completed/stopped application can outlive the worker or be
            # recreated after a process restart. In that case the durable
            # run_status is the status source, and must pass through the same
            # read-time lane reconciliation as an active CrawlerStatus.
            if self.core.ACTIVE_STATUS is None and self.last_request and self.state in {"idle", "complete", "stopped", "failed"}:
                document = self.core.load_context_document(self.last_request.target)
                persisted = self.core.normalize_persisted_run_status(document.get("run_status"))
                for key in (
                    "run_id", "budget_limit", "budget_used", "saved_by_lane", "saved_by_breadth",
                    "target_posts", "breadth1_posts", "breadth2plus_posts", "nearby_posts", "survey_posts", "unclassified_posts",
                    "unknown_lanes", "unknown_lane_count", "accounted_saved_posts",
                    "accounting_valid", "accounting_errors", "started_at", "finished_at",
                    "strategy", "capture_policy", "completion_reason", "current_reason", "candidate_trace",
                    "candidate_rejections", "scouted", "source_failures", "blocked_sources",
                    "frontier_diagnostics", "phase_timings",
                ):
                    if key in persisted:
                        snapshot[key] = persisted[key]
            snapshot["cancel_requested"] = self.cancel_requested.is_set()
            snapshot.update({
                "lifecycle": self.state,
                "application_state": self.state,
                "status_revision": self.status_revision,
                # Terminal states release the one scheduler.  In particular a
                # safely stopped crawl is ready for a different next request.
                "ready": self.state in {"idle", "complete", "stopped", "failed"},
                "error": self.error,
                "request": ({
                    "target": self.last_request.target,
                    "max_posts": self.last_request.max_posts,
                    "context": self.last_request.context,
                    "context_depth": self.last_request.context_depth,
                    "focus": self.last_request.focus,
                    "profile_id": self.last_request.profile_id,
                    "full_res": self.last_request.full_res,
                    "targets": list(self.last_request.targets),
                    "strategy": self.last_request.strategy,
                    "max_breadth": self.last_request.max_breadth,
                    "max_depth": self.last_request.max_depth,
                    "capture_shape": ([
                        {"breadth": point.breadth, "depth": point.depth}
                        for point in (self.last_request.capture_shape or ())
                    ] if self.last_request.capture_shape is not None else None),
                    "capture_curve": ([
                        {"breadth": point.breadth, "history_posts": point.history_posts}
                        for point in (self.last_request.capture_curve or ())
                    ] if self.last_request.capture_curve is not None else None),
                } if self.last_request else None),
            })
            return snapshot

    def start(self, values: dict[str, Any]) -> dict[str, Any]:
        allowed = {"target", "targets", "max_posts", "context", "context_depth", "focus", "profile_id", "full_res", "strategy", "survey_node_limit", "survey_request_limit", "survey_max_distance", "max_breadth", "max_depth", "capture_shape", "capture_curve"}
        unknown = set(values) - allowed
        if unknown:
            raise self.core.PolicyError("unsupported start fields: " + ", ".join(sorted(unknown)))
        request = self.core.build_crawl_request(
            values.get("target", ""),
            max_posts=values.get("max_posts", 300),
            context=values.get("context", "explore"),
            context_depth=values.get("context_depth", 2),
            focus=values.get("focus"),
            profile_id=values.get("profile_id"),
            full_res=values.get("full_res", False),
            targets=tuple(values.get("targets", ())),
            strategy=values.get("strategy"),
            survey_node_limit=values.get("survey_node_limit"),
            survey_request_limit=values.get("survey_request_limit"),
            survey_max_distance=values.get("survey_max_distance"),
            max_breadth=values.get("max_breadth"),
            max_depth=values.get("max_depth"),
            capture_shape=values.get("capture_shape"),
            capture_curve=values.get("capture_curve"),
        )
        return self.start_request(request)

    def start_request(self, request: Any) -> dict[str, Any]:
        with self.lock:
            if self.state in {"starting", "running", "stopping", "finalizing"}:
                raise self.core.PolicyError("a crawl is already active")
            self.last_request = request
            self.error = ""
            self.cancel_requested.clear()
            self.core.PENDING_CANCEL_EVENT.clear()
            self.state = "starting"
            self._advance_status_revision()
            self.core._set_lifecycle("starting")
            self.worker = threading.Thread(target=self._run, args=(request,), name="crawler-worker", daemon=True)
            self.worker.start()
            return self.snapshot()

    def stop(self) -> dict[str, Any]:
        with self.lock:
            if self.state not in {"starting", "running"}:
                raise self.core.PolicyError("no active crawl to stop")
            self.cancel_requested.set()
            self.core.PENDING_CANCEL_EVENT.set()
            self.state = "stopping"
            self._advance_status_revision()
            self.core._set_lifecycle("finalizing")
            runtime = self.core.ACTIVE_RUNTIME
            status = self.core.ACTIVE_STATUS
        if runtime is None or status is None:
            return self.snapshot()
        return self.core.apply_runtime_control("cancel_requested", True, source="browser")

    def _run(self, request: Any) -> None:
        with self.lock:
            if self.state == "starting":
                self.state = "running"
                self._advance_status_revision()
                self.core._set_lifecycle("running")
        try:
            result = self.core.main(request.argv(), install_signal_handlers=False)
            with self.lock:
                stopped = self.cancel_requested.is_set() or self.core.ACTIVE_LIFECYCLE == "stopped"
                self.state = "stopped" if stopped else ("complete" if result == 0 else "failed")
                self._advance_status_revision()
                if result != 0 and not stopped and not self.error:
                    self.error = f"crawler exited with status {result}"
                self.core._set_lifecycle(self.state)
        except Exception as exc:
            with self.lock:
                if self.cancel_requested.is_set():
                    self.state = "stopped"
                    self._advance_status_revision()
                    self.error = "Stopped safely; work already preserved was kept."
                    self.core._set_lifecycle("stopped")
                else:
                    self.state = "failed"
                    self._advance_status_revision()
                    self.error = str(exc)
                    self.core._set_lifecycle("failed")
        finally:
            self.core.PENDING_CANCEL_EVENT.clear()
