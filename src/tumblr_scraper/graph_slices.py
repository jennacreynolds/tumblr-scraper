"""Generation-time offline graph windows for large observation surveys."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def write_graph_slices(graph: dict[str, Any], destination: Path, *, max_nodes_per_slice: int = 1000) -> Path:
    """Write file://-compatible bounded JSON slices; no live server is required."""
    destination.mkdir(parents=True, exist_ok=True)
    nodes = list(graph.get("nodes", []))
    edges = list(graph.get("edges", []))
    slices: list[dict[str, Any]] = []
    for start in range(0, len(nodes), max_nodes_per_slice):
        selected = nodes[start:start + max_nodes_per_slice]
        ids = {node.get("id") for node in selected}
        payload = {**graph, "nodes": selected, "edges": [edge for edge in edges if edge.get("source") in ids and edge.get("target") in ids]}
        name = f"slice-{start // max_nodes_per_slice:05d}.json"
        (destination / name).write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        slices.append({"path": name, "nodes": len(selected), "first": selected[0].get("id") if selected else None})
    index = {"format": "tumblr-archive-graph-slices", "version": 1, "source_format": graph.get("format"), "slices": slices}
    path = destination / "index.json"
    path.write_text(json.dumps(index, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path
