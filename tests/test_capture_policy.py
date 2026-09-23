from pathlib import Path
import time

import main
from tumblr_scraper.config import resolve_capture_policy
from tumblr_scraper.models import ActionCandidate, CaptureCurvePoint, CapturePolicy, CaptureShapePoint


def test_named_presets_equal_manual_resolved_policies():
    source = main.load_context_policy()
    for name in ("archive", "neighborhood", "explore", "survey"):
        preset = resolve_capture_policy(source, name, max_posts=0)
        manual = CapturePolicy(
            max_breadth=preset.max_breadth,
            capture_curve=preset.capture_curve,
            observation_limits=preset.observation_limits,
            media_curve=preset.media_curve,
            enrichment_curve=preset.enrichment_curve,
            global_post_budget=None,
            preset_hint=None,
        )
        assert manual.max_breadth == preset.max_breadth
        assert manual.capture_curve == preset.capture_curve
        assert manual.global_post_budget is None
        assert manual.observation_limits.max_breadth == preset.observation_limits.max_breadth
        assert manual.media_curve == preset.media_curve


def test_preset_hint_does_not_change_resolved_execution_values():
    source = main.load_context_policy()
    a = resolve_capture_policy(source, "explore", max_posts=17, preset_hint="explore")
    b = CapturePolicy(
        max_breadth=a.max_breadth,
        capture_curve=a.capture_curve,
        observation_limits=a.observation_limits,
        media_curve=a.media_curve,
        enrichment_curve=a.enrichment_curve,
        global_post_budget=17,
        preset_hint=None,
    )
    assert (a.max_breadth, a.capture_curve, a.global_post_budget) == (
        b.max_breadth, b.capture_curve, b.global_post_budget
    )


def test_custom_curves_are_piecewise_and_zero_is_observation_only():
    source = main.load_context_policy()
    policy = resolve_capture_policy(
        source,
        "neighborhood",
        max_posts=0,
        max_breadth=8,
        capture_curve=(
            CaptureCurvePoint(0, None),
            CaptureCurvePoint(1, 50),
            CaptureCurvePoint(2, 0),
            CaptureCurvePoint(8, 1),
        ),
    )
    assert policy.history_limit_for_breadth(0) is None
    assert policy.history_limit_for_breadth(2) == 0
    assert policy.history_limit_for_breadth(7) == 0
    assert policy.history_limit_for_breadth(8) == 1


def test_custom_breadth_override_keeps_observation_reach_even_from_archive_preset():
    policy = resolve_capture_policy(
        main.load_context_policy(), "archive", max_posts=0, max_breadth=8,
        capture_curve=(CaptureCurvePoint(0, None), CaptureCurvePoint(8, 0)),
    )
    assert policy.max_breadth == 8
    assert policy.observation_limits.max_breadth == 8
    assert policy.observation_limits.max_observed_blogs > 0


def test_new_candidate_uses_factual_breadth_and_unbounded_remaining():
    candidate = ActionCandidate(
        "candidate", "acquire_canonical_post", "canonical", "policy", 4,
        blog="outer", is_target=False, current_history_count=0,
        desired_history_count=1, global_remaining=None,
        capture_policy_snapshot={"max_breadth": 8},
    )
    assert candidate.breadth == 4
    assert candidate.global_remaining is None
    assert candidate.capture_policy_snapshot["max_breadth"] == 8


def test_curve_controls_are_accessible_and_submit_the_same_policy_shape():
    controls = main._crawler_controls(page=True)
    assert 'id="crawler-max-breadth"' in controls
    assert 'id="crawler-max-depth"' in controls
    assert 'id="crawler-curve-table"' in controls
    script = Path("assets/archive.js").read_text(encoding="utf-8")
    assert "function resolvedCurve" in script
    assert "capture_shape: shapePayload(activeShape)" in script
    assert "function updatePresetState" in script
    assert "status.capture_policy" in script


def test_normalized_shape_scales_breadth_without_changing_vertical_character():
    shape = (CaptureShapePoint(0.0, 1.0), CaptureShapePoint(0.5, 0.5), CaptureShapePoint(1.0, 0.5))
    small = CapturePolicy(max_breadth=3, max_depth=20, capture_shape=shape)
    large = CapturePolicy(max_breadth=12, max_depth=20, capture_shape=shape)
    assert small.history_limit_for_breadth(0) == large.history_limit_for_breadth(0) == 20
    assert small.history_limit_for_breadth(3) == large.history_limit_for_breadth(12) == 10
    assert large.history_limit_for_breadth(6) == 10


def test_normalized_shape_scales_depth_and_preserves_nonzero_quantization():
    shape = (CaptureShapePoint(0.0, 1.0), CaptureShapePoint(1.0, 0.01))
    low = CapturePolicy(max_breadth=6, max_depth=2, capture_shape=shape)
    high = CapturePolicy(max_breadth=6, max_depth=200, capture_shape=shape)
    assert low.history_limit_for_breadth(6) == 1
    assert high.history_limit_for_breadth(6) == 2
    assert low.history_limit_for_breadth(0) == 2
    assert high.history_limit_for_breadth(0) == 200


def test_explicit_observation_limit_replaces_hidden_twenty_blog_cap(monkeypatch):
    document = {"blogs": [], "scout_interactions": [], "adjacency_observations": []}
    evidence = [
        {"source_blog": "target", "from_blog": "target", "to_blog": f"near-{index}", "kind": "direct_reblog"}
        for index in range(25)
    ]
    evidence.append({"source_blog": "near-0", "from_blog": "near-0", "to_blog": "outer", "kind": "direct_reblog"})
    monkeypatch.setattr(main, "recompute_context_deficits", lambda _primary, _document: None)
    monkeypatch.setattr(main, "_all_document_evidence", lambda _primary, _document: evidence)
    config = {"max_depth": 3, "max_observed_blogs": 100, "posts_per_context_blog": 1}
    main.recompute_context_document("target", document, main.load_context_policy(), config)
    assert len(document["blogs"]) == 25
    main.recompute_context_document("target", document, main.load_context_policy(), config)
    assert next(item for item in document["blogs"] if item["blog"] == "outer")["distance"] == 2
    assert document["frontier_diagnostics"]["suppressed_by_observation_limit"] == 0


def test_observation_limit_is_reported_not_frontier_exhaustion(monkeypatch):
    document = {"blogs": [], "scout_interactions": [], "adjacency_observations": []}
    evidence = [
        {"source_blog": "target", "from_blog": "target", "to_blog": f"near-{index}", "kind": "direct_reblog"}
        for index in range(4)
    ]
    monkeypatch.setattr(main, "recompute_context_deficits", lambda _primary, _document: None)
    monkeypatch.setattr(main, "_all_document_evidence", lambda _primary, _document: evidence)
    main.recompute_context_document("target", document, main.load_context_policy(), {
        "max_depth": 3, "max_observed_blogs": 2, "posts_per_context_blog": 1,
    })
    assert len(document["blogs"]) == 2
    assert document["frontier_diagnostics"]["suppressed_by_observation_limit"] == 2


def test_status_serializes_factual_source_failures_and_frontier_limits():
    status = main.CrawlerStatus(target="target", run_id="status-run")
    status.source_failures.append({"blog": "missing-blog", "code": 404, "kind": "http", "operation": "feed", "retryable": False, "message": "missing"})
    status.frontier_diagnostics = {"observed_blogs": 25, "observation_limit": 25, "suppressed_by_observation_limit": 4}
    snapshot = main.status_snapshot(status)
    assert snapshot["source_failures"][0]["blog"] == "missing-blog"
    assert snapshot["frontier_diagnostics"]["suppressed_by_observation_limit"] == 4


def test_frontier_recompute_caches_canonical_evidence_between_scouts(monkeypatch):
    document = {"blogs": [], "scout_interactions": [], "adjacency_observations": []}
    calls = []
    monkeypatch.setattr(main, "_state_records", lambda state: calls.append(state.blog) or [])
    first = main._all_document_evidence("target", document)
    second = main._all_document_evidence("target", document)
    assert first == second == []
    assert calls == ["target"]


def test_resolved_curve_bookkeeping_benchmark_is_bounded():
    """A small deterministic guard against accidental scheduler-scale work."""
    policy = CapturePolicy(
        max_breadth=20,
        max_depth=200,
        capture_shape=(CaptureShapePoint(0.0, 1.0), CaptureShapePoint(0.4, 0.2), CaptureShapePoint(1.0, 0.0)),
    )
    started = time.perf_counter()
    total = 0
    for _ in range(500):
        total += sum(policy.history_limit_for_breadth(breadth) or 0 for breadth in range(21))
    assert total > 0
    assert time.perf_counter() - started < 1.0


def test_historical_lane_normalization_remains_read_only():
    historical = {"budget_used": 8, "saved_by_lane": {"target": 6, "distance-1": 2}}
    normalized = main.normalize_persisted_run_status(historical)
    assert normalized["saved_by_breadth"] == {
        "target": 6, "breadth1": 2, "breadth2plus": 0, "unclassified": 0
    }
    assert historical["saved_by_lane"] == {"target": 6, "distance-1": 2}


def test_policy_candidate_canonical_write_summary_and_status_are_one_path(tmp_path, monkeypatch):
    old_backups = main.CONTENT_ROOT
    main.CONTENT_ROOT = tmp_path / "Backups"
    monkeypatch.setattr(main, "process_batch", lambda ids: len(ids))
    monkeypatch.setattr(main, "capture_blog_profile_metadata", lambda blog: None)
    monkeypatch.setattr(main, "mark_presentation_dirty", lambda reason: None)
    try:
        run_id = "policy-run"
        state = main.BlogState("outer", main.canonical_archive_root("outer"), 0, role="context", distance=4)
        state.run_id = run_id
        candidate = ActionCandidate(
            "policy-run:outer", "acquire_canonical_post", "canonical", "policy", 4,
            blog="outer", is_target=False, desired_history_count=1,
            global_remaining=None, capture_policy_snapshot={"max_breadth": 8},
        )
        source = {"id": 91, "type": "regular", "unix-timestamp": 1, "tumblelog": {"name": "outer"}}
        main._process_source_ids(state, [source], candidate)
        ledger = main._read_acquisition_ledger()
        summary = main.derive_run_summary(run_id, ledger=ledger)
        assert summary.budget_used == 1
        assert summary.breadth2plus_posts == 1
        assert summary.accounted_saved_posts == 1
        row = next(iter(ledger.values()))
        assert row["breadth"] == 4
        assert "acquisition_lane" not in row
        record = next((main.CONTENT_ROOT / "outer" / "json").glob("*.json"))
        payload = __import__("json").loads(record.read_text(encoding="utf-8"))
        assert payload["_puppetbackup_breadth"] == 4
        assert "_puppetbackup_acquisition_lane" not in payload
    finally:
        main.CONTENT_ROOT = old_backups
