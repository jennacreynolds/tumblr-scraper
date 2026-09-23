#!/usr/bin/env python3
"""Save, load, rename, and migrate named archive bundles.

This is a source-side maintenance tool. It never places Python or UI code in
an archive bundle.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tumblr_scraper import archive_manager, paths  # noqa: E402


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Manage portable named Tumblr archive bundles.")
    result.add_argument("--archive-root", type=Path, default=paths.ARCHIVES_ROOT)
    sub = result.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list named archive bundles")
    create = sub.add_parser("create", help="create an empty named bundle")
    create.add_argument("name")
    save = sub.add_parser("save", help="copy the selected bundle into a named bundle")
    save.add_argument("name")
    save.add_argument("--source", type=Path, default=paths.ARCHIVE_ROOT)
    save.add_argument("--overwrite", action="store_true")
    load = sub.add_parser("load", help="select a named bundle for the next process start")
    load.add_argument("name")
    rename = sub.add_parser("rename", help="rename a named bundle")
    rename.add_argument("old_name")
    rename.add_argument("new_name")
    migrate = sub.add_parser("migrate-flat", help="move old flat Archive output into a named bundle")
    migrate.add_argument("name", nargs="?", default="default")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "list":
            bundles = archive_manager.list_archives(args.archive_root)
            for bundle in bundles:
                marker = " *" if archive_manager.active_archive_name(args.archive_root) == bundle.name else ""
                print(f"{bundle.name}{marker}\t{bundle.path}")
            return 0
        if args.command == "create":
            bundle = archive_manager.create_archive(args.archive_root, args.name)
        elif args.command == "save":
            bundle = archive_manager.save_archive(args.source, args.archive_root, args.name, overwrite=args.overwrite)
        elif args.command == "load":
            bundle = archive_manager.load_archive(args.archive_root, args.name)
        elif args.command == "rename":
            bundle = archive_manager.rename_archive(args.archive_root, args.old_name, args.new_name)
        else:
            bundle = archive_manager.migrate_flat_archive(args.archive_root, args.name)
        print(bundle.path)
        return 0
    except archive_manager.ArchiveBundleError as exc:
        print(f"archive management failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
