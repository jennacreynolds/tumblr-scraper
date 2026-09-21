#!/usr/bin/env python3
"""Run the local Pydroid core gate and record separate UI evidence."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import tempfile
import time
from urllib.parse import quote
from xml.etree import ElementTree

from extract_release_artifact import extract_preserving_modes
from verify_release_artifact import verify


PACKAGE = "ru.iiec.pydroid3"
ACTIVITY = "ru.iiec.pydroid.MainActivity"


class GateError(RuntimeError):
    pass


def command_text(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise GateError(f"Command failed: {' '.join(command)}\n{result.stdout}\n{result.stderr}")
    return result


def adb(adb_path: Path, arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return command_text([str(adb_path), *arguments], check=check)


def sdk_tool(sdk_root: Path | None, relative: str, fallback: str) -> Path:
    if sdk_root:
        candidate = sdk_root / relative
        if candidate.is_file():
            return candidate
    resolved = shutil.which(fallback)
    if resolved:
        return Path(resolved)
    raise GateError(f"Could not find {fallback}; set --sdk-root")


def package_name(aapt: Path, apk: Path) -> str:
    result = command_text([str(aapt), "dump", "badging", str(apk)])
    match = re.search(r"^package: name='([^']+)'", result.stdout, re.MULTILINE)
    if not match:
        raise GateError("Could not determine APK package name")
    return match.group(1)


def wait_for_boot(adb_path: Path, timeout: int = 180) -> None:
    adb(adb_path, ["wait-for-device"])
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = adb(adb_path, ["shell", "getprop", "sys.boot_completed"], check=False)
        if result.stdout.strip() == "1":
            return
        time.sleep(2)
    raise GateError("Android emulator did not reach sys.boot_completed=1")


def dump_ui(adb_path: Path, destination: Path) -> str:
    remote = "/sdcard/android-gate-window.xml"
    adb(adb_path, ["shell", "uiautomator", "dump", remote], check=False)
    result = adb(adb_path, ["shell", "cat", remote], check=False)
    destination.write_text(result.stdout, encoding="utf-8")
    return result.stdout


def click_run_if_visible(adb_path: Path, evidence: Path) -> bool:
    xml = dump_ui(adb_path, evidence / "ui-before-run.xml")
    try:
        nodes = ElementTree.fromstring(xml).iter("node")
    except ElementTree.ParseError:
        return False
    bounds = None
    for node in nodes:
        if (
            node.attrib.get("text") == "Run"
            or node.attrib.get("content-desc") == "Run"
            or node.attrib.get("resource-id", "").endswith(":id/fab")
        ):
            bounds = node.attrib.get("bounds")
            break
    if not bounds:
        return False
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
    if not match:
        return False
    left, top, right, bottom = map(int, match.groups())
    command_text([str(adb_path), "shell", "input", "tap", str((left + right) // 2), str((top + bottom) // 2)])
    return True


def dismiss_first_run(adb_path: Path, evidence: Path, timeout: int = 45) -> None:
    """Advance only obvious Pydroid first-run screens before opening the probe."""
    deadline = time.time() + timeout
    step = 0
    labels = {"CONTINUE", "NEXT", "SKIP", "GOT IT"}
    while time.time() < deadline:
        ui_path = evidence / f"ui-first-run-{step}.xml"
        xml = dump_ui(adb_path, ui_path)
        try:
            nodes = ElementTree.fromstring(xml).iter("node")
        except ElementTree.ParseError:
            return
        target = None
        for node in nodes:
            if node.attrib.get("text", "").strip().upper() in labels:
                target = node.attrib.get("bounds")
                break
        if not target:
            return
        match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", target)
        if not match:
            return
        left, top, right, bottom = map(int, match.groups())
        command_text([str(adb_path), "shell", "input", "tap", str((left + right) // 2), str((top + bottom) // 2)])
        step += 1
        time.sleep(1)


def content_uri(relative: str) -> str:
    return "content://com.android.externalstorage.documents/document/primary%3A" + quote(relative, safe="")


def open_python_file(adb_path: Path, relative: str) -> None:
    uri = content_uri(relative)
    adb(adb_path, [
        "shell",
        "am",
        "start",
        "-n",
        f"{PACKAGE}/{ACTIVITY}",
        "-a",
        "android.intent.action.VIEW",
        "-d",
        uri,
        "-t",
        "text/x-python",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zip", dest="zip_path", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--avd", default="Moto_G53_Reference")
    parser.add_argument("--sdk-root", type=Path)
    parser.add_argument("--evidence-dir", type=Path, default=Path("android-gate-evidence"))
    parser.add_argument("--ui-timeout", type=int, default=45)
    args = parser.parse_args()

    identity = verify(args.zip_path, args.manifest)
    if not args.apk.is_file():
        raise GateError(f"APK does not exist: {args.apk}")
    sdk_root = args.sdk_root or (Path(os.environ["ANDROID_SDK_ROOT"]) if os.environ.get("ANDROID_SDK_ROOT") else None)
    adb_path = sdk_tool(sdk_root, "platform-tools/adb", "adb")
    emulator = sdk_tool(sdk_root, "emulator/emulator", "emulator")
    aapt = sdk_tool(sdk_root, "build-tools/34.0.0/aapt", "aapt")
    apk_package = package_name(aapt, args.apk)
    if apk_package != PACKAGE:
        raise GateError(f"Expected Pydroid package {PACKAGE}, got {apk_package}")

    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    (args.evidence_dir / "release-identity.json").write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8")
    (args.evidence_dir / "pydroid-apk-sha256.txt").write_text(
        subprocess.check_output(["sha256sum", str(args.apk)], text=True), encoding="utf-8"
    )

    emulator_process = subprocess.Popen(
        [str(emulator), "-avd", args.avd, "-no-window", "-no-snapshot", "-no-boot-anim", "-no-audio", "-gpu", "swiftshader_indirect"],
        stdout=(args.evidence_dir / "emulator.stdout.log").open("w"),
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        wait_for_boot(adb_path)
        adb(adb_path, ["install", "-r", str(args.apk)])
        with tempfile.TemporaryDirectory(prefix="Tumblr Scraper Android Package ") as temporary:
            extracted = Path(temporary) / "Tumblr Scraper Android Candidate With Spaces"
            extracted.mkdir()
            extract_preserving_modes(args.zip_path, extracted)
            roots = [path for path in extracted.iterdir() if path.is_dir()]
            if len(roots) != 1:
                raise GateError("Android candidate does not have one project directory")
            project = roots[0]
            probe = Path(__file__).with_name("android_gate_probe.py")
            shutil.copy2(probe, project / "android_gate_probe.py")
            device_root = "/sdcard/Download/Tumblr Scraper Beta Gate"
            # Older adb clients split each argument after `shell`; quote the
            # remote path explicitly because the scoped-storage test path
            # intentionally contains spaces.
            adb(adb_path, ["shell", f"mkdir -p {shlex.quote(device_root)}"])
            adb(adb_path, ["push", str(project / "."), device_root + "/"])
            sentinel = device_root + "/android-core-result.json"
            dismiss_first_run(adb_path, args.evidence_dir)
            open_python_file(adb_path, "Download/Tumblr Scraper Beta Gate/android_gate_probe.py")
            deadline = time.time() + args.ui_timeout
            clicked = False
            while time.time() < deadline:
                clicked = click_run_if_visible(adb_path, args.evidence_dir) or clicked
                if clicked:
                    break
                time.sleep(2)
            if not clicked:
                (args.evidence_dir / "android-result.json").write_text(
                    json.dumps({"core": "NOT_RUN", "ui": "SKIPPED-FLAKY", "reason": "Run button not found"}, indent=2) + "\n",
                    encoding="utf-8",
                )
                raise GateError("Android core was not run: Pydroid Run button was not found")
            time.sleep(2)
            dump_ui(adb_path, args.evidence_dir / "ui-after-run.xml")
            deadline = time.time() + 90
            core = None
            while time.time() < deadline:
                result = adb(adb_path, ["shell", f"cat {shlex.quote(sentinel)}"], check=False)
                if result.returncode == 0 and result.stdout.strip():
                    core = json.loads(result.stdout)
                    break
                time.sleep(2)
            if not core or core.get("status") != "PASS":
                (args.evidence_dir / "android-result.json").write_text(
                    json.dumps(
                        {
                            "core": "FAIL",
                            "ui": "PASS",
                            "reason": "Run control was activated but no PASS sentinel was produced",
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                raise GateError("Android core probe did not produce a PASS sentinel")
            (args.evidence_dir / "android-result.json").write_text(
                json.dumps({"core": "PASS", "ui": "SKIPPED", "core_result": core}, indent=2) + "\n",
                encoding="utf-8",
            )
            print(json.dumps({"artifact": identity, "core": "PASS", "ui": "SKIPPED"}, indent=2))
    finally:
        subprocess.run([str(adb_path), "logcat", "-d", "-v", "time"], stdout=(args.evidence_dir / "logcat.txt").open("w"), stderr=subprocess.STDOUT, check=False)
        emulator_process.terminate()
        try:
            emulator_process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            emulator_process.kill()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GateError as exc:
        print(f"ANDROID GATE BLOCKED/FAILED: {exc}", file=os.sys.stderr)
        raise SystemExit(2)
