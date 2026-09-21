#!/usr/bin/env python3
"""Run the non-UI Android/Pydroid core probe inside the extracted project."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import time


# The gate copies this probe directly beside the extracted project's main.py.
# Keep the probe self-locating when Pydroid opens it from shared storage.
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_SENTINEL = Path("/sdcard/Download/Tumblr Scraper Beta Gate/android-core-result.json")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import bootstrap
import main


def load_browser_host():
    path = PROJECT_ROOT / "Tumblr Scraper - Android.py"
    spec = importlib.util.spec_from_file_location("tumblr_android_gate_host", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load browser host: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main_entry(sentinel: Path) -> int:
    runtime = bootstrap.ensure_pydroid_runtime(PROJECT_ROOT)
    if Path(runtime).resolve() != Path(sys.executable).resolve():
        raise RuntimeError("Pydroid core probe selected a different interpreter")
    import tumblr_backup.main  # noqa: F401
    import urllib3

    host = load_browser_host()
    application = main.CrawlerApplication()
    bridge = host.create_live_bridge(application)
    url = bridge.start()
    bridge.close()
    result = {
        "status": "PASS",
        "project_root": str(PROJECT_ROOT),
        "script_root_matches": main.PROJECT_ROOT == PROJECT_ROOT,
        "policy_exists": main.NETWORK_POLICY_FILE.is_file() and main.CONTEXT_POLICY_FILE.is_file(),
        "assets_exists": main.SOURCE_ASSET_DIR.is_dir(),
        "bridge_exists": (PROJECT_ROOT / "bridge" / "local_http.py").is_file(),
        "tumblr_backup": "1.0.7",
        "urllib3": urllib3.__version__,
        "bridge_url": url,
        "timestamp": time.time(),
    }
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    # Pydroid's editor Run button does not provide custom argv values. Keep
    # the explicit argument for direct probes, but make the editor path
    # self-contained and write beside the probe when no argument is supplied.
    if len(sys.argv) == 2:
        sentinel_path = Path(sys.argv[1])
    elif len(sys.argv) == 1:
        sentinel_path = DEFAULT_SENTINEL
    else:
        raise SystemExit("usage: android_gate_probe.py [SENTINEL_PATH]")
    try:
        raise SystemExit(main_entry(sentinel_path))
    except Exception as exc:
        try:
            sentinel_path.parent.mkdir(parents=True, exist_ok=True)
            sentinel_path.write_text(
                json.dumps({"status": "FAIL", "error": repr(exc), "project_root": str(PROJECT_ROOT)}, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass
        print(f"ANDROID CORE FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
