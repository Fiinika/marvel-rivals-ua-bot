from __future__ import annotations

import logging
from dataclasses import dataclass
from xml.etree.ElementTree import Element, ParseError

import httpx
from defusedxml.ElementTree import fromstring as parse_xml
from defusedxml.common import DefusedXmlException


logger = logging.getLogger(__name__)

FEED_URL_TEMPLATE = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
USER_AGENT = "Mozilla/5.0 (compatible; MarvelRivalsUACollector/1.0; +https://t.me/MarvelRivalsUABot)"
REQUEST_TIMEOUT_SECONDS = 20.0

# Atom + YouTube + Media RSS namespaces used in the channel feed.
_ATOM = "{http://www.w3.org/2005/Atom}"
_YT = "{http://www.youtube.com/xml/schemas/2015}"
_MEDIA = "{http://search.yahoo.com/mrss/}"


@dataclass(frozen=True)
class YouTubeVideo:
    video_id: str
    web_url: str
    title: str
    description: str
    published: str | None
    thumbnail_url: str | None
    channel_name: str


class YouTubeFeedFetcher:
    def __init__(self, channel_id: str) -> None:
        self.channel_id = channel_id

    async def fetch_recent_videos(self) -> list[YouTubeVideo]:
        xml_text = await _fetch_feed(self.channel_id)
        if xml_text is None:
            return []

        videos = parse_feed(xml_text)
        if not videos:
            logger.warning("YouTube feed for %s returned no usable videos", self.channel_id)
        else:
            logger.info("Fetched %s YouTube videos for %s", len(videos), self.channel_id)
        return videos


async def _fetch_feed(channel_id: str) -> str | None:
    url = FEED_URL_TEMPLATE.format(channel_id=channel_id)
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
            headers={"User-Agent": USER_AGENT, "Accept": "application/atom+xml, application/xml"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text
    except httpx.HTTPError as exc:
        logger.warning("Failed to fetch YouTube feed for %s: %s", channel_id, exc)
        return None


def parse_feed(xml_text: str) -> list[YouTubeVideo]:
    try:
        # defusedxml: forbid DTDs/entity expansion on the fetched feed.
        root = parse_xml(xml_text)
    except (ParseError, DefusedXmlException) as exc:
        logger.warning("Failed to parse YouTube feed XML: %s", exc)
        return []

    channel_name = (root.findtext(f"{_ATOM}title") or "").strip()

    videos: list[YouTubeVideo] = []
    seen_ids: set[str] = set()
    for entry in root.findall(f"{_ATOM}entry"):
        video = _parse_entry(entry, channel_name)
        if video is None or video.video_id in seen_ids:
            continue
        seen_ids.add(video.video_id)
        videos.append(video)

    # entry[0] in the feed can be backdated, so sort newest-first by <published>.
    videos.sort(key=lambda video: video.published or "", reverse=True)
    return videos


def _parse_entry(entry: Element, channel_name: str) -> YouTubeVideo | None:
    video_id = (entry.findtext(f"{_YT}videoId") or "").strip()
    if not video_id:
        return None

    title = (entry.findtext(f"{_ATOM}title") or "").strip()
    published = _normalize_timestamp(entry.findtext(f"{_ATOM}published"))
    web_url = _extract_link(entry) or f"https://www.youtube.com/watch?v={video_id}"

    description = ""
    thumbnail_url: str | None = None
    group = entry.find(f"{_MEDIA}group")
    if group is not None:
        description = (group.findtext(f"{_MEDIA}description") or "").strip()
        thumbnail = group.find(f"{_MEDIA}thumbnail")
        if thumbnail is not None:
            thumbnail_url = (thumbnail.get("url") or "").strip() or None
    if thumbnail_url is None:
        thumbnail_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

    author_name = (entry.findtext(f"{_ATOM}author/{_ATOM}name") or "").strip() or channel_name

    return YouTubeVideo(
        video_id=video_id,
        web_url=web_url,
        title=title,
        description=description,
        published=published,
        thumbnail_url=thumbnail_url,
        channel_name=author_name,
    )


def _extract_link(entry: Element) -> str | None:
    alternate = entry.find(f"{_ATOM}link[@rel='alternate']")
    if alternate is not None:
        href = (alternate.get("href") or "").strip()
        if href:
            return href

    for link in entry.findall(f"{_ATOM}link"):
        href = (link.get("href") or "").strip()
        if href:
            return href

    return None


def _normalize_timestamp(value: str | None) -> str | None:
    """YouTube emits RFC-3339 timestamps datetime.fromisoformat already accepts;
    normalise a trailing ``Z`` to ``+00:00`` for the rare feed that uses it, and
    return None when there is nothing usable."""
    if not value or not value.strip():
        return None

    text = value.strip()
    if text.endswith("Z"):
        return f"{text[:-1]}+00:00"
    return text
