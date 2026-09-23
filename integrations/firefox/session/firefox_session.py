#!/usr/bin/env python3
"""Firefox-owned process supervision for the temporary Linux integration."""

from __future__ import annotations

import argparse
import hashlib
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.request import urlopen
from urllib.parse import urlsplit, urlunsplit


def state_path(project_root: Path) -> Path:
    return project_root / ".runtime" / "backend.json"


def session_path(project_root: Path) -> Path:
    return project_root / ".runtime" / "firefox-session.json"


def _expected_runtime_identity(project_root: Path) -> dict[str, str]:
    """Mirror the backend's authoritative runtime identity contract."""
    files = [
        project_root / "src" / "tumblr_scraper" / "main.py",
        project_root / "src" / "tumblr_scraper" / "presentation.py",
        project_root / "src" / "tumblr_scraper" / "paths.py",
        project_root / "src" / "tumblr_scraper" / "graph_projection.py",
    ]
    assets = [
        project_root / "assets" / "archive.css",
        project_root / "assets" / "archive.js",
        project_root / "assets" / "graph-view.css",
        project_root / "assets" / "graph-view.js",
    ]

    def digest(paths: list[Path]) -> str:
        h = hashlib.sha256()
        for path in paths:
            h.update(str(path).encode("utf-8"))
            try:
                h.update(path.read_bytes())
            except OSError:
                h.update(b"<unavailable>")
        return h.hexdigest()[:24]

    archive_name = os.environ.get("TUMBLR_SCRAPER_ARCHIVE", "")
    if not archive_name:
        try:
            active = json.loads((project_root / "Archive" / "active.json").read_text(encoding="utf-8"))
            archive_name = str(active.get("name", ""))
        except (OSError, UnicodeError, ValueError, AttributeError):
            archive_name = ""
    if not archive_name or Path(archive_name).name != archive_name:
        archive_name = "default"
    return {
        "bundle_root": str(project_root),
        "source_revision": digest(files),
        "frontend_revision": digest(assets),
        "archive_path": str(project_root / "Archive" / archive_name),
        "archive_name": archive_name,
    }


def _health(state: dict[str, object], project_root: Path | None = None) -> bool:
    url = str(state.get("url", ""))
    instance_id = str(state.get("instance_id", ""))
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or not instance_id:
        return False
    # A backend that advertises the removed generated archive pages is an old
    # runtime, even if that process is still answering HTTP.  Firefox must
    # start/reuse only the source-owned interface, otherwise the extension can
    # keep reopening a stale /Backups/graph.html tab forever.
    if parsed.path.startswith("/Backups/") or parsed.path.startswith("/Neighborhoods/"):
        return False
    health = urlunsplit((parsed.scheme, parsed.netloc, "/__crawler/bootstrap", "", ""))
    try:
        with urlopen(health, timeout=1.0) as response:
            body = json.load(response)
        if not bool(body.get("live")) or body.get("instance_id") != instance_id:
            return False
        if project_root is not None:
            expected = _expected_runtime_identity(project_root)
            if body.get("runtime_identity") != expected:
                return False
        # The native host must not reuse a backend whose mutable archive entry
        # was deleted after the server started. The bridge repairs a missing
        # entrypoint when possible; a real 404 remains stale state.
        with urlopen(url, timeout=1.0) as response:
            return response.status < 400
    except Exception:
        return False


def read_live_state(project_root: Path) -> dict[str, object] | None:
    try:
        state = json.loads(state_path(project_root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict) or not _health(state, project_root):
        return None
    return state


def _remove_stale_backend_state(project_root: Path) -> None:
    """Remove a state file that no longer identifies a live owned bridge."""
    path = state_path(project_root)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(state, dict) or not _health(state, project_root):
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _write_session(project_root: Path, payload: dict[str, object]) -> None:
    path = session_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(path)


def _stop_owned_backend(project_root: Path, session_id: str, backend_pid: int, instance_id: str) -> None:
    state = read_live_state(project_root)
    if not state:
        return
    if (
        state.get("owner") != "firefox-session"
        or state.get("session_id") != session_id
        or state.get("instance_id") != instance_id
        or int(state.get("pid", 0)) != backend_pid
    ):
        return
    try:
        os.killpg(backend_pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            os.kill(backend_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def _remove_owned_state(project_root: Path, session_id: str, instance_id: str) -> None:
    path = state_path(project_root)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("owner") == "firefox-session" and state.get("session_id") == session_id and state.get("instance_id") == instance_id:
            path.unlink(missing_ok=True)
    except (OSError, json.JSONDecodeError):
        pass


def supervise(project_root: Path, firefox_pid: int, session_id: str) -> int:
    runtime = project_root / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    lock_path = runtime / "backend.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        existing = read_live_state(project_root)
        if existing:
            return 0
        _remove_stale_backend_state(project_root)
        log = (runtime / "firefox-session.log").open("ab")
        environment = os.environ.copy()
        environment["TUMBLR_SCRAPER_RUNTIME_OWNER"] = "firefox-session"
        environment["TUMBLR_SCRAPER_FIREFOX_SESSION_ID"] = session_id
        command = [sys.executable, str(project_root / "bootstrap.py"), "browser", "--serve-only"]
        backend = subprocess.Popen(
            command,
            cwd=str(project_root),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log,
            start_new_session=True,
        )
        log.close()
        deadline = time.monotonic() + 35
        state = None
        while time.monotonic() < deadline:
            if backend.poll() is not None:
                return 1
            state = read_live_state(project_root)
            if state and state.get("owner") == "firefox-session" and state.get("session_id") == session_id:
                break
            time.sleep(0.2)
        if not state or state.get("session_id") != session_id:
            _stop_owned_backend(project_root, session_id, backend.pid, str((state or {}).get("instance_id", "")))
            return 1
        _write_session(project_root, {
            "schema": 1,
            "firefox_pid": firefox_pid,
            "supervisor_pid": os.getpid(),
            "backend_pid": backend.pid,
            "backend_instance_id": state["instance_id"],
            "url": state["url"],
            "ownership": "firefox-session",
        })

    try:
        while _pid_alive(firefox_pid) and backend.poll() is None:
            time.sleep(1.0)
    finally:
        _stop_owned_backend(project_root, session_id, backend.pid, str(state["instance_id"]))
        try:
            backend.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(backend.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            backend.wait(timeout=5)
        _remove_owned_state(project_root, session_id, str(state["instance_id"]))
        try:
            session_path(project_root).unlink()
        except FileNotFoundError:
            pass
    return 0


def _pid_alive(pid: int) -> bool:
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        for line in status.splitlines():
            if line.startswith("State:") and "\tZ" in line:
                return False
        os.kill(pid, 0)
    except (FileNotFoundError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--supervise", action="store_true")
    parser.add_argument("project_root", type=Path)
    parser.add_argument("firefox_pid", type=int)
    parser.add_argument("session_id")
    args = parser.parse_args()
    if not args.supervise:
        return 2
    return supervise(args.project_root.resolve(), args.firefox_pid, args.session_id)


if __name__ == "__main__":
    raise SystemExit(main())
