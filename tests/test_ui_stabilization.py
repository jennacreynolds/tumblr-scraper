from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

import graph_projection
import main


ROOT = Path(__file__).resolve().parents[1]


class UiStabilizationTests(unittest.TestCase):
    def test_static_asset_references_resolve_inside_generated_app_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Archive" / "default" / "App"
            (app / "assets").mkdir(parents=True)
            (app / "assets" / "archive.css").write_text("css", encoding="utf-8")
            (app / "assets" / "archive.js").write_text("js", encoding="utf-8")
            page = app / "feed.html"
            source = '<link rel="stylesheet" href="/assets/archive.css"><script src="/assets/archive.js"></script>'
            with patch.object(main, "APP_ROOT", app):
                rendered = main._staticize_html(source, page)
            self.assertIn('href="assets/archive.css"', rendered)
            self.assertIn('src="assets/archive.js"', rendered)
            for relative in ("assets/archive.css", "assets/archive.js"):
                self.assertTrue((page.parent / relative).is_file())

    def test_shared_shell_has_bounded_surface_tokens_and_fixed_drawer_contract(self) -> None:
        css = (ROOT / "assets" / "archive.css").read_text(encoding="utf-8")
        presentation = (ROOT / "src" / "tumblr_scraper" / "presentation.py").read_text(encoding="utf-8")
        self.assertIn("--tool-max-width", css)
        self.assertIn("--reader-max-width", css)
        self.assertIn("--compact-max-width", css)
        self.assertIn("position: fixed", css)
        self.assertIn('class="primary-navigation" aria-label="Primary navigation"', presentation)
        self.assertIn('<details class="primary-tools"><summary>Tools</summary>', presentation)
        self.assertNotIn('class="app-menu-toggle"', presentation)
        self.assertIn('class="reader-settings-global"', presentation)
        self.assertNotIn('class="app-nav-group"><h2>Settings</h2>', presentation)
        self.assertIn('render_links(archive_links)', presentation)
        self.assertNotIn('<h2>Read</h2>', presentation)
        main_source = (ROOT / "src" / "tumblr_scraper" / "main.py").read_text(encoding="utf-8")
        self.assertIn("def render_static_archive", main_source)
        self.assertIn("source-owned presentation", main_source)

    def test_graph_toolbar_keeps_actions_and_display_link_with_graph_view(self) -> None:
        content = graph_projection.render_graph_content({
            "edges": [],
            "format": "tumblr-archive-graph",
            "generated_at": "now",
            "generator_version": 1,
            "nodes": [],
            "relationship_semantics": {},
            "source_summary": {},
            "targets": [],
            "version": 1,
            "warnings": [],
        })
        self.assertIn('class="context-toolbar explore-toolbar shared-explore-toolbar graph-explore-toolbar"', content)
        self.assertIn('id="graph-fit"', content)
        self.assertIn('id="graph-freeze"', content)
        self.assertNotIn('>Display<', content)
        self.assertIn('id="graph-canvas"', content)
        self.assertIn('class="graph-workspace-toolbar"', content)
        self.assertIn('class="graph-inspector graph-inspector-empty"', content)

    def test_list_tags_and_neighborhood_markup_has_responsive_hooks(self) -> None:
        source = (ROOT / "src" / "tumblr_scraper" / "main.py").read_text(encoding="utf-8")
        self.assertIn('data-label="Blog"', source)
        self.assertIn('class="tag-card"', source)
        self.assertIn('data-tag-row', source)
        self.assertIn('class="neighborhood-card"', source)
        self.assertIn('class="index-grid tag-grid"', source)
        self.assertIn('class="index-grid neighborhood-grid"', source)
        self.assertIn('data-tag-count', source)
        self.assertIn('tag-visibility', source)
        self.assertIn('tag-sort', source)
        self.assertIn('list-neighborhood', source)
        self.assertIn('list-distance', source)

    def test_collection_table_lets_content_use_available_width(self) -> None:
        css = (ROOT / "assets" / "archive.css").read_text(encoding="utf-8")
        self.assertIn(".collection-table { table-layout: auto; }", css)
        self.assertIn("nth-child(2), .collection-table td:nth-child(2) { width: 12%;", css)
        self.assertIn("white-space: nowrap", css)
        self.assertNotIn(".collection-table { table-layout: fixed; }", css)

    def test_tool_width_override_does_not_include_reader_width_control(self) -> None:
        css = (ROOT / "assets" / "archive.css").read_text(encoding="utf-8")
        tool_rule = "main.reader-content:has(.graph-view), main.reader-content:has(.explore-page), main.reader-content:has(.crawler-page)"
        self.assertIn(tool_rule, css)
        self.assertIn("main.reader-content:has(.compact-index)", css)
        self.assertIn("--reader-width", css)

    def test_avatar_fallback_states_explain_missing_participant_images(self) -> None:
        main_source = (ROOT / "src" / "tumblr_scraper" / "main.py").read_text(encoding="utf-8")
        graph_source = (ROOT / "src" / "tumblr_scraper" / "graph_projection.py").read_text(encoding="utf-8")
        css = (ROOT / "assets" / "archive.css").read_text(encoding="utf-8")
        js = (ROOT / "assets" / "graph-view.js").read_text(encoding="utf-8")
        self.assertIn('"restricted"', main_source)
        self.assertIn('http_status=exc.code', main_source)
        self.assertIn('avatar_status', graph_source)
        self.assertIn('avatar-state-restricted', css)
        self.assertIn('Profile image hidden by Tumblr', js)

    def test_crawler_mobile_flow_and_dirty_preset_state(self) -> None:
        css = (ROOT / "assets" / "archive.css").read_text(encoding="utf-8")
        script = (ROOT / "assets" / "archive.js").read_text(encoding="utf-8")
        self.assertIn('grid-template-areas: "metrics" "budget" "saved" "curve"', css)
        self.assertIn("var policyDirty = false;", script)
        self.assertIn("if (!policyDirty || newRun)", script)

    def test_live_archive_views_restore_rich_presentation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "Archive"
            backups = archive / "Content"
            blog = backups / "alpha" / "json"
            blog.mkdir(parents=True)
            (blog / "1.json").write_text(json.dumps({
                "id": 1,
                "id_string": "1",
                "timestamp": 1700000000,
                "date": "2023-11-14",
                "title": "Archived post",
                "post_url": "https://alpha.tumblr.com/post/1",
                "tags": ["long tag", "RTL שלום"],
                "body": "<p>Canonical JSON body</p>",
            }), encoding="utf-8")
            posts = blog.parent / "posts"
            posts.mkdir()
            (posts / "1.html").write_text('<article><p>Archived post body</p></article>', encoding="utf-8")
            neighborhoods = archive / "Network" / "Neighborhoods" / "set-opaque-test"
            neighborhoods.mkdir(parents=True)
            (archive / "Network" / "Neighborhoods" / "set-empty").mkdir()
            (neighborhoods / "neighborhood.json").write_text(json.dumps({
                "primary_blog": "alpha",
                "blogs": [{"blog": "beta", "distance": 1, "current_sample_size": 2, "observed_interaction_count": 3}],
                "interactions": [{"from_blog": "alpha", "to_blog": "beta"}],
            }), encoding="utf-8")
            with patch.object(main, "ARCHIVE_ROOT", archive), patch.object(main, "CONTENT_ROOT", backups), patch.object(main, "NETWORK_ROOT", archive / "Network"), patch.object(main, "NEIGHBORHOODS_ROOT", archive / "Network" / "Neighborhoods"), patch.object(main, "APP_ROOT", archive / "App"):
                feed = main.live_interface_page("feed.html") or ""
                tags = main.live_interface_page("tags.html") or ""
                tag_id = main.build_tag_index()["tags"][0]["page_id"]
                tag_page = main.live_interface_page(f"tags.html?tag={tag_id}") or ""
                neighborhoods_page = main.live_interface_page("neighborhoods.html") or ""
                detail = main.live_interface_page("neighborhoods.html?target=alpha") or ""
                listing = main.live_interface_page("list.html") or ""
            self.assertIn('class="post-card"', feed)
            self.assertIn("Archived post", feed)
            self.assertIn("Archived post body", feed)
            self.assertNotIn("Newest 100 canonical posts.</p><ol>", feed)
            self.assertIn('class="tag-card"', tags)
            self.assertIn("/app/tags.html?tag=", tags)
            self.assertIn("Archived post body", tag_page)
            self.assertIn("alpha", neighborhoods_page)
            self.assertNotIn("set-opaque-test", neighborhoods_page)
            self.assertIn("beta", detail)
            self.assertIn('data-label="Captured range"', listing)
            self.assertIn('href="/app/graph.html?blog=alpha"', listing)

    def test_live_blog_view_renders_cards_and_canonical_body_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "Archive"
            content = archive / "Content" / "alpha" / "json"
            content.mkdir(parents=True)
            (content / "1.json").write_text(json.dumps({
                "id": 1,
                "id_string": "1",
                "timestamp": 1700000000,
                "date": "2023-11-14",
                "body": "<p>Body preserved in canonical JSON</p>",
                "post_url": "https://alpha.tumblr.com/post/1",
            }), encoding="utf-8")
            with patch.object(main, "ARCHIVE_ROOT", archive), patch.object(main, "CONTENT_ROOT", archive / "Content"), patch.object(main, "NETWORK_ROOT", archive / "Network"), patch.object(main, "NEIGHBORHOODS_ROOT", archive / "Network" / "Neighborhoods"), patch.object(main, "APP_ROOT", archive / "App"):
                page = main.live_interface_page("blog.html?blog=alpha") or ""
            self.assertIn('class="post-card"', page)
            self.assertIn("Body preserved in canonical JSON", page)
            self.assertNotIn("<ol>", page)

    def test_feed_defaults_to_highest_coverage_pov_and_local_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "Archive" / "default"
            content = archive / "Content"
            network = archive / "Network"
            old = (main.ARCHIVE_ROOT, main.CONTENT_ROOT, main.NETWORK_ROOT,
                   main.NEIGHBORHOODS_ROOT, main.OBSERVATIONS_ROOT, main.APP_ROOT)
            try:
                main.ARCHIVE_ROOT = archive
                main.CONTENT_ROOT = content
                main.NETWORK_ROOT = network
                main.NEIGHBORHOODS_ROOT = network / "Neighborhoods"
                main.OBSERVATIONS_ROOT = network / "Observations"
                main.APP_ROOT = archive / "App"
                for blog, records in {
                    "alpha": [
                        {"id": 1, "id_string": "1", "timestamp": 20, "body": "Alpha body", "tags": ["alpha"]},
                        {"id": 2, "id_string": "2", "timestamp": 10, "body": "Older alpha body"},
                    ],
                    "beta": [{"id": 3, "id_string": "3", "timestamp": 30, "body": "Beta body"}],
                }.items():
                    json_root = content / blog / "json"
                    posts_root = content / blog / "posts"
                    profile_root = content / blog / "profile"
                    json_root.mkdir(parents=True)
                    posts_root.mkdir()
                    profile_root.mkdir()
                    for record in records:
                        (json_root / f'{record["id_string"]}.json').write_text(json.dumps(record), encoding="utf-8")
                        body = record["body"]
                        if blog == "alpha" and record["id_string"] == "1":
                            (content / blog / "media").mkdir(exist_ok=True)
                            (content / blog / "media" / "photo.jpg").write_bytes(b"jpg")
                            body += '<img src="../media/photo.jpg" alt="local photo">'
                        (posts_root / f'{record["id_string"]}.html').write_text(
                            f'<article><p>{body}</p></article>', encoding="utf-8"
                        )
                    if blog == "alpha":
                        (profile_root / "avatar.png").write_bytes(b"png")
                neighborhoods = main.NEIGHBORHOODS_ROOT / "alpha"
                neighborhoods.mkdir(parents=True)
                (neighborhoods / "neighborhood.json").write_text(json.dumps({
                    "primary_blog": "alpha",
                    "blogs": [{"blog": "beta", "distance": 1}],
                    "interactions": [{
                        "from_blog": "alpha", "to_blog": "beta", "kind": "direct_reblog",
                        "source_blog": "alpha", "source_post_id": "1", "reference_url": "alpha/1",
                    }],
                }), encoding="utf-8")
                page = main.live_interface_page("feed.html") or ""
                all_page = main.live_interface_page("feed.html?pov=__all__") or ""
            finally:
                (main.ARCHIVE_ROOT, main.CONTENT_ROOT, main.NETWORK_ROOT,
                 main.NEIGHBORHOODS_ROOT, main.OBSERVATIONS_ROOT, main.APP_ROOT) = old
            self.assertIn('class="primary-navigation"', page)
            self.assertIn('class="archive-selection"', page)
            self.assertIn('class="explore-filter-disclosure', page)
            self.assertIn('id="feed-pov"', page)
            self.assertIn('value="alpha" selected', page)
            self.assertNotIn('value="beta" selected', page)
            self.assertIn('value="__all__"', page)
            self.assertIn('id="feed-include-target" type="checkbox">', page)
            self.assertNotIn("Alpha body", page)
            self.assertIn("Alpha body", all_page)
            self.assertIn("/Archive/default/Content/alpha/media/photo.jpg", all_page)
            self.assertIn("/Archive/default/Content/alpha/profile/avatar.png", all_page)
            self.assertIn('value="__all__" selected', all_page)

    def test_feed_affinity_universe_keeps_old_qualifying_post(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "Archive" / "default"
            content = archive / "Content"
            network = archive / "Network"
            old = (main.ARCHIVE_ROOT, main.CONTENT_ROOT, main.NETWORK_ROOT,
                   main.NEIGHBORHOODS_ROOT, main.OBSERVATIONS_ROOT, main.APP_ROOT)
            try:
                main.ARCHIVE_ROOT = archive
                main.CONTENT_ROOT = content
                main.NETWORK_ROOT = network
                main.NEIGHBORHOODS_ROOT = network / "Neighborhoods"
                main.OBSERVATIONS_ROOT = network / "Observations"
                main.APP_ROOT = archive / "App"
                records = {
                    "a": [],
                    "b": [{"id": 1, "id_string": "1", "timestamp": 1, "body": "Old qualifying B post", "post_url": "https://b.tumblr.com/post/1"}],
                    "c": [],
                }
                for index in range(130):
                    record = {"id": index, "id_string": str(index), "timestamp": 1000 + index, "body": "A post"}
                    if index == 0:
                        record.update({
                            "post_url": "https://a.tumblr.com/post/0",
                            "reblogged_from_name": "b",
                            "reblogged_from_url": "https://b.tumblr.com/post/1",
                        })
                    records["a"].append(record)
                records["c"] = [
                    {"id": index, "id_string": str(index), "timestamp": 2000 + index, "body": "New unrelated C post"}
                    for index in range(120)
                ]
                for blog, blog_records in records.items():
                    root = content / blog / "json"
                    root.mkdir(parents=True)
                    for record in blog_records:
                        (root / f'{record["id_string"]}.json').write_text(json.dumps(record), encoding="utf-8")
                page = main.live_interface_page("feed.html") or ""
            finally:
                (main.ARCHIVE_ROOT, main.CONTENT_ROOT, main.NETWORK_ROOT,
                 main.NEIGHBORHOODS_ROOT, main.OBSERVATIONS_ROOT, main.APP_ROOT) = old
            self.assertEqual(page.count('data-feed-blog="b"'), 1)
            self.assertEqual(page.count('data-feed-blog="a"'), 0)
            self.assertNotIn('data-feed-blog="c"', page)
            self.assertIn('data-feed-universe-size="unknown"', page)
            self.assertIn("Old qualifying B post", page)
            self.assertIn('"eligible_blogs":["b"]', page)
            self.assertNotIn('"eligible_blogs":["b","c"]', page)

    def test_feed_windows_are_bounded_and_cursors_pin_growth_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "Archive" / "default"
            content = archive / "Content" / "alpha" / "json"
            content.mkdir(parents=True)
            old = (main.ARCHIVE_ROOT, main.CONTENT_ROOT, main.NETWORK_ROOT,
                   main.NEIGHBORHOODS_ROOT, main.OBSERVATIONS_ROOT, main.APP_ROOT)
            try:
                main.ARCHIVE_ROOT = archive
                main.CONTENT_ROOT = archive / "Content"
                main.NETWORK_ROOT = archive / "Network"
                main.NEIGHBORHOODS_ROOT = main.NETWORK_ROOT / "Neighborhoods"
                main.OBSERVATIONS_ROOT = main.NETWORK_ROOT / "Observations"
                main.APP_ROOT = archive / "App"
                main._FEED_METADATA_CACHE.clear()
                main._FEED_SNAPSHOT_CACHE.clear()
                for index in range(120):
                    (content / f"{index}.json").write_text(json.dumps({
                        "id": index, "id_string": str(index), "timestamp": index,
                        "body": f"Body {index}",
                    }), encoding="utf-8")
                query = main.FeedQuery(mode="all", pov="__all__", window_size=50)
                first = main.build_feed_window(query)
                self.assertEqual(len(first.records), 50)
                self.assertIsNotNone(first.next_cursor)
                self.assertEqual(first.rich_records_loaded, 50)
                cursor = first.next_cursor
                (content / "120.json").write_text(json.dumps({
                    "id": 120, "id_string": "120", "timestamp": 120,
                    "body": "New material",
                }), encoding="utf-8")
                pinned = main.build_feed_window(query, cursor)
                self.assertEqual(len(pinned.records), 50)
                self.assertNotIn("New material", " ".join(str(item.get("body")) for item in pinned.records))
                fresh = main.build_feed_window(query)
                self.assertEqual(len(fresh.records), 50)
                self.assertEqual(fresh.records[0].get("id_string"), "120")
            finally:
                (main.ARCHIVE_ROOT, main.CONTENT_ROOT, main.NETWORK_ROOT,
                 main.NEIGHBORHOODS_ROOT, main.OBSERVATIONS_ROOT, main.APP_ROOT) = old
                main._FEED_METADATA_CACHE.clear()
                main._FEED_SNAPSHOT_CACHE.clear()


if __name__ == "__main__":
    unittest.main()
