#!/usr/bin/env python3
"""Extract a release ZIP while preserving stored Unix executable modes."""

from __future__ import annotations

from pathlib import Path
import zipfile


def extract_preserving_modes(zip_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            archive.extract(info, destination)
            target = destination / info.filename
            mode = (info.external_attr >> 16) & 0o777
            if mode and target.exists():
                target.chmod(mode)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=True)
    extract_preserving_modes(args.zip, args.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
