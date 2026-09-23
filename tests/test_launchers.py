from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_FILES = (
    "bootstrap.py",
    "Tumblr Scraper - Android.py",
    "Tumblr Scraper - Linux.desktop",
    "Tumblr-Scraper-Linux.sh",
    "Tumblr Scraper - macOS.command",
    "Tumblr Scraper - Windows.bat",
    "tumblr-scraper",
    "main.py",
    "graph_projection.py",
    "global.css",
    "network-policy.json",
    "context-policy.json",
)


@unittest.skipUnless(os.name == "posix", "POSIX launcher tests run on Linux/macOS; Windows uses launcher contract tests")
class LauncherPortabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="launcher acceptance ")
        self.root = Path(self.temp.name) / "Tumblr Scraper Beta Test"
        self.root.mkdir(parents=True)
        for name in LAUNCHER_FILES:
            shutil.copy2(REPOSITORY_ROOT / name, self.root / name)
        shutil.copytree(REPOSITORY_ROOT / "src", self.root / "src")
        shutil.copytree(REPOSITORY_ROOT / "assets", self.root / "assets")
        shutil.copytree(REPOSITORY_ROOT / "bridge", self.root / "bridge")
        self.unrelated_cwd = Path(self.temp.name) / "unrelated cwd"
        self.unrelated_cwd.mkdir()
        self.environment = os.environ.copy()
        self.environment["BROWSER"] = "true"
        self.environment["TUMBLR_SCRAPER_NO_BROWSER"] = "1"
        self.environment["PYTHONDONTWRITEBYTECODE"] = "1"
        runtime_python = self.root / ".runtime" / "venv" / "bin" / "python"
        runtime_python.parent.mkdir(parents=True)
        runtime_python.symlink_to(Path(sys.executable).resolve())
        (self.root / ".runtime" / "bootstrap-state.json").write_text(
            '{"bootstrap_schema": 1, "requirements": ["tumblr-backup==1.0.7", "urllib3>=2.2.2,<2.6"]}\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_launcher(self, command: list[str], *, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=self.unrelated_cwd,
            input="\n",
            text=True,
            capture_output=True,
            env=self.environment,
            timeout=timeout,
            check=False,
        )

    def test_python_authority_uses_file_root_from_unrelated_cwd(self) -> None:
        probe = (
            "import main; "
            "print(main.PROJECT_ROOT); "
            "print(main.NETWORK_POLICY_FILE); "
            "print(main.SOURCE_ASSET_DIR)"
        )
        result = subprocess.run(
            ["python3", "-c", probe],
            cwd=self.unrelated_cwd,
            env={**self.environment, "PYTHONPATH": str(self.root)},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        resolved_root = self.root.resolve()
        self.assertEqual(result.stdout.splitlines(), [
            str(resolved_root),
            str(resolved_root / "network-policy.json"),
            str(resolved_root / "assets"),
        ])

    def test_android_python_launcher_starts_from_unrelated_cwd(self) -> None:
        result = self.run_launcher(["python3", str(self.root / "Tumblr Scraper - Android.py")])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Tumblr-Scraper is ready.", result.stdout)
        self.assertTrue((self.root / "Archive" / "default" / "App" / "feed.html").is_file())
        self.assertFalse((self.unrelated_cwd / "Archive").exists())

    def test_linux_wrapper_starts_from_unrelated_cwd(self) -> None:
        result = self.run_launcher([str(self.root / "Tumblr-Scraper-Linux.sh")])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Tumblr-Scraper is ready.", result.stdout)
        self.assertTrue((self.root / "Archive" / "default" / "App" / "feed.html").is_file())

    def test_macos_wrapper_starts_from_unrelated_cwd(self) -> None:
        result = self.run_launcher([str(self.root / "Tumblr Scraper - macOS.command")])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Tumblr-Scraper is ready.", result.stdout)
        self.assertTrue((self.root / "Archive" / "default" / "App" / "feed.html").is_file())

    def test_cli_wrapper_uses_its_own_main_file(self) -> None:
        result = subprocess.run(
            [str(self.root / "tumblr-scraper"), "--help"],
            cwd=self.unrelated_cwd,
            text=True,
            capture_output=True,
            env=self.environment,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: tumblr-scraper", result.stdout)

    def test_desktop_entry_bootstrap_uses_percent_k_as_shell_argument(self) -> None:
        desktop = self.root / "Tumblr Scraper - Linux.desktop"
        text = desktop.read_text(encoding="utf-8")
        self.assertFalse(text.startswith("#!"))
        self.assertIn("sh %k", text)
        self.assertIn("Tumblr-Scraper-Linux.sh", text)
        self.assertNotIn("Tumblr Scraper - Android.py", text)

        command = 'exec /bin/sh "$(dirname -- "$1")/Tumblr-Scraper-Linux.sh"'
        result = subprocess.run(
            ["/bin/sh", "-c", command, "desktop-bootstrap", str(desktop)],
            cwd=self.unrelated_cwd,
            input="\n",
            text=True,
            capture_output=True,
            env=self.environment,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Tumblr-Scraper is ready.", result.stdout)

    def test_cli_without_arguments_prints_friendly_usage(self) -> None:
        result = subprocess.run(
            [str(self.root / "tumblr-scraper")],
            cwd=self.unrelated_cwd,
            text=True,
            capture_output=True,
            env=self.environment,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Tumblr Scraper CLI", result.stderr)
        self.assertIn("./tumblr-scraper BLOG", result.stderr)


if __name__ == "__main__":
    unittest.main()
