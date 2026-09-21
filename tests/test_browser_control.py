from __future__ import annotations

import json
import threading
import unittest
from urllib.error import HTTPError
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import main
from bridge.local_http import LocalControlBridge


class BrowserControlIntegrationTests(unittest.TestCase):
    def test_browser_start_and_stop_use_shared_application_callbacks(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "Backups"
            archive.mkdir()
            (archive / "index.html").write_text("<html>archive</html>", encoding="utf-8")
            neighborhoods = root / "Neighborhoods"
            neighborhoods.mkdir()
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
                neighborhood_root=neighborhoods,
                global_css=css,
                status_provider=application.snapshot,
                control_handler=lambda _name, _value: {"lifecycle": "running"},
                start_handler=application.start,
                stop_handler=application.stop,
                archive_url_prefix="Backups",
            )
            try:
                url = bridge.start()
                with urlopen(url.rsplit("/", 1)[0] + "/", timeout=2) as response:
                    self.assertEqual(response.status, 200)
                parsed = urlsplit(url)
                endpoint = f"{parsed.scheme}://{parsed.netloc}"
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
