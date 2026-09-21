#Pydroid run terminal
"""Interactive launcher for the Tumblr archive in Pydroid 3 and desktop wrappers."""

from __future__ import annotations

import argparse
import subprocess
import sys
import webbrowser
from pathlib import Path

# Resolve the project before importing sibling modules. Pydroid and file
# managers may choose an unrelated current working directory.
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main
from bridge import LocalControlBridge, LocalControlRequestHandler


ROOT = PROJECT_ROOT
MAIN = PROJECT_ROOT / "main.py"
WHEELS = PROJECT_ROOT / "wheels"


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

    packages = ["tumblr-backup==1.0.7", "urllib3>=2.2.2,<2.6"]
    if WHEELS.is_dir() and any(WHEELS.iterdir()):
        print("\nFirst-time setup required.")
        print("Installing required Tumblr archive components from bundled wheels...")
        command = [sys.executable, "-m", "pip", "install", "--no-index", "--find-links", str(WHEELS), *packages]
    else:
        print("\nFirst run: installing required Tumblr archive components...")
        print("This release needs internet access once to download pinned Python packages.")
        command = [sys.executable, "-m", "pip", "install", *packages]
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        print("Required Python packages could not be installed.")
        print("Check Python/pip and internet access, then run the launcher again.")
        return False
    return True


# Compatibility name for callers that only used the old server's quiet logger.
ArchiveRequestHandler = LocalControlRequestHandler


def create_live_bridge(application: main.CrawlerApplication, archive: Path | None = None) -> LocalControlBridge:
    archive = archive or main.BACKUPS_DIR
    archive_root = archive.resolve()
    archive_prefix = archive_root.relative_to(ROOT).as_posix()
    return LocalControlBridge(
        base_dir=ROOT,
        archive_root=archive_root,
        neighborhood_root=main.NEIGHBORHOODS_DIR,
        global_css=main.GLOBAL_CSS,
        status_provider=application.snapshot,
        control_handler=lambda name, value: main.apply_runtime_control(name, value, source="browser"),
        start_handler=application.start,
        stop_handler=application.stop,
        prepare_entrypoint=main.prepare_live_archive_entrypoint,
        archive_url_prefix=archive_prefix,
    )


def open_archive(archive: Path) -> None:
    application = main.CrawlerApplication()
    bridge = create_live_bridge(application, archive)
    url = bridge.start()
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
        bridge.close()


def open_static_archive(archive: Path) -> None:
    """Open the generated archive directly; no server is required for reading."""
    index = (archive / "index.html").resolve()
    if not index.is_file():
        print(f"Archive index not found: {index}")
        return
    url = index.as_uri()
    opened = webbrowser.open(url, new=2)
    if not opened:
        try:
            import androidhelper
            androidhelper.Android().startActivity("android.intent.action.VIEW", url)
        except Exception:
            print(f"Open this file in your browser:\n{index}")


def run_scraper(
    username: str,
    max_posts: int,
    full_res: bool,
    profile_id: str,
    context_mode: str = "none",
    context_depth: int | None = None,
    focus: str | None = None,
) -> int:
    command = [sys.executable, str(MAIN), username, str(max_posts), "--compact-terminal", "--profile", profile_id]
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
    open_static_archive(archive)
    return 0


def run_live_scraper(
    username: str,
    max_posts: int,
    full_res: bool,
    profile_id: str,
    context_mode: str = "none",
    context_depth: int | None = None,
    focus: str | None = None,
) -> int:
    application = main.CrawlerApplication()
    request = main.build_crawl_request(
        username,
        max_posts=max_posts,
        context=context_mode,
        context_depth=context_depth,
        focus=focus,
        profile_id=profile_id,
        full_res=full_res,
    )
    return run_browser_first(application, initial_request=request)


def terminal_request() -> main.CrawlRequest:
    username = prompt_username()
    max_posts = prompt_max_posts()
    profile_id = prompt_network_profile()
    full_res = prompt_full_res()
    context_mode, context_depth = prompt_context()
    focus = prompt_focus()
    return main.build_crawl_request(
        username,
        max_posts=max_posts,
        context=context_mode,
        context_depth=context_depth,
        focus=focus,
        profile_id=profile_id,
        full_res=full_res,
    )


def stop_application_safely(application: main.CrawlerApplication, timeout: float = 30.0) -> bool:
    """Request the shared safe stop and refuse to abandon a live worker."""
    if application.state not in {"starting", "running", "stopping", "finalizing"}:
        return True
    try:
        application.stop()
    except main.PolicyError:
        pass
    worker = application.worker
    if worker is not None:
        worker.join(timeout=timeout)
    if worker is not None and worker.is_alive():
        print("The crawl is still stopping; leaving the terminal open for safe completion.")
        return False
    return True


def run_browser_first(
    application: main.CrawlerApplication,
    *,
    initial_request: main.CrawlRequest | None = None,
) -> int:
    main.set_host_capabilities(compact_terminal=True, keyboard_controls=False)
    try:
        bridge = create_live_bridge(application)
        url = bridge.start()
    except OSError as exc:
        print(f"Live archive controls are unavailable ({exc}); use terminal fallback.")
        if initial_request is not None:
            return run_scraper(
                initial_request.target,
                initial_request.max_posts,
                initial_request.full_res,
                str(initial_request.profile_id),
                initial_request.context,
                initial_request.context_depth,
                initial_request.focus,
            )
        request = terminal_request()
        application.start_request(request)
        application.worker.join()
        return 0 if application.state == "complete" else 1
    try:
        opened = webbrowser.open(url, new=2)
        print(
            "Tumblr-Scraper is ready.\n\n"
            f"Browser:\n{url}\n\n"
            "Use the browser to start/control a crawl.\n"
            "Terminal fallback: type T.\n"
            "Ctrl+C: stop safely"
        )
        if not opened:
            try:
                import androidhelper
                androidhelper.Android().startActivity("android.intent.action.VIEW", url)
            except Exception:
                print("Open the Browser address above manually.")
        if initial_request is not None:
            application.start_request(initial_request)
        try:
            while True:
                try:
                    command = input().strip().lower()
                except EOFError:
                    if stop_application_safely(application):
                        break
                    continue
                if command == "t":
                    if application.state in {"starting", "running", "stopping", "finalizing"}:
                        print("A crawl is already active; use the browser or Ctrl+C.")
                        continue
                    try:
                        request = terminal_request()
                        application.start_request(request)
                        print("Terminal crawl started; browser controls remain available.")
                    except (argparse.ArgumentTypeError, main.PolicyError) as exc:
                        print(f"Terminal fallback could not start: {exc}")
                elif command in {"q", "quit", "exit"}:
                    if stop_application_safely(application):
                        break
        except KeyboardInterrupt:
            stop_application_safely(application)
            return 130
        return 0 if application.state in {"idle", "complete"} else 1
    finally:
        bridge.close()


def main_entry() -> int:
    print("TUMBLR SCRAPER\n")
    if not install_local_dependencies():
        return 1
    return run_browser_first(main.CrawlerApplication())


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
