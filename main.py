#!/usr/bin/env python3
"""Compatibility entrypoint for the migrated Tumblr Scraper package."""

from __future__ import annotations

from pathlib import Path
import sys


_HERE = Path(__file__).resolve().parent
_SRC = _HERE / "src" if (_HERE / "src" / "tumblr_scraper").is_dir() else _HERE
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from tumblr_scraper import main as _implementation  # noqa: E402

# Existing tests, tools, and integrations historically import ``main`` and
# mutate its module globals. Returning the implementation module preserves that
# behavior while the source now lives under src/tumblr_scraper.
sys.modules[__name__] = _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
