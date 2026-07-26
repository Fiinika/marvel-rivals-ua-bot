from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from urllib.parse import quote, urlsplit
from xml.etree.ElementTree import Element, ParseError

import httpx
from bs4 import BeautifulSoup
from defusedxml.ElementTree import fromstring as parse_xml
from defusedxml.common import DefusedXmlException


logger = logging.getLogger(__name__)

SEARCH_URL_TEMPLATE = "https://www.reddit.com/r/{subreddit}/search.rss"
# Reddit requires a descriptive, non-browser User-Agent and rate-limits hard
# (a burst of requests returns 429). The collector makes ONE request per run.
USER_AGENT = "MarvelRivalsUACollector/1.0 (Telegram news bot; +https://t.me/MarvelRivalsUABot)"
REQUEST_TIMEOUT_SECONDS = 25.0
DEFAULT_LIMIT = 25

_ATOM = "{http://www.w3.org/2005/Atom}"
_MEDIA = "{http://search.yahoo.com/mrss/}"

_IMAGE_EXT_RE = re.compile(r"\.(?:jpe?g|png|gif|webp)(?:\?|$)", re.IGNORECASE)

# Reddit-owned image hosts. We match on the EXACT host (never a substring) so a
# link post pointing at e.g. https://i.redd.it.attacker.com/x.png or
# https://evil.com/i.redd.it/x.jpg can never be served as the post's photo.
_REDDIT_DIRECT_IMAGE_HOST = "i.redd.it"
_REDDIT_THUMBNAIL_HOSTS = frozenset({"preview.redd.it", "external-preview.redd.it", "i.redd.it"})

# A preview.redd.it thumbnail mirrors a Reddit-hosted image under the SAME file
# name on i.redd.it, so the tiny (often 140px) preview can be upgraded to the
# full-resolution original by swapping the host. external-preview.redd.it mirrors
# images hosted ELSEWHERE and has no i.redd.it twin, so it is never upgraded.
_REDDIT_PREVIEW_HOST = "preview.redd.it"
# Single-segment image file name only — anything else is left as-is rather than
# guessed at. The host is hardcoded, so a crafted path can never retarget the URL.
_PREVIEW_FILENAME_RE = re.compile(r"^/[A-Za-z0-9_-]+\.(?:jpe?g|png|gif|webp)$", re.IGNORECASE)
_UPGRADE_TIMEOUT_SECONDS = 10.0
_MAX_CONCURRENT_UPGRADES = 5


def _is_reddit_image_url(url: str, allowed_hosts: frozenset[str]) -> bool:
    parsed = urlsplit(url.strip())
    return parsed.scheme == "https" and parsed.netloc.lower() in allowed_hosts


@dataclass(frozen=True)
class RedditPost:
    post_id: str  # the t3_xxx fullname
    web_url: str  # the reddit comments URL
    title: str
    body_text: str
    created_at: str | None
    image_url: str | None
    author: str


class RedditSearchFetcher:
    """Fetches ONE flair-filtered search.rss feed (leaks use sort=new; the weekly
    fan-art digest uses sort=top + time_filter=week)."""

    def __init__(
        self,
        subreddit: str,
        flairs: Sequence[str],
        *,
        sort: str = "new",
        time_filter: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> None:
        self.subreddit = subreddit
        self.flairs = tuple(flairs)
        self.sort = sort
        self.time_filter = time_filter
        self.limit = limit

    async def fetch_recent_posts(self) -> list[RedditPost]:
        xml_text = await self._fetch_feed()
        if xml_text is None:
            return []

        posts = parse_feed(xml_text)
        if not posts:
            logger.warning("Reddit feed for r/%s returned no usable posts", self.subreddit)
            return posts

        logger.info("Fetched %s Reddit posts for r/%s", len(posts), self.subreddit)
        return await upgrade_preview_images(posts)

    async def _fetch_feed(self) -> str | None:
        if not self.flairs:
            logger.warning("No Reddit flairs configured for r/%s; skipping", self.subreddit)
            return None

        url = SEARCH_URL_TEMPLATE.format(subreddit=quote(self.subreddit, safe=""))
        params = {
            "q": _build_flair_query(self.flairs),
            "restrict_sr": "on",
            "sort": self.sort,
            "limit": str(self.limit),
        }
        if self.time_filter:
            params["t"] = self.time_filter
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
                headers={"User-Agent": USER_AGENT, "Accept": "application/atom+xml, application/xml"},
            ) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                return response.text
        except httpx.HTTPError as exc:
            logger.warning("Failed to fetch Reddit feed for r/%s: %s", self.subreddit, exc)
            return None


def _build_flair_query(flairs: Sequence[str]) -> str:
    return " OR ".join(f'flair:"{flair}"' for flair in flairs)


async def upgrade_preview_images(posts: list[RedditPost]) -> list[RedditPost]:
    """Replace signed preview.redd.it thumbnails with their full-resolution
    i.redd.it original, where one exists.

    Reddit serves such thumbnails as small as 140px, which looks broken as a post
    photo and makes the fan-art digest skip the art entirely (it accepts direct
    i.redd.it images only). The upgrade is verified with a HEAD request per post —
    posts whose twin is missing (video/gallery previews) keep the signed thumbnail.
    """
    candidates = [index for index, post in enumerate(posts) if _upgradable_preview_url(post.image_url)]
    if not candidates:
        return posts

    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_UPGRADES)
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=httpx.Timeout(_UPGRADE_TIMEOUT_SECONDS),
        headers={"User-Agent": USER_AGENT},
    ) as client:

        async def resolve(index: int) -> tuple[int, str | None]:
            async with semaphore:
                return index, await _resolve_full_res(client, str(posts[index].image_url))

        results = await asyncio.gather(*(resolve(index) for index in candidates))

    upgraded = list(posts)
    changed = 0
    for index, full_res in results:
        if full_res:
            upgraded[index] = replace(upgraded[index], image_url=full_res)
            changed += 1
    if changed:
        logger.info("Upgraded %s/%s Reddit preview thumbnails to full resolution", changed, len(candidates))
    return upgraded


def _upgradable_preview_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc.lower() == _REDDIT_PREVIEW_HOST
        and bool(_PREVIEW_FILENAME_RE.match(parsed.path))
    )


async def _resolve_full_res(client: httpx.AsyncClient, preview_url: str) -> str | None:
    """The i.redd.it twin of ``preview_url`` when it really exists, else None."""
    # The host is hardcoded and only the validated file name is reused, so this URL
    # can never be pointed anywhere but Reddit's own image CDN.
    candidate = f"https://{_REDDIT_DIRECT_IMAGE_HOST}{urlsplit(preview_url).path}"
    try:
        response = await client.head(candidate)
    except httpx.HTTPError as exc:
        logger.info("Could not verify full-res Reddit image %s: %s", candidate, exc)
        return None

    content_type = response.headers.get("content-type", "")
    if response.status_code == 200 and content_type.lower().startswith("image/"):
        return candidate
    return None


def parse_feed(xml_text: str) -> list[RedditPost]:
    try:
        # defusedxml: the feed is untrusted, so forbid DTDs/entity expansion.
        root = parse_xml(xml_text)
    except (ParseError, DefusedXmlException) as exc:
        logger.warning("Failed to parse Reddit feed XML: %s", exc)
        return []

    posts: list[RedditPost] = []
    seen_ids: set[str] = set()
    for entry in root.findall(f"{_ATOM}entry"):
        post = _parse_entry(entry)
        if post is None or post.post_id in seen_ids:
            continue
        seen_ids.add(post.post_id)
        posts.append(post)

    return posts


def _parse_entry(entry: Element) -> RedditPost | None:
    post_id = (entry.findtext(f"{_ATOM}id") or "").strip()
    if not post_id:
        return None

    title = (entry.findtext(f"{_ATOM}title") or "").strip()
    created_at = _normalize_timestamp(entry.findtext(f"{_ATOM}published"))

    link = entry.find(f"{_ATOM}link")
    web_url = (link.get("href").strip() if link is not None and link.get("href") else "")
    if not web_url:
        return None

    content_el = entry.find(f"{_ATOM}content")
    body_text, full_res_image = _parse_content(content_el.text if content_el is not None else None)

    thumb = entry.find(f"{_MEDIA}thumbnail")
    thumb_url = (thumb.get("url").strip() if thumb is not None and thumb.get("url") else None)
    if thumb_url and not _is_reddit_image_url(thumb_url, _REDDIT_THUMBNAIL_HOSTS):
        thumb_url = None

    author = (entry.findtext(f"{_ATOM}author/{_ATOM}name") or "").strip()

    return RedditPost(
        post_id=post_id,
        web_url=web_url,
        title=title,
        body_text=body_text,
        created_at=created_at,
        # Prefer the clean full-resolution direct image; fall back to the preview
        # thumbnail (used by video / gallery / external-link posts that have no
        # i.redd.it image — its signed query string must be kept intact).
        image_url=full_res_image or thumb_url,
        author=author,
    )


def _parse_content(content_html: str | None) -> tuple[str, str | None]:
    """Return (clean body text, full-res direct image URL) from a post's content.

    Reddit wraps the selftext in ``<div class="md">`` and appends a
    "submitted by ... [link] [comments]" footer; the ``[link]`` anchor of an
    image post points at the full-resolution ``i.redd.it`` file. Returns an empty
    body and no image when neither is present.
    """
    if not content_html or not content_html.strip():
        return "", None

    soup = BeautifulSoup(content_html, "html.parser")

    md = soup.find("div", class_="md")
    body_text = md.get_text(" ", strip=True) if md else ""

    full_res_image: str | None = None
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if _is_reddit_image_url(href, frozenset({_REDDIT_DIRECT_IMAGE_HOST})) and _IMAGE_EXT_RE.search(href):
            full_res_image = href.split("?", 1)[0]
            break

    return body_text, full_res_image


def _normalize_timestamp(value: str | None) -> str | None:
    if not value or not value.strip():
        return None

    text = value.strip()
    if text.endswith("Z"):
        return f"{text[:-1]}+00:00"
    return text
