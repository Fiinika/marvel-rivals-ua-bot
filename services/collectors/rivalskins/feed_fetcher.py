"""Fetch and parse the rivalskins.com leaks RSS feed (a WordPress RSS 2.0 feed).

rivalskins.com posts full-resolution skin renders a few days before a patch. The
feed needs no special User-Agent. Each <item> carries the skin name, the post URL,
a pubDate and the body in <content:encoded>; the first non-banner image in that
HTML is the skin render.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from xml.etree import ElementTree

import httpx
from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)

DEFAULT_FEED_URL = "https://rivalskins.com/category/leaks/feed/"
USER_AGENT = "MarvelRivalsUACollector/1.0 (Telegram news bot; +https://t.me/MarvelRivalsUABot)"
REQUEST_TIMEOUT_SECONDS = 25.0

_CONTENT = "{http://purl.org/rss/1.0/modules/content/}"

_IMAGE_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp)(?:\?|$)", re.IGNORECASE)
# The site's own host (incl. any subdomain/CDN). Only images here are trusted as
# the post's photo, so a third-party <img> embedded in the body is never sent.
_IMAGE_HOST_SUFFIX = "rivalskins.com"
# A recurring season-launch banner that appears in every post — never the unique
# skin render, so it is skipped when picking the post image.
_BANNER_RE = re.compile(r"launch-skins", re.IGNORECASE)


@dataclass(frozen=True)
class RivalSkinsPost:
    post_id: str  # the feed guid (?p=<id>), stable across edits
    web_url: str
    title: str
    body_text: str
    created_at: str | None
    image_url: str | None


class RivalSkinsFeedFetcher:
    def __init__(self, feed_url: str) -> None:
        self.feed_url = feed_url

    async def fetch_recent_posts(self) -> list[RivalSkinsPost]:
        xml_text = await self._fetch_feed()
        if xml_text is None:
            return []

        posts = parse_feed(xml_text)
        if not posts:
            logger.warning("rivalskins feed %s returned no usable posts", self.feed_url)
        else:
            logger.info("Fetched %s rivalskins posts", len(posts))
        return posts

    async def _fetch_feed(self) -> str | None:
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
                headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml"},
            ) as client:
                response = await client.get(self.feed_url)
                response.raise_for_status()
                return response.text
        except httpx.HTTPError as exc:
            logger.warning("Failed to fetch rivalskins feed %s: %s", self.feed_url, exc)
            return None


def parse_feed(xml_text: str) -> list[RivalSkinsPost]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        logger.warning("Failed to parse rivalskins feed XML: %s", exc)
        return []

    channel = root.find("channel")
    if channel is None:
        return []

    posts: list[RivalSkinsPost] = []
    seen_ids: set[str] = set()
    for item in channel.findall("item"):
        post = _parse_item(item)
        if post is None or post.post_id in seen_ids:
            continue
        seen_ids.add(post.post_id)
        posts.append(post)

    return posts


def _parse_item(item: ElementTree.Element) -> RivalSkinsPost | None:
    link = (item.findtext("link") or "").strip()
    guid = (item.findtext("guid") or "").strip()
    post_id = guid or link
    if not post_id or not link:
        return None

    title = (item.findtext("title") or "").strip()
    created_at = _normalize_pubdate(item.findtext("pubDate"))
    body_text, image_url = _parse_content(item.findtext(f"{_CONTENT}encoded"))

    return RivalSkinsPost(
        post_id=post_id,
        web_url=link,
        title=title,
        body_text=body_text,
        created_at=created_at,
        image_url=image_url,
    )


def _parse_content(content_html: str | None) -> tuple[str, str | None]:
    """Return (clean body text, the skin-render image URL) from a post's HTML."""
    if not content_html or not content_html.strip():
        return "", None

    soup = BeautifulSoup(content_html, "html.parser")
    body_text = soup.get_text(" ", strip=True)

    image_url: str | None = None
    for anchor in soup.find_all("img", src=True):
        src = str(anchor["src"]).strip()
        if _is_allowed_image(src) and not _BANNER_RE.search(src):
            image_url = src
            break

    return body_text, image_url


def _is_allowed_image(url: str) -> bool:
    parsed = urlsplit(url.strip())
    host = parsed.netloc.lower()
    on_site = host == _IMAGE_HOST_SUFFIX or host.endswith("." + _IMAGE_HOST_SUFFIX)
    return parsed.scheme == "https" and on_site and bool(_IMAGE_EXT_RE.search(url))


def _normalize_pubdate(value: str | None) -> str | None:
    """RFC-822 pubDate -> a UTC ISO timestamp datetime.fromisoformat can parse."""
    if not value or not value.strip():
        return None
    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()
