"""Observed relationship evidence from canonical Tumblr records.

This module interprets only references that are actually present in a saved
Tumblr record.  It performs no network access, filesystem work, scheduling,
inference, or presentation generation.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


def blog_from_reference(name: Any, url: Any) -> tuple[str, str] | None:
    """Return a canonical blog name and reference URL when one is explicit."""
    observed_name = str(name or "").strip()
    observed_url = str(url or "").strip()
    candidate = observed_name
    if observed_url:
        parsed = urlparse(observed_url)
        host = (parsed.hostname or "").lower()
        path_parts = [part for part in parsed.path.split("/") if part]
        if (
            host in {"www.tumblr.com", "tumblr.com"}
            and len(path_parts) >= 2
            and path_parts[0] == "blog"
            and path_parts[1] == "view"
        ):
            candidate = path_parts[2] if len(path_parts) >= 3 else ""
        elif (
            host.endswith(".tumblr.com")
            and host.count(".") == 2
            and host.split(".")[0] not in {"www", "api"}
        ):
            candidate = host[:-len(".tumblr.com")]
        elif not candidate and len(path_parts) >= 2 and path_parts[0] == "blog" and path_parts[1] == "view":
            candidate = path_parts[2] if len(path_parts) >= 3 else ""
    if not re.fullmatch(r"[A-Za-z0-9-]+", candidate or ""):
        return None
    if candidate.lower() in {"www", "api", "tumblr"}:
        return None
    return candidate.lower(), observed_url or f"https://{candidate.lower()}.tumblr.com/"


def interaction_evidence(owner: str, record: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract directional reblog, explicit-like, and ask evidence."""
    evidence = []
    source_id = str(record.get("id_string") or record.get("id") or "")
    source_url = str(record.get("post_url") or record.get("url-with-slug") or record.get("url") or "")
    direct = blog_from_reference(
        record.get("reblogged_from_name") or record.get("reblogged-from-name"),
        record.get("reblogged_from_url") or record.get("reblogged-from-url"),
    )
    if direct and direct[0] != owner:
        evidence.append({
            "from_blog": owner,
            "to_blog": direct[0],
            "kind": "direct_reblog",
            "direction": "upstream",
            "source_blog": owner,
            "source_post_id": source_id,
            "source_post_url": source_url,
            "reference_url": direct[1],
            "direction_known": True,
        })

    liked = blog_from_reference(
        record.get("liked_name") or record.get("liked-from-name") or record.get("liked_from_name") or record.get("liked_blog_name"),
        record.get("liked_url") or record.get("liked-from-url") or record.get("liked_from_url") or record.get("liked_blog_url"),
    )
    if liked and liked[0] != owner:
        evidence.append({
            "from_blog": owner,
            "to_blog": liked[0],
            "kind": "direct_like",
            "direction": "upstream",
            "source_blog": owner,
            "source_post_id": source_id,
            "source_post_url": source_url,
            "reference_url": liked[1],
            "direction_known": True,
        })
    asker = blog_from_reference(
        record.get("asking_name") or record.get("asking-name"),
        record.get("asking_url") or record.get("asking-url"),
    )
    if asker and asker[0] != owner:
        evidence.append({
            "from_blog": asker[0],
            "to_blog": owner,
            "kind": "structured_ask",
            "direction": "downstream",
            "source_blog": owner,
            "source_post_id": source_id,
            "source_post_url": source_url,
            "reference_url": asker[1],
            "direction_known": True,
        })
    return evidence


def evidence_key(item: dict[str, Any]) -> tuple[Any, ...]:
    """Return the existing deduplication key for one evidence item."""
    return (
        item.get("from_blog"), item.get("to_blog"), item.get("kind"),
        item.get("source_blog"), item.get("source_post_id"),
        item.get("reference_url"), item.get("referenced_post_id"),
    )


def public_like_evidence(liker: str, liked_post: dict[str, Any], *, source_url: str,
                         observed_at: float | None = None) -> dict[str, Any] | None:
    """Normalize a public Likes-list item without making it a canonical post."""
    post_id = str(liked_post.get("id_string") or liked_post.get("id") or "")
    blog_value = liked_post.get("blog_name") or liked_post.get("blog")
    blog_name = blog_value.get("name") if isinstance(blog_value, dict) else blog_value
    blog_url = blog_value.get("url") if isinstance(blog_value, dict) else ""
    source = blog_from_reference(blog_name, liked_post.get("post_url") or liked_post.get("url") or blog_url)
    if not post_id or not source:
        return None
    item = {
        "kind": "explicit_public_like",
        "liker_blog": liker.lower(),
        "from_blog": liker.lower(),
        "to_blog": source[0],
        "liked_post_id": post_id,
        "referenced_post_id": post_id,
        "source_blog": source[0],
        "source_url": source_url,
        "reference_url": liked_post.get("post_url") or liked_post.get("url") or source[1],
        "direction": "liker_to_source",
        "direction_known": True,
        "acquisition_mode": "public_web",
    }
    if observed_at is not None:
        item["observed_at"] = observed_at
    return item


def public_follow_evidence(follower: str, followed: dict[str, Any], *, source_url: str,
                           observed_at: float | None = None) -> dict[str, Any] | None:
    """Normalize a public Following-list item as factual follower -> followed evidence."""
    target = blog_from_reference(followed.get("name"), followed.get("url"))
    if not target:
        return None
    item = {
        "kind": "explicit_public_follow",
        "follower_blog": follower.lower(),
        "from_blog": follower.lower(),
        "to_blog": target[0],
        "followed_blog": target[0],
        "source_url": source_url,
        "reference_url": target[1],
        "direction": "follower_to_followed",
        "direction_known": True,
        "acquisition_mode": "public_web",
    }
    if observed_at is not None:
        item["observed_at"] = observed_at
    return item
