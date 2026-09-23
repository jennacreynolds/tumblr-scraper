from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import main
from bridge.local_http import LocalControlBridge
from integrations.firefox.session import firefox_session


class BrowserControlIntegrationTests(unittest.TestCase):
    def test_firefox_expected_identity_matches_backend_identity(self) -> None:
        self.assertEqual(
            firefox_session._expected_runtime_identity(Path(__file__).resolve().parents[1]),
            main.runtime_identity(),
        )

    def test_firefox_session_rejects_legacy_generated_interface_state(self) -> None:
        self.assertFalse(firefox_session._health({
            "url": "http://127.0.0.1:38149/Backups/graph.html",
            "instance_id": "stale",
        }))
        self.assertFalse(firefox_session._health({
            "url": "http://127.0.0.1:38149/Neighborhoods/index.html",
            "instance_id": "stale",
        }))

    def test_firefox_extension_registers_a_cross_version_toolbar_action(self) -> None:
        extension = (Path(__file__).resolve().parents[1] / "integrations" / "firefox" / "extension" / "background.js").read_text(encoding="utf-8")
        self.assertIn("browser.action || browser.browserAction", extension)
        self.assertIn("toolbarAction.onClicked", extension)
        self.assertNotIn("browser.action.onClicked", extension)

    def test_crawler_form_hydrates_once_so_completed_request_does_not_overwrite_new_target(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "assets" / "archive.js").read_text(encoding="utf-8")
        self.assertIn("var formHydrated = false;", script)
        self.assertIn("status.request && !formHydrated", script)
        self.assertIn("formHydrated = true;", script)

    def test_terminal_status_cannot_be_overwritten_by_an_older_running_poll(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "assets" / "archive.js").read_text(encoding="utf-8")
        self.assertIn("var renderedStatusRevision = -1;", script)
        self.assertIn("revision < renderedStatusRevision", script)
        self.assertIn("Number(status.finished_at) > 0", script)
        self.assertIn("no active crawl to stop", script)
        self.assertIn("errorOutput.hidden = true", script)

    def test_live_interface_uses_source_routes_not_generated_archive_pages(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "bridge" / "local_http.py").read_text(encoding="utf-8")
        self.assertIn('self.server.start_page = "graph.html"', source)
        self.assertIn('self.server.interface_prefix', source)
        self.assertNotIn('_ensure_archive_entrypoint', source)
        session = (Path(__file__).resolve().parents[1] / "integrations" / "firefox" / "session" / "firefox_session.py").read_text(encoding="utf-8")
        self.assertIn('with urlopen(url, timeout=1.0)', session)
        self.assertIn("mutable archive entry", session)

    def test_source_owned_reader_routes_are_the_only_active_routes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "Archive"
            neighborhoods = archive / "Network" / "Neighborhoods"
            neighborhoods.mkdir(parents=True)
            (archive / "Content").mkdir(parents=True)
            css = root / "global.css"
            css.write_text("body {}", encoding="utf-8")
            pages = {"graph.html", "list.html", "feed.html", "tags.html", "neighborhoods.html", "crawler.html"}
            bridge = LocalControlBridge(
                base_dir=root,
                archive_root=archive,
                network_root=archive / "Network",
                global_css=css,
                interface_provider=lambda page: "<html>source " + page + "</html>" if page in pages else None,
                status_provider=lambda: {},
                control_handler=lambda _name, _value: {},
                interface_root=root,
            )
            try:
                endpoint = bridge.start().split("/app/", 1)[0]
                for page in pages:
                    with urlopen(endpoint + "/app/" + page, timeout=2) as response:
                        self.assertEqual(response.status, 200)
                        self.assertIn(f"source {page}".encode(), response.read())
                for alias in ("/Backups/index.html", "/Neighborhoods/index.html"):
                    with self.assertRaises(HTTPError) as error:
                        urlopen(endpoint + alias, timeout=2)
                    self.assertEqual(error.exception.code, 404)
            finally:
                bridge.close()

    def test_stale_firefox_backend_state_is_removed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / ".runtime"
            runtime.mkdir()
            path = runtime / "backend.json"
            path.write_text(json.dumps({"url": "http://127.0.0.1:9/app/graph.html", "instance_id": "stale"}), encoding="utf-8")
            firefox_session._remove_stale_backend_state(root)
            self.assertFalse(path.exists())

    def test_crawler_ui_exposes_saved_counts_by_region(self) -> None:
        controls = main._crawler_controls(page=True)
        for element_id in ("crawler-count-target", "crawler-count-nearby", "crawler-count-outer"):
            self.assertIn(f'id="{element_id}"', controls)
        self.assertIn('id="crawler-count-unknown"', controls)
        script = (Path(__file__).resolve().parents[1] / "assets" / "archive.js").read_text(encoding="utf-8")
        self.assertIn("status.target_posts", script)
        self.assertIn("status.nearby_posts", script)
        self.assertIn("status.survey_posts", script)
        self.assertIn("status.unclassified_posts", script)
        self.assertNotIn("saved['distance-1']", script)
        self.assertNotIn("saved['distance-2']", script)

    def test_crawler_ui_exposes_strategy_and_supported_advanced_settings(self) -> None:
        controls = main._crawler_controls(page=True)
        self.assertIn('<div class="crawler-live" hidden>', controls)
        self.assertIn('class="crawler-live-overview"', controls)
        self.assertIn('id="crawler-strategy"', controls)
        self.assertIn('id="crawler-strategy-description"', controls)
        self.assertIn('class="crawler-advanced"', controls)
        self.assertIn('id="crawler-survey-nodes"', controls)
        self.assertIn('id="crawler-survey-requests"', controls)
        self.assertNotIn('id="crawler-context-mode"', controls)
        self.assertNotIn('id="crawler-depth"', controls)
        self.assertNotIn('id="crawler-start-focus"', controls)
        self.assertNotIn('id="crawler-images"', controls)
        for element_id in ("crawler-progress", "crawler-speed-meter", "crawler-eta"):
            self.assertIn(f'id="{element_id}"', controls)
        self.assertIn("Request pacing and media parallelism", controls)
        script = (Path(__file__).resolve().parents[1] / "assets" / "archive.js").read_text(encoding="utf-8")
        self.assertIn("strategyDescriptions", script)
        self.assertIn("strategy: strategy.value", script)
        self.assertIn("targets: targets", script)
        self.assertIn("function renderMetrics", script)
        self.assertIn("status.started_at", script)
        self.assertIn("formatDuration", script)

    def test_crawler_ui_strategy_descriptions_cover_the_continuum(self) -> None:
        controls = main._crawler_controls(page=True)
        for label in ("Archive", "Neighborhood", "Explore", "Survey"):
            self.assertIn(f'>{label}</option>', controls)
        script = (Path(__file__).resolve().parents[1] / "assets" / "archive.js").read_text(encoding="utf-8")
        self.assertIn("concentrated capture shape", script)
        self.assertIn("shallow, wide capture shape", script)

    def test_crawler_ui_is_vertical_first_and_submits_explicit_strategy(self) -> None:
        root = Path(__file__).resolve().parents[1]
        css = (root / "assets" / "archive.css").read_text(encoding="utf-8")
        script = (root / "assets" / "archive.js").read_text(encoding="utf-8")
        self.assertIn(".crawler-page .crawler-setup { grid-template-columns: repeat(2", css)
        self.assertIn(".crawler-page .crawler-setup, .crawler-advanced-grid { grid-template-columns: 1fr; }", css)
        self.assertIn("targets: targets", script)
        self.assertIn("strategy: strategy.value", script)
        self.assertIn("Choose a coverage strategy before starting", script)

    def test_crawler_eta_uses_a_stable_deadline(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "assets" / "archive.js").read_text(encoding="utf-8")
        self.assertIn("var etaDeadline = null;", script)
        self.assertIn("etaDeadline - Date.now() / 1000", script)
        self.assertIn("whenever the observed rate temporarily falls", script)

    def test_live_interface_is_source_owned_not_rebuilt_under_backups(self) -> None:
        core = (Path(__file__).resolve().parents[1] / "src" / "tumblr_scraper" / "main.py").read_text(encoding="utf-8")
        self.assertIn("def live_interface_page", core)
        self.assertIn("Render the loopback UI from source", core)
        self.assertNotIn("def prepare_live_archive_entrypoint", core)

    def test_live_graph_links_both_source_owned_stylesheets(self) -> None:
        page = main.live_interface_page("graph.html") or ""
        self.assertIn('href="/assets/archive.css"', page)
        self.assertIn('href="/assets/graph-view.css?', page)

    def test_all_primary_navigation_routes_are_source_owned(self) -> None:
        for page in ("graph.html", "list.html", "feed.html", "tags.html", "neighborhoods.html", "crawler.html"):
            rendered = main.live_interface_page(page) or ""
            self.assertIn('href="/assets/archive.css"', rendered)
            self.assertIn('/app/list.html', rendered)
            self.assertNotIn('href="/Backups/"', rendered)

    def test_live_graph_rewrites_local_avatar_and_blog_links_to_live_routes(self) -> None:
        graph = {
            "format": main.graph_projection.GRAPH_FORMAT,
            "version": main.graph_projection.GRAPH_VERSION,
            "targets": [],
            "nodes": [{"id": "sample", "username": "sample", "avatar_src": "sample/profile/avatar.png", "degree": 0}],
            "edges": [],
        }
        with patch.object(main.graph_projection, "build_graph_projection", return_value=graph):
            page = main.live_interface_page("graph.html") or ""
        self.assertIn('"avatar_src":"/Archive/default/Content/sample/profile/avatar.png"', page)
        self.assertIn('/app/blog.html?blog=sample', page)

    def test_live_pages_do_not_create_disposable_data_directories(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            old_roots = (
                main.ARCHIVE_ROOT, main.CONTENT_ROOT, main.NETWORK_ROOT,
                main.OBSERVATIONS_ROOT, main.NEIGHBORHOODS_ROOT, main.APP_ROOT,
            )
            main.ARCHIVE_ROOT = root / "Archive"
            main.CONTENT_ROOT = main.ARCHIVE_ROOT / "Content"
            main.NETWORK_ROOT = main.ARCHIVE_ROOT / "Network"
            main.OBSERVATIONS_ROOT = main.NETWORK_ROOT / "Observations"
            main.NEIGHBORHOODS_ROOT = main.NETWORK_ROOT / "Neighborhoods"
            main.APP_ROOT = main.ARCHIVE_ROOT / "App"
            try:
                self.assertIn("Graph", main.live_interface_page("graph.html") or "")
                self.assertIn("Crawler", main.live_interface_page("crawler.html") or "")
                self.assertFalse((root / "Backups").exists())
                self.assertFalse((root / "Neighborhoods").exists())
                main.ensure_shared_archive_assets()
                self.assertTrue((main.APP_ROOT / "assets" / "archive.js").is_file())
                self.assertFalse((root / "Backups").exists())
            finally:
                (main.ARCHIVE_ROOT, main.CONTENT_ROOT, main.NETWORK_ROOT,
                 main.OBSERVATIONS_ROOT, main.NEIGHBORHOODS_ROOT, main.APP_ROOT) = old_roots

    def test_browser_start_and_stop_use_shared_application_callbacks(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "Archive"
            archive.mkdir()
            neighborhoods = archive / "Network" / "Neighborhoods"
            neighborhoods.mkdir(parents=True)
            css = root / "global.css"
            css.write_text("body {}", encoding="utf-8")
            started: list[dict[str, object]] = []
            stopped = threading.Event()
            state = {"lifecycle": "idle"}

            class FakeApplication:
                def snapshot(self) -> dict[str, object]:
                    return dict(state)

                def start(self, values: dict[str, object]) -> dict[str, object]:
                    started.append(values)
                    state.update({"lifecycle": "running", "request": values})
                    return self.snapshot()

                def stop(self) -> dict[str, object]:
                    stopped.set()
                    state.update({"lifecycle": "stopping", "cancel_requested": True})
                    return self.snapshot()

            application = FakeApplication()
            bridge = LocalControlBridge(
                base_dir=root,
                archive_root=archive,
                network_root=archive / "Network",
                global_css=css,
                interface_root=root,
                interface_provider=lambda page: "<html>source " + page + "</html>" if page in {"graph.html", "crawler.html"} else None,
                status_provider=application.snapshot,
                control_handler=lambda _name, _value: {"lifecycle": "running"},
                start_handler=application.start,
                stop_handler=application.stop,
            )
            try:
                url = bridge.start()
                self.assertTrue(url.endswith("/app/graph.html"))
                parsed = urlsplit(url)
                endpoint = f"{parsed.scheme}://{parsed.netloc}"
                with urlopen(endpoint + "/", timeout=2) as response:
                    self.assertEqual(response.status, 200)
                with urlopen(url, timeout=2) as response:
                    self.assertIn(b"source graph.html", response.read())
                with self.assertRaises(HTTPError) as error:
                    urlopen(endpoint + "/Backups/graph.html", timeout=2)
                self.assertEqual(error.exception.code, 404)
                headers = {"X-Crawler-Capability": bridge.capability, "Content-Type": "application/json"}
                payload = json.dumps({
                    "target": "synthetic",
                    "max_posts": 2,
                    "context": "none",
                    "context_depth": None,
                    "focus": "balanced",
                    "profile_id": "gentle",
                    "full_res": False,
                }).encode()
                with urlopen(Request(endpoint + "/__crawler/start", data=payload, headers=headers, method="POST"), timeout=2) as response:
                    self.assertEqual(response.status, 200)
                self.assertEqual(len(started), 1)
                self.assertEqual(started[0]["target"], "synthetic")
                self.assertEqual(started[0]["max_posts"], 2)
                with urlopen(endpoint + "/__crawler/status", timeout=2) as response:
                    status = json.load(response)
                self.assertEqual(status["lifecycle"], "running")
                with self.assertRaises(HTTPError) as unauthorized:
                    urlopen(Request(endpoint + "/__crawler/stop", data=b"{}", method="POST"), timeout=2)
                self.assertEqual(unauthorized.exception.code, 403)
                with urlopen(Request(endpoint + "/__crawler/stop", data=b"{}", headers=headers, method="POST"), timeout=2) as response:
                    self.assertEqual(response.status, 200)
                self.assertTrue(stopped.is_set())
                with urlopen(endpoint + "/__crawler/status", timeout=2) as response:
                    self.assertEqual(json.load(response)["lifecycle"], "stopping")
            finally:
                bridge.close()


if __name__ == "__main__":
    unittest.main()
