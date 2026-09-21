#!/usr/bin/env python3
"""Serve the already-generated local archive on loopback and open it."""

from __future__ import annotations

import argparse
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent
BACKUPS = (ROOT / "Backups").resolve()
NEIGHBORHOODS = (ROOT / "Neighborhoods").resolve()


class ArchiveHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        request_path = unquote(urlsplit(path).path)
        candidate = (ROOT / request_path.lstrip("/")).resolve()
        allowed = (BACKUPS, NEIGHBORHOODS)
        if any(candidate == root or root in candidate.parents for root in allowed):
            return str(candidate)
        return str(ROOT / "__archive_not_found__")

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="Open the generated local Tumblr archive")
    parser.add_argument("--no-open", action="store_true", help="print the URL without opening a browser")
    args = parser.parse_args()
    if not (BACKUPS / "index.html").is_file():
        parser.error(f"archive index not found: {BACKUPS / 'index.html'}")
    server = ThreadingHTTPServer(("127.0.0.1", 0), ArchiveHandler)
    url = f"http://127.0.0.1:{server.server_port}/Backups/index.html"
    print(f"Local archive: {url}")
    if not args.no_open:
        webbrowser.open(url, new=2)
    print("Press Ctrl-C to stop the local viewer.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nLocal archive viewer stopped.")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
