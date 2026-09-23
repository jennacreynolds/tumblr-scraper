from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main


class PresentationVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.old_roots = (
            main.ARCHIVE_ROOT, main.CONTENT_ROOT, main.NETWORK_ROOT,
            main.NEIGHBORHOODS_ROOT, main.APP_ROOT,
        )
        main.ARCHIVE_ROOT = root / "Archive"
        main.CONTENT_ROOT = main.ARCHIVE_ROOT / "Content"
        main.NETWORK_ROOT = main.ARCHIVE_ROOT / "Network"
        main.NEIGHBORHOODS_ROOT = main.NETWORK_ROOT / "Neighborhoods"
        main.APP_ROOT = main.ARCHIVE_ROOT / "App"

    def tearDown(self) -> None:
        (main.ARCHIVE_ROOT, main.CONTENT_ROOT, main.NETWORK_ROOT,
         main.NEIGHBORHOODS_ROOT, main.APP_ROOT) = self.old_roots
        self.temp.cleanup()

    def _state(self, version: int | None = main.PRESENTATION_SCHEMA_VERSION, dirty: bool = False) -> None:
        main.CONTENT_ROOT.mkdir(parents=True, exist_ok=True)
        main.APP_ROOT.mkdir(parents=True, exist_ok=True)
        main.NETWORK_ROOT.mkdir(parents=True, exist_ok=True)
        (main.APP_ROOT / "list.html").write_text("index", encoding="utf-8")
        (main.APP_ROOT / "feed.html").write_text("dashboard", encoding="utf-8")
        (main.APP_ROOT / "tags.html").write_text("tags", encoding="utf-8")
        (main.APP_ROOT / "crawler.html").write_text("crawler", encoding="utf-8")
        (main.APP_ROOT / "settings.html").write_text("settings", encoding="utf-8")
        (main.CONTENT_ROOT / "tag-index.json").write_text("{}", encoding="utf-8")
        (main.APP_ROOT / "tags").mkdir()
        (main.APP_ROOT / "tags" / "index.html").write_text("tags", encoding="utf-8")
        (main.APP_ROOT / "assets").mkdir()
        (main.APP_ROOT / "assets" / "archive.css").write_text("css", encoding="utf-8")
        (main.APP_ROOT / "assets" / "archive.js").write_text("js", encoding="utf-8")
        (main.APP_ROOT / "assets" / "graph-view.css").write_text("css", encoding="utf-8")
        (main.APP_ROOT / "assets" / "graph-view.js").write_text("js", encoding="utf-8")
        (main.APP_ROOT / "assets" / "vendor").mkdir()
        (main.APP_ROOT / "assets" / "vendor" / "cytoscape.min.js").write_text("js", encoding="utf-8")
        for name in ("layout-base.min.js", "cose-base.min.js", "cytoscape-layout-utilities.min.js", "cytoscape-fcose.min.js"):
            (main.APP_ROOT / "assets" / "vendor" / name).write_text("js", encoding="utf-8")
        (main.APP_ROOT / "graph.html").write_text("graph", encoding="utf-8")
        (main.NETWORK_ROOT / "graph.json").write_text("{}", encoding="utf-8")
        (main.NEIGHBORHOODS_ROOT).mkdir()
        (main.APP_ROOT / "neighborhoods.html").write_text("neighborhoods", encoding="utf-8")
        state = {"dirty": dirty}
        if version is not None:
            state["generator_version"] = version
            state["presentation_identity"] = main._presentation_identity()
        (main.APP_ROOT / "presentation-state.json").write_text(json.dumps(state), encoding="utf-8")

    def test_matching_version_does_not_rebuild(self) -> None:
        self._state()
        with mock.patch.object(main, "render_global_pages") as render:
            main.regenerate_global_presentation()
        render.assert_not_called()

    def test_old_version_rebuilds(self) -> None:
        self._state(version=0)
        with mock.patch.object(main, "render_global_pages") as render:
            main.regenerate_global_presentation()
        render.assert_called_once()
        state = json.loads((main.APP_ROOT / "presentation-state.json").read_text())
        self.assertEqual(state["generator_version"], main.PRESENTATION_SCHEMA_VERSION)

    def test_missing_state_rebuilds(self) -> None:
        self._state(version=None)
        with mock.patch.object(main, "render_global_pages") as render:
            main.regenerate_global_presentation()
        render.assert_called_once()

    def test_missing_required_html_rebuilds(self) -> None:
        self._state()
        (main.APP_ROOT / "feed.html").unlink()
        with mock.patch.object(main, "render_global_pages") as render:
            main.regenerate_global_presentation()
        render.assert_called_once()

    def test_presentation_source_identity_rebuilds_stale_output(self) -> None:
        self._state()
        state_path = main.APP_ROOT / "presentation-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["presentation_identity"]["frontend_revision"] = "stale"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        with mock.patch.object(main, "render_global_pages") as render:
            main.regenerate_global_presentation()
        render.assert_called_once()

    def test_live_entrypoint_checks_the_crawler_page_not_the_index(self) -> None:
        self._state()
        required = (
            'id="crawler-target"',
            'class="crawler-start"',
            'id="crawler-count-target"',
            'id="crawler-count-nearby"',
            'id="crawler-count-outer"',
            'id="crawler-max-breadth"',
            'id="crawler-max-depth"',
            'id="crawler-curve-table"',
            'Breadth 1',
            'id="crawler-progress"',
            'id="crawler-speed-meter"',
            'id="crawler-eta"',
            'class="crawler-setting-help"',
        )
        (main.APP_ROOT / "list.html").write_text("".join(required), encoding="utf-8")
        main.render_static_archive()
        self.assertTrue((main.APP_ROOT / "crawler.html").is_file())
        self.assertIn('id="crawler-max-depth"', (main.APP_ROOT / "crawler.html").read_text(encoding="utf-8"))

    def test_canonical_source_survives_real_regeneration(self) -> None:
        source = main.CONTENT_ROOT / "example" / "json" / "1.json"
        source.parent.mkdir(parents=True)
        source.write_text(json.dumps({"id": 1, "type": "regular"}, sort_keys=True), encoding="utf-8")
        before = hashlib.sha256(source.read_bytes()).digest()
        main.regenerate_global_presentation(force=True)
        self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), before)
        self.assertTrue((main.APP_ROOT / "graph.html").is_file())
        self.assertTrue((main.NETWORK_ROOT / "graph.json").is_file())
        self.assertTrue((main.APP_ROOT / "assets" / "graph-view.js").is_file())
        self.assertTrue((main.APP_ROOT / "assets" / "vendor" / "cytoscape.min.js").is_file())
        for name in ("layout-base.min.js", "cose-base.min.js", "cytoscape-layout-utilities.min.js", "cytoscape-fcose.min.js"):
            self.assertTrue((main.APP_ROOT / "assets" / "vendor" / name).is_file())
        graph_page = (main.APP_ROOT / "graph.html").read_text(encoding="utf-8")
        self.assertLess(graph_page.index("assets/vendor/cytoscape.min.js"), graph_page.index("assets/graph-view.js"))
        self.assertLess(graph_page.index("assets/vendor/cytoscape-fcose.min.js"), graph_page.index("assets/graph-view.js"))
        self.assertNotIn("https://unpkg.com", graph_page)
        self.assertIn("graph-renderer-status", graph_page)
        self.assertNotIn('<details class="graph-text-view" open>', graph_page)

    def test_presentation_bootstraps_when_user_content_directories_are_absent(self) -> None:
        old = (
            main.SOURCE_ASSET_DIR, main.SOURCE_ARCHIVE_CSS,
            main.SOURCE_ARCHIVE_JS, main.GLOBAL_CSS,
        )
        root = Path(self.temp.name)
        main.SOURCE_ASSET_DIR = Path(__file__).resolve().parents[1] / "assets"
        main.SOURCE_ARCHIVE_CSS = main.SOURCE_ASSET_DIR / "archive.css"
        main.SOURCE_ARCHIVE_JS = main.SOURCE_ASSET_DIR / "archive.js"
        main.GLOBAL_CSS = root / "global.css"
        try:
            self.assertFalse(main.CONTENT_ROOT.exists())
            self.assertFalse(main.NEIGHBORHOODS_ROOT.exists())
            main.render_static_archive()
            self.assertTrue((main.APP_ROOT / "list.html").is_file())
            self.assertTrue((main.APP_ROOT / "crawler.html").is_file())
            self.assertTrue((main.APP_ROOT / "graph.html").is_file())
        finally:
            main.SOURCE_ASSET_DIR, main.SOURCE_ARCHIVE_CSS, main.SOURCE_ARCHIVE_JS, main.GLOBAL_CSS = old


class ApplicationLifecycleTests(unittest.TestCase):
    def test_stop_immediately_after_start_is_application_level(self) -> None:
        import threading

        entered = threading.Event()
        release = threading.Event()
        old_main = main.main

        def fake_main(_argv: list[str], *, install_signal_handlers: bool = True) -> int:
            entered.set()
            release.wait(timeout=2)
            return 0

        try:
            main.main = fake_main
            application = main.CrawlerApplication()
            request = main.build_crawl_request("example", context="none", profile_id="gentle")
            application.start_request(request)
            self.assertTrue(entered.wait(timeout=2))
            snapshot = application.stop()
            self.assertEqual(snapshot["lifecycle"], "stopping")
            self.assertTrue(snapshot["cancel_requested"])
            release.set()
            assert application.worker is not None
            application.worker.join(timeout=2)
            self.assertEqual(application.snapshot()["lifecycle"], "stopped")
        finally:
            release.set()
            main.main = old_main
            main.PENDING_CANCEL_EVENT.clear()

    def test_stop_while_idle_is_rejected(self) -> None:
        with self.assertRaises(main.PolicyError):
            main.CrawlerApplication().stop()

    def test_terminal_safe_stop_waits_for_active_worker(self) -> None:
        path = Path(__file__).parents[1] / "Tumblr Scraper - Android.py"
        spec = importlib.util.spec_from_file_location("android_launcher_for_test", path)
        assert spec is not None and spec.loader is not None
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)

        old_main = main.main
        release = __import__("threading").Event()

        def fake_main(_argv: list[str], *, install_signal_handlers: bool = True) -> int:
            while not main.PENDING_CANCEL_EVENT.is_set():
                release.wait(0.01)
            return 0

        try:
            main.main = fake_main
            application = main.CrawlerApplication()
            application.start({"target": "example", "profile_id": "gentle"})
            self.assertTrue(launcher.stop_application_safely(application, timeout=2))
            self.assertEqual(application.snapshot()["lifecycle"], "stopped")
        finally:
            release.set()
            main.main = old_main
            main.PENDING_CANCEL_EVENT.clear()

    def test_worker_failure_returns_controller_to_usable_state(self) -> None:
        old_main = main.main

        def failing_main(_argv: list[str], *, install_signal_handlers: bool = True) -> int:
            raise RuntimeError("synthetic failure")

        try:
            main.main = failing_main
            application = main.CrawlerApplication()
            application.start({"target": "example", "profile_id": "gentle"})
            assert application.worker is not None
            application.worker.join(timeout=2)
            failed = application.snapshot()
            self.assertEqual(failed["lifecycle"], "failed")
            failed_revision = failed["status_revision"]
            main.main = lambda _argv, *, install_signal_handlers=True: 0
            application.start({"target": "example", "profile_id": "gentle"})
            assert application.worker is not None
            application.worker.join(timeout=2)
            complete = application.snapshot()
            self.assertEqual(complete["lifecycle"], "complete")
            self.assertGreater(complete["status_revision"], failed_revision)
            # A completed run must release the controller for a different
            # subsequent request; it is not an active crawl.
            application.start({"target": "different-example", "profile_id": "gentle"})
            assert application.worker is not None
            application.worker.join(timeout=2)
            self.assertEqual(application.snapshot()["lifecycle"], "complete")
        finally:
            main.main = old_main
            main.PENDING_CANCEL_EVENT.clear()
