#!/usr/bin/env python3
"""Run the native-host release gate against one exact ZIP."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import sys

from extract_release_artifact import extract_preserving_modes
from verify_release_artifact import verify


def run(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    input_text: str = "",
    timeout: int = 180,
) -> None:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
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
        environment["TUMBLR_SCRAPER_NO_BROWSER"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"

        run([sys.executable, str(project / "bootstrap.py"), "cli", "--help"], cwd=unrelated, environment=environment)
        runtime = project / ".runtime" / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not runtime.is_file():
            raise RuntimeError(f"Private runtime was not created: {runtime}")
        run([str(runtime), "-m", "unittest", "discover", "-q"], cwd=project, environment=environment)
        run([str(runtime), str(project / "tumblr-scraper"), "--help"], cwd=unrelated, environment=environment)

        if os.name == "nt":
            launcher = ["cmd.exe", "/d", "/c", str(project / "Tumblr Scraper - Windows.bat")]
        elif sys.platform == "darwin":
            launcher = ["/bin/sh", str(project / "Tumblr Scraper - macOS.command")]
        else:
            launcher = ["/bin/sh", str(project / "Tumblr-Scraper-Linux.sh")]
        run(launcher, cwd=unrelated, environment=environment, input_text="\n")

        if os.name == "posix":
            run(["sh", "-n", str(project / "Tumblr-Scraper-Linux.sh")], cwd=project, environment=environment)
            run(["sh", "-n", str(project / "Tumblr Scraper - macOS.command")], cwd=project, environment=environment)
            if shutil.which("desktop-file-validate"):
                run(["desktop-file-validate", str(project / "Tumblr Scraper - Linux.desktop")], cwd=project, environment=environment)
            if shutil.which("gio"):
                try:
                    result = subprocess.run(
                        ["gio", "launch", str(project / "Tumblr Scraper - Linux.desktop")],
                        cwd=unrelated,
                        env=environment,
                        input="\n",
                        text=True,
                        capture_output=True,
                        check=False,
                        timeout=20,
                    )
                    print(f"gio launch best-effort result: {result.returncode}")
                except subprocess.TimeoutExpired:
                    print("gio launch best-effort result: timeout (desktop session unavailable or launcher remained open)")

    print(f"Native release gate passed: {identity['artifact']} {identity['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
