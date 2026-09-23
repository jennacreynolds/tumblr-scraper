"""Small loopback HTTP bridge for the canonical archive interface.

This module knows transport and safe static-file serving only. It does not
know crawler policy, archive semantics, or platform behavior.
"""

from __future__ import annotations

import json
import os
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
        request = urlsplit(self.path)
        path = request.path
        if path in {"", "/"}:
            self.send_response(302)
            self.send_header("Location", "/" + self.server.interface_prefix.strip("/") + "/" + self.server.start_page)
            self.end_headers()
            return
        interface_prefix = "/" + self.server.interface_prefix.strip("/") + "/"
        if path.startswith(interface_prefix):
            page_key = path[len(interface_prefix):]
            # Query parameters are part of source-owned routes (for example,
            # a selected local blog).  Keep them out of static-file routing,
            # but pass them to the interface producer.
            if request.query:
                page_key += "?" + request.query
            page = self.server.interface_provider(page_key)
            if page is None:
                self.send_error(404)
            else:
                body = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return
        if path == "/__crawler/bootstrap":
            if self._local_request_allowed():
                self._json({
                    "live": True,
                    "capability": self.server.capability,
                    "instance_id": self.server.instance_id,
                    "owner": self.server.runtime_owner,
                    "server_control": self.server.shutdown_handler is not None,
                    "runtime_identity": self.server.identity_provider(),
                    "archive_path": str(self.server.archive_root),
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
        if path == "/__crawler/shutdown":
            if not self._local_request_allowed(mutate=True):
                return
            if self.server.shutdown_handler is None:
                self._json({"error": "server shutdown is unavailable"}, 409)
                return
            try:
                self._json(self.server.shutdown_handler())
            except Exception as exc:
                self._json({"error": str(exc)}, 400)
            return
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
        network_root = self.server.network_root.resolve()
        global_css = self.server.global_css.resolve()
        interface_root = self.server.interface_root.resolve()
        allowed = (
            translated == global_css
            or translated == archive_root
            or archive_root in translated.parents
            or translated == network_root
            or network_root in translated.parents
            or translated == interface_root
            or interface_root in translated.parents
        )
        return str(translated) if allowed else str(self.server.base_dir / "__not_found__")

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def log_request(self, code: int | str = "-", _size: int | str = "-") -> None:
        """Log actionable missing routes without promoting browser noise."""
        try:
            status = int(code)
        except (TypeError, ValueError):
            status = 0
        path = urlsplit(self.path).path
        if status >= 400 and path != "/favicon.ico":
            print(f"{status} {self.command} {path}", file=sys.stderr)

    def log_error(self, format: str, *args: object) -> None:
        # send_error calls log_request, which includes the request path.
        # Keep this hook quiet to avoid duplicate, path-less diagnostics.
        return


class LocalControlBridge:
    """Host one canonical UI against explicit application callbacks."""

    def __init__(
        self,
        *,
        base_dir: Path,
        archive_root: Path,
        network_root: Path,
        global_css: Path,
        status_provider: Callable[[], dict[str, Any]],
        control_handler: Callable[[str, Any], dict[str, Any]],
        start_handler: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        stop_handler: Callable[[], dict[str, Any]] | None = None,
        interface_provider: Callable[[str], str | None],
        interface_root: Path,
        interface_prefix: str = "app",
        archive_url_prefix: str = "Archive",
        runtime_state_path: Path | None = None,
        runtime_owner: str = "external",
        runtime_session_id: str = "",
        identity_provider: Callable[[], dict[str, Any]] | None = None,
        shutdown_handler: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.base_dir = base_dir.resolve()
        self.capability = secrets.token_urlsafe(32)
        handler = partial(LocalControlRequestHandler, directory=str(self.base_dir))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.base_dir = self.base_dir
        self.server.archive_root = archive_root
        self.server.network_root = network_root
        self.server.global_css = global_css
        self.server.interface_root = interface_root.resolve()
        self.server.interface_provider = interface_provider
        self.server.status_provider = status_provider
        self.server.control_handler = control_handler
        self.server.start_handler = start_handler
        self.server.stop_handler = stop_handler
        self.server.capability = self.capability
        self.server.shutdown_handler = shutdown_handler
        self.server.instance_id = secrets.token_urlsafe(18)
        self.server.runtime_state_path = runtime_state_path.resolve() if runtime_state_path else None
        self.server.runtime_owner = runtime_owner
        self.server.runtime_session_id = runtime_session_id
        self.server.identity_provider = identity_provider or (lambda: {})
        self.archive_url_prefix = archive_url_prefix.strip("/")
        self.server.archive_url_prefix = self.archive_url_prefix
        self.interface_prefix = interface_prefix.strip("/")
        self.server.interface_prefix = self.interface_prefix
        self.server.start_page = "graph.html"
        self.thread = threading.Thread(target=self.server.serve_forever, name="archive-bridge", daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/{self.interface_prefix}/{self.server.start_page}"

    def start(self) -> str:
        if self.server.runtime_state_path is not None:
            path = self.server.runtime_state_path
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            payload = {
                "schema": 2,
                "pid": os.getpid(),
                "url": self.url,
                "instance_id": self.server.instance_id,
                "owner": self.server.runtime_owner,
                "session_id": self.server.runtime_session_id,
                "runtime_identity": self.server.identity_provider(),
                "archive_path": str(self.server.archive_root),
            }
            temporary.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
            temporary.replace(path)
        self.thread.start()
        return self.url

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.server.capability = ""
        path = self.server.runtime_state_path
        if path is not None:
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                if current.get("instance_id") == self.server.instance_id:
                    path.unlink(missing_ok=True)
            except (OSError, json.JSONDecodeError):
                pass
