from __future__ import annotations

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import graph_projection


class GraphProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory(prefix="Tumblr Graph Test ")
        self.root = Path(self.temp.name)
        self.backups = self.root / "Backups With Spaces"
        self.neighborhoods = self.root / "Neighborhoods With Spaces"
        self.backups.mkdir()
        self.neighborhoods.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_neighborhood(self, target: str, document: dict) -> None:
        path = self.neighborhoods / target
        path.mkdir()
        (path / "neighborhood.json").write_text(json.dumps(document), encoding="utf-8")

    def test_deduplicates_nodes_and_preserves_directional_reciprocal_edges(self) -> None:
        (self.backups / "alpha" / "json").mkdir(parents=True)
        (self.backups / "alpha" / "json" / "1.json").write_text("{}", encoding="utf-8")
        (self.backups / "shared" / "json").mkdir(parents=True)
        (self.backups / "shared" / "json" / "1.json").write_text("{}", encoding="utf-8")
        common = {
            "blogs": [{"blog": "shared", "distance": 1, "status": "partial"}],
            "scout_interactions": [], "adjacency_observations": [],
        }
        self.write_neighborhood("alpha", {
            "primary_blog": "alpha", **common,
            "interactions": [{"from_blog": "alpha", "to_blog": "shared", "kind": "direct_reblog", "source_blog": "alpha", "source_post_id": "1", "source_post_url": "a", "direction_known": True}],
        })
        self.write_neighborhood("beta", {
            "primary_blog": "beta", "blogs": [{"blog": "shared", "distance": 1, "status": "partial"}],
            "scout_interactions": [], "adjacency_observations": [],
            "interactions": [{"from_blog": "shared", "to_blog": "alpha", "kind": "direct_reblog", "source_blog": "shared", "source_post_id": "2", "source_post_url": "b", "direction_known": True}],
        })
        data = graph_projection.build_graph_projection(self.backups, self.neighborhoods)
        self.assertEqual([node["id"] for node in data["nodes"]], ["alpha", "beta", "shared"])
        self.assertEqual(len(data["edges"]), 2)
        self.assertTrue(all(edge["reciprocal"] for edge in data["edges"]))
        shared = next(node for node in data["nodes"] if node["id"] == "shared")
        self.assertEqual(set(shared["neighborhoods"]), {"alpha", "beta"})
        self.assertEqual(shared["graph_distances"]["alpha"], 1)
        self.assertEqual(data["source_summary"]["accepted_evidence"], 2)

    def test_graph_excludes_scout_material_and_malformed_evidence(self) -> None:
        self.write_neighborhood("target", {
            "primary_blog": "target",
            "blogs": [{"blog": "scout", "distance": 1, "status": "queued"}],
            "interactions": [{"from_blog": "target", "to_blog": "scout", "kind": "likes", "source_blog": "target", "source_post_id": "1"}],
            "scout_interactions": [{"from_blog": "scout", "to_blog": "target", "kind": "structured_ask", "source_blog": "scout", "source_post_id": "2", "direction_known": True}],
            "adjacency_observations": [],
        })
        data = graph_projection.build_graph_projection(self.backups, self.neighborhoods)
        self.assertEqual([node["id"] for node in data["nodes"]], ["target"])
        self.assertEqual(data["source_summary"]["accepted_evidence"], 0)
        self.assertEqual(data["source_summary"]["skipped_evidence"], 1)
        self.assertEqual(data["edges"], [])
        self.assertIn("structured_ask", data["relationship_semantics"])
        self.assertNotIn("likes", data["relationship_semantics"])

    def test_embedded_json_escapes_script_terminators(self) -> None:
        encoded = graph_projection.embedded_json({"value": "</script><script>alert(1)</script>"})
        self.assertNotIn("</script>", encoded)
        self.assertIn("\\u003c/script\\u003e", encoded)

    def test_projection_does_not_modify_neighborhood_authority(self) -> None:
        self.write_neighborhood("target", {"primary_blog": "target", "blogs": [], "interactions": [], "scout_interactions": [], "adjacency_observations": []})
        source = self.neighborhoods / "target" / "neighborhood.json"
        before = hashlib.sha256(source.read_bytes()).digest()
        graph_projection.build_graph_projection(self.backups, self.neighborhoods)
        self.assertEqual(hashlib.sha256(source.read_bytes()).digest(), before)

    def test_projection_schema_validation_rejects_unknown_edge_nodes(self) -> None:
        projection = graph_projection.build_graph_projection(self.backups, self.neighborhoods)
        projection["edges"].append({"source": "missing", "target": "alpha"})
        with self.assertRaises(ValueError):
            graph_projection.validate_graph_projection(projection)

    def test_generated_archive_directories_are_not_graph_nodes(self) -> None:
        for name in ("assets", "tags", "dashboard", "graph", "participant-assets"):
            (self.backups / name).mkdir()
        data = graph_projection.build_graph_projection(self.backups, self.neighborhoods)
        self.assertEqual(data["nodes"], [])

    def test_graph_renderer_only_applies_avatar_images_to_nonempty_assets(self) -> None:
        script = Path(__file__).resolve().parents[1] / "assets" / "graph-view.js"
        text = script.read_text(encoding="utf-8")
        self.assertIn("hasAvatar: node.avatar_src ? 'true' : 'false'", text)

    def test_projection_exposes_archive_depth_breadth_and_local_avatar_facts(self) -> None:
        (self.backups / "target" / "json").mkdir(parents=True)
        for post_id in ("1", "2", "3"):
            (self.backups / "target" / "json" / f"{post_id}.json").write_text(
                json.dumps({"id": post_id, "timestamp": 1700000000 + int(post_id)}), encoding="utf-8"
            )
        avatar = self.backups / "target" / "profile" / "avatar.png"
        avatar.parent.mkdir(parents=True)
        avatar.write_bytes(b"avatar")
        self.write_neighborhood("target", {
            "primary_blog": "target",
            "blogs": [{"blog": "outer", "distance": 3, "status": "queued"}],
            "interactions": [], "scout_interactions": [], "adjacency_observations": [],
        })
        observations = self.neighborhoods / "target" / "observations"
        observations.mkdir()
        (observations / "relationships.jsonl").write_text(json.dumps({
            "kind": "direct_reblog", "from_blog": "target", "to_blog": "outer",
            "source_blog": "target", "source_post_id": "1", "reference_url": "https://example.invalid/1",
        }) + "\n", encoding="utf-8")
        data = graph_projection.build_graph_projection(self.backups, self.neighborhoods)
        target = next(node for node in data["nodes"] if node["id"] == "target")
        outer = next(node for node in data["nodes"] if node["id"] == "outer")
        self.assertEqual(target["canonical_post_count"], 3)
        self.assertEqual(target["breadth"], 0)
        self.assertTrue(target["is_target"])
        self.assertEqual(target["avatar_src"], "target/profile/avatar.png")
        self.assertGreater(target["node_size"], outer["node_size"])
        self.assertEqual(outer["canonical_post_count"], 0)
        self.assertEqual(outer["capture_status"], "observation_only")
        # The projection uses the nearest factual path; the relationship
        # evidence gives this node a shorter path than its queued hint.
        self.assertEqual(outer["breadth"], 1)
        self.assertLess(outer["breadth_opacity"], target["breadth_opacity"])

    def test_graph_styles_use_independent_depth_size_breadth_opacity_and_target_ring(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "assets" / "graph-view.js").read_text(encoding="utf-8")
        self.assertIn("nodeSize", script)
        self.assertIn("baseOpacity", script)
        self.assertIn('node[isTarget = "true"]', script)
        self.assertIn("underlay-padding", script)

    def test_graph_renderer_focuses_hovered_or_tapped_node_by_distance(self) -> None:
        text = (Path(__file__).resolve().parents[1] / "assets" / "graph-view.js").read_text(encoding="utf-8")
        self.assertIn("function focusNode(id)", text)
        self.assertIn("Math.max(0.12, 1 - (distance * 0.25))", text)
        self.assertIn("edge.style('opacity', relevant", text)
        self.assertIn("focusNode(event.target.id())", text)
        self.assertIn("node[hasAvatar = \"true\"]", text)
        self.assertIn("Loading graph renderer...", text)
        self.assertIn("assets/vendor/", text)
        self.assertIn("window.setTimeout(boot, 50)", text)
        self.assertIn("Graph layout could not load", text)

    def test_graph_renderer_distinguishes_mutual_asks_and_distant_nodes(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "assets" / "graph-view.js").read_text(encoding="utf-8")
        self.assertIn("function hasVisibleReverse(edge, visibleEdges)", script)
        self.assertIn("reciprocal: hasVisibleReverse(edge, visibleEdges) ? 'true' : 'false'", script)
        self.assertIn('edge[relationshipType = "direct_reblog"][reciprocal = "true"]', script)
        self.assertIn('edge[relationshipType = "structured_ask"]', script)
        self.assertIn('node[distanceClass = "distance-unknown"]', script)
        self.assertIn('edge[distanceClass = "distance-unknown"]', script)
        self.assertIn("#b56de8", script)
        self.assertIn("#e0a84f", script)

    def test_graph_view_defaults_to_archived_targets_with_collapsed_display_settings(self) -> None:
        self.write_neighborhood("target", {
            "primary_blog": "target", "blogs": [], "interactions": [],
            "scout_interactions": [], "adjacency_observations": [],
        })
        content = graph_projection.render_graph_content(
            graph_projection.build_graph_projection(self.backups, self.neighborhoods)
        )
        self.assertNotIn("graph-include-scout", content)
        self.assertIn('<details class="graph-settings">', content)
        self.assertNotIn('<details class="graph-settings" open>', content)
        for control in (
            "graph-spacing", "graph-label-mode", "graph-zoom-sensitivity", "graph-repulsion",
            "graph-link-length", "graph-gravity", "graph-settle",
        ):
            self.assertIn(f'id="{control}"', content)
        self.assertIn('id="graph-spacing" type="range" min="0.1" max="5"', content)
        self.assertIn('id="graph-settle">Refresh</button>', content)
        self.assertNotIn("graph-settle-time", content)
        self.assertIn("Degree means the number of distinct connected blogs", content)
        self.assertNotIn("Observed relationships", content)

        script = (Path(__file__).resolve().parents[1] / "assets" / "graph-view.js").read_text(encoding="utf-8")
        self.assertIn("spacingFactor", script)
        self.assertIn("idealEdgeLength", script)
        self.assertIn("wheelSensitivity", script)
        self.assertIn("name: 'fcose'", script)
        self.assertIn("packComponents: true", script)
        self.assertIn("nodeDimensionsIncludeLabels: true", script)
        self.assertIn("node.data('isTarget')", script)
        self.assertIn("zoom >= 1.5", script)
        self.assertIn("String(node.data('label') || '')", script)
        self.assertIn("function nearestDistance(node)", script)
        self.assertIn("distanceClass: distanceClass(node)", script)
        self.assertIn("relationshipType: relationshipType(edge)", script)
        self.assertIn("distance-unknown", script)
        self.assertIn("animate: false", script)
        self.assertIn("edgeElasticity", script)
        self.assertIn("graph-layout-pending", script)
        self.assertIn("tumblr-archive-graph-settings-v1", script)
        self.assertIn("localStorage", script)
        self.assertIn("Computing a settled graph layout...", script)
        self.assertIn("connection", script)
        self.assertNotIn("reblog observation", script)


if __name__ == "__main__":
    unittest.main()
