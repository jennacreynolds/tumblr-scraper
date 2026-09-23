#!/usr/bin/env python3
"""Break the named-archive boundary deliberately and record the result."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tumblr_scraper import archive_manager  # noqa: E402


def main() -> int:
    lines: list[str] = ["Tumblr archive reliability probe", ""]
    failures = 0
    with tempfile.TemporaryDirectory(prefix="tumblr archive break probe ") as directory:
        root = Path(directory) / "Archive"

        invalid_names = ("../escape", "/tmp/escape", "nested/name", "bad\x00name")
        for value in invalid_names:
            try:
                archive_manager.create_archive(root, value)
            except archive_manager.ArchiveBundleError as exc:
                lines.append(f"PASS break-1 invalid archive name {value!r}: {exc}")
            else:
                failures += 1
                lines.append(f"FAIL break-1 invalid archive name was accepted: {value!r}")

        archive_manager.create_archive(root, "safe")
        (root / "active.json").write_text("not json\n", encoding="utf-8")
        selected = archive_manager.active_archive_name(root)
        if selected == "default":
            lines.append("PASS break-2 corrupt active pointer: fell back to default")
        else:
            failures += 1
            lines.append(f"FAIL break-2 corrupt active pointer selected {selected!r}")

        broken = root / "safe" / "archive.json"
        broken.write_text("{not json\n", encoding="utf-8")
        try:
            archive_manager.load_archive(root, "safe")
        except archive_manager.ArchiveBundleError as exc:
            lines.append(f"PASS break-2 corrupt manifest: {exc}")
        else:
            failures += 1
            lines.append("FAIL break-2 corrupt manifest was accepted")

        bundle = root / "safe"
        broken.unlink()
        (bundle / "Content" / "keep.json").write_text(json.dumps({"kept": True}) + "\n", encoding="utf-8")
        (bundle / "App").rmdir()
        (bundle / "App").mkdir()
        if (bundle / "Content" / "keep.json").is_file() and (bundle / "Network").is_dir():
            lines.append("PASS break-3 disposable App loss: data remained recoverable")
        else:
            failures += 1
            lines.append("FAIL break-3 disposable App loss damaged archive data")

    lines.append("")
    lines.append(f"result={'PASS' if failures == 0 else 'FAIL'} failures={failures}")
    output = ROOT / "diagnostics" / "reliability-last-run.txt"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"diagnostics: {output}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
