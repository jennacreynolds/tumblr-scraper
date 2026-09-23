#!/usr/bin/env python3
"""Install or remove the temporary per-user Linux Native Messaging manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import os


HOST_NAME = "tumblr_scraper_firefox"
EXTENSION_ID = "tumblr-scraper-firefox@local.invalid"


def manifest_path() -> Path:
    return Path.home() / ".mozilla" / "native-messaging-hosts" / f"{HOST_NAME}.json"


def install() -> None:
    host = Path(__file__).resolve().with_name("native_host.py")
    host.chmod(host.stat().st_mode | 0o111)
    target = manifest_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "name": HOST_NAME,
        "description": "Tumblr Scraper temporary Firefox host",
        "path": str(host),
        "type": "stdio",
        "allowed_extensions": [EXTENSION_ID],
    }, indent=2) + "\n", encoding="utf-8")
    print(f"Installed {target}")


def uninstall() -> None:
    target = manifest_path()
    target.unlink(missing_ok=True)
    print(f"Removed {target}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("install", "uninstall"))
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("this temporary installer is for Linux only")
    (install if args.command == "install" else uninstall)()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
