"""Build the disposable, offline Graph View projection.

Canonical post JSON and Neighborhood JSON remain authoritative.  This module
only reads them and produces a bounded presentation document.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
import hashlib
import html
import json
import math
from pathlib import Path
import re
from typing import Any

from . import presentation


GRAPH_FORMAT = "tumblr-archive-graph"
GRAPH_VERSION = 1
GRAPH_GENERATOR_VERSION = 2
MAX_EVIDENCE_REFS = 20
BLOG_RE = re.compile(r"^[a-z0-9-]+$")
SUPPORTED_RELATIONSHIPS = {
    "direct_reblog": "source reblogged from target",
    "structured_ask": "source asked or addressed target",
    "explicit_public_like": "publicly liked post from",
    "explicit_public_follow": "explicitly follows",
}


def _blog(value: Any) -> str | None:
    candidate = str(value or "").strip().lower()
    if not BLOG_RE.fullmatch(candidate) or candidate in {"www", "api", "tumblr"}:
        return None
    return candidate


def _load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, str(exc)
    return (value, None) if isinstance(value, dict) else (None, "JSON root is not an object")


def _avatar(backups_root: Path, blog: str) -> str:
    candidates = [
        backups_root / blog / "profile" / "avatar.png",
        backups_root / blog / "profile" / "avatar.jpg",
        backups_root / blog / "profile" / "avatar.jpeg",
        backups_root / blog / "profile" / "avatar.gif",
        backups_root / blog / "profile" / "avatar.webp",
        backups_root / blog / "theme" / "avatar.png",
        backups_root / blog / "theme" / "avatar.jpg",
        backups_root / blog / "theme" / "avatar.jpeg",
        backups_root / blog / "theme" / "avatar.gif",
        backups_root / blog / "theme" / "avatar.webp",
        backups_root / "participant-assets" / blog / "avatar.png",
        backups_root / "participant-assets" / blog / "avatar.jpg",
        backups_root / "participant-assets" / blog / "avatar.jpeg",
        backups_root / "participant-assets" / blog / "avatar.gif",
        backups_root / "participant-assets" / blog / "avatar.webp",
    ]
    for path in candidates:
        if path.is_file():
            return path.relative_to(backups_root).as_posix()
    return ""


def _avatar_status(backups_root: Path, blog: str, avatar: str) -> str:
    if avatar:
        return "available"
    status_path = backups_root / "participant-assets" / blog / "status.json"
    try:
        value = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        value = {}
    if isinstance(value, dict) and value.get("status") in {"restricted", "unavailable"}:
        return str(value["status"])
    if (backups_root / blog / "json").is_dir() or (backups_root / blog / "profile" / "profile.json").is_file():
        return "null"
    return "unknown"


def _node(backups_root: Path, blog: str) -> dict[str, Any]:
    json_root = backups_root / blog / "json"
    records = []
    if json_root.is_dir():
        for path in json_root.glob("*.json"):
            record, _error = _load_json(path)
            if record is not None:
                records.append(record)
    local_count = len(records)
    archived = local_count > 0
    avatar = _avatar(backups_root, blog)
    timestamps = sorted(int(record.get("timestamp") or 0) for record in records if record.get("timestamp"))
    # This is a presentation transform of durable count, not a policy or
    # completeness estimate.  The cap keeps a large archive from swallowing
    # the viewport while preserving monotonic differences between counts.
    visual_depth = min(1.0, math.log1p(local_count) / math.log1p(1000)) if local_count else 0.0
    return {
        "id": blog,
        "username": blog,
        "archive_status": "archived" if archived else "observed",
        "local_post_count": local_count,
        "canonical_post_count": local_count,
        "captured_earliest_timestamp": timestamps[0] if timestamps else None,
        "captured_latest_timestamp": timestamps[-1] if timestamps else None,
        "visual_depth": visual_depth,
        "node_size": 12.0 + (34.0 * visual_depth),
        "depth_opacity": 0.52 + (0.48 * visual_depth),
        "breadth_opacity": 1.0,
        "avatar_src": avatar,
        "has_local_avatar": bool(avatar),
        "avatar_status": _avatar_status(backups_root, blog, avatar),
        "archive_href": f"{blog}/index.html" if archived else "",
        "neighborhoods": {},
        "graph_distances": {},
        "degree": 0,
    }


def _evidence_key(item: dict[str, Any]) -> tuple[str, ...] | None:
    source = _blog(item.get("from_blog"))
    target = _blog(item.get("to_blog"))
    kind = str(item.get("kind") or "")
    source_blog = _blog(item.get("source_blog"))
    post_id = str(item.get("source_post_id") or "")
    reference_url = str(item.get("reference_url") or "")
    if not source or not target or source == target or kind not in SUPPORTED_RELATIONSHIPS or not source_blog or not post_id:
        return None
    return (source, target, kind, source_blog, post_id, reference_url, str(item.get("referenced_post_id") or ""))


def _distance_map(start: str, adjacency: dict[str, set[str]]) -> dict[str, int]:
    distances = {start: 0}
    queue: deque[str] = deque([start])
    while queue:
        current = queue.popleft()
        for neighbor in sorted(adjacency.get(current, ())):
            if neighbor not in distances:
                distances[neighbor] = distances[current] + 1
                queue.append(neighbor)
    return distances


def _safe_id(source: str, target: str) -> str:
    return "edge-" + hashlib.sha256(f"{source}\0{target}".encode("utf-8")).hexdigest()[:24]


def _fingerprint(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "unavailable"


def _observation_records(neighborhoods_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(neighborhoods_root.glob("**/observations/*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("kind") in SUPPORTED_RELATIONSHIPS:
                records.append(value)
    return records


def build_graph_projection(backups_root: Path, neighborhoods_root: Path, *,
                           max_nodes: int | None = None,
                           max_distance: int | None = None) -> dict[str, Any]:
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[tuple[str, str], dict[str, Any]] = {}
    target_paths: list[tuple[str, Path]] = []
    warnings: list[dict[str, str]] = []
    source_records = 0
    accepted = 0
    skipped = 0
    seen_evidence: dict[tuple[str, ...], dict[str, Any]] = {}

    if neighborhoods_root.is_dir():
        for directory in sorted(neighborhoods_root.iterdir()):
            path = directory / "neighborhood.json"
            if directory.is_dir() and path.is_file():
                target = _blog(directory.name)
                if target:
                    target_paths.append((target, path))

    target_ids = {target for target, _path in target_paths}
    observation_records = _observation_records(neighborhoods_root)
    for item in observation_records:
        for identity in (item.get("from_blog"), item.get("to_blog")):
            blog = _blog(identity)
            if blog:
                nodes.setdefault(blog, _node(backups_root, blog))
    generated_roots = {"assets", "tags", "dashboard", "graph", "participant-assets"}
    for blog_dir in sorted(backups_root.iterdir()) if backups_root.is_dir() else []:
        if blog_dir.is_dir() and blog_dir.name not in generated_roots and _blog(blog_dir.name):
            candidate = _node(backups_root, blog_dir.name)
            if candidate["archive_status"] == "archived":
                nodes.setdefault(blog_dir.name, candidate)

    for target, path in target_paths:
        nodes.setdefault(target, _node(backups_root, target))
        document, error = _load_json(path)
        if error:
            warnings.append({"target": target, "kind": "neighborhood_unreadable", "message": error})
            continue
        assert document is not None
        for item in document.get("blogs", []):
            if not isinstance(item, dict):
                warnings.append({"target": target, "kind": "malformed_blog", "message": "blog entry is not an object"})
                continue
            blog = _blog(item.get("blog"))
            if not blog:
                warnings.append({"target": target, "kind": "malformed_blog", "message": "blog identity is invalid"})
                continue
            candidate = _node(backups_root, blog)
            if candidate["archive_status"] != "archived" and blog not in target_ids and not observation_records:
                continue
            node = nodes.setdefault(blog, candidate)
            try:
                distance = max(0, int(item.get("distance", 0)))
            except (TypeError, ValueError):
                distance = 0
            node["neighborhoods"][target] = {
                "distance": distance,
                "status": str(item.get("status") or "unknown"),
                "parents": sorted({_blog(parent) for parent in item.get("parents", []) if _blog(parent)}),
            }
        nodes[target]["neighborhoods"].setdefault(target, {"distance": 0, "status": "target", "parents": []})

        # Graph View represents preserved archive evidence only. Scout probes
        # are useful to acquisition but are not social-graph evidence.
        evidence_items: list[tuple[str, Any]] = []
        evidence_items.extend(("archived", item) for item in document.get("interactions", []) if isinstance(item, dict))
        for lane, item in evidence_items:
            source_records += 1
            key = _evidence_key(item)
            if key is None:
                skipped += 1
                warnings.append({"target": target, "kind": "malformed_evidence", "message": "unsupported or incomplete relationship evidence"})
                continue
            source, destination = key[0], key[1]
            if source not in nodes or destination not in nodes:
                skipped += 1
                continue
            accepted += 1
            if key in seen_evidence:
                seen_evidence[key].setdefault("observed_in", set()).add(target)
                if lane == "archived":
                    seen_evidence[key]["archived"] = True
                continue
            source, destination, kind, source_blog, post_id, reference_url, referenced_post_id = key
            entry = {
                "source": source,
                "target": destination,
                "kind": kind,
                "source_blog": source_blog,
                "source_post_id": post_id,
                "reference_url": reference_url,
                "referenced_post_id": referenced_post_id,
                "archived": lane == "archived",
                "observed_in": {target},
            }
            seen_evidence[key] = entry

    for item in observation_records:
        source = _blog(item.get("from_blog"))
        destination = _blog(item.get("to_blog"))
        if not source or not destination or source == destination:
            continue
        key = (source, destination, str(item.get("kind")),
               _blog(item.get("source_blog")) or destination,
               str(item.get("source_post_id") or item.get("liked_post_id") or ""),
               str(item.get("source_url") or item.get("reference_url") or ""),
               str(item.get("referenced_post_id") or item.get("liked_post_id") or ""))
        if key in seen_evidence:
            continue
        seen_evidence[key] = {
            "source": source, "target": destination, "kind": str(item.get("kind")),
            "source_blog": key[3], "source_post_id": key[4],
            "reference_url": key[5], "referenced_post_id": key[6],
            "archived": False, "observed_in": {"observation-registry"},
        }

    for evidence in seen_evidence.values():
        pair = (evidence["source"], evidence["target"])
        edge = edges.setdefault(pair, {
            "id": _safe_id(*pair),
            "source": pair[0],
            "target": pair[1],
            "relationship_counts": {},
            "observation_count": 0,
            "archived_observation_count": 0,
            "scout_only_observation_count": 0,
            "observed_in": set(),
            "evidence_refs": [],
        })
        counts = edge["relationship_counts"].setdefault(evidence["kind"], {"count": 0, "archived": 0, "scout_only": 0})
        counts["count"] += 1
        counts["archived" if evidence["archived"] else "scout_only"] += 1
        edge["observation_count"] += 1
        edge["archived_observation_count"] += int(evidence["archived"])
        edge["scout_only_observation_count"] += int(not evidence["archived"])
        edge["observed_in"].update(evidence["observed_in"])
        if len(edge["evidence_refs"]) < MAX_EVIDENCE_REFS:
            edge["evidence_refs"].append({
                "kind": evidence["kind"],
                "source_blog": evidence["source_blog"],
                "source_post_id": evidence["source_post_id"],
                "source_post_url": evidence["reference_url"],
                "neighborhoods": sorted(evidence["observed_in"]),
            })

    adjacency: dict[str, set[str]] = defaultdict(set)
    for source, target in edges:
        adjacency[source].add(target)
        adjacency[target].add(source)
    target_names = sorted({target for target, _ in target_paths})
    all_distances = {target: _distance_map(target, adjacency) for target in target_names}
    for blog, node in nodes.items():
        node["degree"] = len(adjacency.get(blog, ()))
        node["graph_distances"] = {target: distances[blog] for target, distances in all_distances.items() if blog in distances}
        neighborhood_distances = [
            int(value.get("distance"))
            for value in node.get("neighborhoods", {}).values()
            if isinstance(value, dict) and str(value.get("distance", "")).lstrip("-").isdigit()
        ]
        distances = list(node["graph_distances"].values()) + neighborhood_distances
        node["nearest_target_distance"] = min(distances) if distances else None
        node["breadth"] = node["nearest_target_distance"]
        breadth = node["nearest_target_distance"]
        node["breadth_opacity"] = 1.0 if breadth is None else max(0.34, 1.0 - (0.11 * min(max(0, breadth), 6)))
        node["capture_status"] = "captured" if node["canonical_post_count"] else "observation_only"

    reciprocal = {(target, source) for source, target in edges}
    for pair, edge in edges.items():
        edge["reciprocal"] = (pair[1], pair[0]) in reciprocal
        edge["visual_strength"] = min(8.0, 1.0 + math.log1p(edge["observation_count"]))
        edge["observed_in"] = sorted(edge["observed_in"])

    for node in nodes.values():
        node["archive_status"] = "archived" if node["local_post_count"] > 0 else "observed"
        node["is_target"] = node["id"] in target_ids

    fingerprints = [{"target": target, "sha256": _fingerprint(path)} for target, path in target_paths]
    if max_distance is not None:
        keep = set(target_names) | {blog for blog, node in nodes.items() if node.get("nearest_target_distance") is not None and node["nearest_target_distance"] <= max_distance}
        nodes = {blog: node for blog, node in nodes.items() if blog in keep}
        edges = {pair: edge for pair, edge in edges.items() if pair[0] in keep and pair[1] in keep}
    if max_nodes is not None and len(nodes) > max_nodes:
        ranked = sorted(nodes, key=lambda blog: (blog not in target_ids, nodes[blog].get("nearest_target_distance") is None, -(nodes[blog].get("degree", 0)), blog))
        keep = set(ranked[:max_nodes]) | target_ids
        nodes = {blog: node for blog, node in nodes.items() if blog in keep}
        edges = {pair: edge for pair, edge in edges.items() if pair[0] in keep and pair[1] in keep}
    return {
        "format": GRAPH_FORMAT,
        "version": GRAPH_VERSION,
        "generator_version": GRAPH_GENERATOR_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "relationship_semantics": SUPPORTED_RELATIONSHIPS,
        "source_summary": {
            "neighborhoods": len(target_paths),
            "evidence_records": source_records,
            "accepted_evidence": accepted,
            "skipped_evidence": skipped,
            "nodes": len(nodes),
            "edges": len(edges),
            "neighborhood_fingerprints": fingerprints,
        },
        "targets": [{"id": target, "username": target} for target in target_names],
        "nodes": [nodes[blog] for blog in sorted(nodes)],
        "edges": [
            {
                **edge,
                "relationship_counts": {kind: edge["relationship_counts"][kind] for kind in sorted(edge["relationship_counts"])},
            }
            for _pair, edge in sorted(edges.items())
        ],
        "warnings": warnings[:200],
    }


def embedded_json(data: dict[str, Any]) -> str:
    """Serialize JSON safely inside a script element, not just as JSON."""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def validate_graph_projection(data: Any) -> dict[str, Any]:
    """Validate the small public contract consumed by the offline page."""
    if not isinstance(data, dict) or data.get("format") != GRAPH_FORMAT or data.get("version") != GRAPH_VERSION:
        raise ValueError("unsupported graph projection format or version")
    nodes = data.get("nodes")
    edges = data.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError("graph projection nodes and edges must be lists")
    node_ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or node["id"] in node_ids:
            raise ValueError("graph projection contains an invalid or duplicate node")
        node_ids.add(node["id"])
    for edge in edges:
        if not isinstance(edge, dict) or edge.get("source") not in node_ids or edge.get("target") not in node_ids:
            raise ValueError("graph projection contains an edge to an unknown node")
    return data


def render_graph_content(data: dict[str, Any]) -> str:
    validate_graph_projection(data)
    options = ['<option value="__all__" selected>All neighborhoods</option>']
    options.extend(f'<option value="{html.escape(target["id"])}">{html.escape(target["username"])}</option>' for target in data["targets"])
    node_rows = []
    for node in sorted(data["nodes"], key=lambda item: (-int(item.get("degree", 0)), item["username"].casefold())):
        degree = int(node.get("degree", 0))
        node_rows.append(
            f'<li data-graph-text-node="{html.escape(node["id"])}"><bdi dir="auto">{html.escape(node["username"])}</bdi> '
            f'({degree} connection{"s" if degree != 1 else ""})</li>'
        )
    edge_rows = []
    for edge in sorted(data["edges"], key=lambda item: (-int(item.get("observation_count", 0)), item["source"].casefold(), item["target"].casefold())):
        semantics = "; ".join(
            f'{values["count"]} {"reblog" if kind == "direct_reblog" else "ask"}{"s" if values["count"] != 1 else ""}'
            for kind, values in edge["relationship_counts"].items()
        )
        edge_rows.append(f'<li><bdi dir="auto">{html.escape(edge["source"])}</bdi> -&gt; <bdi dir="auto">{html.escape(edge["target"])}</bdi>: {html.escape(semantics)}</li>')
    graph_filters = (
        '<div class="graph-controls" role="group" aria-label="Explore filters">'
        '<label class="explore-search" for="graph-search">Search blogs<input id="graph-search" type="search" autocomplete="off" placeholder="Search blogs"></label>'
        '<div class="graph-scope"><label for="graph-target">Neighborhood</label><select id="graph-target">' + "".join(options) + '</select></div>'
        '<label for="graph-distance">Distance<select id="graph-distance"><option value="all">All</option><option value="0">0</option><option value="1">1</option><option value="2">2</option><option value="3">3+</option></select></label>'
        '<label for="graph-min-strength">Relationship strength<select id="graph-min-strength"><option value="0">Any</option><option value="1">1+</option><option value="3">3+</option><option value="10">10+</option></select></label>'
        '<fieldset class="graph-filter-group"><legend>Show relationship types</legend><label><input type="checkbox" name="graph-type" value="direct_reblog" checked> Reblogs</label><label><input type="checkbox" name="graph-type" value="structured_ask" checked> Asks</label></fieldset></div>'
    )
    graph_toolbar = presentation.explore_toolbar(
        "Graph",
        filter_content=graph_filters,
        extra_class="graph-explore-toolbar",
    )
    return (
        '<section class="graph-view" id="graph-view">'
        '<div class="explore-heading"><p class="eyebrow">Explore</p><h1>Graph</h1><p class="graph-intro">Explore preserved blogs and observed relationships.</p></div>'
        + graph_toolbar
        + '<div class="graph-toolbar"><p id="graph-help" class="graph-help">Select a node to inspect it. Text view is available below for keyboard access.</p><p id="graph-renderer-status" class="graph-renderer-status" role="status" aria-live="polite">Loading graph renderer...</p></div>'
        '<div class="graph-workspace-toolbar"><div class="graph-legend" aria-label="Graph visual key"><span class="graph-legend-heading">Network</span><span>fainter = greater Breadth</span><span class="graph-legend-heading">Archive</span><span><i class="graph-legend-swatch graph-legend-observed"></i>Observation only</span><span><i class="graph-legend-swatch graph-legend-captured"></i>Captured posts</span><span><i class="graph-legend-swatch graph-legend-target"></i>Target ring</span><span class="graph-legend-note">Larger, brighter nodes contain more captured history.</span></div><div class="graph-actions"><button type="button" id="graph-fit">Fit graph</button><button type="button" id="graph-freeze">Freeze layout</button></div></div>'
        '<div class="graph-stage"><div id="graph-canvas" class="graph-canvas" aria-hidden="true"></div>'
        '<aside id="graph-inspector" class="graph-inspector graph-inspector-empty" aria-live="polite" aria-label="Selected blog details"><p><strong>No blog selected</strong><br>Select a node or blog to inspect it.</p></aside></div>'
        '<details class="graph-settings"><summary id="graph-settings">Graph display settings</summary>'
        '<div class="graph-settings-grid">'
        '<div class="graph-setting"><label for="graph-spacing">Node spacing <output id="graph-spacing-value" for="graph-spacing">1.6x</output></label><input id="graph-spacing" type="range" min="0.1" max="5" step="0.1" value="1.6"></div>'
        '<div class="graph-setting"><label for="graph-label-mode">Labels</label><select id="graph-label-mode"><option value="auto" selected>Auto</option><option value="selected">Selected blog only</option><option value="all">Always show</option><option value="none">Hide labels</option></select></div>'
        '<div class="graph-setting"><label for="graph-zoom-sensitivity">Zoom sensitivity <output id="graph-zoom-sensitivity-value" for="graph-zoom-sensitivity">1.0x</output></label><input id="graph-zoom-sensitivity" type="range" min="0.5" max="2" step="0.1" value="1"></div>'
        '<div class="graph-setting"><label for="graph-repulsion">Node repulsion <output id="graph-repulsion-value" for="graph-repulsion">1.4x</output></label><input id="graph-repulsion" type="range" min="0.5" max="3" step="0.1" value="1.4"></div>'
        '<div class="graph-setting"><label for="graph-link-length">Link length <output id="graph-link-length-value" for="graph-link-length">1.2x</output></label><input id="graph-link-length" type="range" min="0.6" max="2.2" step="0.1" value="1.2"></div>'
        '<div class="graph-setting"><label for="graph-gravity">Gravity <output id="graph-gravity-value" for="graph-gravity">0.4x</output></label><input id="graph-gravity" type="range" min="0" max="1.5" step="0.1" value="0.4"></div>'
        '<div class="graph-setting graph-setting-action"><button type="button" id="graph-settle">Refresh</button></div>'
        '</div><p class="graph-setting-help">Higher repulsion and link length give nodes more room. Gravity gently keeps the graph together. Settings are saved in this browser only.</p></details>'
        '<details class="graph-text-view"><summary>Text view of graph data</summary><div id="graph-text-results"><h2>Blogs by connections</h2><p class="graph-text-note">Degree means the number of distinct connected blogs.</p><ul>'
        + "".join(node_rows)
        + '</ul><h2>Relationships</h2><p class="graph-text-note">Degree means the number of distinct connected blogs. An arrow points from the blog that reblogged or asked to the other blog; counts are captured instances.</p><ul>'
        + "".join(edge_rows)
        + '</ul></div></details>'
        '<script id="graph-data" type="application/json">' + embedded_json(data) + '</script>'
        '</section>'
    )
