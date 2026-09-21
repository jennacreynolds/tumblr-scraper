from __future__ import annotations

import json
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import bootstrap


class BootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="bootstrap test ")
        self.root = Path(self.temp.name) / "Tumblr Scraper Beta Test"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def seed_runtime(self) -> Path:
        runtime = bootstrap.runtime_python(self.root)
        runtime.parent.mkdir(parents=True)
        if os.name == "nt":
            shutil.copy2(sys.executable, runtime)
        else:
            runtime.symlink_to(Path(sys.executable).resolve())
        stamp = self.root / ".runtime" / "bootstrap-state.json"
        stamp.write_text(json.dumps(bootstrap._expected_stamp()) + "\n", encoding="utf-8")
        return runtime

    def test_valid_runtime_is_reused(self) -> None:
        runtime = self.seed_runtime()
        self.assertTrue(bootstrap.runtime_is_valid(self.root))
        with mock.patch.object(bootstrap, "_create_venv") as create:
            self.assertEqual(bootstrap.ensure_desktop_runtime(self.root), runtime)
        create.assert_not_called()

    def test_stale_stamp_invalidates_runtime(self) -> None:
        runtime = self.seed_runtime()
        stamp = self.root / ".runtime" / "bootstrap-state.json"
        stamp.write_text('{"bootstrap_schema": 0}\n', encoding="utf-8")
        self.assertFalse(bootstrap.runtime_is_valid(self.root))
        self.assertTrue(runtime.is_file())

    def test_partial_runtime_is_removed_before_repair(self) -> None:
        partial = self.root / ".runtime" / "venv"
        partial.mkdir(parents=True)
        (partial / "partial-marker").write_text("interrupted", encoding="utf-8")
        repaired = self.root / ".runtime" / "new-venv" / "bin" / "python"
        repaired.parent.mkdir(parents=True)
        repaired.write_text("python", encoding="utf-8")
        with (
            mock.patch.object(bootstrap, "_create_venv", return_value=repaired) as create,
            mock.patch.object(bootstrap, "_install_dependencies") as install,
            mock.patch.object(bootstrap, "_runtime_imports_are_valid", return_value=True),
        ):
            result = bootstrap.ensure_desktop_runtime(self.root)
        self.assertEqual(result, repaired)
        self.assertFalse(partial.exists())
        create.assert_called_once_with(self.root)
        install.assert_called_once_with(self.root, repaired)

    def test_dependency_install_targets_private_runtime(self) -> None:
        runtime = bootstrap.runtime_python(self.root)
        runtime.parent.mkdir(parents=True)
        runtime.write_text("python", encoding="utf-8")
        completed = subprocess.CompletedProcess([], 0)
        with mock.patch.object(bootstrap.subprocess, "run", return_value=completed) as run:
            bootstrap._install_dependencies(self.root, runtime)
        self.assertEqual(run.call_count, 2)
        install_command = run.call_args_list[1].args[0]
        self.assertEqual(install_command[0], str(runtime))
        self.assertNotEqual(install_command[0], sys.executable)

    def test_reexec_preserves_cli_arguments_and_uses_main(self) -> None:
        runtime = Path(sys.executable)
        with (
            mock.patch.object(bootstrap, "ensure_desktop_runtime", return_value=runtime),
            mock.patch.object(bootstrap.os, "execve", side_effect=AssertionError("captured")) as execve,
        ):
            with self.assertRaises(AssertionError):
                bootstrap.launch("cli", ["BLOG", "12", "--context", "explore"])
        command = execve.call_args.args
        self.assertEqual(command[0], str(runtime))
        self.assertEqual(command[1][1:], [str(bootstrap.MAIN_PATH), "BLOG", "12", "--context", "explore"])
        self.assertEqual(command[2][bootstrap.BOOTSTRAP_ACTIVE], "1")

    def test_browser_rejects_recursion(self) -> None:
        with mock.patch.dict(os.environ, {bootstrap.BOOTSTRAP_ACTIVE: "1"}, clear=False):
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap.launch("browser")

    def test_browser_host_delegates_once_then_runs_under_marker(self) -> None:
        source = Path(__file__).resolve().parents[1] / "Tumblr Scraper - Android.py"
        spec = importlib.util.spec_from_file_location("tumblr_android_host_test", source)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(module.bootstrap, "launch", return_value=7) as launch,
        ):
            self.assertEqual(module.main_entry(), 7)
        launch.assert_called_once_with("browser", pydroid=False)

        with (
            mock.patch.dict(os.environ, {bootstrap.BOOTSTRAP_ACTIVE: "1"}, clear=True),
            mock.patch.object(module, "run_browser_first", return_value=3) as run_browser,
            mock.patch.object(module.main, "CrawlerApplication", return_value=object()),
        ):
            self.assertEqual(module.main_entry(), 3)
        run_browser.assert_called_once()


if __name__ == "__main__":
    unittest.main()
