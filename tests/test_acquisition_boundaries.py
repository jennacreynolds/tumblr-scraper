from __future__ import annotations

import ast
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import main  # noqa: F401 - compatibility facade establishes source-tree imports
from tumblr_scraper import archive, network, normalize, profile, relationships


class BoundaryTests(unittest.TestCase):
    def test_network_retrieval_uses_explicit_blog_and_host(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'var tumblr_api_read = {"posts": [{"id": 7}]};'

        result = network.fetch_public_page(
            blog="example",
            blog_host="example.tumblr.com",
            start=0,
            count=50,
            user_agent="test-agent",
            opener=mock.Mock(return_value=Response()),
        )
        self.assertEqual(result["posts"][0]["id"], 7)

    def test_network_module_has_no_application_import(self) -> None:
        tree = ast.parse(Path(network.__file__).read_text(encoding="utf-8"))
        imported = {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        self.assertNotIn("main", imported)

    def test_normalization_matches_compatibility_wrapper_without_io(self) -> None:
        raw = {
            "id": 7,
            "type": "photo",
            "photo-url-1280": "https://media.example/large.jpg",
            "photo-url-500": "https://media.example/medium.jpg",
            "tags": ["kept"],
            "reblogged-from-name": "source-blog",
        }
        feed = {"tumblelog": {"title": "Example"}, "posts-total": 10}
        before = set(Path.cwd().iterdir())
        expected = normalize.normalize_post(
            raw,
            feed,
            10,
            blog="example",
            blog_host="example.tumblr.com",
            full_res=False,
        )
        with mock.patch.object(main, "BLOG", "example"), mock.patch.object(
            main, "BLOG_HOST", "example.tumblr.com"
        ), mock.patch.object(main, "FULL_RES", False):
            actual = main.normalize_post(raw, feed, 10)
        after = set(Path.cwd().iterdir())
        self.assertEqual(expected, actual)
        self.assertEqual(before, after)

    def test_normalization_module_has_no_application_import(self) -> None:
        tree = ast.parse(Path(normalize.__file__).read_text(encoding="utf-8"))
        imported = {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        self.assertNotIn("main", imported)

    def test_profile_parser_is_deterministic_and_tolerates_missing_fields(self) -> None:
        html = (
            "<html><head><title>  A   blog </title>"
            '<meta property="og:description" content=" public words "></head></html>'
        )
        expected = {"title": "A blog", "description": "public words"}
        self.assertEqual(profile.parse_profile_observation(html), expected)
        self.assertEqual(profile.parse_profile_observation(html), expected)
        self.assertEqual(profile.parse_profile_observation("<html><body>partial"), {
            "title": "",
            "description": "",
        })

    def test_profile_parser_has_no_application_import_or_file_effect(self) -> None:
        tree = ast.parse(Path(profile.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("main", imported)
        with tempfile.TemporaryDirectory() as directory:
            before = set(Path(directory).iterdir())
            profile.parse_profile_observation("<title>unchanged</title>")
            self.assertEqual(before, set(Path(directory).iterdir()))

    def test_relationship_evidence_matches_compatibility_wrappers(self) -> None:
        record = {
            "id": 8,
            "post_url": "https://example.tumblr.com/post/8/x",
            "reblogged-from-name": "source-blog",
            "reblogged-from-url": "https://source-blog.tumblr.com/",
            "asking_name": "question-blog",
            "asking_url": "https://www.tumblr.com/blog/view/question-blog/2",
        }
        self.assertEqual(
            relationships.interaction_evidence("example", record),
            main.interaction_evidence("example", record),
        )
        self.assertEqual(
            relationships.blog_from_reference("", "https://www.tumblr.com/blog/view/foo-bar/123"),
            ("foo-bar", "https://www.tumblr.com/blog/view/foo-bar/123"),
        )
        self.assertIsNone(relationships.blog_from_reference("", "https://www.tumblr.com/blog/view/www/123"))

    def test_relationship_module_is_deterministic_and_has_no_application_import(self) -> None:
        tree = ast.parse(Path(relationships.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("main", imported)
        before = relationships.interaction_evidence("owner", {"id": 1})
        after = relationships.interaction_evidence("owner", {"id": 1})
        self.assertEqual(before, after)

    def test_archive_source_round_trip_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            json_dir = Path(directory) / "json"
            path = archive.source_record_path(json_dir, 42)
            record = {"id": 42, "_puppetbackup_source_record": {"id": 42}}
            archive.write_json_atomic(path, record)
            self.assertTrue(archive.source_record_exists(json_dir, 42))
            self.assertEqual(archive.read_json_records(json_dir), [record])
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_source_remains_after_downstream_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = archive.source_record_path(Path(directory) / "json", "questionable")
            record = {"id": "questionable", "raw": "preserve exactly"}
            archive.write_json_atomic(path, record)
            try:
                raise RuntimeError("optional renderer failed")
            except RuntimeError:
                pass
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), record)


if __name__ == "__main__":
    unittest.main()
