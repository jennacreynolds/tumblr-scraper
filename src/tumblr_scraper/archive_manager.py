"""Named archive bundle management.

This module owns filesystem operations for portable archive data bundles.  It
does not render HTML, run the crawler, or decide CapturePolicy.  The live
application can use one selected bundle; the application source remains
outside every bundle.

Bundle layout::

    Archive/<name>/
        archive.json       small manifest, not canonical post data
        Content/            canonical records and local media
        Network/            observations and derived graph data
        App/                disposable generated reader export

``App/`` can be deleted and regenerated.  ``archive.json`` is deliberately
small so a bundle can be copied, renamed, or moved without a database.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import time
import uuid
from dataclasses import dataclass
from typing import Any


ARCHIVE_SCHEMA_VERSION = 1
_BUNDLE_DIRECTORIES = ("Content", "Network", "App")


class ArchiveBundleError(ValueError):
    """Raised when a named archive bundle is unsafe or unavailable."""


@dataclass(frozen=True)
class ArchiveBundle:
    name: str
    path: Path
    manifest: dict[str, Any]


def validate_archive_name(name: str) -> str:
    value = str(name or "").strip()
    if not value or value in {".", ".."} or Path(value).name != value:
        raise ArchiveBundleError("archive name must be one non-empty path component")
    if any(character in value for character in ('/', "\\", "\x00")):
        raise ArchiveBundleError("archive name contains a path separator")
    return value


def manifest_path(bundle: Path) -> Path:
    return Path(bundle) / "archive.json"


def _safe_bundle_path(archives_root: Path, name: str) -> Path:
    root = Path(archives_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    candidate = root / validate_archive_name(name)
    resolved = candidate.resolve(strict=False)
    if resolved.parent != root:
        raise ArchiveBundleError("archive path escapes the Archive container")
    if candidate.is_symlink():
        raise ArchiveBundleError("archive bundle path may not be a symlink")
    return candidate


def _read_manifest(bundle: Path) -> dict[str, Any]:
    path = manifest_path(bundle)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        value = {}
    return value if isinstance(value, dict) else {}


def _write_manifest(bundle: Path, *, name: str, source: str = "") -> dict[str, Any]:
    payload = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "name": name,
        "source": source,
        "created_at": time.time(),
        "updated_at": time.time(),
        "generated_app_is_disposable": True,
        "application_source_is_external": True,
    }
    temporary = manifest_path(bundle).with_name(f".archive.json.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, manifest_path(bundle))
    return payload


def _looks_like_bundle(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and (
        manifest_path(path).is_file()
        or any((path / name).is_dir() for name in _BUNDLE_DIRECTORIES)
    )


def list_archives(archives_root: Path) -> list[ArchiveBundle]:
    """List named bundles, ignoring source code and deprecated material."""
    root = Path(archives_root)
    if not root.is_dir():
        return []
    result = []
    for path in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        if not _looks_like_bundle(path):
            continue
        result.append(ArchiveBundle(path.name, path, _read_manifest(path)))
    return result


def get_archive(archives_root: Path, name: str) -> ArchiveBundle:
    safe = validate_archive_name(name)
    path = _safe_bundle_path(archives_root, safe)
    if not _looks_like_bundle(path):
        raise ArchiveBundleError(f"archive bundle does not exist: {safe}")
    manifest = _read_manifest(path)
    if manifest_path(path).is_file() and not manifest:
        raise ArchiveBundleError(f"archive manifest is unreadable: {safe}")
    return ArchiveBundle(safe, path, manifest)


def ensure_archive_manifest(bundle: Path, name: str) -> ArchiveBundle:
    """Add the small manifest to an existing named bundle if it is absent."""
    safe = validate_archive_name(name)
    path = Path(bundle)
    if not path.is_dir() or path.name != safe:
        raise ArchiveBundleError(f"not an archive bundle: {path}")
    manifest = _read_manifest(path)
    if not manifest:
        manifest = _write_manifest(path, name=safe, source="existing named bundle")
    return ArchiveBundle(safe, path, manifest)


def create_archive(archives_root: Path, name: str) -> ArchiveBundle:
    safe = validate_archive_name(name)
    path = _safe_bundle_path(archives_root, safe)
    if path.exists():
        raise ArchiveBundleError(f"archive bundle already exists: {safe}")
    path.mkdir(parents=True)
    for directory in _BUNDLE_DIRECTORIES:
        (path / directory).mkdir()
    manifest = _write_manifest(path, name=safe, source="created")
    return ArchiveBundle(safe, path, manifest)


def _copy_bundle_contents(source: Path, destination: Path) -> None:
    for name in _BUNDLE_DIRECTORIES:
        source_path = source / name
        if not source_path.exists():
            continue
        target = destination / name
        if target.exists():
            shutil.rmtree(target)
        if source_path.is_dir():
            shutil.copytree(source_path, target)
        else:
            shutil.copy2(source_path, target)


def save_archive(source_root: Path, archives_root: Path, name: str, *, overwrite: bool = False) -> ArchiveBundle:
    """Copy one active bundle into a named portable sibling bundle."""
    safe = validate_archive_name(name)
    source = Path(source_root).resolve()
    parent = Path(archives_root).resolve()
    destination = _safe_bundle_path(parent, safe)
    if not source.is_dir():
        raise ArchiveBundleError(f"archive source is not a directory: {source}")
    if source == destination or source in destination.parents:
        raise ArchiveBundleError("cannot save an archive into itself")
    if destination.exists():
        if not overwrite:
            raise ArchiveBundleError(f"archive bundle already exists: {safe}")
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    _copy_bundle_contents(source, destination)
    manifest = _write_manifest(destination, name=safe, source=str(source))
    return ArchiveBundle(safe, destination, manifest)


def load_archive(archives_root: Path, name: str) -> ArchiveBundle:
    """Resolve a named bundle and mark it as the next process's active bundle."""
    bundle = get_archive(archives_root, name)
    pointer_root = Path(archives_root)
    pointer_root.mkdir(parents=True, exist_ok=True)
    pointer = pointer_root / "active.json"
    temporary = pointer.with_name(f".active.json.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps({"schema_version": ARCHIVE_SCHEMA_VERSION, "name": bundle.name}, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, pointer)
    return bundle


def active_archive_name(archives_root: Path, default: str = "default") -> str:
    pointer = Path(archives_root) / "active.json"
    try:
        value = json.loads(pointer.read_text(encoding="utf-8"))
        selected = validate_archive_name(value.get("name") or default)
        return selected if _looks_like_bundle(Path(archives_root) / selected) else validate_archive_name(default)
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError, ArchiveBundleError):
        return validate_archive_name(default)


def rename_archive(archives_root: Path, old_name: str, new_name: str) -> ArchiveBundle:
    old = get_archive(archives_root, old_name)
    new = validate_archive_name(new_name)
    destination = _safe_bundle_path(archives_root, new)
    if destination.exists():
        raise ArchiveBundleError(f"archive bundle already exists: {new}")
    was_active = active_archive_name(archives_root) == old.name
    old.path.rename(destination)
    manifest = _write_manifest(destination, name=new, source=f"renamed from {old.name}")
    if was_active:
        load_archive(archives_root, new)
    return ArchiveBundle(new, destination, manifest)


def migrate_flat_archive(archives_root: Path, name: str = "default") -> ArchiveBundle:
    """Move the old flat ``Archive/{Content,Network,App}`` layout into a bundle.

    This is an explicit migration used by recovery tooling, never an import
    side effect.  It refuses to merge into an existing named bundle.
    """
    safe = validate_archive_name(name)
    parent = Path(archives_root)
    parent.mkdir(parents=True, exist_ok=True)
    destination = _safe_bundle_path(parent, safe)
    if destination.exists():
        raise ArchiveBundleError(f"archive bundle already exists: {safe}")
    present = [parent / item for item in _BUNDLE_DIRECTORIES if (parent / item).exists()]
    if not present:
        raise ArchiveBundleError("no flat Archive/{Content,Network,App} layout found")
    destination.mkdir(parents=True)
    for source in present:
        shutil.move(str(source), str(destination / source.name))
    manifest = _write_manifest(destination, name=safe, source="migrated from flat Archive layout")
    load_archive(parent, safe)
    return ArchiveBundle(safe, destination, manifest)
