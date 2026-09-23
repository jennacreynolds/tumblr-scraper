#!/usr/bin/env python3
"""Prepare the project runtime, then re-exec the existing application."""

from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Sequence


APPLICATION_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = APPLICATION_ROOT.parent if APPLICATION_ROOT.name == "app" else APPLICATION_ROOT
RUNTIME_ROOT = PROJECT_ROOT / ".runtime"
VENV_ROOT = RUNTIME_ROOT / "venv"
STAMP_PATH = RUNTIME_ROOT / "bootstrap-state.json"
MAIN_PATH = APPLICATION_ROOT / "main.py"
BROWSER_HOST_PATH = APPLICATION_ROOT / "Tumblr Scraper - Android.py"
BOOTSTRAP_SCHEMA = 1
BOOTSTRAP_ACTIVE = "TUMBLR_BOOTSTRAP_ACTIVE"
DEPENDENCY_SPECS = (
    "tumblr-backup==1.0.7",
    "urllib3>=2.2.2,<2.6",
)


class BootstrapError(RuntimeError):
    """A user-actionable runtime preparation failure."""


def _version_tuple(value: str) -> tuple[int, ...]:
    numbers = [int(part) for part in re.findall(r"\d+", value)]
    return tuple(numbers or [0])


def _distribution_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _versions_satisfy() -> bool:
    try:
        import tumblr_backup.main  # noqa: F401
    except ImportError:
        return False

    tumblr_version = _distribution_version("tumblr-backup")
    urllib_version = _distribution_version("urllib3")
    if tumblr_version != "1.0.7" or urllib_version is None:
        return False
    version = _version_tuple(urllib_version)
    return _version_tuple("2.2.2") <= version < _version_tuple("2.6")


def runtime_python(project_root: Path = PROJECT_ROOT) -> Path:
    venv_root = project_root / ".runtime" / "venv"
    if os.name == "nt":
        return venv_root / "Scripts" / "python.exe"
    return venv_root / "bin" / "python"


def _expected_stamp() -> dict[str, object]:
    return {
        "bootstrap_schema": BOOTSTRAP_SCHEMA,
        "requirements": list(DEPENDENCY_SPECS),
    }


def _stamp_matches(project_root: Path) -> bool:
    try:
        with (project_root / ".runtime" / "bootstrap-state.json").open(encoding="utf-8") as source:
            return json.load(source) == _expected_stamp()
    except (OSError, json.JSONDecodeError):
        return False


def _runtime_imports_are_valid(runtime: Path) -> bool:
    if not runtime.is_file():
        return False
    probe = (
        "import importlib.metadata as m; "
        "import tumblr_backup.main; "
        "from urllib3 import __version__ as u; "
        "assert m.version('tumblr-backup') == '1.0.7'; "
        "parts=tuple(int(x) for x in u.split('.')[:3]); "
        "assert (2, 2, 2) <= parts < (2, 6, 0)"
    )
    result = subprocess.run(
        [str(runtime), "-c", probe],
        cwd=str(runtime.parent.parent.parent.parent),
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def runtime_is_valid(project_root: Path = PROJECT_ROOT) -> bool:
    runtime = runtime_python(project_root)
    return _stamp_matches(project_root) and _runtime_imports_are_valid(runtime)


def _remove_invalid_runtime(project_root: Path) -> None:
    runtime_root = project_root / ".runtime"
    venv_root = runtime_root / "venv"
    if venv_root.exists():
        shutil.rmtree(venv_root)
    runtime_root.mkdir(parents=True, exist_ok=True)


def _create_venv(project_root: Path) -> Path:
    runtime_root = project_root / ".runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    runtime = runtime_python(project_root)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "venv", str(runtime_root / "venv")],
            cwd=str(project_root),
            check=False,
        )
    except OSError as exc:
        raise BootstrapError(
            "Could not create Tumblr Scraper's private Python environment.\n"
            "Your Python installation does not provide venv support.\n"
            "No system packages were modified.\n"
            "On Debian/Ubuntu this is commonly supplied by python3-venv."
        ) from exc
    if result.returncode != 0 or not runtime.is_file():
        raise BootstrapError(
            "Could not create Tumblr Scraper's private Python environment.\n"
            "Your Python installation does not provide usable venv support.\n"
            "No system packages were modified.\n"
            "On Debian/Ubuntu this is commonly supplied by python3-venv."
        )
    return runtime


def _install_dependencies(project_root: Path, runtime: Path) -> None:
    pip_probe = subprocess.run(
        [str(runtime), "-m", "pip", "--version"],
        cwd=str(project_root),
        check=False,
    )
    if pip_probe.returncode != 0:
        raise BootstrapError(
            "Tumblr Scraper created its private Python environment, but pip is "
            "unavailable inside it. No system packages were modified."
        )

    command = [str(runtime), "-m", "pip", "install", "--disable-pip-version-check"]
    wheels_path = APPLICATION_ROOT / "wheels"
    if wheels_path.is_dir() and any(wheels_path.iterdir()):
        command.extend(["--no-index", "--find-links", str(wheels_path)])
    command.extend(DEPENDENCY_SPECS)
    result = subprocess.run(command, cwd=str(project_root), check=False)
    if result.returncode != 0:
        raise BootstrapError(
            "Tumblr Scraper could not install its pinned dependencies into the "
            "private environment. Check internet access or the bundled wheels.\n"
            "No system packages were modified."
        )


def _write_stamp(project_root: Path) -> None:
    runtime_root = project_root / ".runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    temporary = runtime_root / "bootstrap-state.json.tmp"
    temporary.write_text(json.dumps(_expected_stamp(), indent=2) + "\n", encoding="utf-8")
    temporary.replace(runtime_root / "bootstrap-state.json")


def ensure_desktop_runtime(project_root: Path = PROJECT_ROOT) -> Path:
    runtime = runtime_python(project_root)
    if runtime_is_valid(project_root):
        return runtime

    _remove_invalid_runtime(project_root)
    runtime = _create_venv(project_root)
    print("First run: installing required Tumblr archive components in a private environment...", flush=True)
    _install_dependencies(project_root, runtime)
    if not _runtime_imports_are_valid(runtime):
        raise BootstrapError(
            "Tumblr Scraper's private environment was created, but its required "
            "dependencies did not validate. Remove .runtime and try again."
        )
    _write_stamp(project_root)
    return runtime


def is_pydroid() -> bool:
    return (
        sys.platform == "android"
        or bool(os.environ.get("ANDROID_ARGUMENT"))
        or bool(os.environ.get("P4A_BOOTSTRAP"))
    )


def ensure_pydroid_runtime(project_root: Path = PROJECT_ROOT) -> Path:
    if _versions_satisfy():
        return Path(sys.executable)
    command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *DEPENDENCY_SPECS]
    wheels_path = APPLICATION_ROOT / "wheels"
    if wheels_path.is_dir() and any(wheels_path.iterdir()):
        command[3:3] = ["--no-index", "--find-links", str(wheels_path)]
    result = subprocess.run(command, cwd=str(project_root), check=False)
    if result.returncode != 0 or not _versions_satisfy():
        raise BootstrapError(
            "Pydroid could not install the required Tumblr archive components.\n"
            "Check Pydroid's package support and internet access."
        )
    return Path(sys.executable)


def launch(mode: str, argv: Sequence[str] = (), *, pydroid: bool = False) -> int:
    if mode not in {"browser", "cli"}:
        raise BootstrapError(f"Unsupported bootstrap mode: {mode}")
    if os.environ.get(BOOTSTRAP_ACTIVE) == "1":
        raise BootstrapError("Bootstrap recursion detected; refusing to start another bootstrap cycle.")

    runtime = ensure_pydroid_runtime() if pydroid else ensure_desktop_runtime()
    target = BROWSER_HOST_PATH if mode == "browser" else MAIN_PATH
    environment = os.environ.copy()
    environment[BOOTSTRAP_ACTIVE] = "1"
    environment["TUMBLR_BOOTSTRAP_ROOT"] = str(PROJECT_ROOT)
    if os.name == "nt":
        result = subprocess.run(
            [str(runtime), str(target), *[str(value) for value in argv]],
            cwd=str(PROJECT_ROOT),
            env=environment,
            check=False,
        )
        return result.returncode
    os.execve(str(runtime), [str(runtime), str(target), *[str(value) for value in argv]], environment)
    raise AssertionError("os.execve returned unexpectedly")


def _usage() -> str:
    return (
        "Usage: bootstrap.py {browser|cli} [application arguments]\n"
        "\n"
        "This is an internal project launcher. Use ./tumblr-scraper for CLI work.\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if not values or values[0] not in {"browser", "cli"}:
        print(_usage(), file=sys.stderr)
        return 2
    try:
        return launch(values[0], values[1:])
    except BootstrapError as exc:
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
