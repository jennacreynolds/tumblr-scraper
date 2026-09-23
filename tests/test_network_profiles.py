from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import urlsplit

import main


class NetworkPolicyTests(unittest.TestCase):
    def test_bundled_profiles_and_default(self) -> None:
        policy = main.load_network_policy()
        self.assertEqual(policy["default_profile"], "gentle")
        profiles = {profile["id"]: profile for profile in policy["profiles"]}
        self.assertEqual(profiles["gentle"]["media_workers"], 1)
        self.assertEqual(profiles["normal"]["media_workers"], 2)
        self.assertEqual(profiles["urgent"]["media_workers"], 4)
        self.assertEqual(profiles["gentle"]["feed_delay_seconds"], 2.0)
        self.assertEqual(profiles["normal"]["feed_jitter_seconds"], 0.25)

    def test_network_policy_override_is_read_at_call_time(self) -> None:
        policy = json.loads(main.NETWORK_POLICY_FILE.read_text(encoding="utf-8"))
        policy["default_profile"] = "urgent"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "network-policy.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
            with mock.patch.object(main, "NETWORK_POLICY_FILE", path):
                self.assertEqual(main.load_network_policy()["default_profile"], "urgent")

    def test_context_policy_override_is_read_at_call_time(self) -> None:
        policy = json.loads(main.CONTEXT_POLICY_FILE.read_text(encoding="utf-8"))
        policy["default_focus"] = "wide"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context-policy.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
            with mock.patch.object(main, "CONTEXT_POLICY_FILE", path):
                self.assertEqual(main.load_context_policy()["default_focus"], "wide")

    def test_unknown_version_one_field_fails(self) -> None:
        policy = json.loads(main.NETWORK_POLICY_FILE.read_text(encoding="utf-8"))
        policy["profiles"][0]["media_worker"] = 3
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "network-policy.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
            with self.assertRaises(main.PolicyError):
                main.load_network_policy(path)

    def test_bounds_fail_closed(self) -> None:
        policy = json.loads(main.NETWORK_POLICY_FILE.read_text(encoding="utf-8"))
        policy["profiles"][0]["media_workers"] = 9
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "network-policy.json"
            path.write_text(json.dumps(policy), encoding="utf-8")
            with self.assertRaises(main.PolicyError):
                main.load_network_policy(path)

    def test_profile_resolution_is_data_driven(self) -> None:
        policy = main.load_network_policy()
        selected = main.resolve_network_profile(policy, "urgent")
        self.assertEqual(selected["id"], "urgent")
        self.assertEqual(selected["media_workers"], 4)
        self.assertEqual(main.resolve_network_profile(policy)["id"], "gentle")

    def test_upstream_command_receives_selected_worker_count(self) -> None:
        main.configure(
            "example",
            300,
            profile={
                "id": "urgent",
                "label": "Urgent",
                "description": "Test",
                "feed_delay_seconds": 0.0,
                "feed_jitter_seconds": 0.0,
                "media_workers": 4,
            },
        )
        completed = mock.Mock(returncode=0)
        with (
            mock.patch.object(main, "ensure_tumblr_backup"),
            mock.patch.object(main, "tumblr_backup_cli", return_value=Path("/bin/true")),
            mock.patch.object(main.subprocess, "run", return_value=completed) as run,
        ):
            main.run_tumblr_backup([], new_ids=[1])
        command = run.call_args.args[0]
        thread_index = command.index("--threads")
        self.assertEqual(command[thread_index + 1], "4")

    def test_import_does_not_require_upstream_package(self) -> None:
        self.assertNotIn("tumblr_backup", main.__dict__)

    def test_feed_pacing_skips_first_and_trailing_wait(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            main.configure(
                "example",
                100,
                profile={
                    "id": "test",
                    "label": "Test",
                    "description": "Test",
                    "feed_delay_seconds": 2.0,
                    "feed_jitter_seconds": 0.5,
                    "media_workers": 1,
                },
            )
            main.OUT = Path(directory) / "archive"
            main.JSON_DIR = main.OUT / "json"
            feeds = [
                {"posts": [{"id": 1, "type": "regular"}], "posts-total": 1},
            ]
            with (
                mock.patch.object(main, "fetch_public_page", side_effect=feeds),
                mock.patch.object(main.time, "sleep") as sleep,
                mock.patch.object(main.random, "uniform", return_value=0.25),
                mock.patch.object(main, "process_batch", return_value=1),
                mock.patch.object(main, "post_is_complete", return_value=False),
                mock.patch.object(main, "write_json_atomic"),
            ):
                main.acquire_and_process()
        sleep.assert_not_called()

    def test_feed_429_retries_once_after_retry_after(self) -> None:
        main.BLOG_HOST = "example.tumblr.com"
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = (
            b'var tumblr_api_read = {"posts": []};'
        )
        error = HTTPError(
            "https://example.tumblr.com/api/read/json",
            429,
            "Too Many Requests",
            {"Retry-After": "3"},
            None,
        )
        with (
            mock.patch.object(main, "urlopen", side_effect=[error, response]),
            mock.patch.object(main.time, "sleep") as sleep,
        ):
            result = main.fetch_public_page(0)
        self.assertEqual(result, {"posts": []})
        sleep.assert_called_once_with(3.0)

    def test_feed_429_without_retry_after_fails(self) -> None:
        main.BLOG_HOST = "example.tumblr.com"
        error = HTTPError(
            "https://example.tumblr.com/api/read/json",
            429,
            "Too Many Requests",
            {},
            None,
        )
        with mock.patch.object(main, "urlopen", side_effect=error):
            with self.assertRaises(main.TumblrRateLimitedError):
                main.fetch_public_page(0)

    def test_feed_404_is_classified_as_one_blog_failure(self) -> None:
        main.BLOG = "missing-blog"
        main.BLOG_HOST = "missing-blog.tumblr.com"
        error = HTTPError(
            "https://missing-blog.tumblr.com/api/read/json",
            404,
            "Not Found",
            {},
            None,
        )
        with mock.patch.object(main, "urlopen", side_effect=error):
            with self.assertRaises(main.BlogSourceFailure) as raised:
                main.fetch_public_page(0)
        self.assertEqual(raised.exception.blog, "missing-blog")
        self.assertEqual(raised.exception.code, 404)
        self.assertFalse(raised.exception.retryable)


class PresentationTests(unittest.TestCase):
    def test_reblogger_comment_after_trail_is_rendered_once(self) -> None:
        body = (
            '<p><a class="tumblr_blog" href="https://origin.tumblr.com/post/1">origin</a>:</p>'
            '<blockquote><p>original</p></blockquote><p>my added comment</p>'
        )
        rendered = main._render_flat_reblog_trail(body, main.CONTENT_ROOT / "dashboard.html", "reblogger")
        self.assertIn("original", rendered)
        self.assertIn("my added comment", rendered)
        self.assertEqual(rendered.count("my added comment"), 1)
        self.assertIn("reblog-trail-current", rendered)

    def test_plain_reblog_without_trailing_comment_is_unchanged(self) -> None:
        body = '<p><a class="tumblr_blog" href="https://origin.tumblr.com/post/1">origin</a>:</p><blockquote><p>original</p></blockquote><footer>42 notes</footer>'
        rendered = main._render_flat_reblog_trail(body, main.CONTENT_ROOT / "dashboard.html", "reblogger")
        self.assertIn("original", rendered)
        self.assertNotIn("reblog-trail-current", rendered)

    def test_trail_less_markup_returns_empty_entries(self) -> None:
        parser = main._TrailMarkupParser()
        parser.feed("<article><p>An ordinary post without a reblog trail.</p></article>")
        parser.close()
        self.assertEqual(main._trail_entries(parser.root.children), [])

    def test_trail_less_render_batch_does_not_block_next_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_out, old_json_dir = main.OUT, main.JSON_DIR
            try:
                main.OUT = Path(directory) / "archive"
                main.JSON_DIR = main.OUT / "json"
                main.JSON_DIR.mkdir(parents=True)
                posts_dir = main.OUT / "posts"
                posts_dir.mkdir(parents=True)
                for post_id in (1, 2):
                    (main.JSON_DIR / f"{post_id}.json").write_text("{}", encoding="utf-8")

                def render(ids: list[int]) -> None:
                    for post_id in ids:
                        (posts_dir / f"{post_id}.html").write_text(
                            "<article><p>An ordinary post without a reblog trail.</p></article>",
                            encoding="utf-8",
                        )

                with mock.patch.object(main, "render_with_tumblr_backup", side_effect=render):
                    self.assertEqual(main.process_batch([1]), 1)
                    self.assertEqual(main.process_batch([2]), 1)
            finally:
                main.OUT, main.JSON_DIR = old_out, old_json_dir

    def test_failed_render_writes_source_fallback_and_reaches_next_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_out, old_json_dir, old_blog = main.OUT, main.JSON_DIR, main.BLOG
            try:
                main.BLOG = "example"
                main.OUT = Path(directory) / "archive"
                main.JSON_DIR = main.OUT / "json"
                main.JSON_DIR.mkdir(parents=True)
                raw = {"id": 1, "type": "regular", "regular-body": "Questionable source"}
                for post_id in (1, 2):
                    record = {
                        "id": post_id,
                        "id_string": str(post_id),
                        "type": "text",
                        "title": "Fallback post",
                        "body": "Questionable source",
                        "timestamp": 1,
                        "_puppetbackup_source_record": {**raw, "id": post_id},
                    }
                    (main.JSON_DIR / f"{post_id}.json").write_text(json.dumps(record), encoding="utf-8")

                with mock.patch.object(main, "render_with_tumblr_backup", side_effect=RuntimeError("synthetic renderer failure")):
                    self.assertEqual(main.process_batch([1]), 1)
                    self.assertEqual(main.process_batch([2]), 1)

                self.assertIn("Preserved source record", (main.OUT / "posts" / "1.html").read_text(encoding="utf-8"))
                self.assertIn("Questionable source", (main.OUT / "posts" / "2.html").read_text(encoding="utf-8"))
                tasks = (main.OUT / "repair" / "tasks.jsonl").read_text(encoding="utf-8")
                self.assertIn('"post_id": "1"', tasks)
                self.assertIn('"post_id": "2"', tasks)
            finally:
                main.OUT, main.JSON_DIR, main.BLOG = old_out, old_json_dir, old_blog

    def test_avatar_enrichment_failure_is_a_post_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_out, old_json_dir, old_blog = main.OUT, main.JSON_DIR, main.BLOG
            try:
                main.BLOG = "example"
                main.OUT = Path(directory) / "archive"
                main.JSON_DIR = main.OUT / "json"
                main.JSON_DIR.mkdir(parents=True)
                (main.JSON_DIR / "1.json").write_text(
                    json.dumps({"id": 1, "type": "text", "body": "source"}),
                    encoding="utf-8",
                )
                with (
                    mock.patch.object(main, "render_with_tumblr_backup"),
                    mock.patch.object(main, "capture_participant_avatars", side_effect=RuntimeError("trail parser failed")),
                ):
                    (main.OUT / "posts").mkdir(parents=True)
                    (main.OUT / "posts" / "1.html").write_text("<article>rendered</article>", encoding="utf-8")
                    self.assertEqual(main.process_batch([1]), 1)
                tasks = (main.OUT / "repair" / "tasks.jsonl").read_text(encoding="utf-8")
                self.assertIn('"stage": "participant-avatar"', tasks)
                self.assertIn('"post_id": "1"', tasks)
            finally:
                main.OUT, main.JSON_DIR, main.BLOG = old_out, old_json_dir, old_blog

    def test_flat_reblog_trail_reads_utterances_chronologically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_backups = main.CONTENT_ROOT
            main.CONTENT_ROOT = Path(directory) / "Backups"
            try:
                body = (
                    '<p><a class="tumblr_blog" href="https://a.tumblr.com/post/1">a</a>:</p>'
                    '<blockquote><p>A content</p><p><a class="tumblr_blog" href="https://b.tumblr.com/post/2">b</a>:</p>'
                    '<blockquote><p>B content</p><blockquote><p>an actual quote</p></blockquote></blockquote></blockquote>'
                )
                rendered = main._render_flat_reblog_trail(body, main.CONTENT_ROOT / "dashboard.html")
                self.assertEqual(rendered.count("reblog-trail-entry"), 2)
                self.assertLess(rendered.index(">b</bdi>"), rendered.index(">a</bdi>"))
                self.assertIn("A content", rendered)
                self.assertIn("B content", rendered)
                self.assertIn("<blockquote><p>an actual quote</p></blockquote>", rendered)
                self.assertFalse((main.CONTENT_ROOT / "a").exists())
                self.assertFalse((main.CONTENT_ROOT / "b").exists())
            finally:
                main.CONTENT_ROOT = old_backups

    def test_profile_snapshot_uses_available_bio_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_backups = main.CONTENT_ROOT
            main.CONTENT_ROOT = Path(directory) / "Backups"
            try:
                records = [
                    {"timestamp": 2, "tumblelog": "example", "blog": {"title": "Example", "description": ""}},
                    {"timestamp": 1, "tumblelog": "example", "blog": {"title": "Example", "description": "A durable bio"}},
                ]
                profile = main._write_profile_snapshot("example", records)
                self.assertEqual(profile["description"], "A durable bio")
                self.assertEqual(json.loads((main.CONTENT_ROOT / "example" / "profile" / "profile.json").read_text())["description"], "A durable bio")
            finally:
                main.CONTENT_ROOT = old_backups

    def test_normalization_preserves_source_blog_title_and_bio(self) -> None:
        old_blog = main.BLOG
        main.BLOG = "example"
        try:
            record = main.normalize_post(
                {
                    "id": 1,
                    "unix-timestamp": 1,
                    "type": "regular",
                    "regular-body": "Hello",
                    "tumblelog": {"name": "example", "title": "The Example", "description": "A real bio"},
                },
                {"tumblelog": {"name": "example", "title": "Fallback"}},
                1,
            )
            self.assertEqual(record["blog"]["title"], "The Example")
            self.assertEqual(record["blog"]["description"], "A real bio")
        finally:
            main.BLOG = old_blog

    def test_profile_homepage_capture_is_one_time_and_metadata_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_backups = main.CONTENT_ROOT
            main.CONTENT_ROOT = Path(directory) / "Backups"
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.read.return_value = b'<title>The Example</title><meta name="description" content="A public bio">'
            try:
                with mock.patch.object(main, "urlopen", return_value=response) as opened:
                    main.capture_blog_profile_metadata("example")
                    main.capture_blog_profile_metadata("example")
                profile = json.loads((main.CONTENT_ROOT / "example" / "profile" / "profile.json").read_text())
                self.assertEqual(profile["title"], "The Example")
                self.assertEqual(profile["description"], "A public bio")
                opened.assert_called_once()
            finally:
                main.CONTENT_ROOT = old_backups

    def test_participant_avatar_is_cached_without_creating_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_backups = main.CONTENT_ROOT
            main.CONTENT_ROOT = Path(directory) / "Backups"
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.headers = {"Content-Type": "image/jpeg"}
            response.read.return_value = b"low-resolution-avatar"
            try:
                with mock.patch.object(main, "urlopen", return_value=response) as opened:
                    self.assertTrue(main.capture_participant_avatar("Participant"))
                    self.assertFalse(main.capture_participant_avatar("Participant"))
                avatar = main.CONTENT_ROOT / "participant-assets" / "participant" / "avatar.jpg"
                self.assertEqual(avatar.read_bytes(), b"low-resolution-avatar")
                self.assertFalse((main.CONTENT_ROOT / "participant").exists())
                self.assertEqual(opened.call_count, 1)
                self.assertEqual(
                    main._participant_avatar("participant", main.CONTENT_ROOT / "dashboard.html"),
                    "participant-assets/participant/avatar.jpg",
                )
            finally:
                main.CONTENT_ROOT = old_backups

    def test_participant_avatar_prefers_source_record_url_over_broken_blog_shortcut(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_backups = main.CONTENT_ROOT
            main.CONTENT_ROOT = Path(directory) / "Backups"
            source_url = "https://64.media.tumblr.com/example/s64x64/avatar.png"
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.headers = {"Content-Type": "image/png"}
            response.read.return_value = b"source-avatar"
            try:
                with mock.patch.object(main, "urlopen", return_value=response) as opened:
                    self.assertTrue(main.capture_participant_avatar("Participant", source_url))
                self.assertEqual(opened.call_args.args[0].full_url, source_url)
                self.assertEqual(
                    (main.CONTENT_ROOT / "participant-assets" / "participant" / "avatar.png").read_bytes(),
                    b"source-avatar",
                )
            finally:
                main.CONTENT_ROOT = old_backups

    def test_participant_avatar_sources_are_read_from_preserved_raw_record(self) -> None:
        record = {
            "_puppetbackup_source_record": {
                "reblogged-from-name": "nearby-blog",
                "reblogged_from_avatar_url_64": "https://64.media.tumblr.com/nearby.png",
                "reblogged-root-name": "original-blog",
                "reblogged_root_avatar_url_64": "https://64.media.tumblr.com/original.png",
            }
        }
        self.assertEqual(
            main._participant_avatar_sources(record),
            {
                "nearby-blog": "https://64.media.tumblr.com/nearby.png",
                "original-blog": "https://64.media.tumblr.com/original.png",
            },
        )

    def test_global_catalog_orders_blogs_by_local_post_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_backups = main.CONTENT_ROOT
            main.CONTENT_ROOT = Path(directory) / "Backups"
            try:
                for blog, count in (("small", 5), ("largest", 135), ("middle", 50)):
                    json_root = main.CONTENT_ROOT / blog / "json"
                    json_root.mkdir(parents=True)
                    for post_id in range(count):
                        (json_root / f"{post_id}.json").write_text(
                            json.dumps({"id": post_id}), encoding="utf-8"
                        )
                catalog = main.build_global_catalog()
                self.assertEqual(
                    [(item["blog"], item["local_post_count"]) for item in catalog["blogs"]],
                    [("largest", 135), ("middle", 50), ("small", 5)],
                )
            finally:
                main.CONTENT_ROOT = old_backups

    def test_post_fragment_rebases_and_hardens_aggregate_markup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_backups = main.CONTENT_ROOT
            main.CONTENT_ROOT = root / "Backups"
            try:
                blog_root = main.CONTENT_ROOT / "example"
                post_page = blog_root / "posts" / "7.html"
                post_page.parent.mkdir(parents=True)
                post_page.write_text(
                    '<article id="post-7"><header>discarded standalone metadata</header>'
                    '<p id="body-7"><a href="../media/source.html">link</a></p>'
                    '<img src="../media/image.png" srcset="../media/image.png 1x, https://example.invalid/x.png 2x">'
                    '<video autoplay data-crt-video data-crt-options="{}" poster="https://media.example/poster.jpg"><source src="https://media.example/video.mp4" type="video/mp4"><source src="../media/local.mp4" type="video/mp4"></video>'
                    '<script>alert(1)</script><form action="../submit"><input></form>'
                    '<a href="#body-7" onclick="alert(1)">jump</a></article>',
                    encoding="utf-8",
                )
                record = {
                    "_canonical_blog": "example",
                    "id": 7,
                    "id_string": "7",
                    "blog_name": "example",
                    "timestamp": 100,
                    "date": "2026-01-01 00:00:00 GMT",
                    "post_url": "https://example.tumblr.com/post/7",
                    "tags": ["art"],
                    "blog": {"title": "Example"},
                }
                destination = main.CONTENT_ROOT / "dashboard.html"
                rendered = main._render_post_card(record, destination)
                self.assertIn('src="example/media/image.png"', rendered)
                self.assertIn('srcset="example/media/image.png 1x"', rendered)
                self.assertNotIn('<img src="https://', rendered)
                self.assertIn('<video poster="https://media.example/poster.jpg"', rendered)
                self.assertIn('src="https://media.example/video.mp4"', rendered)
                self.assertIn('src="example/media/local.mp4"', rendered)
                self.assertNotIn("data-crt-video", rendered)
                self.assertNotIn("autoplay", rendered)
                self.assertNotIn("<script", rendered)
                self.assertNotIn("<form", rendered)
                self.assertNotIn("onclick", rendered)
                self.assertIn('id="post-example-7-body-7"', rendered)
                self.assertIn('href="#post-example-7-body-7"', rendered)
                self.assertIn("#art", rendered)
            finally:
                main.CONTENT_ROOT = old_backups

    def test_global_presentation_uses_cards_and_profile_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_backups, old_network, old_neighborhoods, old_app, old_assets = (
                main.CONTENT_ROOT, main.NETWORK_ROOT, main.NEIGHBORHOODS_ROOT,
                main.APP_ROOT, main.SOURCE_ASSET_DIR,
            )
            old_source_css, old_source_js = main.SOURCE_ARCHIVE_CSS, main.SOURCE_ARCHIVE_JS
            main.CONTENT_ROOT = root / "Archive" / "Content"
            main.NETWORK_ROOT = root / "Archive" / "Network"
            main.NEIGHBORHOODS_ROOT = main.NETWORK_ROOT / "Neighborhoods"
            main.APP_ROOT = root / "Archive" / "App"
            main.SOURCE_ASSET_DIR = root / "assets"
            main.SOURCE_ARCHIVE_CSS = main.SOURCE_ASSET_DIR / "archive.css"
            main.SOURCE_ARCHIVE_JS = main.SOURCE_ASSET_DIR / "archive.js"
            main.SOURCE_ASSET_DIR.mkdir(parents=True)
            main.SOURCE_ARCHIVE_CSS.write_text("body {}", encoding="utf-8")
            main.SOURCE_ARCHIVE_JS.write_text("", encoding="utf-8")
            try:
                blog_root = main.CONTENT_ROOT / "example"
                (blog_root / "json").mkdir(parents=True)
                (blog_root / "posts").mkdir(parents=True)
                record = {
                    "id": 7, "id_string": "7", "blog_name": "example", "tumblelog": "example",
                    "timestamp": 100, "date": "2026-01-01 00:00:00 GMT",
                    "post_url": "https://example.tumblr.com/post/7", "tags": ["art"],
                    "blog": {"title": "Example", "description": "A local archive"}, "title": "Hello",
                }
                (blog_root / "json" / "7.json").write_text(json.dumps(record), encoding="utf-8")
                (blog_root / "posts" / "7.html").write_text('<article><header>metadata</header><p>Hello</p></article>', encoding="utf-8")
                main.render_global_pages()
                feed = (main.APP_ROOT / "feed.html").read_text(encoding="utf-8")
                self.assertIn("post-card", feed)
                self.assertIn("Hello", feed)
                self.assertIn('class="post-blog" href="../Content/example/index.html"><bdi dir="auto">example</bdi>', feed)
                self.assertNotIn('class="post-username"', feed)
                self.assertTrue((blog_root / "profile" / "profile.json").is_file())
                self.assertIn("A local archive", (blog_root / "index.html").read_text(encoding="utf-8"))
            finally:
                main.CONTENT_ROOT, main.NETWORK_ROOT, main.NEIGHBORHOODS_ROOT, main.APP_ROOT, main.SOURCE_ASSET_DIR = (
                    old_backups, old_network, old_neighborhoods, old_app, old_assets
                )
                main.SOURCE_ARCHIVE_CSS, main.SOURCE_ARCHIVE_JS = old_source_css, old_source_js


class ContextTests(unittest.TestCase):
    def _run_cold_start_fixture(self, focus: str, depth: int = 2) -> dict:
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        old_backups, old_neighborhoods, old_css = main.CONTENT_ROOT, main.NEIGHBORHOODS_ROOT, main.GLOBAL_CSS
        main.CONTENT_ROOT = root / "Backups"
        main.NEIGHBORHOODS_ROOT = root / "Neighborhoods"
        main.GLOBAL_CSS = root / "global.css"
        main.GLOBAL_CSS.write_text("body {}", encoding="utf-8")
        policy = main.load_context_policy()

        def record(number: int, other: str | None = None) -> dict:
            value = {"id": number, "type": "regular", "unix-timestamp": number, "date-gmt": "2026-01-01 00:00:00 GMT"}
            if other:
                value["reblogged-from-name"] = other
                value["reblogged-from-url"] = f"https://{other}.tumblr.com/post/{number}"
            return value

        feeds = {
            "target.tumblr.com": [record(index, f"neighbor-{index % 2}") for index in range(1, 31)],
            "neighbor-0.tumblr.com": [record(100 + index, "outer") for index in range(1, 21)],
            "neighbor-1.tumblr.com": [record(200 + index, "outer") for index in range(1, 21)],
            "outer.tumblr.com": [record(300 + index) for index in range(1, 21)],
        }

        def fetch(start: int, count: int = 50) -> dict:
            posts = feeds.get(main.BLOG_HOST, [])
            return {"posts": posts[start:start + count], "posts-total": len(posts), "tumblelog": {"name": main.BLOG}}

        main.configure("target", 30, profile={
            "id": "test", "label": "Test", "description": "", "feed_delay_seconds": 0.0,
            "feed_jitter_seconds": 0.0, "media_workers": 1,
        })
        try:
            with (
                mock.patch.object(main, "fetch_public_page", side_effect=fetch),
                mock.patch.object(main, "process_batch", side_effect=lambda ids: len(ids)),
                mock.patch.object(main, "ensure_blog_stylesheets"),
                mock.patch.object(main, "repair_interrupted_work", return_value=0),
                mock.patch.object(main.ProgressRenderer, "render"),
            ):
                _, document = main.run_incremental_capture("target", 30, False, "explore", depth, policy, focus)
            physical = {
                blog: len(list((main.CONTENT_ROOT / blog / "json").glob("*.json")))
                for blog in ("target", "neighbor-0", "neighbor-1", "outer")
            }
            ledger = [json.loads(line) for line in (main.CONTENT_ROOT / "acquisition-ledger.jsonl").read_text(encoding="utf-8").splitlines()]
            return document, physical, ledger
        finally:
            main.CONTENT_ROOT, main.NEIGHBORHOODS_ROOT, main.GLOBAL_CSS = old_backups, old_neighborhoods, old_css
            directory.cleanup()

    def test_cold_start_balanced_generates_neighbor_and_outer_acquisition(self) -> None:
        document, physical, ledger = self._run_cold_start_fixture("balanced")
        saved = document["run_status"]["saved_by_breadth"]
        self.assertEqual(document["run_status"]["budget_used"], 30)
        self.assertEqual(document["run_status"]["strategy"], "explore")
        self.assertGreater(saved["target"], 0)
        self.assertGreater(saved["breadth1"], 0)
        self.assertGreater(saved["breadth2plus"], 0)
        self.assertEqual(sum(physical.values()), 30)
        self.assertEqual(len(ledger), 30)
        self.assertEqual(sum(1 for event in ledger if event.get("breadth", 0) >= 2), saved["breadth2plus"])

    def test_cold_start_outward_never_acquires_target(self) -> None:
        document, physical, ledger = self._run_cold_start_fixture("neighbors")
        saved = document["run_status"]["saved_by_breadth"]
        self.assertGreater(saved["target"], 0)
        self.assertGreater(saved["breadth1"], 0)
        self.assertGreater(saved["breadth2plus"], 0)
        self.assertGreater(document["run_status"]["scouted"], 0)
        self.assertGreater(physical["target"], 0)
        self.assertEqual(len(ledger), sum(physical.values()))

    def test_depth_outside_selected_mode_is_exhausted_and_redistributed(self) -> None:
        document, physical, _ledger = self._run_cold_start_fixture("balanced", depth=1)
        self.assertEqual(document["run_status"]["budget_used"], 30)
        self.assertEqual(document["run_status"]["saved_by_breadth"]["breadth2plus"], 0)

    def test_neighbor_candidate_writes_only_to_its_canonical_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_backups = main.CONTENT_ROOT
            try:
                main.CONTENT_ROOT = Path(directory) / "Backups"
                state = main.BlogState("neighbor", main.canonical_archive_root("neighbor"), 0)
                state.run_id = "run-neighbor"
                status = main.CrawlerStatus("target", budget_limit=2, run_id="run-neighbor")
                state.progress = status
                main.BLOG = "target"
                main.OUT = main.canonical_archive_root("target")
                candidate = main.ActionCandidate(
                    identity="acquire:neighbor:next",
                    kind="acquire_neighbor_post",
                    resource_class="acquisition",
                    lane="distance-1",
                    graph_depth=1,
                    blog="neighbor",
                    planned_cost=1,
                    reason_codes=["test neighbor"],
                )
                source = {"id": 7, "type": "regular", "unix-timestamp": 7, "date-gmt": "2026-01-01 00:00:00 GMT"}
                with mock.patch.object(main, "process_batch", return_value=1):
                    main._process_source_ids(state, [source], candidate)
                self.assertTrue((main.CONTENT_ROOT / "neighbor" / "json" / "7.json").is_file())
                self.assertFalse((main.CONTENT_ROOT / "target" / "json" / "7.json").exists())
                event = json.loads((main.CONTENT_ROOT / "acquisition-ledger.jsonl").read_text().splitlines()[0])
                self.assertEqual(event["blog"], "neighbor")
                self.assertEqual(event["breadth"], 1)
                self.assertNotIn("acquisition_lane", event)
                self.assertEqual(event["action_kind"], "acquire_neighbor_post")
                record = json.loads((main.CONTENT_ROOT / "neighbor" / "json" / "7.json").read_text())
                self.assertEqual(record["_puppetbackup_breadth"], 1)
                self.assertNotIn("_puppetbackup_acquisition_lane", record)
                self.assertEqual(status.saved_by_lane["depth1"], 1)
                self.assertEqual(status.budget_used, sum(status.saved_by_lane.values()))
            finally:
                main.CONTENT_ROOT = old_backups

    def test_runtime_controls_apply_without_terminal_and_record_changes(self) -> None:
        runtime = main.RuntimeControls(0.5, 300)
        status = main.CrawlerStatus("target", budget_limit=300, budget_used=184)
        controller = main.RuntimeKeyController(runtime, status, stream=__import__("io").StringIO())
        controller.apply("n")
        controller.apply("-")
        controller.apply("]")
        controller.apply("q")
        self.assertEqual(runtime.focus_bias, 1.2)
        self.assertEqual(runtime.budget_limit, 275)
        self.assertEqual(runtime.context_multiplier, 1.25)
        self.assertTrue(runtime.cancel_requested)
        self.assertEqual(len(runtime.changes), 4)

    def test_tag_indexes_preserve_variants_and_pending_render_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_backups, old_network, old_neighborhoods, old_app = (
                main.CONTENT_ROOT, main.NETWORK_ROOT, main.NEIGHBORHOODS_ROOT, main.APP_ROOT
            )
            try:
                archive = Path(directory) / "Archive"
                main.CONTENT_ROOT = archive / "Content"
                main.NETWORK_ROOT = archive / "Network"
                main.NEIGHBORHOODS_ROOT = main.NETWORK_ROOT / "Neighborhoods"
                main.APP_ROOT = archive / "App"
                source = main.CONTENT_ROOT / "one" / "json"
                source.mkdir(parents=True)
                (source / "1.json").write_text(json.dumps({
                    "id": 1, "id_string": "1", "timestamp": 20,
                    "tags": ["#My Tag", "#my tag", "#藝術", "#👋"],
                }), encoding="utf-8")
                (source / "2.json").write_text(json.dumps({
                    "id": 2, "id_string": "2", "timestamp": 10,
                    "tags": ["#My Tag"],
                }), encoding="utf-8")
                (main.CONTENT_ROOT / "one" / "posts").mkdir()
                (main.CONTENT_ROOT / "one" / "posts" / "1.html").write_text("post", encoding="utf-8")
                main.render_global_pages()
                index = json.loads((main.CONTENT_ROOT / "tag-index.json").read_text(encoding="utf-8"))
                my_tag = next(item for item in index["tags"] if item["canonical_tag_key"] == "#My Tag")
                self.assertEqual(my_tag["variants"], ["#My Tag"])
                self.assertTrue(any(item["canonical_tag_key"] == "#my tag" for item in index["tags"]))
                self.assertEqual([(post["post_id"], post["rendered"]) for post in my_tag["posts"]], [("1", True), ("2", False)])
                page = (main.APP_ROOT / "tags" / f'{my_tag["page_id"]}.html').read_text(encoding="utf-8")
                self.assertIn("source preserved; rendering pending", page)
                self.assertIn("../../Content/one/posts/1.html", page)
                self.assertFalse((main.CONTENT_ROOT / "one" / "tags").exists())
            finally:
                main.CONTENT_ROOT, main.NETWORK_ROOT, main.NEIGHBORHOODS_ROOT, main.APP_ROOT = (
                    old_backups, old_network, old_neighborhoods, old_app
                )

    def test_context_policy_is_bounded_and_mode_driven(self) -> None:
        policy = main.load_context_policy()
        nearby = main.context_config(policy, "nearby", None)
        explore = main.context_config(policy, "explore", 3)
        self.assertEqual(nearby["max_depth"], 1)
        self.assertEqual(explore["max_depth"], 3)
        self.assertEqual(policy["defaults"]["global_context_post_budget"], 500)
        self.assertEqual(policy["defaults"]["initial_neighborhood_posts"], 5)

    def test_structured_evidence_preserves_direction_and_ignores_root(self) -> None:
        record = {
            "id": 11,
            "post_url": "https://target.tumblr.com/post/11",
            "reblogged_from_name": "source",
            "reblogged_from_url": "https://source.tumblr.com/post/10",
            "reblogged_root_name": "root",
            "reblogged_root_url": "https://root.tumblr.com/post/9",
        }
        evidence = main.interaction_evidence("target", record)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["from_blog"], "target")
        self.assertEqual(evidence[0]["to_blog"], "source")
        self.assertEqual(evidence[0]["kind"], "direct_reblog")

    def test_blog_view_url_uses_path_blog_not_www(self) -> None:
        parsed = main._blog_from_reference("", "https://www.tumblr.com/blog/view/foo-bar/123")
        self.assertEqual(parsed[0], "foo-bar")
        self.assertIsNone(main._blog_from_reference("", "https://www.tumblr.com/blog/view/www/123"))

    def test_focus_profiles_are_policy_driven(self) -> None:
        policy = main.load_context_policy()
        self.assertEqual(policy["default_focus"], "balanced")
        self.assertEqual(policy["focus_profiles"]["deep"]["target_share"], 80)
        self.assertEqual(main.context_config(policy, "explore", 2, "wide")["focus"], "wide")

    def test_outward_focus_can_zero_target_without_negative_shares(self) -> None:
        policy = main.load_context_policy()
        config = main.context_config(policy, "explore", 2, "wide")
        shares = main.effective_focus_shares(config, 1.2)
        self.assertEqual(shares["target"], 0.0)
        self.assertAlmostEqual(sum(shares.values()), 100.0)
        self.assertTrue(all(value >= 0 for value in shares.values()))

    def test_runtime_controls_record_run_only_changes(self) -> None:
        controls = main.RuntimeControls(0.5, 300)
        controls.update("budget_limit", 500, "test")
        controls.update("context_multiplier", 1.8, "test")
        self.assertEqual(controls.budget_limit, 500)
        self.assertEqual(len(controls.changes), 2)
        self.assertEqual(controls.changes[0]["old"], 300)

    def test_directional_scores_are_saturated_and_separate(self) -> None:
        policy = main.load_context_policy()
        document = {"interactions": [
            {"from_blog": "target", "to_blog": "source", "kind": "direct_reblog", "source_blog": "target", "source_post_id": str(i)}
            for i in range(100)
        ] + [
            {"from_blog": "fan", "to_blog": "target", "kind": "direct_reblog", "source_blog": "fan", "source_post_id": str(i)}
            for i in range(2)
        ]}
        scores = main._candidate_scores("target", document, policy)
        self.assertGreater(scores["source"]["source_affinity"], 0)
        self.assertGreater(scores["fan"]["audience_activity"], 0)
        self.assertLess(scores["source"]["source_affinity"], 100)

    def test_support_posts_do_not_create_recursive_context_deficits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_backups = main.CONTENT_ROOT
            try:
                main.CONTENT_ROOT = Path(directory) / "Backups"
                for post_id, role in (("10", "anchor"), ("11", "support")):
                    path = main.canonical_archive_root("target") / "json" / f"{post_id}.json"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps({"id": int(post_id), "id_string": post_id, "_puppetbackup_anchor_role": role}), encoding="utf-8")
                document = {
                    "primary_blog": "target",
                    "blogs": [],
                    "adjacency_observations": [{
                        "blog": "target", "anchor_post_id": "10", "direction": "after",
                        "adjacent_post_id": "11", "region_id": "r1",
                    }],
                }
                main.recompute_context_deficits("target", document)
                self.assertIn("target:10", document["context_deficits"])
                self.assertNotIn("target:11", document["context_deficits"])
            finally:
                main.CONTENT_ROOT = old_backups

    def test_repeated_scout_evidence_deduplicates_by_observation_identity(self) -> None:
        item = {
            "from_blog": "target", "to_blog": "source", "kind": "direct_reblog",
            "source_blog": "target", "source_post_id": "7", "reference_url": "https://source.tumblr.com/post/7",
        }
        document = {"scout_interactions": [dict(item), dict(item)], "blogs": []}
        evidence = main._all_document_evidence("target", document)
        self.assertEqual(len(evidence), 1)

    def test_adjacency_requires_observed_region_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old_backups = main.CONTENT_ROOT
            try:
                main.CONTENT_ROOT = Path(directory) / "Backups"
                path = main.canonical_archive_root("target") / "json" / "10.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"id": 10, "id_string": "10"}), encoding="utf-8")
                document = {"primary_blog": "target", "blogs": [], "adjacency_observations": []}
                main.recompute_context_deficits("target", document)
                self.assertEqual(document["context_deficits"]["target:10"]["immediate_before"]["state"], "unknown")
                document["adjacency_observations"].append({
                    "blog": "target", "anchor_post_id": "10", "direction": "before",
                    "adjacent_post_id": "9", "region_id": "region-1",
                })
                main.recompute_context_deficits("target", document)
                self.assertEqual(document["context_deficits"]["target:10"]["immediate_before"]["state"], "missing")
            finally:
                main.CONTENT_ROOT = old_backups

    def test_progress_renderer_non_tty_is_plain_text(self) -> None:
        import io
        stream = io.StringIO()
        status = main.CrawlerStatus("example", focus="balanced", budget_limit=10)
        main.ProgressRenderer(status, stream=stream).render(force=True)
        self.assertIn("Targets:", stream.getvalue())
        self.assertNotIn("\\033[", stream.getvalue())

    def test_progress_renderer_android_uses_one_carriage_return_line(self) -> None:
        import io

        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

        stream = TTY()
        status = main.CrawlerStatus("example", focus="wide", budget_limit=100)
        status.saved_by_lane.update({"target": 18, "depth1": 19, "depth2": 7})
        status.budget_used = 44
        status.current_action = "Scouting blog-a"
        old_capabilities = dict(main.HOST_CAPABILITIES)
        main.set_host_capabilities(compact_terminal=True, keyboard_controls=False)
        try:
            renderer = main.ProgressRenderer(status, stream=stream)
            controller = main.RuntimeKeyController(main.RuntimeControls(0.5, 100), status, stream=stream)
            renderer.render(force=True)
            first = stream.getvalue()
            status.budget_used = 45
            renderer.render(force=True)
            status.warning("blog-a temporarily unavailable")
            renderer.render(force=True)
            renderer.finish()
        finally:
            main.HOST_CAPABILITIES.clear()
            main.HOST_CAPABILITIES.update(old_capabilities)
        output = stream.getvalue()
        self.assertTrue(renderer.compact_terminal)
        self.assertFalse(controller.enabled)
        self.assertIn("\r", output)
        self.assertNotIn("\033[", output)
        self.assertIn("WARNING: blog-a temporarily unavailable\n", output)
        self.assertEqual(output.count("WARNING: blog-a temporarily unavailable\n"), 1)
        self.assertTrue(output.endswith("\n"))
        self.assertGreater(len(output), len(first))

    def test_progress_renderer_ansi_replaces_frame_without_clearing_terminal(self) -> None:
        import io

        class TTY(io.StringIO):
            def isatty(self) -> bool:
                return True

        stream = TTY()
        status = main.CrawlerStatus("example", focus="balanced", budget_limit=10)
        with mock.patch.dict(main.os.environ, {"TERM": "xterm", "ANDROID_ARGUMENT": ""}, clear=False):
            with mock.patch.object(main.shutil, "get_terminal_size", return_value=main.os.terminal_size((72, 24))):
                renderer = main.ProgressRenderer(status, stream=stream)
                renderer.render(force=True)
                status.budget_used = 1
                renderer.render(force=True)
                renderer.finish()
        output = stream.getvalue()
        self.assertTrue(renderer.live)
        self.assertIn("\033[", output)
        self.assertNotIn("\033[2J", output)
        self.assertTrue(output.endswith("\n"))

    def test_progress_renderer_deduplicates_warning_history_and_survives_broken_stream(self) -> None:
        import io

        stream = io.StringIO()
        status = main.CrawlerStatus("example", budget_limit=10)
        status.warning("blog unavailable", key="unavailable", detail="blog-a")
        status.warning("blog unavailable", key="unavailable", detail="blog-a")
        main.ProgressRenderer(status, stream=stream).render(force=True)
        self.assertEqual(stream.getvalue().count("WARNING: blog unavailable\n"), 1)
        self.assertEqual(status.warning_counts["unavailable"], 2)

        class Broken:
            def isatty(self) -> bool:
                return True

            def write(self, _value: str) -> int:
                raise OSError("terminal closed")

            def flush(self) -> None:
                raise OSError("terminal closed")

        renderer = main.ProgressRenderer(main.CrawlerStatus("example"), stream=Broken())
        renderer.render(force=True)
        renderer.finish()

    def test_global_catalog_deduplicates_canonical_posts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old = main.CONTENT_ROOT, main.NETWORK_ROOT, main.NEIGHBORHOODS_ROOT, main.APP_ROOT
            try:
                archive = Path(directory) / "Archive"
                main.CONTENT_ROOT = archive / "Content"
                main.NETWORK_ROOT = archive / "Network"
                main.NEIGHBORHOODS_ROOT = main.NETWORK_ROOT / "Neighborhoods"
                main.APP_ROOT = archive / "App"
                for blog, pid, timestamp in (("one", "1", 20), ("two", "1", 10)):
                    path = main.CONTENT_ROOT / blog / "json"
                    path.mkdir(parents=True)
                    (path / f"{pid}.json").write_text(json.dumps({"id": int(pid), "id_string": pid, "timestamp": timestamp}), encoding="utf-8")
                main.render_global_pages()
                catalog = json.loads((main.CONTENT_ROOT / "catalog.json").read_text(encoding="utf-8"))
                self.assertEqual([item["blog"] for item in catalog["blogs"]], ["one", "two"])
                # The default Feed is source-selected Affinity and may not
                # include unrelated blogs.  All Archive is the explicit
                # scope for asserting global chronological card order.
                feed = main.live_interface_page("feed.html?pov=__all__") or ""
                self.assertLess(feed.index('id="post-one-1-card"'), feed.index('id="post-two-1-card"'))
            finally:
                main.CONTENT_ROOT, main.NETWORK_ROOT, main.NEIGHBORHOODS_ROOT, main.APP_ROOT = old

    def test_acquisition_ledger_reconciles_from_source_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            old = main.CONTENT_ROOT
            try:
                main.CONTENT_ROOT = Path(directory) / "Backups"
                source = main.CONTENT_ROOT / "one" / "json"
                source.mkdir(parents=True)
                (source / "7.json").write_text(json.dumps({"id": 7, "id_string": "7"}), encoding="utf-8")
                recovered = main.reconcile_acquisition_ledger()
                self.assertIn(("one", "7"), recovered)
                (main.CONTENT_ROOT / "acquisition-ledger.jsonl").write_text("not-json\n", encoding="utf-8")
                recovered_again = main.reconcile_acquisition_ledger()
                self.assertIn(("one", "7"), recovered_again)
            finally:
                main.CONTENT_ROOT = old

    def test_presentation_graph_has_shared_shell_paths_and_hides_empty_blogs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_root = root / "Archive"
            old = (main.ARCHIVE_ROOT, main.CONTENT_ROOT, main.NETWORK_ROOT, main.NEIGHBORHOODS_ROOT, main.APP_ROOT, main.GLOBAL_CSS)
            try:
                main.ARCHIVE_ROOT = archive_root
                main.CONTENT_ROOT = archive_root / "Content"
                main.NETWORK_ROOT = archive_root / "Network"
                main.NEIGHBORHOODS_ROOT = main.NETWORK_ROOT / "Neighborhoods"
                main.APP_ROOT = archive_root / "App"
                main.GLOBAL_CSS = root / "global.css"
                main.GLOBAL_CSS.write_text("body { background: black; }", encoding="utf-8")

                for blog, post_id, rendered in (("target", "1", True), ("neighbor", "2", False)):
                    json_dir = main.CONTENT_ROOT / blog / "json"
                    json_dir.mkdir(parents=True, exist_ok=True)
                    (json_dir / f"{post_id}.json").write_text(json.dumps({
                        "id": int(post_id), "id_string": post_id, "timestamp": int(post_id),
                        "title": f"Post {post_id}", "tags": ["#Art", "مَرْحَبًا"],
                    }), encoding="utf-8")
                    if rendered:
                        posts = main.CONTENT_ROOT / blog / "posts"
                        posts.mkdir(parents=True, exist_ok=True)
                        (posts / f"{post_id}.html").write_text(
                            "<!doctype html><html><head><title>Post</title></head><body><article>Post</article></body></html>",
                            encoding="utf-8",
                        )
                (main.CONTENT_ROOT / "empty-blog" / "json").mkdir(parents=True)
                main.NEIGHBORHOODS_ROOT.mkdir(parents=True, exist_ok=True)
                (main.NEIGHBORHOODS_ROOT / "target").mkdir()
                (main.NEIGHBORHOODS_ROOT / "target" / "neighborhood.json").write_text(json.dumps({
                    "primary_blog": "target",
                    "blogs": [{"blog": "empty-blog", "distance": 1, "current_sample_size": 0, "observed_interaction_count": 1}],
                    "interactions": [],
                }), encoding="utf-8")
                main.regenerate_global_presentation(force=True)

                pages = [main.APP_ROOT / name for name in ("graph.html", "list.html", "feed.html", "tags.html", "neighborhoods.html")]
                for page in pages:
                    self.assertTrue(page.is_file(), page)
                    text = page.read_text(encoding="utf-8")
                    self.assertIn("class=\"archive-chrome\"", text)
                    self.assertIn("class=\"primary-navigation\" aria-label=\"Primary navigation\"", text)
                    self.assertNotIn("Backups/", text)
                    self.assertNotIn("Neighborhoods/index.html", text)
                listing = (main.APP_ROOT / "list.html").read_text(encoding="utf-8")
                self.assertIn("target", listing)
                self.assertIn("neighbor", listing)
                self.assertIn("empty-blog", listing)
                self.assertIn('data-label="Posts">0', listing)
            finally:
                (main.ARCHIVE_ROOT, main.CONTENT_ROOT, main.NETWORK_ROOT, main.NEIGHBORHOODS_ROOT, main.APP_ROOT, main.GLOBAL_CSS) = old

    def test_balanced_synthetic_crawl_uses_canonical_lanes_and_terminates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_backups, old_network, old_neighborhoods, old_app, old_css = (
                main.CONTENT_ROOT, main.NETWORK_ROOT, main.NEIGHBORHOODS_ROOT, main.APP_ROOT, main.GLOBAL_CSS
            )
            try:
                main.CONTENT_ROOT = root / "Archive" / "Content"
                main.NETWORK_ROOT = root / "Archive" / "Network"
                main.NEIGHBORHOODS_ROOT = main.NETWORK_ROOT / "Neighborhoods"
                main.APP_ROOT = root / "Archive" / "App"
                main.GLOBAL_CSS = root / "global.css"
                main.GLOBAL_CSS.write_text("body {}", encoding="utf-8")
                policy = main.load_context_policy()

                def record(blog: str, number: int, other: str | None = None) -> dict:
                    value = {"id": number, "type": "regular", "unix-timestamp": number, "date-gmt": "2026-01-01 00:00:00 GMT"}
                    if other:
                        value["reblogged-from-name"] = other
                        value["reblogged-from-url"] = f"https://{other}.tumblr.com/post/{number}"
                    return value

                feeds = {
                    "target.tumblr.com": [record("target", index, "neighbor-a" if index % 2 else "neighbor-b") for index in range(1, 31)],
                    "neighbor-a.tumblr.com": [record("neighbor-a", 100 + index, "outer-a") for index in range(1, 21)],
                    "neighbor-b.tumblr.com": [record("neighbor-b", 200 + index, "outer-b") for index in range(1, 21)],
                    "outer-a.tumblr.com": [record("outer-a", 300 + index) for index in range(1, 11)],
                    "outer-b.tumblr.com": [record("outer-b", 400 + index) for index in range(1, 11)],
                }

                def fetch(start: int, count: int = 50) -> dict:
                    posts = feeds.get(main.BLOG_HOST, [])
                    page = posts[start:start + count]
                    return {"posts": page, "posts-total": len(posts), "tumblelog": {"name": main.BLOG}}

                main.configure("target", 30, profile={
                    "id": "test", "label": "Test", "description": "", "feed_delay_seconds": 0.0,
                    "feed_jitter_seconds": 0.0, "media_workers": 1,
                })
                with (
                    mock.patch.object(main, "fetch_public_page", side_effect=fetch),
                    mock.patch.object(main, "process_batch", side_effect=lambda ids: len(ids)),
                    mock.patch.object(main, "ensure_blog_stylesheets"),
                ):
                    _, document = main.run_incremental_capture("target", 30, False, "explore", 2, policy, "balanced")
                main.regenerate_global_presentation(force=True)
                run_status = document["run_status"]
                self.assertEqual(run_status["budget_used"], 30)
                self.assertEqual(run_status["budget_used"], sum(run_status["saved_by_lane"].values()))
                self.assertGreater(run_status["saved_by_lane"]["target"], run_status["saved_by_lane"]["depth1"])
                self.assertGreater(run_status["saved_by_lane"]["depth1"], 0)
                self.assertTrue((main.CONTENT_ROOT / "target").is_dir())
                self.assertTrue((main.CONTENT_ROOT / "neighbor-a").is_dir() or (main.CONTENT_ROOT / "neighbor-b").is_dir())
                self.assertFalse((main.CONTENT_ROOT / "target" / "context" / "blogs").exists())
                self.assertTrue((main.APP_ROOT / "list.html").is_file())
                self.assertTrue((main.APP_ROOT / "feed.html").is_file())
                self.assertTrue((main.CONTENT_ROOT / "catalog.json").is_file())
                self.assertTrue((main.APP_ROOT / "assets").is_dir())
            finally:
                main.CONTENT_ROOT, main.NETWORK_ROOT, main.NEIGHBORHOODS_ROOT, main.APP_ROOT, main.GLOBAL_CSS = (
                    old_backups, old_network, old_neighborhoods, old_app, old_css
                )

    def test_context_identity_is_source_blog_plus_post_id(self) -> None:
        state = main.BlogState("source", Path("/tmp/source-context"), 0, role="context")
        self.assertEqual((state.blog, "123"), ("source", "123"))

    def test_context_state_uses_canonical_archive_root(self) -> None:
        item = {"blog": "Neighbor-A", "distance": 1}
        state = main.context_state("target", item)
        self.assertEqual(state.out, main.canonical_archive_root("neighbor-a"))
        self.assertNotIn("context", state.out.parts)

    def test_sparse_history_scout_seeks_and_refines_to_newest_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_backups = main.CONTENT_ROOT
            old_network = dict(main.NETWORK_PROFILE)
            try:
                main.CONTENT_ROOT = root / "Backups"
                main.configure("target", 300, profile={
                    "id": "test", "label": "Test", "description": "",
                    "feed_delay_seconds": 0.0, "feed_jitter_seconds": 0.0,
                    "media_workers": 1,
                })
                posts = [
                    {"id": 100000 - index, "type": "regular", "unix-timestamp": 200000 - index}
                    for index in range(1000)
                ]
                json_dir = main.canonical_archive_root("target") / "json"
                json_dir.mkdir(parents=True)
                for source in posts[:500]:
                    (json_dir / f"{source['id']}.json").write_text("{}", encoding="utf-8")
                calls: list[int] = []

                def fetch(start: int, count: int = 50) -> dict:
                    calls.append(start)
                    return {
                        "posts": posts[start:start + count],
                        "posts-total": len(posts),
                        "tumblelog": {"name": "target"},
                    }

                state = main.BlogState("target", main.canonical_archive_root("target"), 300)
                state.run_id = "synthetic-gap"
                with mock.patch.object(main, "fetch_public_page", side_effect=fetch):
                    result = main.scout_blog_page(state, "target", {"scout_interactions": [], "adjacency_observations": []})

                self.assertEqual(calls[:6], [0, 50, 100, 200, 400, 800])
                self.assertIn(500, calls[6:])
                self.assertEqual(result.frontier["post_id"], str(posts[500]["id"]))
                self.assertEqual(state.scout_frontier["timestamp"], posts[500]["unix-timestamp"])
                self.assertEqual(str(state.feed_buffer[0]["id"]), str(posts[500]["id"]))
                self.assertEqual(state.feed_start, 550)
                persisted = json.loads((main.canonical_archive_root("target") / "scout" / "state.json").read_text(encoding="utf-8"))
                self.assertNotIn("feed_start", persisted)
                self.assertEqual(persisted["frontier"]["post_id"], str(posts[500]["id"]))
                self.assertTrue(persisted["verified_complete_anchors"])

                candidate = main.ActionCandidate(
                    identity="acquire:target:frontier", kind="acquire_target_post", resource_class="acquisition",
                    lane="target", graph_depth=0, blog="target", consumes_budget=True,
                )
                handed_off: list[str] = []
                state.batch_limit = 1
                with mock.patch.object(
                    main,
                    "_process_source_ids",
                    side_effect=lambda _state, sources, candidate=None: handed_off.extend(str(item["id"]) for item in sources) or 1,
                ):
                    self.assertTrue(main.acquire_one_target_batch(state, candidate))
                self.assertEqual(handed_off, [str(posts[500]["id"])])
            finally:
                main.CONTENT_ROOT = old_backups
                main.NETWORK_PROFILE = old_network

    def test_scout_handles_small_recent_island_without_declaring_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_backups = main.CONTENT_ROOT
            old_network = dict(main.NETWORK_PROFILE)
            try:
                main.CONTENT_ROOT = root / "Backups"
                main.configure("target", 30, profile={
                    "id": "test", "label": "Test", "description": "",
                    "feed_delay_seconds": 0.0, "feed_jitter_seconds": 0.0,
                    "media_workers": 1,
                })
                posts = [{"id": 200000 - index, "type": "regular"} for index in range(200)]
                json_dir = main.canonical_archive_root("target") / "json"
                json_dir.mkdir(parents=True)
                for source in posts[:26]:
                    (json_dir / f"{source['id']}.json").write_text("{}", encoding="utf-8")

                with mock.patch.object(main, "fetch_public_page", return_value={
                    "posts": posts[:50], "posts-total": len(posts), "tumblelog": {"name": "target"},
                }):
                    state = main.BlogState("target", main.canonical_archive_root("target"), 30)
                    result = main.scout_blog_page(state, "target", {"scout_interactions": [], "adjacency_observations": []})

                self.assertEqual(result.frontier["post_id"], str(posts[26]["id"]))
                self.assertFalse(state.exhausted)
                self.assertFalse(state.scout_horizon_reached)
            finally:
                main.CONTENT_ROOT = old_backups
                main.NETWORK_PROFILE = old_network

    def test_target_acquisition_skips_a_later_present_island(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_backups = main.CONTENT_ROOT
            try:
                main.CONTENT_ROOT = root / "Backups"
                state = main.BlogState("target", main.canonical_archive_root("target"), 100)
                state.feed_buffer = [{"id": 1}, {"id": 2}, {"id": 3}]
                state.feed_total = 3
                state.batch_limit = 2
                json_dir = state.json_dir
                json_dir.mkdir(parents=True)
                (json_dir / "2.json").write_text("{}", encoding="utf-8")
                candidate = main.ActionCandidate(
                    identity="acquire:target:next", kind="acquire_target_post", resource_class="acquisition",
                    lane="target", graph_depth=0, blog="target", consumes_budget=True,
                )
                acquired: list[str] = []
                with mock.patch.object(main, "_process_source_ids", side_effect=lambda _state, sources, candidate=None: acquired.extend(str(item["id"]) for item in sources) or 1):
                    self.assertTrue(main.acquire_one_target_batch(state, candidate))
                self.assertEqual(acquired, ["1", "3"])
            finally:
                main.CONTENT_ROOT = old_backups

    def test_resumed_run_repairs_target_before_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_backups = main.CONTENT_ROOT
            old_neighborhoods = main.NEIGHBORHOODS_ROOT
            old_observations = main.OBSERVATIONS_ROOT
            old_css = main.GLOBAL_CSS
            try:
                main.CONTENT_ROOT = root / "Backups"
                main.NEIGHBORHOODS_ROOT = root / "Neighborhoods"
                main.OBSERVATIONS_ROOT = root / "Observations"
                main.GLOBAL_CSS = root / "global.css"
                main.GLOBAL_CSS.write_text("body {}", encoding="utf-8")
                context_root = main.OBSERVATIONS_ROOT / "target"
                context_root.mkdir(parents=True)
                (context_root / "context.json").write_text(json.dumps({
                    "schema_version": 1,
                    "primary_blog": "target",
                    "blogs": [{
                        "blog": "neighbor",
                        "distance": 1,
                        "status": "partial",
                        "configured_cap": 10,
                        "current_sample_size": 1,
                        "sampled_post_ids": ["1"],
                    }],
                    "interactions": [],
                    "pending": ["neighbor"],
                }), encoding="utf-8")
                main.configure("target", 100, profile={
                    "id": "test", "label": "Test", "description": "",
                    "feed_delay_seconds": 0.0, "feed_jitter_seconds": 0.0,
                    "media_workers": 1,
                })
                order = []

                def repair() -> int:
                    order.append(f"repair:{main.BLOG}")
                    return 0

                def target_batch(state: main.BlogState, candidate=None) -> bool:
                    order.append("batch:target")
                    state.exhausted = True
                    return False

                def context_batch(state: main.BlogState, target_size: int, candidate=None) -> bool:
                    order.append(f"batch:{state.blog}")
                    state.exhausted = True
                    return False

                def scout(state: main.BlogState, primary: str, document: dict) -> main.ScoutResult:
                    state.scouted += 1
                    state.feed_buffer.append({"id": state.scouted, "type": "regular"})
                    return main.ScoutResult(state.blog, observed=1, exhausted=False)

                policy = main.load_context_policy()
                with (
                    mock.patch.object(main, "ensure_blog_stylesheets"),
                    mock.patch.object(main, "repair_interrupted_work", side_effect=repair),
                    mock.patch.object(main, "scout_blog_page", side_effect=scout),
                    mock.patch.object(main, "acquire_one_target_batch", side_effect=target_batch),
                    mock.patch.object(main, "acquire_one_context_batch", side_effect=context_batch),
                    mock.patch.object(main, "save_context_document"),
                ):
                    main.run_incremental_capture("target", 100, False, "nearby", None, policy)
                self.assertLess(order.index("repair:target"), order.index("repair:neighbor"))
                self.assertLess(order.index("repair:neighbor"), order.index("batch:neighbor"))
            finally:
                main.CONTENT_ROOT = old_backups
                main.NEIGHBORHOODS_ROOT = old_neighborhoods
                main.OBSERVATIONS_ROOT = old_observations
                main.GLOBAL_CSS = old_css


class TerminalOutputTests(unittest.TestCase):
    def test_tumblr_backup_chatter_is_captured_unless_verbose(self) -> None:
        old_verbose = main.VERBOSE
        try:
            main.VERBOSE = False
            main.configure("example", 10, profile={
                "id": "test", "label": "Test", "description": "",
                "feed_delay_seconds": 0.0, "feed_jitter_seconds": 0.0, "media_workers": 1,
            })
            completed = mock.Mock(returncode=0, stdout="example: Getting basic information\n", stderr="")
            with (
                mock.patch.object(main, "ensure_tumblr_backup"),
                mock.patch.object(main, "tumblr_backup_cli", return_value=Path("/bin/true")),
                mock.patch.object(main.subprocess, "run", return_value=completed) as run,
            ):
                main.run_tumblr_backup([], new_ids=[1])
            self.assertTrue(run.call_args.kwargs["capture_output"])
            main.VERBOSE = True
            with (
                mock.patch.object(main, "ensure_tumblr_backup"),
                mock.patch.object(main, "tumblr_backup_cli", return_value=Path("/bin/true")),
                mock.patch.object(main.subprocess, "run", return_value=completed) as verbose_run,
            ):
                main.run_tumblr_backup([], new_ids=[1])
            self.assertNotIn("capture_output", verbose_run.call_args.kwargs)
        finally:
            main.VERBOSE = old_verbose

    def test_localhost_handler_suppresses_routine_access_logs(self) -> None:
        import contextlib
        import io
        path = Path(__file__).parents[1] / "Tumblr Scraper - Android.py"
        spec = importlib.util.spec_from_file_location("android_tumblr_scraper_logs", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            module.ArchiveRequestHandler.log_message(None, "%s", "GET /index.html")
        self.assertEqual(stream.getvalue(), "")


class LiveControlTests(unittest.TestCase):
    def test_browser_and_terminal_requests_build_the_same_configuration(self) -> None:
        browser = main.build_crawl_request(
            "Example",
            max_posts=300,
            context="explore",
            context_depth=2,
            focus="balanced",
            profile_id="gentle",
            full_res=False,
        )
        terminal = main.build_crawl_request(
            "example",
            max_posts=300,
            context="explore",
            context_depth=2,
            focus="balanced",
            profile_id="gentle",
            full_res=False,
        )
        self.assertEqual(browser, terminal)
        self.assertEqual(browser.argv(), terminal.argv())

    def test_application_starts_idle_and_rejects_duplicate_active_crawls(self) -> None:
        import threading

        started = threading.Event()
        release = threading.Event()
        old_main = main.main

        def fake_main(_argv: list[str], *, install_signal_handlers: bool = True) -> int:
            self.assertFalse(install_signal_handlers)
            started.set()
            release.wait(timeout=2)
            return 0

        try:
            main.main = fake_main
            application = main.CrawlerApplication()
            self.assertEqual(application.snapshot()["lifecycle"], "idle")
            application.start({"target": "example", "profile_id": "gentle"})
            self.assertTrue(started.wait(timeout=2))
            with self.assertRaises(main.PolicyError):
                application.start({"target": "example", "profile_id": "gentle"})
            release.set()
            assert application.worker is not None
            application.worker.join(timeout=2)
            self.assertEqual(application.snapshot()["lifecycle"], "complete")
        finally:
            release.set()
            main.main = old_main

    def test_browser_controls_reuse_runtime_and_record_source(self) -> None:
        old_runtime, old_status = main.ACTIVE_RUNTIME, main.ACTIVE_STATUS
        try:
            status = main.CrawlerStatus("example", budget_limit=100)
            runtime = main.RuntimeControls(0.5, 100)
            status.runtime = runtime
            main.ACTIVE_RUNTIME, main.ACTIVE_STATUS = runtime, status
            snapshot = main.apply_runtime_control("focus", "neighbors", source="browser")
            self.assertEqual(runtime, status.runtime)
            self.assertEqual(runtime.focus_bias, 1.2)
            self.assertEqual(snapshot["focus"], "neighbors")
            self.assertEqual(runtime.changes[-1]["source"], "browser")
            main.apply_runtime_control("cancel_requested", True, source="browser")
            self.assertTrue(runtime.cancel_requested)
        finally:
            main.ACTIVE_RUNTIME, main.ACTIVE_STATUS = old_runtime, old_status

    def test_invalid_browser_control_does_not_mutate_runtime(self) -> None:
        old_runtime, old_status = main.ACTIVE_RUNTIME, main.ACTIVE_STATUS
        try:
            status = main.CrawlerStatus("example", budget_limit=100)
            runtime = main.RuntimeControls(0.5, 100)
            status.runtime = runtime
            main.ACTIVE_RUNTIME, main.ACTIVE_STATUS = runtime, status
            with self.assertRaises(main.PolicyError):
                main.apply_runtime_control("focus", "invalid", source="browser")
            with self.assertRaises(main.PolicyError):
                main.apply_runtime_control("budget_limit", -1, source="browser")
            self.assertEqual(runtime.focus_bias, 0.5)
            self.assertEqual(runtime.changes, [])
        finally:
            main.ACTIVE_RUNTIME, main.ACTIVE_STATUS = old_runtime, old_status


class FailureRecoveryTests(unittest.TestCase):
    def test_failed_neighbor_is_skipped_and_next_neighbor_writes_canonical_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_backups = main.CONTENT_ROOT
            old_neighborhoods = main.NEIGHBORHOODS_ROOT
            old_css = main.GLOBAL_CSS
            main.CONTENT_ROOT = root / "Backups"
            main.NEIGHBORHOODS_ROOT = root / "Neighborhoods"
            main.GLOBAL_CSS = root / "global.css"
            main.GLOBAL_CSS.write_text("body {}", encoding="utf-8")

            def record(number: int, other: str | None = None) -> dict:
                value = {
                    "id": number,
                    "type": "regular",
                    "unix-timestamp": number,
                    "date-gmt": "2026-01-01 00:00:00 GMT",
                }
                if other:
                    value["reblogged-from-name"] = other
                    value["reblogged-from-url"] = f"https://{other}.tumblr.com/post/{number}"
                return value

            target_posts = [record(index, "dead-blog" if index < 10 else "live-blog") for index in range(1, 15)]
            live_posts = [record(100 + index) for index in range(1, 10)]
            calls: list[str] = []

            def fetch(start: int, count: int = 50) -> dict:
                calls.append(main.BLOG_HOST)
                if main.BLOG_HOST == "dead-blog.tumblr.com":
                    raise main.BlogSourceFailure(
                        "dead-blog", "feed", "Tumblr returned HTTP 404 while reading dead-blog.tumblr.com",
                        code=404, kind="unavailable", retryable=False,
                    )
                posts = target_posts if main.BLOG_HOST == "target.tumblr.com" else live_posts
                return {"posts": posts[start:start + count], "posts-total": len(posts), "tumblelog": {"name": main.BLOG}}

            main.configure("target", 12, profile={
                "id": "test", "label": "Test", "description": "",
                "feed_delay_seconds": 0.0, "feed_jitter_seconds": 0.0, "media_workers": 1,
            })
            policy = main.load_context_policy()
            try:
                with (
                    mock.patch.object(main, "fetch_public_page", side_effect=fetch),
                    mock.patch.object(main, "process_batch", side_effect=lambda ids: len(ids)),
                    mock.patch.object(main, "ensure_blog_stylesheets"),
                    mock.patch.object(main, "repair_interrupted_work", return_value=0),
                    mock.patch.object(main.ProgressRenderer, "render"),
                ):
                    _, document = main.run_incremental_capture(
                        "target", 12, False, "explore", 1, policy, "wide"
                    )

                dead = next(item for item in document["blogs"] if item["blog"] == "dead-blog")
                live_json = list((main.CONTENT_ROOT / "live-blog" / "json").glob("*.json"))
                ledger_path = main.CONTENT_ROOT / "acquisition-ledger.jsonl"
                ledger = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
                self.assertTrue(dead["run_blocked"])
                self.assertEqual(dead["failure_details"]["code"], 404)
                self.assertGreater(len(live_json), 0)
                self.assertIn("dead-blog.tumblr.com", calls)
                self.assertIn("live-blog.tumblr.com", calls)
                self.assertEqual(document["crawl_status"], "complete_with_warnings")
                self.assertEqual(document["run_status"]["budget_used"], len(ledger))
                self.assertTrue(any(item["blog"] == "dead-blog" for item in document["run_status"]["source_failures"]))
            finally:
                main.CONTENT_ROOT = old_backups
                main.NEIGHBORHOODS_ROOT = old_neighborhoods
                main.GLOBAL_CSS = old_css


class AndroidLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        path = Path(__file__).parents[1] / "Tumblr Scraper - Android.py"
        spec = importlib.util.spec_from_file_location("android_tumblr_scraper", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.module = module

    def test_scraper_command_passes_only_profile_id(self) -> None:
        with mock.patch.object(self.module.subprocess, "run") as run:
            run.return_value.returncode = 1
            result = self.module.run_scraper("example", 300, False, "normal")
        self.assertEqual(result, 1)
        command = run.call_args.args[0]
        self.assertEqual(command[-2:], ["--profile", "normal"])
        self.assertNotIn("--threads", command)


if __name__ == "__main__":
    unittest.main()
