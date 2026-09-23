"""Canonical source-record storage mechanics.

Generated HTML, media, indexes, and presentation state are intentionally not
owned here.  Canonical source JSON is the durable acquisition boundary.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def source_record_path(json_dir: Path, post_id: int | str) -> Path:
    return Path(json_dir) / f"{post_id}.json"


def source_record_exists(json_dir: Path, post_id: int | str) -> bool:
    return source_record_path(json_dir, post_id).is_file()


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Durably replace one JSON record without exposing a partial file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def read_json_records(directory: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            with path.open(encoding="utf-8") as handle:
                record = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records
