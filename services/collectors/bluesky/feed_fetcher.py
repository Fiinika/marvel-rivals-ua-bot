from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx


logger = logging.getLogger(__name__)

AUTHOR_FEED_URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"
USER_AGENT = "Mozilla/5.0 (compatible; MarvelRivalsUACollector/1.0; +https://t.me/MarvelRivalsUABot)"
REQUEST_TIMEOUT_SECONDS = 20.0
DEFAULT_LIMIT = 20

_TIMESTAMP_RE = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?P<frac>\.\d+)?(?P<tz>Z|[+-]\d{2}:?\d{2})?"
)

# Bluesky's own image CDN. The fullsize URL of a `#view` embed is server-generated
# from the author's uploaded blob, so it always lives here; anything else is a
# tampered/hostile URL that must never be fetched bot-side or handed to Telegram.
_BLUESKY_IMAGE_HOST = "cdn.bsky.app"


def _is_bluesky_image_url(url: str) -> bool:
    parsed = urlsplit(url.strip())
    return parsed.scheme == "https" and (parsed.hostname or "").lower() == _BLUESKY_IMAGE_HOST


# A CIDv1 in base32 (what Bluesky embeds use): "baf" + lowercase base32 chars. The
# value selects which of the AUTHOR's own blobs to fetch, so it just needs to be a
# well-formed CID — but validating it keeps junk out of the getBlob URL.
_CID_RE = re.compile(r"^baf[a-z2-7]{20,}$")


def _is_valid_cid(cid: str) -> bool:
    return bool(_CID_RE.match(cid))


@dataclass(frozen=True)
class BlueskyPost:
    uri: str
    web_url: str
    text: str
    created_at: str | None
    image_urls: tuple[str, ...]
    has_video: bool
    # The video blob's CID (from an app.bsky.embed.video#view), used together with
    # the author DID to fetch the original MP4 via com.atproto.sync.getBlob. None
    # when the post has no video.
    video_cid: str | None = None

    @property
    def author_did(self) -> str:
        # at://<did>/app.bsky.feed.post/<rkey> — the DID owns the video blob.
        parts = self.uri.split("/")
        return parts[2] if len(parts) > 2 else ""


class BlueskyFeedFetcher:
    def __init__(self, actor: str, *, limit: int = DEFAULT_LIMIT) -> None:
        self.actor = actor
        self.limit = limit

    async def fetch_recent_posts(self) -> list[BlueskyPost]:
        data = await _fetch_author_feed(self.actor, self.limit)
        if data is None:
            return []

        posts = parse_author_feed(data, actor=self.actor)
        if not posts:
            logger.warning("Bluesky feed for %s returned no usable posts", self.actor)
        else:
            logger.info("Fetched %s Bluesky posts for %s", len(posts), self.actor)
        return posts


async def _fetch_author_feed(actor: str, limit: int) -> dict[str, Any] | None:
    params = {"actor": actor, "limit": str(limit), "filter": "posts_no_replies"}
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        ) as client:
            response = await client.get(AUTHOR_FEED_URL, params=params)
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Failed to fetch Bluesky feed for %s: %s", actor, exc)
        return None


def parse_author_feed(data: Any, *, actor: str) -> list[BlueskyPost]:
    if not isinstance(data, dict):
        return []

    posts: list[BlueskyPost] = []
    seen_uris: set[str] = set()
    for item in data.get("feed") or []:
        if not isinstance(item, dict):
            continue
        # Skip reposts: the account's own posts have no "reason"; reposts carry
        # a reasonRepost. Replies are already excluded via the API filter.
        if item.get("reason"):
            continue

        try:
            post = _parse_post(item.get("post"))
        except Exception:
            # One malformed entry must never abort the whole feed and drop every
            # other valid post; log it and move on.
            logger.warning("Skipping malformed Bluesky feed entry", exc_info=True)
            continue
        if post is None or post.uri in seen_uris:
            continue

        seen_uris.add(post.uri)
        posts.append(post)

    return posts


def _parse_post(post: Any) -> BlueskyPost | None:
    if not isinstance(post, dict):
        return None

    uri = str(post.get("uri") or "").strip()
    if not uri:
        return None

    author = post.get("author")
    if not isinstance(author, dict):
        author = {}
    handle = str(author.get("handle") or "").strip()
    rkey = uri.rsplit("/", 1)[-1]
    web_url = f"https://bsky.app/profile/{handle}/post/{rkey}" if handle and rkey else uri

    record = post.get("record")
    if not isinstance(record, dict):
        record = {}
    text = str(record.get("text") or "").strip()
    created_at = _normalize_timestamp(record.get("createdAt") or post.get("indexedAt"))

    image_urls, video_cid = _extract_media(post.get("embed"))

    return BlueskyPost(
        uri=uri,
        web_url=web_url,
        text=text,
        created_at=created_at,
        image_urls=tuple(image_urls),
        has_video=video_cid is not None,
        video_cid=video_cid,
    )


def _extract_media(embed: Any) -> tuple[list[str], str | None]:
    """Return (image URLs, video blob CID). At most one of the two is populated."""
    if not isinstance(embed, dict):
        return [], None

    embed_type = str(embed.get("$type") or "")
    if embed_type == "app.bsky.embed.images#view":
        urls: list[str] = []
        for image in embed.get("images") or []:
            if not isinstance(image, dict):
                continue
            url = str(image.get("fullsize") or "").strip()
            # Drop anything not on Bluesky's image CDN so a tampered fullsize value
            # can never become an SSRF target or an arbitrary URL sent to Telegram.
            if url and _is_bluesky_image_url(url):
                urls.append(url)
        return urls, None

    if embed_type == "app.bsky.embed.video#view":
        cid = str(embed.get("cid") or "").strip()
        return [], (cid if _is_valid_cid(cid) else None)

    # A post can carry both a quote and media; the media lives under "media".
    if embed_type == "app.bsky.embed.recordWithMedia#view":
        return _extract_media(embed.get("media"))

    return [], None


def _normalize_timestamp(value: Any) -> str | None:
    """Normalize Bluesky's ISO timestamp into one datetime.fromisoformat accepts.

    Bluesky emits up to nanosecond precision and a trailing ``Z``; trim the
    fraction to microseconds and convert ``Z`` to ``+00:00`` so downstream
    date-gating can parse it. Returns None when there is nothing usable.
    """
    if not isinstance(value, str) or not value.strip():
        return None

    match = _TIMESTAMP_RE.match(value.strip())
    if match is None:
        return value.strip()

    base = match.group("base")
    frac = match.group("frac") or ""
    if frac:
        frac = frac[:7]  # ".ffffff" — at most six fractional digits
    tz = match.group("tz") or ""
    if tz == "Z":
        tz = "+00:00"

    return f"{base}{frac}{tz}"
