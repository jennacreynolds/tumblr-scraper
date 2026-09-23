"""Application path authority.

The editable application bundle and generated archive are deliberately
separate.  ``Archive`` is disposable user data; deleting it cannot delete the
implementation.
"""

from __future__ import annotations

import os
from pathlib import Path


def _roots(anchor: Path) -> tuple[Path, Path]:
    override = os.environ.get("TUMBLR_SCRAPER_BUNDLE_ROOT")
    if override:
        bundle = Path(override).expanduser().resolve()
        resource = bundle / "app" if (bundle / "app" / "assets").is_dir() else bundle
        return bundle, resource

    anchor = anchor.resolve()
    for candidate in (anchor, *anchor.parents):
        if candidate.name == "app" and (candidate / "assets").is_dir():
            return candidate.parent, candidate
        if (candidate / "assets").is_dir() and (candidate / "global.css").is_file():
            return candidate, candidate
    # Development fallback for an unusual editable-install location.
    return anchor.parent, anchor.parent


BUNDLE_ROOT, RESOURCE_ROOT = _roots(Path(__file__).parent)
DATA_ROOT = Path(os.environ.get("TUMBLR_SCRAPER_DATA_ROOT", BUNDLE_ROOT)).expanduser().resolve()

# ``Archive`` is a container for portable, named data bundles.  It is not the
# application bundle and it is not required for the live source-owned UI to
# exist.  The environment override is intentionally boring: hosts can select
# a bundle without teaching the crawler or browser adapter a second archive
# model.
ARCHIVES_ROOT = DATA_ROOT / "Archive"
DEFAULT_ARCHIVE_NAME = "default"
_selected_archive = os.environ.get("TUMBLR_SCRAPER_ARCHIVE", "")
if not _selected_archive:
    try:
        import json
        _selected_archive = json.loads((ARCHIVES_ROOT / "active.json").read_text(encoding="utf-8")).get("name", "")
    except (OSError, UnicodeError, ValueError, AttributeError):
        _selected_archive = ""
ACTIVE_ARCHIVE_NAME = _selected_archive or DEFAULT_ARCHIVE_NAME
if not ACTIVE_ARCHIVE_NAME or Path(ACTIVE_ARCHIVE_NAME).name != ACTIVE_ARCHIVE_NAME:
    ACTIVE_ARCHIVE_NAME = DEFAULT_ARCHIVE_NAME
ARCHIVE_ROOT = ARCHIVES_ROOT / ACTIVE_ARCHIVE_NAME
CONTENT_ROOT = ARCHIVE_ROOT / "Content"
NETWORK_ROOT = ARCHIVE_ROOT / "Network"
OBSERVATIONS_ROOT = NETWORK_ROOT / "Observations"
NEIGHBORHOODS_ROOT = NETWORK_ROOT / "Neighborhoods"
APP_ROOT = ARCHIVE_ROOT / "App"
RUNTIME_DIR = DATA_ROOT / ".runtime"
ASSET_DIR = RESOURCE_ROOT / "assets"
POLICY_DIR = RESOURCE_ROOT


def resource_path(*parts: str) -> Path:
    return RESOURCE_ROOT.joinpath(*parts)


def data_path(*parts: str) -> Path:
    return DATA_ROOT.joinpath(*parts)


def archive_bundle_path(name: str) -> Path:
    """Return a named archive bundle below the disposable Archive folder."""
    safe = str(name or "").strip()
    if not safe or Path(safe).name != safe or safe in {".", ".."}:
        raise ValueError("archive name must be one path component")
    return ARCHIVES_ROOT / safe
