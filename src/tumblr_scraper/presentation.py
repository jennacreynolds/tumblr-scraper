"""Shared generated-archive presentation mechanics.

This module owns reusable HTML shell/control markup, presentation-state
tracking, and copying browser assets into the generated archive. Page-specific
rendering and crawler orchestration remain in ``main.py`` until their inputs
are independent enough to move safely.

Canonical source JSON is not owned here. Generated HTML and copied assets are
derived output and may be rebuilt.
"""

from __future__ import annotations

import json
import shutil
import time
from html import escape
from pathlib import Path
from typing import Any

from . import archive


def presentation_state_path(app_root: Path) -> Path:
    return app_root / "presentation-state.json"


def presentation_is_dirty(
    app_root: Path,
    schema_version: int,
    identity: dict[str, str] | None = None,
) -> bool:
    path = presentation_state_path(app_root)
    if not path.is_file():
        return True
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return (
            bool(state.get("dirty"))
            or state.get("generator_version") != schema_version
            or (identity is not None and state.get("presentation_identity") != identity)
        )
    except (OSError, json.JSONDecodeError, AttributeError):
        return True


def mark_presentation_dirty(
    app_root: Path,
    schema_version: int,
    reason: str = "canonical data changed",
) -> None:
    app_root.mkdir(parents=True, exist_ok=True)
    archive.write_json_atomic(
        presentation_state_path(app_root),
        {
            "generator_version": schema_version,
            "dirty": True,
            "reason": reason,
            "updated_at": time.time(),
        },
    )


def shared_asset_paths(app_root: Path) -> tuple[Path, Path]:
    assets = app_root / "assets"
    return assets / "archive.css", assets / "archive.js"


def copy_shared_archive_assets(
    app_root: Path,
    source_asset_dir: Path,
    global_css: Path,
    source_archive_css: Path,
    source_archive_js: Path,
) -> None:
    css, js = shared_asset_paths(app_root)
    css.parent.mkdir(parents=True, exist_ok=True)
    base_css = ""
    source_css = source_archive_css if source_archive_css.is_file() else global_css
    if source_css.is_file():
        try:
            base_css = source_css.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            base_css = ""
    css.write_text(
        base_css + "\n/* Generated copy: source authority is assets/archive.css. */\n",
        encoding="utf-8",
    )
    if source_archive_js.is_file():
        shutil.copy2(source_archive_js, js)
    graph_assets = app_root / "assets"
    graph_assets.mkdir(parents=True, exist_ok=True)
    for relative in (
        Path("graph-view.css"),
        Path("graph-view.js"),
        Path("vendor") / "cytoscape.min.js",
        Path("vendor") / "layout-base.min.js",
        Path("vendor") / "cose-base.min.js",
        Path("vendor") / "cytoscape-layout-utilities.min.js",
        Path("vendor") / "cytoscape-fcose.min.js",
    ):
        source = source_asset_dir / relative
        if source.is_file():
            destination = graph_assets / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def relative_archive_url(blog: str, post_id: str | None = None) -> str:
    if post_id is None:
        return f"{blog}/index.html"
    return f"{blog}/posts/{post_id}.html"


def archive_shell(
    title: str,
    content: str,
    links: list[tuple[str, str]],
    *,
    active: str = "",
    stylesheet: str = "assets/archive.css",
    script: str = "assets/archive.js",
    extra: str = "",
    archive_name: str = "",
) -> str:
    return (
        '<!doctype html><html lang="en" dir="auto"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title>"
        f'<link rel="stylesheet" href="{escape(stylesheet)}">'
        f'<script defer src="{escape(script)}"></script>{extra}</head><body>'
        + application_chrome(links, active, archive_name=archive_name)
        + '<main class="reader-content">'
        + content
        + "</main></body></html>"
    )


def application_chrome(links: list[tuple[str, str]], active: str = "", *, archive_name: str = "") -> str:
    app_prefix = application_prefix(links)
    return (
        '<!-- puppetbackup-shared-shell-v4 -->'
        '<header class="archive-chrome">'
        '<div class="app-header">'
        f'<a class="app-title" href="{escape(app_prefix + "graph.html")}">Tumblr Archive</a>'
        + (f'<span class="archive-selection" aria-label="Active archive">Archive: {escape(archive_name)}</span>' if archive_name else '')
        + f'<a class="app-crawl-status" id="crawler-header-status" href="{escape(app_prefix + "crawler.html")}" aria-label="Crawler status: Idle">Idle</a>'
        + global_reader_controls(app_prefix)
        + '</div>'
        + primary_navigation(app_prefix, active, dict(links))
        + '</header>'
    )


def application_prefix(links: list[tuple[str, str]]) -> str:
    link_map = dict(links)
    graph_href = link_map.get("Graph", "graph.html")
    if graph_href.endswith("graph.html"):
        return graph_href[: -len("graph.html")]
    return ""


def application_drawer(prefix: str, active: str, link_map: dict[str, str]) -> str:
    """Compatibility shim; the archive now uses the primary navigation bar."""
    return ""


def primary_navigation(prefix: str, active: str, link_map: dict[str, str]) -> str:
    current = active if active in {"Explore", "List", "Feed", "Tags", "Neighborhoods", "Crawler", "Graph", "Settings"} else ""
    if active == "Blog tags":
        current = "Tags"
    elif active and not current:
        # Individual blog/post pages remain in the archive's List section.
        # Keep one explicit current-page marker for keyboard/screen-reader users.
        current = "List"
    archive_links = [
        ("Graph", link_map.get("Graph", f"{prefix}graph.html")),
        ("List", link_map.get("List", link_map.get("Blogs", f"{prefix}list.html"))),
        ("Feed", link_map.get("Feed", link_map.get("Dashboard", f"{prefix}feed.html"))),
        ("Tags", link_map.get("Tags", f"{prefix}tags.html")),
        ("Neighborhoods", link_map.get("Neighborhoods", f"{prefix}neighborhoods.html")),
    ]

    def render_links(items: list[tuple[str, str]]) -> str:
        rendered = []
        for label, href in items:
            marker = ' aria-current="page"' if label == current else ""
            extra_label = ' aria-label="Dashboard feed"' if label == "Feed" else ""
            rendered.append(f'<a href="{escape(href)}"{marker}{extra_label}>{escape(label)}</a>')
        return "".join(rendered)

    tools_href = link_map.get("Crawler", f"{prefix}crawler.html")
    return (
        '<nav class="primary-navigation" aria-label="Primary navigation">'
        '<div class="primary-navigation-scroll">'
        + render_links(archive_links)
        + '</div><details class="primary-tools"><summary>Tools</summary><div class="primary-tools-menu">'
        + f'<a href="{escape(tools_href)}"' + (' aria-current="page"' if current == "Crawler" else '') + '>Crawler</a>'
        + '</div></details>'
        + '</nav>'
    )


def global_nav(prefix: str = "") -> list[tuple[str, str]]:
    return [
        ("Graph", f"{prefix}graph.html"),
        ("List", f"{prefix}list.html"),
        ("Feed", f"{prefix}feed.html"),
        ("Tags", f"{prefix}tags.html"),
        ("Neighborhoods", f"{prefix}neighborhoods.html"),
        ("Crawler", f"{prefix}crawler.html"),
    ]


def explore_toolbar(current: str, *, prefix: str = "", filter_content: str = "", after: str = "", extra_class: str = "") -> str:
    """Render the shared Graph/List/Tags/Filters toolbar."""
    links = {
        "Graph": f"{prefix}graph.html",
        "List": f"{prefix}list.html",
        "Feed": f"{prefix}feed.html",
        "Tags": f"{prefix}tags.html",
        "Neighborhoods": f"{prefix}neighborhoods.html",
    }
    view_links = "".join(
        f'<a href="{escape(href)}"' + (' aria-current="page"' if label == current else "") + f'>{label}</a>'
        for label, href in links.items()
    )
    filters = (
        '<details class="explore-filter-disclosure graph-filter-disclosure">'
        '<summary>Filters</summary>' + filter_content + '</details>'
        if filter_content else ""
    )
    return (
        f'<div class="context-toolbar explore-toolbar shared-explore-toolbar {escape(extra_class)}">'
        + filters + after + '</div>'
    )


def global_reader_controls(prefix: str = "") -> str:
    return (
        '<details class="reader-settings-global"><summary aria-label="Reader controls">Aa</summary>'
        '<form class="reader-settings-panel" aria-label="Global reader settings">'
        '<div class="reader-setting-global"><label for="reader-size-global">Text size</label><input id="reader-size-global" type="range" min="0.9" max="1.8" step="0.05" value="1.08" data-reader-setting="size" data-unit="rem"></div>'
        '<div class="reader-setting-global"><label for="reader-leading-global">Line spacing</label><input id="reader-leading-global" type="range" min="1.2" max="2.2" step="0.05" value="1.58" data-reader-setting="leading"></div>'
        '<div class="reader-setting-global"><label for="reader-width-global">Content width</label><input id="reader-width-global" type="range" min="30" max="80" step="2" value="52" data-reader-setting="width" data-unit="rem"></div>'
        f'<a class="reader-settings-more" href="{escape(prefix + "settings.html")}">More settings...</a>'
        '</form></details>'
    )


def reader_controls(*, page: bool = False) -> str:
    opening = '<section class="reader-settings settings-page">' if page else '<details class="reader-settings">'
    heading = '<h1>Settings</h1><h2>Reader settings</h2>' if page else '<summary>Reader settings</summary>'
    closing = '</section>' if page else '</details>'
    return (
        opening + heading
        + '<form class="reader-settings-panel" aria-label="Reader settings">'
        '<div class="reader-setting"><label for="reader-size">Text size <output id="reader-size-value" for="reader-size">1.08rem</output></label>'
        '<input id="reader-size" type="range" min="0.9" max="1.8" step="0.05" value="1.08" data-reader-setting="size" data-unit="rem"></div>'
        '<div class="reader-setting"><label for="reader-leading">Line spacing <output id="reader-leading-value" for="reader-leading">1.58</output></label>'
        '<input id="reader-leading" type="range" min="1.2" max="2.2" step="0.05" value="1.58" data-reader-setting="leading"></div>'
        '<div class="reader-setting"><label for="reader-width">Content width <output id="reader-width-value" for="reader-width">52rem</output></label>'
        '<input id="reader-width" type="range" min="30" max="80" step="2" value="52" data-reader-setting="width" data-unit="rem"></div></form>' + closing
    )


def crawler_controls(*, page: bool = False, known_blogs: list[tuple[str, int]] | None = None) -> str:
    opening = '<section id="crawler-controls" class="crawler-controls crawler-page">' if page else '<div id="crawler-controls" class="crawler-controls" hidden>'
    heading = '<h1>Crawler</h1>' if page else ''
    closing = '</section>' if page else '</div>'
    curve_figure = ('<figure class="crawler-curve" aria-labelledby="crawler-curve-title"><figcaption id="crawler-curve-title">Capture curve: depth by relationship breadth</figcaption><svg id="crawler-curve-svg" role="img" aria-describedby="crawler-curve-description" viewBox="0 0 640 180" preserveAspectRatio="none"><desc id="crawler-curve-description">The table below provides the same curve information as text.</desc><polyline id="crawler-curve-line" fill="none" points=""></polyline></svg><table id="crawler-curve-table"><caption>Resolved capture policy</caption><thead><tr><th scope="col">Breadth</th><th scope="col">Depth (posts)</th></tr></thead><tbody></tbody></table></figure>')
    return (
        opening + heading
        + '<p id="crawler-status" class="crawler-status" aria-live="polite">Checking local crawler...</p>'
        '<div class="crawler-setup">'
        '<div class="crawler-setting crawler-setting-targets"><label for="crawler-target">Targets</label><input id="crawler-target" list="crawler-known-blogs" type="text" autocomplete="off" placeholder="blog-one, blog-two"><datalist id="crawler-known-blogs">'
        + ''.join(f'<option value="{escape(blog)}" label="{int(count)} archived posts">' for blog, count in (known_blogs or []))
        + '</datalist><p class="crawler-setting-help">Enter one or more Tumblr blogs, separated by commas.</p></div>'
        '<div class="crawler-setting"><label for="crawler-strategy">Capture preset <span id="crawler-preset-state">Neighborhood</span></label><select id="crawler-strategy"><option value="archive">Archive</option><option value="neighborhood" selected>Neighborhood</option><option value="explore">Explore</option><option value="survey">Survey</option></select><p id="crawler-strategy-description" class="crawler-setting-help">A convenience preset for the ordinary capture curve.</p></div>'
        '<div class="crawler-setting"><label for="crawler-max-posts">Maximum new posts</label><input id="crawler-max-posts" type="number" min="0" value="300"></div>'
        '<div class="crawler-setting"><label for="crawler-max-breadth">Maximum breadth</label><input id="crawler-max-breadth" type="number" min="0" value="1"><p class="crawler-setting-help">Relationship hops. Depth means chronological history.</p></div>'
        '<div class="crawler-setting"><label for="crawler-max-depth">Maximum depth</label><input id="crawler-max-depth" type="number" min="0" value="100"><p class="crawler-setting-help">Chronological posts per blog at the highest point of the capture curve.</p></div>'
        '<div class="crawler-setting"><label for="crawler-start-network">Network</label><select id="crawler-start-network"><option value="gentle">Gentle</option><option value="normal" selected>Normal</option><option value="urgent">Urgent</option></select><p class="crawler-setting-help">Request pacing and media parallelism.</p></div>'
        '<div class="crawler-setting"><label for="crawler-media-policy">Media storage</label><select id="crawler-media-policy" disabled aria-describedby="crawler-media-policy-help"><option>Distance-aware capture policy</option></select><p id="crawler-media-policy-help" class="crawler-setting-help">Media quality follows coverage distance. Optimizer configuration is not available yet.</p></div>'
        '</div>' + curve_figure + '<details class="crawler-advanced"><summary>Advanced</summary><div class="crawler-advanced-grid">'
        '<div class="crawler-setting"><label for="crawler-survey-nodes">Maximum observed blogs</label><input id="crawler-survey-nodes" type="number" min="0" value="10000"></div>'
        '<div class="crawler-setting"><label for="crawler-survey-requests">Observation request ceiling</label><input id="crawler-survey-requests" type="number" min="0" value="10000"></div>'
        '<p class="crawler-setting-help crawler-advanced-note">Observation can continue where the capture curve is 0; it does not consume post budget.</p>'
        '<output id="crawler-frontier-diagnostics" class="crawler-setting-help" aria-live="polite"></output>'
        '</div></details><div class="crawler-live" hidden><div class="crawler-live-overview">'
        '<fieldset><legend>Budget</legend><div class="crawler-buttons">'
        '<button type="button" data-crawler-step="budget_limit" data-step="-25">-25</button><output id="crawler-budget" aria-label="Crawler budget">-</output><button type="button" data-crawler-step="budget_limit" data-step="25">+25</button>'
        '</div></fieldset><div class="crawler-metrics" aria-label="Crawler progress metrics">'
        '<div class="crawler-metric crawler-metric-progress"><div class="crawler-metric-heading"><span>Budget progress</span><output id="crawler-progress-label">0 / unbounded</output></div><progress id="crawler-progress" max="1" value="0">0%</progress></div>'
        '<div class="crawler-metric"><div class="crawler-metric-heading"><span>Speed</span><output id="crawler-speed-label">0.0 posts/s</output></div><meter id="crawler-speed-meter" min="0" max="1" value="0">0 posts/s</meter></div>'
        '<div class="crawler-metric"><div class="crawler-metric-heading"><span>Estimated time left</span><output id="crawler-eta">--</output></div><p class="crawler-setting-help">Based on the current acquisition rate.</p></div></div>'
        '<fieldset><legend>Saved posts</legend><div class="crawler-region-counts"><span>Targets <output id="crawler-count-target" aria-label="Target blog posts">0</output></span><span>Breadth 1 <output id="crawler-count-nearby" aria-label="Breadth 1 posts">0</output></span><span>Breadth 2+ <output id="crawler-count-outer" aria-label="Breadth 2 and beyond posts">0</output></span><span id="crawler-unknown-lane" hidden>Unclassified <output id="crawler-count-unknown" aria-label="Unclassified posts">0</output></span></div></fieldset></div></div><div class="crawler-actions">'
        '<button type="button" class="crawler-start">Start crawl</button><button type="button" class="crawler-stop" data-crawler-stop hidden>Stop safely</button><button type="button" data-crawler-refresh hidden>Refresh archive</button><button type="button" data-server-shutdown hidden>Stop Tumblr Scraper server</button></div><p id="crawler-error" class="crawler-error" role="alert" hidden></p>' + closing
    )
