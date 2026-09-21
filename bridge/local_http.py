"""Small loopback HTTP bridge for the canonical archive interface.

This module knows transport and safe static-file serving only. It does not
know crawler policy, archive semantics, or platform behavior.
"""

from __future__ import annotations

import json
import secrets
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit


class LocalControlRequestHandler(SimpleHTTPRequestHandler):
    server_version = "TumblrArchive/1"

    def _json(self, payload: dict[str, Any], code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _local_request_allowed(self, *, mutate: bool = False) -> bool:
        port = self.server.server_port
        expected_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "").lower()
        origin = self.headers.get("Origin")
        if host not in expected_hosts:
            self._json({"error": "invalid host"}, 403)
            return False
        if origin and origin.lower() not in {f"http://{item}" for item in expected_hosts}:
            self._json({"error": "invalid origin"}, 403)
            return False
        if mutate and self.headers.get("X-Crawler-Capability") != self.server.capability:
            self._json({"error": "missing or invalid capability"}, 403)
            return False
        return True

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/__crawler/bootstrap":
            if self._local_request_allowed():
                self._json({
                    "live": True,
                    "capability": self.server.capability,
                    "status": self.server.status_provider(),
                })
            return
        if path == "/__crawler/status":
            if self._local_request_allowed():
                self._json(self.server.status_provider())
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path not in {"/__crawler/control", "/__crawler/start", "/__crawler/stop"}:
            self.send_error(404)
            return
        if not self._local_request_allowed(mutate=True):
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 16384:
                raise ValueError("control body must be between 1 and 16384 bytes")
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if path == "/__crawler/start":
                if self.server.start_handler is None:
                    raise ValueError("crawler start is unavailable")
                if not isinstance(data, dict):
                    raise ValueError("start request must be an object")
                self._json(self.server.start_handler(data))
                return
            if path == "/__crawler/stop":
                if self.server.stop_handler is None:
                    raise ValueError("crawler stop is unavailable")
                self._json(self.server.stop_handler())
                return
            if not isinstance(data, dict) or len(data) != 1:
                raise ValueError("control request must contain exactly one field")
            name, value = next(iter(data.items()))
            self._json(self.server.control_handler(name, value))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, 400)
        except Exception as exc:
            self._json({"error": str(exc)}, 400)

    def translate_path(self, path: str) -> str:
        request_path = unquote(urlsplit(path).path)
        translated = Path(super().translate_path(request_path)).resolve()
        archive_root = self.server.archive_root.resolve()
        neighborhood_root = self.server.neighborhood_root.resolve()
        global_css = self.server.global_css.resolve()
        allowed = (
            translated == global_css
            or translated == archive_root
            or archive_root in translated.parents
            or translated == neighborhood_root
            or neighborhood_root in translated.parents
        )
        return str(translated) if allowed else str(self.server.base_dir / "__not_found__")

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def log_error(self, format: str, *args: object) -> None:
        print(f"Archive server error: {format % args}", file=sys.stderr)


class LocalControlBridge:
    """Host one canonical UI against explicit application callbacks."""

    def __init__(
        self,
        *,
        base_dir: Path,
        archive_root: Path,
        neighborhood_root: Path,
        global_css: Path,
        status_provider: Callable[[], dict[str, Any]],
        control_handler: Callable[[str, Any], dict[str, Any]],
        start_handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        stop_handler: Callable[[], dict[str, Any]] | None = None,
        prepare_entrypoint: Callable[[], None] | None = None,
        archive_url_prefix: str = "Backups",
    ) -> None:
        self.base_dir = base_dir.resolve()
        self.capability = secrets.token_urlsafe(32)
        handler = partial(LocalControlRequestHandler, directory=str(self.base_dir))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.base_dir = self.base_dir
        self.server.archive_root = archive_root
        self.server.neighborhood_root = neighborhood_root
        self.server.global_css = global_css
        self.server.status_provider = status_provider
        self.server.control_handler = control_handler
        self.server.start_handler = start_handler
        self.server.stop_handler = stop_handler
        self.server.capability = self.capability
        self.archive_url_prefix = archive_url_prefix.strip("/")
        self.prepare_entrypoint = prepare_entrypoint
        self.thread = threading.Thread(target=self.server.serve_forever, name="archive-bridge", daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/{self.archive_url_prefix}/index.html"

    def start(self) -> str:
        if self.prepare_entrypoint is not None:
            self.prepare_entrypoint()
        self.thread.start()
        return self.url

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.server.capability = ""
