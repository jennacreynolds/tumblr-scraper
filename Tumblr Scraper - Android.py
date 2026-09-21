#Pydroid run terminal
"""Interactive launcher for the Tumblr archive in Pydroid 3 and desktop wrappers."""

from __future__ import annotations

import argparse
import functools
import subprocess
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import main


ROOT = Path(__file__).resolve().parent
MAIN = ROOT / "main.py"
WHEELS = ROOT / "wheels"


def prompt_username() -> str:
    while True:
        value = input("Blog name:\n> ").strip()
        if not value:
            print("Please enter a Tumblr blog name.")
            continue
        try:
            return main.canonical_username(value)
        except argparse.ArgumentTypeError as exc:
            print(f"Please enter a valid Tumblr blog name: {exc}.")


def prompt_action() -> str:
    archive_index = main.BACKUPS_DIR / "index.html"
    print(
        "Choose an action:\n\n"
        "1. Open existing archive\n"
        "2. Run a new crawl\n"
    )
    while True:
        value = input("Choice [1]:\n> ").strip() or "1"
        if value == "1":
            if archive_index.is_file():
                return "open"
            print(f"No generated archive found at {archive_index}.")
            print("Choose 2 to run a crawl first.")
            continue
        if value == "2":
            return "crawl"
        print("Please choose 1 or 2.")


def prompt_max_posts() -> int:
    while True:
        value = input("\nMaximum posts [300]:\n> ").strip()
        if not value:
            return 300
        try:
            return main.nonnegative_int(value)
        except argparse.ArgumentTypeError:
            print("Please enter a number, or leave blank for 300.")


def prompt_network_profile() -> str:
    policy = main.load_network_policy()
    profiles = policy["profiles"]
    default_id = policy["default_profile"]
    default_index = next(index for index, profile in enumerate(profiles, start=1) if profile["id"] == default_id)

    print(
        "\nNetwork aggression:\n\n"
        "More polite modes use less request pressure and may take longer.\n"
        "More aggressive modes may finish sooner but are more likely to be\n"
        "throttled or temporarily blocked by Tumblr.\n"
    )
    for index, profile in enumerate(profiles, start=1):
        print(f"{index}. {profile['label']}\n   {profile['description']}\n")

    while True:
        value = input(f"Choice [{default_index}]:\n> ").strip()
        if not value:
            return default_id
        try:
            index = int(value)
        except ValueError:
            index = 0
        if 1 <= index <= len(profiles):
            return profiles[index - 1]["id"]
        print("Please choose one of the displayed numbers.")


def prompt_full_res() -> bool:
    while True:
        value = input("\nDownload full-resolution images? [y/N]:\n> ").strip().lower()
        if value in ("", "n", "no"):
            return False
        if value in ("y", "yes"):
            return True
        print("Please enter y or n, or leave blank for smaller images.")


def prompt_context() -> tuple[str, int | None]:
    print(
        "\nSurrounding public context:\n\n"
        "1. This blog only\n"
        "2. This blog + nearby context\n"
        "3. Explore its blog neighborhood\n"
    )
    while True:
        value = input("Choice [3]:\n> ").strip() or "3"
        if value == "1":
            return "none", None
        if value == "2":
            return "nearby", None
        if value == "3":
            while True:
                depth = input("How far outward? [2]:\n> ").strip() or "2"
                try:
                    parsed = main.nonnegative_int(depth)
                except argparse.ArgumentTypeError:
                    print("Please enter a non-negative depth.")
                    continue
                if parsed < 1:
                    print("Explore depth must be at least 1.")
                    continue
                return "explore", parsed
        print("Please choose one of the displayed numbers.")


def prompt_focus() -> str:
    policy = main.load_context_policy()
    default = policy.get("default_focus", "balanced")
    labels = {
        "deep": "Deep - concentrate mostly on the target",
        "balanced": "Balanced - steadily reconstruct the neighborhood",
        "wide": "Wide - spread more effort through the neighborhood",
        "neighbors": "Neighbors - acquire outward context; target posts may be zero",
    }
    print("\nWhere should this crawl spend its effort?\n")
    choices = list(policy.get("focus_profiles", {default: {}}))
    if "neighbors" not in choices:
        choices.append("neighbors")
    for index, focus in enumerate(choices, start=1):
        print(f"{index}. {labels.get(focus, focus.title())}")
    default_index = choices.index(default) + 1
    while True:
        value = input(f"Choice [{default_index}]:\n> ").strip()
        if not value:
            return default
        try:
            index = int(value)
        except ValueError:
            index = 0
        if 1 <= index <= len(choices):
            return choices[index - 1]
        print("Please choose one of the displayed numbers.")


def install_local_dependencies() -> bool:
    try:
        import tumblr_backup.main  # noqa: F401
        return True
    except ImportError:
        pass

    print("\nFirst-time setup required.")
    print("Installing required Python packages from this folder...")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(WHEELS),
            "tumblr-backup==1.0.7",
            "urllib3>=2.2.2,<2.6",
        ],
        check=False,
    )
    if result.returncode != 0:
        print("Required Python packages could not be installed.")
        return False
    return True


class ArchiveRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, archive_prefix: str, archive_root: Path, **kwargs):
        self.archive_prefix = archive_prefix.rstrip("/")
        self.archive_root = archive_root.resolve()
        super().__init__(*args, **kwargs)

    def translate_path(self, path: str) -> str:
        request_path = unquote(urlsplit(path).path)
        if request_path == "/global.css":
            return str(ROOT / "global.css")
        if (request_path == self.archive_prefix or request_path.startswith(self.archive_prefix + "/")
                or request_path == "/Neighborhoods" or request_path.startswith("/Neighborhoods/")):
            translated = Path(super().translate_path(path)).resolve()
            allowed = [self.archive_root, (ROOT / "Neighborhoods").resolve()]
            if any(translated == root or root in translated.parents for root in allowed):
                return str(translated)
        return str(ROOT / "__tumblr_scraper_not_found__")

    def log_message(self, _format: str, *_args: object) -> None:
        # Routine localhost requests are normal browser activity, not crawler output.
        return

    def log_error(self, format: str, *args: object) -> None:
        print(f"Archive server error: {format % args}", file=sys.stderr)


def open_archive(archive: Path) -> None:
    archive_root = archive.resolve()
    archive_prefix = "/" + archive_root.relative_to(ROOT).as_posix()
    handler = functools.partial(
        ArchiveRequestHandler,
        directory=str(ROOT),
        archive_prefix=archive_prefix,
        archive_root=archive_root,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}{archive_prefix}/index.html"

    try:
        opened = webbrowser.open(url, new=2)
        if not opened:
            try:
                import androidhelper
                androidhelper.Android().startActivity("android.intent.action.VIEW", url)
            except Exception:
                print(f"Open this address in your browser:\n{url}")
        print("Leave this screen running while you browse.")
        try:
            input("Return here and press Enter when finished.\n")
        except EOFError:
            pass
    finally:
        server.shutdown()
        server.server_close()


def run_scraper(
    username: str,
    max_posts: int,
    full_res: bool,
    profile_id: str,
    context_mode: str = "none",
    context_depth: int | None = None,
    focus: str | None = None,
) -> int:
    command = [sys.executable, str(MAIN), username, str(max_posts), "--profile", profile_id]
    if full_res:
        command.append("--full-res")
    if context_mode != "none":
        command.extend(["--context", context_mode])
    if context_depth is not None:
        command.extend(["--context-depth", str(context_depth)])
    if focus is not None:
        command.extend(["--focus", focus])
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        print(
            "\nBackup stopped before completion.\n"
            "Anything already saved is still safe.\n"
            "Run this file again to continue."
        )
        return result.returncode

    archive = main.BACKUPS_DIR
    print("\nOpening local archive...")
    open_archive(archive)
    return 0


def main_entry() -> int:
    print("TUMBLR SCRAPER\n")
    action = prompt_action()
    if action == "open":
        print("Opening local archive...")
        open_archive(main.BACKUPS_DIR)
        return 0

    username = prompt_username()
    max_posts = prompt_max_posts()
    try:
        profile_id = prompt_network_profile()
    except main.PolicyError as exc:
        print(f"Network policy could not be loaded: {exc}")
        return 1
    full_res = prompt_full_res()
    context_mode, context_depth = prompt_context()
    try:
        focus = prompt_focus()
    except main.PolicyError as exc:
        print(f"Context policy could not be loaded: {exc}")
        return 1
    print(f"\nChecking Tumblr...\nBacking up {username}...")
    if not install_local_dependencies():
        return 1
    return run_scraper(username, max_posts, full_res, profile_id, context_mode, context_depth, focus)


if __name__ == "__main__":
    exit_code = 1
    try:
        exit_code = main_entry()
    except KeyboardInterrupt:
        print("\nStopped by user. Saved work is safe; run the launcher again to continue.")
        exit_code = 130
    except Exception as exc:
        print(f"\nThe backup launcher failed: {exc}")
        print("Nothing already saved was removed. Run the launcher again to continue.")
    finally:
        try:
            input("\nPress Enter to close this terminal.\n")
        except EOFError:
            pass
    raise SystemExit(exit_code)
