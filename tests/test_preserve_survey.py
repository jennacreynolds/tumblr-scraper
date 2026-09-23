import json
from pathlib import Path
from unittest import mock

import main
from tumblr_scraper.config import coverage_strategy, load_context_policy
from tumblr_scraper.coverage import append_frontier, load_coverage_state, save_coverage_state, target_set_id
from tumblr_scraper.graph_projection import build_graph_projection
from tumblr_scraper.graph_slices import write_graph_slices
from tumblr_scraper.models import ActionCandidate, CaptureCurvePoint, CrawlRequest, SurveyEnvelope
from tumblr_scraper.network import parse_public_surface
from tumblr_scraper.observations import ObservationRegistry
from tumblr_scraper.relationships import public_follow_evidence, public_like_evidence
from tumblr_scraper.scheduler import SchedulerBudget, ordered_actions
from tumblr_scraper.application import Application


def test_target_set_and_compact_restart_state(tmp_path: Path):
    targets = ("Beta", "alpha", "alpha")
    assert target_set_id(targets).startswith("set-")
    state = load_coverage_state(tmp_path, targets)
    state["blogs"]["ghost"] = {"status": "OBSERVED", "nearest_target_distance": 9}
    save_coverage_state(tmp_path, targets, state)
    append_frontier(tmp_path, targets, {"blog": "ghost", "distance": 9})
    restored = load_coverage_state(tmp_path, targets)
    assert restored["blogs"]["ghost"]["status"] == "OBSERVED"
    assert (tmp_path / target_set_id(targets) / "frontier.jsonl").is_file()


def test_observation_registry_deduplicates_without_rewriting_coverage(tmp_path: Path):
    registry = ObservationRegistry(tmp_path)
    like = {"kind": "explicit_public_like", "liker_blog": "a", "liked_post_id": "9", "source_blog": "b"}
    assert registry.append(like)
    assert not registry.append(like)
    assert sum(1 for _ in registry.iter_records()) == 1


def test_like_follow_are_directional_and_distinct():
    like = public_like_evidence("a", {"id": 5, "blog_name": "b", "post_url": "https://b.tumblr.com/post/5"}, source_url="https://www.tumblr.com/liked/by/a")
    follow = public_follow_evidence("a", {"name": "b", "url": "https://b.tumblr.com/"}, source_url="https://www.tumblr.com/a/following")
    assert like["kind"] == "explicit_public_like" and like["from_blog"] == "a" and like["to_blog"] == "b"
    assert follow["kind"] == "explicit_public_follow" and follow["from_blog"] == "a" and follow["to_blog"] == "b"


def test_public_surface_fails_closed_and_empty_is_distinct():
    assert parse_public_surface('{"liked_posts": []}', "likes")[0] == "AVAILABLE_EMPTY"
    assert parse_public_surface('<html>client rendered</html>', "likes")[0] == "UNKNOWN_OR_UNAVAILABLE"
    assert parse_public_surface('{"blogs": [{"name": "b"}]}', "following")[0] == "AVAILABLE_NONEMPTY"


def test_post_and_survey_budgets_are_independent():
    budget = SchedulerBudget(max_posts=1)
    budget.record_post()
    envelope = SurveyEnvelope(max_observed_blogs=2, max_requests=2, max_distance=99, allow_after_post_budget=True)
    assert not budget.can_acquire_post()
    assert budget.can_survey(envelope, "ghost")
    budget.record_survey(envelope, "ghost")
    assert budget.survey_requests == 1


def test_scheduler_prioritizes_preservation_and_supports_large_distance():
    candidates = [
        ActionCandidate("survey", "survey", "network", "survey", 99),
        ActionCandidate("target", "target", "canonical", "target", 0),
    ]
    assert ordered_actions(candidates)[0].kind == "target"
    request = CrawlRequest(target="a", targets=("a", "b"), context_depth=99)
    assert request.targets == ("a", "b")


def test_survey_scheduler_yields_to_frontier_after_initial_target_turn():
    target = (0, "target", object(), None, None)
    outward = (2, "ghost", object(), {}, {"distance": 2})
    assert main._select_fair_strategy_candidate([target, outward], 0)[1] == "target"
    assert main._select_fair_strategy_candidate([target, outward], 3)[1] == "ghost"


def test_legacy_lane_aliases_reconcile_to_ui_lane_names():
    status = main.CrawlerStatus("target", budget_limit=3, run_id="run-lanes")
    status.record_acquisition(
        ActionCandidate("neighbor", "sample", "canonical", "distance-1", 1),
        "neighbor",
        "1",
    )
    status.record_acquisition(
        ActionCandidate("outer", "sample", "canonical", "distance-2", 2),
        "outer",
        "2",
    )
    status.record_acquisition(
        ActionCandidate("far", "survey", "canonical", "distance-4", 4),
        "far",
        "3",
    )
    assert status.budget_used == 3
    assert status.saved_by_lane == {"target": 0, "depth1": 1, "depth2": 1, "survey": 1}
    assert {event["acquisition_lane"] for event in status.acquisition_events} == {"depth1", "depth2", "survey"}


def test_historical_mixed_run_status_merges_aliases_without_rewriting():
    historical = {
        "budget_used": 134,
        "saved_by_lane": {"target": 125, "distance-1": 9, "depth1": 0, "depth2": 0},
    }
    normalized = main.normalize_persisted_run_status(historical)
    assert normalized["saved_by_lane"] == {"target": 125, "depth1": 9, "depth2": 0, "survey": 0}
    assert normalized["unknown_lane_count"] == 0
    assert normalized["accounted_saved_posts"] == 134
    assert normalized["accounting_valid"] is True
    assert historical["saved_by_lane"]["distance-1"] == 9


def test_unknown_lane_is_diagnostic_and_not_silently_reclassified():
    lanes, unknown = main.aggregate_lane_counts({"target": 2, "mystery": 3})
    assert lanes["target"] == 2
    assert unknown == {"mystery": 3}
    with __import__("pytest").raises(main.UnknownAcquisitionLane):
        main.canonical_acquisition_lane("mystery")


def test_status_snapshot_reconciles_active_stopped_completed_and_resumed_shapes():
    for lifecycle in ("running", "stopped", "complete"):
        main.ACTIVE_LIFECYCLE = lifecycle
        status = main.CrawlerStatus("revelware", budget_limit=134, run_id="b756")
        status.budget_used = 134
        status.saved_by_lane.update({"target": 125, "distance-1": 9})
        snapshot = main.status_snapshot(status)
        assert snapshot["saved_by_lane"] == {"target": 125, "depth1": 9, "depth2": 0, "survey": 0}
        assert snapshot["accounted_saved_posts"] == 134
        assert snapshot["accounting_valid"] is True


def test_restarted_application_reads_persisted_aliases_for_status_api():
    app = Application(main)
    request = main.build_crawl_request("revelware", max_posts=100, context="explore", context_depth=2)
    app.last_request = request
    app.state = "complete"
    with mock.patch.object(main, "load_context_document", return_value={
        "run_status": {"budget_used": 100, "saved_by_lane": {"target": 94, "distance-1": 6}},
    }), mock.patch.object(main, "ACTIVE_STATUS", None):
        snapshot = app.snapshot()
    assert snapshot["saved_by_lane"] == {"target": 94, "depth1": 6, "depth2": 0, "survey": 0}
    assert snapshot["budget_used"] == snapshot["accounted_saved_posts"] == 100


def test_durable_run_summary_is_the_status_accounting_authority(tmp_path, monkeypatch):
    content = tmp_path / "Archive" / "Content"
    monkeypatch.setattr(main, "CONTENT_ROOT", content)
    run_id = "fixture-run"
    ledger = []
    for index in range(100):
        blog = "target" if index < 94 else "nearby"
        post_id = str(index + 1)
        root = content / blog / "json"
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{post_id}.json").write_text(json.dumps({
            "id": post_id,
            "_puppetbackup_breadth": 0 if index < 94 else 1,
            "_puppetbackup_is_target": index < 94,
        }), encoding="utf-8")
        ledger.append({
            "run_id": run_id,
            "blog": blog,
            "post_id": post_id,
            "breadth": 0 if index < 94 else 1,
            "is_target": index < 94,
            "lane": "target" if index < 94 else "distance-1",
            "state": "acquired",
        })
    (content / "acquisition-ledger.jsonl").write_text("\n".join(json.dumps(item) for item in ledger) + "\n", encoding="utf-8")
    summary = main.derive_run_summary(run_id)
    assert summary.budget_used == 100
    assert summary.target_posts == 94
    assert summary.nearby_posts == 6
    assert summary.survey_posts == 0
    assert summary.unclassified_posts == 0
    assert summary.accounted_saved_posts == 100
    assert {event["raw_lane"] for event in summary.events} == {"target", "distance-1"}
    assert {event["normalized_lane"] for event in summary.events} == {"target", "depth1"}
    assert summary.reconciliation["valid"] is True


def test_survey_frontier_progresses_before_a_large_target_budget_is_spent():
    target = (0, "target", object(), None, None)
    outward = (2, "ghost", object(), {}, {"distance": 2})
    target_turns = 0
    selected = []
    for _ in range(20):
        choice = main._select_fair_strategy_candidate([target, outward], target_turns)
        selected.append(choice[1])
        target_turns = target_turns + 1 if choice[4] is None else 0
    assert "ghost" in selected[:4]
    assert selected.count("ghost") >= 5


def test_archive_policy_keeps_target_preservation_priority_before_bounded_survey_yield():
    target = (0, "target", object(), None, None)
    outward = (2, "ghost", object(), {}, {"distance": 2})
    assert main._select_fair_strategy_candidate([target], 99)[1] == "target"
    assert main._select_fair_strategy_candidate([target, outward], 2)[1] == "target"
    assert main._select_fair_strategy_candidate([target, outward], 3)[1] == "ghost"


def test_enabled_survey_saves_sparse_outward_posts_before_target_budget_is_spent(tmp_path, monkeypatch):
    old = (
        main.CONTENT_ROOT, main.NEIGHBORHOODS_ROOT, main.GLOBAL_CSS,
        main.SOURCE_ASSET_DIR, main.SOURCE_ARCHIVE_CSS, main.SOURCE_ARCHIVE_JS,
    )
    main.CONTENT_ROOT = tmp_path / "Backups"
    main.NEIGHBORHOODS_ROOT = tmp_path / "Neighborhoods"
    main.GLOBAL_CSS = tmp_path / "global.css"
    main.SOURCE_ASSET_DIR = Path("assets")
    main.SOURCE_ARCHIVE_CSS = Path("assets/archive.css")
    main.SOURCE_ARCHIVE_JS = Path("assets/archive.js")
    main.GLOBAL_CSS.write_text("body {}", encoding="utf-8")

    def record(post_id, blog, source=None):
        value = {
            "id": post_id, "type": "regular", "unix-timestamp": post_id,
            "date-gmt": "2026-01-01 00:00:00 GMT", "url": f"https://{blog}.tumblr.com/post/{post_id}",
            "tumblelog": {"name": blog},
        }
        if source:
            value.update({"reblogged-from-name": source, "reblogged-from-url": f"https://{source}.tumblr.com/post/{post_id}"})
        return value

    feeds = {
        "target.tumblr.com": [record(index, "target", "ghost") for index in range(1, 121)],
        "ghost.tumblr.com": [record(1000 + index, "ghost", "far") for index in range(1, 21)],
        "far.tumblr.com": [record(2000 + index, "far") for index in range(1, 10)],
    }

    def fetch(start, count=50):
        page = feeds.get(main.BLOG_HOST, [])[start:start + count]
        return {"posts": page, "posts-total": len(feeds.get(main.BLOG_HOST, [])), "tumblelog": {"name": main.BLOG}}

    policy = main.load_context_policy()
    main.configure("target", 100, profile={"id": "test", "label": "Test", "description": "", "feed_delay_seconds": 0.0, "feed_jitter_seconds": 0.0, "media_workers": 1})
    try:
        with (
            mock.patch.object(main, "fetch_public_page", side_effect=fetch),
            mock.patch.object(main, "process_batch", side_effect=lambda ids: len(ids)),
            mock.patch.object(main, "ensure_blog_stylesheets"),
            mock.patch.object(main, "repair_interrupted_work", return_value=0),
            mock.patch.object(main, "capture_blog_profile_metadata"),
            mock.patch.object(main, "capture_participant_avatars"),
            mock.patch.object(main, "mark_presentation_dirty"),
            mock.patch.object(main.ProgressRenderer, "render"),
            mock.patch.object(main.ProgressRenderer, "finish"),
        ):
            _state, document = main.run_incremental_capture(
                "target", 100, False, "explore", 3, policy,
                strategy_name="survey", survey_max_distance=6,
                survey_node_limit=100, survey_request_limit=100,
            )
        run_status = document["run_status"]
        # Survey is a resolved sparse curve, not a second scheduler. The
        # fixture's curve is target=2, breadth1=2, breadth2=2.
        assert run_status["budget_used"] == 6
        assert run_status["saved_by_lane"]["target"] == 2
        assert run_status["saved_by_lane"]["depth1"] > 0
        assert len(list((main.CONTENT_ROOT / "ghost" / "json").glob("*.json"))) > 0
        trace = run_status.get("candidate_trace", [])
        far_candidates = [entry for entry in trace if entry["blog"] == "far" and entry["distance"] == 2]
        assert far_candidates
        assert all(entry["generated"] for entry in far_candidates)
        assert min(entry["requested_sample_count"] for entry in far_candidates) == 1
        assert all(entry["requested_sample_count"] >= 1 for entry in far_candidates)
        assert any(entry["selected"] for entry in far_candidates)
        far_files = list((main.CONTENT_ROOT / "far" / "json").glob("*.json"))
        assert far_files
        far_rows = [
            row for row in main._read_acquisition_ledger().values()
            if row.get("run_id") == run_status["run_id"] and row.get("blog") == "far"
        ]
        assert far_rows
        assert all(row.get("breadth") == 2 for row in far_rows)
        assert all(row.get("action_kind") == "acquire_canonical_post" for row in far_rows)
        assert all("acquisition_lane" not in row for row in far_rows)
        assert document["survey_enabled"] is True
    finally:
        (
            main.CONTENT_ROOT, main.NEIGHBORHOODS_ROOT, main.GLOBAL_CSS,
            main.SOURCE_ASSET_DIR, main.SOURCE_ARCHIVE_CSS, main.SOURCE_ARCHIVE_JS,
        ) = old


def test_empty_archive_bootstraps_target_candidate_before_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "CONTENT_ROOT", tmp_path / "Archive" / "Content")
    monkeypatch.setattr(main, "NEIGHBORHOODS_ROOT", tmp_path / "Archive" / "Network" / "Neighborhoods")
    monkeypatch.setattr(main, "GLOBAL_CSS", tmp_path / "global.css")
    monkeypatch.setattr(main, "SOURCE_ASSET_DIR", Path("assets"))
    monkeypatch.setattr(main, "SOURCE_ARCHIVE_CSS", Path("assets/archive.css"))
    monkeypatch.setattr(main, "SOURCE_ARCHIVE_JS", Path("assets/archive.js"))
    main.GLOBAL_CSS.write_text("body {}", encoding="utf-8")

    feeds = {
        "revelware.tumblr.com": [
            {
                "id": index,
                "type": "regular",
                "unix-timestamp": index,
                "date-gmt": "2026-01-01 00:00:00 GMT",
                "url": f"https://revelware.tumblr.com/post/{index}",
                "tumblelog": {"name": "revelware"},
            }
            for index in range(1, 5)
        ]
    }
    calls = []

    def fetch(start, count=50):
        calls.append((main.BLOG_HOST, start, count))
        page = feeds.get(main.BLOG_HOST, [])[start:start + count]
        return {"posts": page, "posts-total": len(feeds.get(main.BLOG_HOST, [])), "tumblelog": {"name": "revelware"}}

    policy = load_context_policy(Path("context-policy.json"))
    main.configure(
        "revelware", 3,
        profile={"id": "test", "label": "Test", "description": "", "feed_delay_seconds": 0.0,
                 "feed_jitter_seconds": 0.0, "media_workers": 1},
    )
    with (
        mock.patch.object(main, "fetch_public_page", side_effect=fetch),
        mock.patch.object(main, "process_batch", side_effect=lambda ids: len(ids)),
        mock.patch.object(main, "ensure_blog_stylesheets"),
        mock.patch.object(main, "repair_interrupted_work", return_value=0),
        mock.patch.object(main, "capture_blog_profile_metadata"),
        mock.patch.object(main, "mark_presentation_dirty"),
        mock.patch.object(main.ProgressRenderer, "render"),
        mock.patch.object(main.ProgressRenderer, "finish"),
    ):
        _state, document = main.run_strategy_capture(
            ("revelware",), 3, "none", None, policy, None, "archive",
            None, None, None, False, max_breadth=0,
            capture_curve=(CaptureCurvePoint(0, 2),),
        )

    run_status = document["run_status"]
    files = sorted((main.CONTENT_ROOT / "revelware" / "json").glob("*.json"))
    assert calls, "fresh target must attempt a feed fetch"
    assert len(files) == 2
    assert run_status["budget_used"] == 2
    assert run_status["target_posts"] == 2
    assert run_status["completion_reason"] == "capture curve reached and frontier exhausted"
    assert any(item["blog"] == "revelware" and item["selected"] for item in run_status["candidate_trace"])
    assert all(json.loads(path.read_text())["_puppetbackup_breadth"] == 0 for path in files)


def test_deep_existing_target_does_not_stop_outward_capture(tmp_path, monkeypatch):
    """A satisfied target and an outward curve share one queue and budget."""
    monkeypatch.setattr(main, "CONTENT_ROOT", tmp_path / "Archive" / "Content")
    monkeypatch.setattr(main, "NEIGHBORHOODS_ROOT", tmp_path / "Archive" / "Network" / "Neighborhoods")
    monkeypatch.setattr(main, "GLOBAL_CSS", tmp_path / "global.css")
    monkeypatch.setattr(main, "SOURCE_ASSET_DIR", Path("assets"))
    monkeypatch.setattr(main, "SOURCE_ARCHIVE_CSS", Path("assets/archive.css"))
    monkeypatch.setattr(main, "SOURCE_ARCHIVE_JS", Path("assets/archive.js"))
    main.GLOBAL_CSS.write_text("body {}", encoding="utf-8")

    target_root = main.CONTENT_ROOT / "revelware" / "json"
    target_root.mkdir(parents=True)
    for post_id in range(1, 5):
        (target_root / f"{post_id}.json").write_text(
            json.dumps({
                "id": post_id,
                "id_string": str(post_id),
                "_canonical_blog": "revelware",
            }),
            encoding="utf-8",
        )

    def record(post_id, blog, source=None):
        value = {
            "id": post_id,
            "type": "regular",
            "unix-timestamp": post_id,
            "date-gmt": "2026-01-01 00:00:00 GMT",
            "url": f"https://{blog}.tumblr.com/post/{post_id}",
            "tumblelog": {"name": blog},
        }
        if source:
            value.update({
                "reblogged-from-name": source,
                "reblogged-from-url": f"https://{source}.tumblr.com/post/{post_id}",
            })
        return value

    feeds = {
        "revelware.tumblr.com": [record(post_id, "revelware", "outer") for post_id in range(1, 41)],
        "outer.tumblr.com": [record(100 + post_id, "outer", "far") for post_id in range(1, 3)],
        "far.tumblr.com": [record(200 + post_id, "far") for post_id in range(1, 3)],
    }
    calls = []

    def fetch(start, count=50):
        calls.append(main.BLOG_HOST)
        page = feeds.get(main.BLOG_HOST, [])[start:start + count]
        return {
            "posts": page,
            "posts-total": len(feeds.get(main.BLOG_HOST, [])),
            "tumblelog": {"name": main.BLOG},
        }

    policy = load_context_policy(Path("context-policy.json"))
    main.configure(
        "revelware", 2,
        profile={
            "id": "test", "label": "Test", "description": "",
            "feed_delay_seconds": 0.0, "feed_jitter_seconds": 0.0,
            "media_workers": 1,
        },
    )
    with (
        mock.patch.object(main, "fetch_public_page", side_effect=fetch),
        mock.patch.object(main, "process_batch", side_effect=lambda ids: len(ids)),
        mock.patch.object(main, "ensure_blog_stylesheets"),
        mock.patch.object(main, "repair_interrupted_work", return_value=0),
        mock.patch.object(main, "capture_blog_profile_metadata"),
        mock.patch.object(main, "mark_presentation_dirty"),
        mock.patch.object(main.ProgressRenderer, "render"),
        mock.patch.object(main.ProgressRenderer, "finish"),
    ):
        _state, document = main.run_strategy_capture(
            ("revelware",), 20, "none", None, policy, None, "survey",
            None, None, None, False, max_breadth=2,
            capture_curve=(CaptureCurvePoint(0, None), CaptureCurvePoint(1, 1), CaptureCurvePoint(2, 1)),
        )

    run_status = document["run_status"]
    outer_files = sorted((main.CONTENT_ROOT / "outer" / "json").glob("*.json"))
    far_files = sorted((main.CONTENT_ROOT / "far" / "json").glob("*.json"))
    target_files = sorted((main.CONTENT_ROOT / "revelware" / "json").glob("*.json"))
    assert "revelware.tumblr.com" in calls
    assert "outer.tumblr.com" in calls
    assert "far.tumblr.com" in calls
    assert len(target_files) > 4, "target acquisition should remain active"
    assert len(outer_files) == 1, "breadth-1 work must proceed after target depth is satisfied"
    assert len(far_files) == 1, "breadth-2 work must proceed after target depth is satisfied"
    outer_record = json.loads(outer_files[0].read_text(encoding="utf-8"))
    assert outer_record["_puppetbackup_breadth"] == 1
    assert outer_record["_puppetbackup_is_target"] is False
    far_record = json.loads(far_files[0].read_text(encoding="utf-8"))
    assert far_record["_puppetbackup_breadth"] == 2
    assert far_record["_puppetbackup_is_target"] is False
    assert run_status["budget_used"] == 20
    assert run_status["target_posts"] > 0
    assert run_status["breadth1_posts"] == 1
    assert run_status["breadth2plus_posts"] == 1
    ledger_rows = [
        row for row in main._read_acquisition_ledger().values()
        if row.get("run_id") == run_status["run_id"]
    ]
    assert len(ledger_rows) == run_status["budget_used"]
    assert {("outer", 1), ("far", 2)} <= {
        (row["blog"], row["breadth"]) for row in ledger_rows
    }
    assert len({(row["blog"], row["post_id"]) for row in ledger_rows}) == len(ledger_rows)
    assert all(
        not (row["blog"] == "revelware" and int(row["post_id"]) <= 4)
        for row in ledger_rows
    )
    assert any(
        item["blog"] == "outer" and item["selected"]
        for item in run_status["candidate_trace"]
    )


def test_blocked_outward_source_is_skipped_so_next_blog_can_be_sampled(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "CONTENT_ROOT", tmp_path / "Archive" / "Content")
    monkeypatch.setattr(main, "NEIGHBORHOODS_ROOT", tmp_path / "Archive" / "Network" / "Neighborhoods")
    monkeypatch.setattr(main, "GLOBAL_CSS", tmp_path / "global.css")
    monkeypatch.setattr(main, "SOURCE_ASSET_DIR", Path("assets"))
    monkeypatch.setattr(main, "SOURCE_ARCHIVE_CSS", Path("assets/archive.css"))
    monkeypatch.setattr(main, "SOURCE_ARCHIVE_JS", Path("assets/archive.js"))
    main.GLOBAL_CSS.write_text("body {}", encoding="utf-8")

    target_root = main.CONTENT_ROOT / "target" / "json"
    target_root.mkdir(parents=True)
    for post_id in (1, 2):
        (target_root / f"{post_id}.json").write_text(
            json.dumps({"id": post_id, "id_string": str(post_id), "_canonical_blog": "target"}),
            encoding="utf-8",
        )

    def record(post_id, blog, source):
        return {
            "id": post_id,
            "type": "regular",
            "unix-timestamp": post_id,
            "date-gmt": "2026-01-01 00:00:00 GMT",
            "url": f"https://{blog}.tumblr.com/post/{post_id}",
            "tumblelog": {"name": blog},
            "reblogged-from-name": source,
            "reblogged-from-url": f"https://{source}.tumblr.com/post/{post_id}",
        }

    feeds = {
        "target.tumblr.com": [
            record(1, "target", "bad-blog"),
            record(2, "target", "good-blog"),
        ],
        "good-blog.tumblr.com": [{
            "id": 100,
            "type": "regular",
            "unix-timestamp": 100,
            "date-gmt": "2026-01-01 00:00:00 GMT",
            "url": "https://good-blog.tumblr.com/post/100",
            "tumblelog": {"name": "good-blog"},
        }],
    }

    def fetch(start, count=50):
        if main.BLOG_HOST == "bad-blog.tumblr.com":
            raise main.BlogSourceFailure(
                "bad-blog", "feed", "fixture source unavailable", code=404,
                kind="unavailable", retryable=False,
            )
        page = feeds.get(main.BLOG_HOST, [])[start:start + count]
        return {
            "posts": page,
            "posts-total": len(feeds.get(main.BLOG_HOST, [])),
            "tumblelog": {"name": main.BLOG},
        }

    policy = load_context_policy(Path("context-policy.json"))
    main.configure(
        "target", 1,
        profile={
            "id": "test", "label": "Test", "description": "",
            "feed_delay_seconds": 0.0, "feed_jitter_seconds": 0.0,
            "media_workers": 1,
        },
    )
    with (
        mock.patch.object(main, "fetch_public_page", side_effect=fetch),
        mock.patch.object(main, "process_batch", side_effect=lambda ids: len(ids)),
        mock.patch.object(main, "ensure_blog_stylesheets"),
        mock.patch.object(main, "repair_interrupted_work", return_value=0),
        mock.patch.object(main, "capture_blog_profile_metadata"),
        mock.patch.object(main, "mark_presentation_dirty"),
        mock.patch.object(main.ProgressRenderer, "render"),
        mock.patch.object(main.ProgressRenderer, "finish"),
    ):
        _state, document = main.run_strategy_capture(
            ("target",), 1, "none", None, policy, None, "survey",
            None, None, None, False, max_breadth=1,
            capture_curve=(CaptureCurvePoint(0, None), CaptureCurvePoint(1, 1)),
        )

    bad = next(item for item in document["blogs"] if item["blog"] == "bad-blog")
    good_files = list((main.CONTENT_ROOT / "good-blog" / "json").glob("*.json"))
    run_status = document["run_status"]
    assert bad["run_blocked"] is True
    assert len(good_files) == 1
    assert run_status["budget_used"] == 1
    assert any(item["blog"] == "bad-blog" for item in run_status["source_failures"])
    assert any(
        item["blog"] == "good-blog" and item["selected"]
        for item in run_status["candidate_trace"]
    )


def test_archive_without_survey_keeps_outward_sampling_disabled(tmp_path):
    policy = load_context_policy(Path("context-policy.json"))
    resolved = coverage_strategy(policy, "archive")
    assert resolved.max_distance == 0
    assert resolved.survey_enabled is False


def test_blog_stylesheet_setup_uses_active_blog_without_presentation_name_error(tmp_path, monkeypatch):
    old = main.CONTENT_ROOT, main.GLOBAL_CSS
    main.CONTENT_ROOT = tmp_path / "Backups"
    main.GLOBAL_CSS = tmp_path / "global.css"
    main.GLOBAL_CSS.write_text("body {}", encoding="utf-8")
    main.configure("target", 10, profile={"id": "test", "label": "Test", "description": "", "media_workers": 1})
    try:
        main.ensure_blog_stylesheets()
    finally:
        main.CONTENT_ROOT, main.GLOBAL_CSS = old


def test_graph_reads_observation_nodes_and_bounds_projection(tmp_path: Path):
    neighborhoods = tmp_path / "Neighborhoods" / "set-x" / "observations"
    neighborhoods.mkdir(parents=True)
    with (neighborhoods / "00.jsonl").open("w", encoding="utf-8") as handle:
        for index in range(1200):
            handle.write(json.dumps({"kind": "explicit_public_follow", "from_blog": "target", "to_blog": f"ghost-{index}"}) + "\n")
    graph = build_graph_projection(tmp_path / "Backups", tmp_path / "Neighborhoods", max_nodes=1000)
    assert len(graph["nodes"]) <= 1001
    assert any(node["archive_status"] == "observed" for node in graph["nodes"])


def test_graph_slices_are_static_and_bounded(tmp_path: Path):
    graph = {"format": "tumblr-archive-graph", "nodes": [{"id": str(i)} for i in range(2100)], "edges": []}
    index = write_graph_slices(graph, tmp_path / "Graph", max_nodes_per_slice=1000)
    value = json.loads(index.read_text(encoding="utf-8"))
    assert len(value["slices"]) == 3
    assert all(len(json.loads((index.parent / item["path"]).read_text())["nodes"]) <= 1000 for item in value["slices"])


def test_policy_has_preserve_to_survey_points():
    policy = load_context_policy(Path("context-policy.json"))
    assert {"archive", "neighborhood", "explore", "survey"} <= set(policy["coverage_strategies"])
    resolved = coverage_strategy(policy, "survey", max_distance=99)
    assert resolved.max_distance == policy["coverage_strategies"]["explore"]["max_distance"]
    assert resolved.survey_enabled and resolved.survey.max_distance == 99
