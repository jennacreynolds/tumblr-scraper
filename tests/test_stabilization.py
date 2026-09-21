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
        self.old_backups = main.BACKUPS_DIR
        self.old_neighborhoods = main.NEIGHBORHOODS_DIR
        main.BACKUPS_DIR = root / "Backups"
        main.NEIGHBORHOODS_DIR = root / "Neighborhoods"

    def tearDown(self) -> None:
        main.BACKUPS_DIR = self.old_backups
        main.NEIGHBORHOODS_DIR = self.old_neighborhoods
        self.temp.cleanup()

    def _state(self, version: int | None = main.PRESENTATION_SCHEMA_VERSION, dirty: bool = False) -> None:
        main.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
        (main.BACKUPS_DIR / "index.html").write_text("index", encoding="utf-8")
        (main.BACKUPS_DIR / "dashboard.html").write_text("dashboard", encoding="utf-8")
        (main.BACKUPS_DIR / "crawler.html").write_text("crawler", encoding="utf-8")
        (main.BACKUPS_DIR / "settings.html").write_text("settings", encoding="utf-8")
        (main.BACKUPS_DIR / "tag-index.json").write_text("{}", encoding="utf-8")
        (main.BACKUPS_DIR / "tags").mkdir()
        (main.BACKUPS_DIR / "tags" / "index.html").write_text("tags", encoding="utf-8")
        (main.BACKUPS_DIR / "assets").mkdir()
        (main.BACKUPS_DIR / "assets" / "archive.css").write_text("css", encoding="utf-8")
        (main.BACKUPS_DIR / "assets" / "archive.js").write_text("js", encoding="utf-8")
        (main.NEIGHBORHOODS_DIR).mkdir()
        (main.NEIGHBORHOODS_DIR / "index.html").write_text("neighborhoods", encoding="utf-8")
        state = {"dirty": dirty}
        if version is not None:
            state["generator_version"] = version
        (main.BACKUPS_DIR / "presentation-state.json").write_text(json.dumps(state), encoding="utf-8")

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
        state = json.loads((main.BACKUPS_DIR / "presentation-state.json").read_text())
        self.assertEqual(state["generator_version"], main.PRESENTATION_SCHEMA_VERSION)

    def test_missing_state_rebuilds(self) -> None:
        self._state(version=None)
        with mock.patch.object(main, "render_global_pages") as render:
            main.regenerate_global_presentation()
        render.assert_called_once()

    def test_missing_required_html_rebuilds(self) -> None:
        self._state()
        (main.BACKUPS_DIR / "dashboard.html").unlink()
        with mock.patch.object(main, "render_global_pages") as render:
            main.regenerate_global_presentation()
        render.assert_called_once()

    def test_canonical_source_survives_real_regeneration(self) -> None:
        source = main.BACKUPS_DIR / "example" / "json" / "1.json"
        source.parent.mkdir(parents=True)
        source.write_text(json.dumps({"id": 1, "type": "regular"}, sort_keys=True), encoding="utf-8")
        before = hashlib.sha256(source.read_bytes()).digest()
        main.regenerate_global_presentation(force=True)
        self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), before)


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
            self.assertEqual(application.snapshot()["lifecycle"], "complete")
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
            self.assertEqual(application.snapshot()["lifecycle"], "complete")
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
            self.assertEqual(application.snapshot()["lifecycle"], "failed")
            main.main = lambda _argv, *, install_signal_handlers=True: 0
            application.start({"target": "example", "profile_id": "gentle"})
            assert application.worker is not None
            application.worker.join(timeout=2)
            self.assertEqual(application.snapshot()["lifecycle"], "complete")
        finally:
            main.main = old_main
            main.PENDING_CANCEL_EVENT.clear()
