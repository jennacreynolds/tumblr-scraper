#!/usr/bin/env python3
"""Build the single allowlisted release ZIP and its identity manifest."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
FILES = (
    ".gitignore",
    "README.md",
    "README.txt",
    "RELEASE_GATE.md",
    "ARCHITECTURE.md",
    "KNOWN_ISSUES.md",
    "THIRD_PARTY.md",
    "bootstrap.py",
    "main.py",
    "global.css",
    "network-policy.json",
    "context-policy.json",
    "Tumblr Scraper - Android.py",
    "Tumblr Scraper - Linux.desktop",
    "Tumblr Scraper - Windows.bat",
    "Tumblr Scraper - macOS.command",
    "Tumblr-Scraper-Linux.sh",
    "tumblr-scraper",
)
DIRECTORIES = ("assets", "bridge", "tests")
FORBIDDEN_PARTS = {
    "Backups",
    "Neighborhoods",
    ".git",
    ".runtime",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
}


def git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def worktree_is_dirty() -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def copy_allowlist(destination: Path) -> None:
    for relative in FILES:
        source = ROOT / relative
        if not source.is_file():
            raise RuntimeError(f"Missing allowlisted file: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    ignored = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".mypy_cache")
    for relative in DIRECTORIES:
        source = ROOT / relative
        if not source.is_dir():
            raise RuntimeError(f"Missing allowlisted directory: {relative}")
        shutil.copytree(source, destination / relative, ignore=ignored)


def iter_files(root: Path):
    yield root
    for path in sorted(root.rglob("*")):
        yield path


def zip_mode(path: Path) -> int:
    mode = stat.S_IMODE(path.stat().st_mode)
    if path.is_dir():
        return stat.S_IFDIR | mode
    return stat.S_IFREG | mode


def write_zip(staging: Path, output: Path, top_level: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in iter_files(staging):
            relative = path.relative_to(staging.parent).as_posix()
            if path.is_dir():
                name = relative.rstrip("/") + "/"
            else:
                name = relative
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = zip_mode(path) << 16
            if path.is_dir():
                info.external_attr |= 0x10
                archive.writestr(info, b"")
            else:
                archive.writestr(info, path.read_bytes())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--artifact-name", default="Tumblr-Scraper-Candidate.zip")
    parser.add_argument("--commit", default=None, help="Expected source commit; defaults to HEAD")
    parser.add_argument("--allow-dirty", action="store_true", help="Preview only; not valid for a release gate")
    args = parser.parse_args()

    if not args.artifact_name.endswith(".zip") or Path(args.artifact_name).name != args.artifact_name:
        parser.error("--artifact-name must be one ZIP filename")
    if worktree_is_dirty() and not args.allow_dirty:
        raise RuntimeError("Release artifact requires a clean worktree; commit changes before building")
    commit = args.commit or git_commit()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = args.output_dir / args.artifact_name
    top_level = output.stem

    with tempfile.TemporaryDirectory(prefix="tumblr-release-stage ") as temporary:
        staging = Path(temporary) / top_level
        staging.mkdir()
        copy_allowlist(staging)
        write_zip(staging, output, top_level)

    manifest = {
        "schema": 1,
        "commit": commit,
        "artifact": output.name,
        "sha256": sha256(output),
        "size": output.stat().st_size,
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "allowlist": {"files": list(FILES), "directories": list(DIRECTORIES)},
    }
    (args.output_dir / "release-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"release artifact build failed: {exc}", file=sys.stderr)
        raise SystemExit(2)
