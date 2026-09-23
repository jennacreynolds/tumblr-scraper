"""Pure conversion from Tumblr public-feed records to canonical source records."""

from __future__ import annotations

from typing import Any


def blog_info(feed: dict[str, Any], total: int, *, blog: str, blog_host: str) -> dict[str, Any]:
    t = feed.get("tumblelog") or feed.get("blog") or {}
    return {
        "name": blog,
        "title": t.get("title") or feed.get("title") or blog,
        "description": t.get("description") or feed.get("description") or "",
        "url": t.get("url") or f"https://{blog_host}/",
        "posts": total,
        # tumblr-backup does not need a real UUID for this public-feed path.
        "uuid": f"public-feed:{blog}",
    }


QUALITY_MAX = {"NONE": 0, "THUMBNAIL": 250, "COMPACT": 500, "STANDARD": 1280, "HIGH": 10**9}


def photo_object(p: dict[str, Any], full_res: bool = False, quality: str | None = None) -> dict[str, Any]:
    candidates: list[tuple[int, str]] = []
    for size in (1280, 500, 400, 250, 100, 75):
        u = p.get(f"photo-url-{size}")
        if u:
            candidates.append((size, u))
    if not candidates:
        return {"caption": "", "original_size": {"url": ""}, "alt_sizes": []}

    largest_size, largest_url = candidates[0]
    selected_quality = "HIGH" if full_res else (quality or "COMPACT")
    if selected_quality == "NONE":
        selected = (0, "")
    elif selected_quality == "HIGH":
        selected = candidates[0]
    else:
        smaller = [candidate for candidate in candidates if candidate[0] <= QUALITY_MAX.get(selected_quality, 500)]
        selected = max(smaller, default=candidates[-1])
    selected_size, selected_url = selected
    alt = [] if not selected_url else [{"width": selected_size, "height": 0, "url": selected_url}]
    alt.extend(
        {"width": size, "height": 0, "url": url}
        for size, url in candidates
        if (size, url) != selected
    )
    return {
        "caption": "",
        "original_size": {"width": largest_size, "height": 0, "url": largest_url},
        "alt_sizes": alt,
    }


def normalize_post(
    p: dict[str, Any],
    feed: dict[str, Any],
    total: int,
    *,
    blog: str,
    blog_host: str,
    full_res: bool = False,
    quality: str | None = None,
) -> dict[str, Any]:
    """Translate one raw public-feed record without performing I/O."""
    legacy_type = str(p.get("type") or "regular")
    post_type = {
        "regular": "text",
        "conversation": "chat",
    }.get(legacy_type, legacy_type)

    post_url = p.get("url-with-slug") or p.get("url") or f"https://{blog_host}/post/{p['id']}"
    timestamp = int(p.get("unix-timestamp") or 0)

    feed_blog = feed.get("tumblelog") or feed.get("blog") or {}
    source_blog = p.get("tumblelog") if isinstance(p.get("tumblelog"), dict) else {}
    merged_blog = dict(feed_blog) if isinstance(feed_blog, dict) else {}
    if isinstance(source_blog, dict):
        merged_blog.update(source_blog)

    out: dict[str, Any] = {
        "id": int(p["id"]),
        "id_string": str(p["id"]),
        "blog_name": blog,
        "tumblelog": blog,
        "blog": blog_info({"tumblelog": merged_blog}, total, blog=blog, blog_host=blog_host),
        "post_url": post_url,
        "short_url": post_url,
        "type": post_type,
        "timestamp": timestamp,
        "date": p.get("date-gmt") or p.get("date") or "",
        "tags": list(p.get("tags") or []),
        "reblog_key": p.get("reblog-key") or "",
        "slug": p.get("slug") or "",
        "note_count": int(p.get("note-count") or 0),
        # Preserve the exact source record inside the normalized record.
        "_puppetbackup_source": "tumblr-public-blog-feed",
        "_puppetbackup_source_record": p,
    }

    remap = {
        "reblogged-from-url": "reblogged_from_url",
        "reblogged-from-name": "reblogged_from_name",
        "reblogged-root-url": "reblogged_root_url",
        "reblogged-root-name": "reblogged_root_name",
    }
    for old, new in remap.items():
        if p.get(old):
            out[new] = p[old]

    if post_type == "text":
        out["title"] = p.get("regular-title") or ""
        out["body"] = p.get("regular-body") or ""
    elif post_type == "answer":
        out["question"] = p.get("question") or ""
        out["answer"] = p.get("answer") or ""
        if p.get("asking-name"):
            out["asking_name"] = p["asking-name"]
        if p.get("asking-url"):
            out["asking_url"] = p["asking-url"]
    elif post_type == "photo":
        out["caption"] = p.get("photo-caption") or ""
        out["link_url"] = p.get("photo-link-url") or ""
        out["photos"] = [photo_object(p, full_res=full_res, quality=quality)]
    elif post_type == "quote":
        out["text"] = p.get("quote-text") or ""
        out["source"] = p.get("quote-source") or ""
    elif post_type == "link":
        out["title"] = p.get("link-text") or p.get("link-url") or ""
        out["url"] = p.get("link-url") or post_url
        out["description"] = p.get("link-description") or ""
    elif post_type == "chat":
        out["title"] = p.get("conversation-title") or ""
        out["dialogue"] = [
            {
                "label": row.get("label") or "",
                "name": row.get("name") or "",
                "phrase": row.get("phrase") or "",
            }
            for row in p.get("conversation") or []
        ]
    elif post_type == "video":
        embed = p.get("video-player-500") or p.get("video-player") or ""
        out["caption"] = p.get("video-caption") or ""
        out["video_type"] = "tumblr"
        out["video_url"] = p.get("video-source") or ""
        out["player"] = [{"width": 500, "embed_code": embed}] if embed else []
    elif post_type == "audio":
        out["caption"] = p.get("audio-caption") or ""
        out["audio_type"] = "tumblr"
        out["audio_url"] = p.get("audio-source") or p.get("audio-url") or ""
        out["player"] = p.get("audio-player") or ""

    return out
