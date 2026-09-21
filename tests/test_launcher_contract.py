from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class LauncherBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="launcher contract ")
        self.base = Path(self.temp.name) / "Tumblr Scraper Candidate With Spaces"
        self.base.mkdir()
        self.fakebin = self.base / "fake interpreter bin"
        self.fakebin.mkdir()
        self.unrelated = Path(self.temp.name) / "unrelated cwd"
        self.unrelated.mkdir()
        self.record = self.base / "invocation.txt"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _posix_python3(self) -> None:
        fake = self.fakebin / "python3"
        fake.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$PWD\" > {shlex.quote(str(self.record))}\n"
            f"printf '%s\\n' \"$@\" >> {shlex.quote(str(self.record))}\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)

    @unittest.skipUnless(os.name == "posix", "POSIX wrapper behavior runs on POSIX hosts")
    def test_linux_and_macos_wrappers_find_project_from_unrelated_cwd(self) -> None:
        self._posix_python3()
        environment = os.environ.copy()
        environment["PATH"] = f"{self.fakebin}{os.pathsep}{environment['PATH']}"
        for launcher in ("Tumblr-Scraper-Linux.sh", "Tumblr Scraper - macOS.command"):
            with self.subTest(launcher=launcher):
                self.record.unlink(missing_ok=True)
                result = subprocess.run(
                    ["/bin/sh", str(ROOT / launcher)],
                    cwd=self.unrelated,
                    input="\n",
                    text=True,
                    capture_output=True,
                    env=environment,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                lines = self.record.read_text(encoding="utf-8").splitlines()
                self.assertEqual(lines[0], str(ROOT))
                self.assertEqual(lines[1:], [str(ROOT / "bootstrap.py"), "browser"])

    @unittest.skipUnless(os.name == "nt", "Windows batch behavior runs on Windows hosts")
    def test_windows_launcher_uses_each_available_python_convention(self) -> None:
        shutil.copy2(ROOT / "Tumblr Scraper - Windows.bat", self.base / "Tumblr Scraper - Windows.bat")
        system_root = Path(os.environ["SystemRoot"])
        fake_where = self.fakebin / "where.cmd"
        fake_where.write_text(
            "@echo off\n"
            "if /I \"%1\"==\"py\" if /I \"%ONLY\"==\"py\" exit /b 0\n"
            "if /I \"%1\"==\"python\" if /I \"%ONLY\"==\"python\" exit /b 0\n"
            "if /I \"%1\"==\"python3\" if /I \"%ONLY\"==\"python3\" exit /b 0\n"
            "exit /b 1\n",
            encoding="utf-8",
        )
        for convention in ("py", "python", "python3"):
            runner = self.fakebin / f"{convention}.cmd"
            runner.write_text(
                "@echo off\n"
                f"echo {convention}>\"%RECORD%\"\n"
                "echo %*>\"%RECORD%\"\n",
                encoding="utf-8",
            )
        environment = os.environ.copy()
        environment["PATH"] = f"{self.fakebin};{system_root / 'System32'}"
        environment["RECORD"] = str(self.record)
        for convention in ("py", "python", "python3"):
            with self.subTest(convention=convention):
                self.record.unlink(missing_ok=True)
                environment["ONLY"] = convention
                result = subprocess.run(
                    ["cmd.exe", "/d", "/c", str(self.base / "Tumblr Scraper - Windows.bat")],
                    cwd=self.unrelated,
                    input="\n",
                    text=True,
                    capture_output=True,
                    env=environment,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                recorded = self.record.read_text(encoding="utf-8")
                self.assertIn(convention, recorded)
                self.assertIn("bootstrap.py", recorded)
                self.assertIn("browser", recorded)

        environment["ONLY"] = "none"
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", str(self.base / "Tumblr Scraper - Windows.bat")],
            cwd=self.unrelated,
            input="\n",
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Python 3 was not found", result.stdout)


if __name__ == "__main__":
    unittest.main()
