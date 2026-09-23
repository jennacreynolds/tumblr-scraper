#!/usr/bin/env python3
"""Tiny Firefox Native Messaging adapter for Tumblr Scraper."""

from __future__ import annotations

import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
import uuid

SESSION_DIR = Path(__file__).resolve().parents[1] / "session"
sys.path.insert(0, str(SESSION_DIR))
import firefox_session  # noqa: E402


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def read_message() -> dict[str, object] | None:
    raw_length = sys.stdin.buffer.read(4)
    if not raw_length:
        return None
    if len(raw_length) != 4:
        raise ValueError("incomplete Native Messaging length")
    length = struct.unpack("@I", raw_length)[0]
    if length > 1024 * 1024:
        raise ValueError("Native Messaging request is too large")
    payload = sys.stdin.buffer.read(length)
    if len(payload) != length:
        raise ValueError("incomplete Native Messaging payload")
    message = json.loads(payload.decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError("request must be an object")
    return message


def write_message(message: dict[str, object]) -> None:
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("@I", len(payload)))
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()


def firefox_pid() -> int:
    current = os.getppid()
    while current > 1:
        try:
            name = Path(f"/proc/{current}/comm").read_text(encoding="utf-8").strip().lower()
            if name in {"firefox", "firefox-bin"}:
                return current
            current = int(Path(f"/proc/{current}/stat").read_text(encoding="utf-8").split()[3])
        except (OSError, ValueError, IndexError):
            break
    raise RuntimeError("could not identify the Firefox parent process")


def open_application() -> dict[str, object]:
    existing = firefox_session.read_live_state(PROJECT_ROOT)
    if existing:
        return {"ok": True, "url": existing["url"], "owner": existing.get("owner", "external")}

    parent = firefox_pid()
    session_id = uuid.uuid4().hex
    command = [
        sys.executable,
        str(SESSION_DIR / "firefox_session.py"),
        "--supervise",
        str(PROJECT_ROOT),
        str(parent),
        session_id,
    ]
    subprocess.Popen(
        command,
        cwd=str(PROJECT_ROOT),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        state = firefox_session.read_live_state(PROJECT_ROOT)
        if state and state.get("owner") == "firefox-session" and state.get("session_id") == session_id:
            return {"ok": True, "url": state["url"], "owner": "firefox-session"}
        time.sleep(0.2)
    return {"ok": False, "error": "Tumblr Scraper did not become ready within 40 seconds."}


def main() -> int:
    try:
        message = read_message()
        if message is None:
            return 0
        if message.get("action") == "open":
            write_message(open_application())
        else:
            write_message({"ok": False, "error": "unsupported action"})
    except Exception as exc:
        write_message({"ok": False, "error": str(exc)})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
