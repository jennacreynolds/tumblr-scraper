#!/usr/bin/env python3
"""Run the repeatable Linux gate against one exact release ZIP."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from extract_release_artifact import extract_preserving_modes
from verify_release_artifact import verify


def run(command: list[str], *, cwd: Path, environment: dict[str, str], input_text: str = "") -> None:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout[-4000:]}\n"
            f"stderr:\n{result.stderr[-4000:]}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", dest="zip_path", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--skip-browser", action="store_true", help="Skip loopback browser-wrapper smoke")
    args = parser.parse_args()

    identity = verify(args.zip_path, args.manifest)
    with tempfile.TemporaryDirectory(prefix="Tumblr Scraper Release Test ") as temporary:
        base = Path(temporary)
        extracted = base / "extracted package with spaces"
        extracted.mkdir()
        extract_preserving_modes(args.zip_path, extracted)
        roots = [path for path in extracted.iterdir() if path.is_dir()]
        if len(roots) != 1:
            raise RuntimeError("Extracted release does not have one project directory")
        project = roots[0]
        unrelated = base / "unrelated cwd"
        unrelated.mkdir()
        environment = os.environ.copy()
        environment["BROWSER"] = "true"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"

        run(["python3", str(project / "bootstrap.py"), "cli", "--help"], cwd=unrelated, environment=environment)
        runtime = project / ".runtime" / "venv" / "bin" / "python"
        if not runtime.is_file():
            raise RuntimeError("Clean bootstrap did not create .runtime/venv/bin/python")
        run([str(runtime), "-m", "unittest", "discover", "-q"], cwd=project, environment=environment)
        run([str(runtime), "-m", "py_compile", str(project / "bootstrap.py"), str(project / "main.py")], cwd=project, environment=environment)
        if shutil.which("node"):
            run(["node", "--check", str(project / "assets" / "archive.js")], cwd=project, environment=environment)
        if shutil.which("desktop-file-validate"):
            run(["desktop-file-validate", str(project / "Tumblr Scraper - Linux.desktop")], cwd=project, environment=environment)
        if not args.skip_browser:
            run(["/bin/sh", str(project / "Tumblr-Scraper-Linux.sh")], cwd=unrelated, environment=environment)

    print(f"Linux release gate passed: {identity['artifact']} {identity['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
