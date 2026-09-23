"""Compatibility import for the graph projection module."""

from __future__ import annotations

from pathlib import Path
import sys


_HERE = Path(__file__).resolve().parent
_SRC = _HERE / "src" if (_HERE / "src" / "tumblr_scraper").is_dir() else _HERE
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from tumblr_scraper import graph_projection as _implementation  # noqa: E402

sys.modules[__name__] = _implementation
