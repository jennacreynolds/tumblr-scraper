"""Deterministic interpretation of Tumblr blog profile HTML.

This module parses title and public description metadata only. It performs no
HTTP, filesystem, status, or profile-history work.
"""

from __future__ import annotations

from html.parser import HTMLParser


class ProfileMetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.description = ""
        self.in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title":
            self.in_title = True
            return
        if tag.lower() != "meta":
            return
        values = {name.lower(): value or "" for name, value in attrs}
        key = values.get("name", "").lower() or values.get("property", "").lower()
        if key in {"description", "og:description", "twitter:description"} and values.get("content"):
            self.description = values["content"].strip()

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)

    @property
    def title(self) -> str:
        return " ".join("".join(self.title_parts).split())


def parse_profile_observation(html: str) -> dict[str, str]:
    """Return the profile fields currently recognized by the application."""
    parser = ProfileMetadataParser()
    parser.feed(html)
    parser.close()
    return {"title": parser.title, "description": parser.description}
