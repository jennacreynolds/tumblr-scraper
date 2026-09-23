from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tumblr_scraper import archive_manager


class ArchiveManagerTests(unittest.TestCase):
    def test_named_bundles_are_independent_and_renameable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Archive"
            source = root / "default"
            source.mkdir(parents=True)
            (source / "Content").mkdir()
            (source / "Network").mkdir()
            (source / "App").mkdir()
            (source / "Content" / "one.json").write_text("one", encoding="utf-8")
            saved = archive_manager.save_archive(source, root, "survey-test")
            (source / "Content" / "one.json").write_text("changed", encoding="utf-8")
            self.assertEqual((saved.path / "Content" / "one.json").read_text(), "one")
            renamed = archive_manager.rename_archive(root, "survey-test", "renamed")
            self.assertTrue(renamed.path.is_dir())
            loaded = archive_manager.load_archive(root, "renamed")
            self.assertEqual(loaded.name, "renamed")
            self.assertEqual(archive_manager.active_archive_name(root), "renamed")

    def test_renaming_default_updates_implicit_active_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Archive"
            archive_manager.create_archive(root, "default")
            renamed = archive_manager.rename_archive(root, "default", "new-name")
            self.assertEqual(renamed.name, "new-name")
            self.assertEqual(archive_manager.active_archive_name(root), "new-name")

    def test_invalid_names_and_symlink_bundle_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Archive"
            for name in ("../escape", "/tmp/escape", "x/y", "bad\x00name"):
                with self.assertRaises(archive_manager.ArchiveBundleError):
                    archive_manager.create_archive(root, name)
            target = Path(directory) / "outside"
            target.mkdir()
            root.mkdir()
            (root / "linked").symlink_to(target, target_is_directory=True)
            with self.assertRaises(archive_manager.ArchiveBundleError):
                archive_manager.get_archive(root, "linked")

    def test_corrupt_manifest_is_reported_and_app_is_disposable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Archive"
            bundle = archive_manager.create_archive(root, "default")
            (bundle.path / "Content" / "keep.json").write_text("keep", encoding="utf-8")
            (bundle.path / "archive.json").write_text("{broken\n", encoding="utf-8")
            with self.assertRaises(archive_manager.ArchiveBundleError):
                archive_manager.load_archive(root, "default")
            (bundle.path / "archive.json").unlink()
            for path in (bundle.path / "App").iterdir():
                path.unlink()
            (bundle.path / "App").rmdir()
            self.assertEqual((bundle.path / "Content" / "keep.json").read_text(), "keep")
            self.assertTrue((bundle.path / "Network").is_dir())


if __name__ == "__main__":
    unittest.main()
