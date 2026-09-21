#!/usr/bin/env python3
"""Run the native-host release gate against one exact ZIP."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from extract_release_artifact import extract_preserving_modes
from verify_release_artifact import verify


def run(command: list[str], *, cwd: Path, environment: dict[str, str]) -> None:
    result = subprocess.run(command, cwd=cwd, env=environment, text=True, capture_output=True, check=False, timeout=180)
    if result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout[-5000:]}\n"
            f"stderr:\n{result.stderr[-5000:]}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", dest="zip_path", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    identity = verify(args.zip_path, args.manifest)

    with tempfile.TemporaryDirectory(prefix="Tumblr Scraper Native Gate ") as temporary:
        base = Path(temporary)
        extracted = base / "candidate extracted with spaces"
        extracted.mkdir()
        extract_preserving_modes(args.zip_path, extracted)
        roots = [path for path in extracted.iterdir() if path.is_dir()]
        if len(roots) != 1:
            raise RuntimeError("Candidate ZIP must extract to one project directory")
        project = roots[0]
        unrelated = base / "unrelated cwd"
        unrelated.mkdir()
        environment = os.environ.copy()
        environment["BROWSER"] = "true"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"

        run([str(Path(os.environ.get("PYTHON", shutil.which("python") or "python3"))), str(project / "bootstrap.py"), "cli", "--help"], cwd=unrelated, environment=environment)
        runtime = project / ".runtime" / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not runtime.is_file():
            raise RuntimeError(f"Private runtime was not created: {runtime}")
        run([str(runtime), "-m", "unittest", "discover", "-q"], cwd=project, environment=environment)
        run([str(runtime), str(project / "tumblr-scraper"), "--help"], cwd=unrelated, environment=environment)

        if os.name == "posix":
            run(["sh", "-n", str(project / "Tumblr-Scraper-Linux.sh")], cwd=project, environment=environment)
            run(["sh", "-n", str(project / "Tumblr Scraper - macOS.command")], cwd=project, environment=environment)
            if shutil.which("desktop-file-validate"):
                run(["desktop-file-validate", str(project / "Tumblr Scraper - Linux.desktop")], cwd=project, environment=environment)
            if shutil.which("gio"):
                result = subprocess.run(
                    ["gio", "launch", str(project / "Tumblr Scraper - Linux.desktop")],
                    cwd=unrelated,
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=20,
                )
                print(f"gio launch best-effort result: {result.returncode}")

    print(f"Native release gate passed: {identity['artifact']} {identity['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
