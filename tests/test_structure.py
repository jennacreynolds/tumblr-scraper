from __future__ import annotations

import ast
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_release_builder():
    path = ROOT / "tools" / "build_release_artifact.py"
    spec = spec_from_file_location("release_builder_for_test", path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StructureTests(unittest.TestCase):
    def test_beginner_source_map_and_documentation_exist(self) -> None:
        self.assertTrue((ROOT / "DEVELOPMENT.md").is_file())
        self.assertTrue((ROOT / "src" / "tumblr_scraper" / "README.md").is_file())
        for name in ("ARCHITECTURE.md", "KNOWN_ISSUES.md", "RELEASE_GATE.md", "THIRD_PARTY.md"):
            self.assertTrue((ROOT / "docs" / name).is_file())

    def test_main_compatibility_facade_is_single_module_authority(self) -> None:
        import main
        import tumblr_scraper.main as package_main

        self.assertIs(main, package_main)
        self.assertIs(main.__dict__, package_main.__dict__)

    def test_paths_keep_bundle_resource_and_data_roots_distinct_concepts(self) -> None:
        from tumblr_scraper import paths

        self.assertEqual(paths.ARCHIVES_ROOT, paths.DATA_ROOT / "Archive")
        self.assertEqual(paths.ARCHIVE_ROOT, paths.ARCHIVES_ROOT / paths.ACTIVE_ARCHIVE_NAME)
        self.assertEqual(paths.ACTIVE_ARCHIVE_NAME, "default")
        self.assertEqual(paths.CONTENT_ROOT, paths.ARCHIVE_ROOT / "Content")
        self.assertEqual(paths.NETWORK_ROOT, paths.ARCHIVE_ROOT / "Network")
        self.assertEqual(paths.NEIGHBORHOODS_ROOT, paths.NETWORK_ROOT / "Neighborhoods")
        self.assertEqual(paths.APP_ROOT, paths.ARCHIVE_ROOT / "App")
        self.assertEqual(paths.ASSET_DIR, paths.RESOURCE_ROOT / "assets")
        self.assertEqual(paths.RUNTIME_DIR, paths.DATA_ROOT / ".runtime")

    def test_disposable_archive_declares_source_boundary(self) -> None:
        boundary = ROOT / "docs" / "ARCHIVE_BOUNDARY.md"
        archive_format = ROOT / "docs" / "ARCHIVE_FORMAT.md"
        self.assertTrue(boundary.is_file())
        self.assertTrue(archive_format.is_file())
        text = boundary.read_text(encoding="utf-8")
        self.assertIn("rm -rf Archive/", text)
        self.assertIn("part of the application source", text)
        self.assertIn("Archive/<name>/App/", text)
        self.assertFalse((ROOT / "Archive" / "AGENTS.md").exists())
        tracked_archive = subprocess.run(
            ["git", "ls-files", "Archive", ".runtime"],
            cwd=ROOT, text=True, capture_output=True, check=True,
        ).stdout.splitlines()
        self.assertEqual([], tracked_archive)

    def test_release_staging_has_application_bundle_without_mutable_data(self) -> None:
        builder = load_release_builder()
        with tempfile.TemporaryDirectory(prefix="release staging ") as temporary:
            staging = Path(temporary) / "Tumblr-Scraper"
            staging.mkdir()
            builder.copy_allowlist(staging)
            self.assertTrue((staging / "app" / "bootstrap.py").is_file())
            self.assertTrue((staging / "app" / "tumblr_scraper" / "main.py").is_file())
            self.assertTrue((staging / "README.txt").is_file())
            self.assertTrue((staging / "README - START HERE.txt").is_file())
            self.assertTrue((staging / "docs" / "RELEASE_GATE.md").is_file())
            self.assertTrue((staging / "Start Tumblr Scraper - Windows.bat").is_file())
            self.assertTrue((staging / "Start Tumblr Scraper - Linux.desktop").is_file())
            self.assertFalse((staging / ".runtime").exists())
            self.assertFalse((staging / "Backups").exists())
            self.assertFalse((staging / "Neighborhoods").exists())
            self.assertFalse((staging / "Graph").exists())

    def test_firefox_integration_stays_at_the_host_boundary(self) -> None:
        firefox = ROOT / "integrations" / "firefox"
        self.assertTrue((firefox / "README.md").is_file())
        self.assertTrue((firefox / "extension" / "README.md").is_file())
        self.assertTrue((firefox / "native-host" / "README.md").is_file())
        self.assertTrue((firefox / "extension" / "manifest.json").is_file())
        self.assertTrue((firefox / "extension" / "background.js").is_file())
        self.assertTrue((firefox / "native-host" / "native_host.py").is_file())
        self.assertTrue((firefox / "native-host" / "install-linux.py").is_file())
        self.assertTrue((firefox / "session" / "firefox_session.py").is_file())
        for path in firefox.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("tumblr_scraper.main", text)
            self.assertNotIn("import main", text)
            self.assertNotIn("tumblr_backup", text)
        for path in (ROOT / "src" / "tumblr_scraper").rglob("*.py"):
            self.assertNotIn("integrations", path.read_text(encoding="utf-8"))

    def test_lower_layers_do_not_import_application_edges(self) -> None:
        package = ROOT / "src" / "tumblr_scraper"
        forbidden = {
            "paths.py": {"main", "cli", "hosts", "presentation", "application"},
            "models.py": {"main", "cli", "hosts", "presentation", "application"},
            "config.py": {"main", "cli", "hosts", "presentation", "application"},
            "network.py": {"main", "cli", "hosts", "presentation", "application"},
            "archive.py": {"main", "cli", "hosts", "presentation", "application"},
            "profile.py": {"main", "cli", "hosts", "presentation", "application"},
            "relationships.py": {"main", "cli", "hosts", "presentation", "application"},
            "presentation.py": {"main", "cli", "hosts", "application"},
        }
        for filename, blocked in forbidden.items():
            path = package / filename
            if not path.is_file():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            self.assertTrue(blocked.isdisjoint(imported), f"{filename} imports a forbidden layer")


if __name__ == "__main__":
    unittest.main()
