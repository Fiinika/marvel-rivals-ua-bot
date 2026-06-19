"""Tests for the YouTube collector: Atom-feed parsing (video id / link / thumbnail
/ description, dedup by video id, newest-first sort, malformed handling) and the
collector wiring — esports/Shorts keyword filtering and the normalised-title
dedup that collapses the channel's re-uploads of the same trailer."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from services.collectors.base import ListingEntry
from services.collectors.youtube.collector import (
    YouTubeCollector,
    _dedup_key,
    _is_excluded,
    _normalize_title,
)
from services.collectors.youtube.feed_fetcher import (
    YouTubeVideo,
    _normalize_timestamp,
    parse_feed,
)


def _entry(
    video_id: str,
    title: str,
    published: str,
    *,
    description: str = "Опис відео.",
    thumb: bool = True,
    with_video_id: bool = True,
    link: bool = True,
) -> str:
    lines = ["  <entry>", f"    <id>yt:video:{video_id}</id>"]
    if with_video_id:
        lines.append(f"    <yt:videoId>{video_id}</yt:videoId>")
    lines.append(f"    <title>{title}</title>")
    if link:
        lines.append(f'    <link rel="alternate" href="https://www.youtube.com/watch?v={video_id}"/>')
    lines.append("    <author><name>Marvel Rivals</name></author>")
    lines.append(f"    <published>{published}</published>")
    lines.append("    <media:group>")
    lines.append(f"      <media:title>{title}</media:title>")
    if thumb:
        lines.append(
            f'      <media:thumbnail url="https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" '
            'width="480" height="360"/>'
        )
    lines.append(f"      <media:description>{description}</media:description>")
    lines.append("    </media:group>")
    lines.append("  </entry>")
    return "\n".join(lines)


def _feed(*entries: str, channel: str = "Marvel Rivals") -> str:
    body = "\n".join(entries)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
        'xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">\n'
        f"  <title>{channel}</title>\n"
        f"{body}\n"
        "</feed>"
    )


def test_parse_feed_extracts_fields() -> None:
    feed = _feed(_entry("VID1", "Season 8.5 Official Trailer", "2026-06-12T16:00:00+00:00"))
    videos = parse_feed(feed)

    assert len(videos) == 1
    video = videos[0]
    assert isinstance(video, YouTubeVideo)
    assert video.video_id == "VID1"
    assert video.title == "Season 8.5 Official Trailer"
    assert video.web_url == "https://www.youtube.com/watch?v=VID1"
    assert video.published == "2026-06-12T16:00:00+00:00"
    assert video.description == "Опис відео."
    assert video.thumbnail_url == "https://i.ytimg.com/vi/VID1/hqdefault.jpg"
    assert video.channel_name == "Marvel Rivals"


def test_parse_feed_sorts_newest_first() -> None:
    # Older entry listed first (entry[0] is backdated) — must come out newest-first.
    feed = _feed(
        _entry("OLD", "Old video", "2026-06-10T09:00:00+00:00"),
        _entry("NEW", "New video", "2026-06-14T09:00:00+00:00"),
    )
    videos = parse_feed(feed)

    assert [v.video_id for v in videos] == ["NEW", "OLD"]


def test_parse_feed_dedups_by_video_id() -> None:
    feed = _feed(
        _entry("SAME", "First", "2026-06-12T16:00:00+00:00"),
        _entry("SAME", "Duplicate", "2026-06-12T16:00:00+00:00"),
    )
    videos = parse_feed(feed)

    assert len(videos) == 1


def test_parse_feed_skips_entry_without_video_id() -> None:
    feed = _feed(
        _entry("NOID", "No id", "2026-06-12T16:00:00+00:00", with_video_id=False),
        _entry("OK", "Good", "2026-06-12T16:00:00+00:00"),
    )
    videos = parse_feed(feed)

    assert [v.video_id for v in videos] == ["OK"]


def test_parse_feed_thumbnail_fallback_when_missing() -> None:
    feed = _feed(_entry("NOTHUMB", "No thumb", "2026-06-12T16:00:00+00:00", thumb=False))
    videos = parse_feed(feed)

    assert videos[0].thumbnail_url == "https://i.ytimg.com/vi/NOTHUMB/hqdefault.jpg"


def test_parse_feed_invalid_xml_returns_empty() -> None:
    assert parse_feed("not xml at all") == []
    assert parse_feed("") == []


def test_normalize_timestamp_handles_z_and_empty() -> None:
    assert _normalize_timestamp("2026-06-12T16:00:00Z") == "2026-06-12T16:00:00+00:00"
    assert _normalize_timestamp("2026-06-12T16:00:00+00:00") == "2026-06-12T16:00:00+00:00"
    assert _normalize_timestamp("") is None
    assert _normalize_timestamp(None) is None


def test_normalize_title_collapses_case_and_punctuation() -> None:
    assert _normalize_title("Season 8.5 — Official Trailer!") == _normalize_title("season 8 5   official trailer")


def test_dedup_key_collapses_same_day_reupload_but_not_cross_day() -> None:
    # Same title, same publish day (the channel's double-post quirk) -> same key.
    a = YouTubeVideo("ID1", "u1", "Season 8.5 Trailer", "", "2026-06-13T18:00:00+00:00", None, "MR")
    b = YouTubeVideo("ID2", "u2", "Season 8.5 Trailer", "", "2026-06-13T18:05:00+00:00", None, "MR")
    assert _dedup_key(a) == _dedup_key(b)
    assert _dedup_key(a).startswith("yt-title:")

    # Same title reused on a DIFFERENT day -> distinct key, so a genuinely-new
    # video with a recycled title is not dropped forever (seen_sources has no TTL).
    c = YouTubeVideo("ID3", "u3", "Season 8.5 Trailer", "", "2026-07-20T18:00:00+00:00", None, "MR")
    assert _dedup_key(a) != _dedup_key(c)

    # Empty title falls back to a per-video key so distinct videos stay distinct.
    e = YouTubeVideo("ID4", "u4", "", "", "2026-06-13T18:00:00+00:00", None, "MR")
    f = YouTubeVideo("ID5", "u5", "", "", "2026-06-13T18:00:00+00:00", None, "MR")
    assert _dedup_key(e) != _dedup_key(f)
    assert _dedup_key(e) == "yt-video:ID4"


def test_is_excluded_matches_case_insensitive_substring() -> None:
    keywords = frozenset({"esports", "highlights", "#shorts"})
    assert _is_excluded("Grand Finals HIGHLIGHTS", keywords) is True
    assert _is_excluded("Funny clip #Shorts", keywords) is True
    assert _is_excluded("Season 8.5 Official Trailer", keywords) is False
    assert _is_excluded("", keywords) is False


class _StubFetcher:
    def __init__(self, videos: list[YouTubeVideo]) -> None:
        self._videos = videos

    async def fetch_recent_videos(self) -> list[YouTubeVideo]:
        return self._videos


def _collector(videos: list[YouTubeVideo]) -> YouTubeCollector:
    config = SimpleNamespace(
        youtube_channel_id="UCWzmOSSiSPbVnVu3ZAyDx2w",
        youtube_exclude_keywords=frozenset({"esports", "highlights"}),
    )
    collector = YouTubeCollector(config=config, db=None, bot=None)
    collector.fetcher = _StubFetcher(videos)
    return collector


def test_fetch_listing_filters_excluded_and_collapses_reuploads() -> None:
    videos = [
        YouTubeVideo("E1", "u", "Pro League Highlights Day 1", "", "2026-06-14T00:00:00+00:00", None, "MR"),
        YouTubeVideo("T1", "u", "Season 8.5 Trailer", "desc", "2026-06-13T00:00:00+00:00", None, "MR"),
        YouTubeVideo("T2", "u", "Season 8.5 Trailer", "desc", "2026-06-13T00:01:00+00:00", None, "MR"),
        YouTubeVideo("R1", "u", "Dev Vision Update", "desc", "2026-06-12T00:00:00+00:00", None, "MR"),
    ]
    collector = _collector(videos)

    entries = asyncio.run(collector.fetch_listing())

    # Highlights excluded; the two identical-title trailers collapse to one entry.
    titles = [e.payload.title for e in entries]
    assert "Pro League Highlights Day 1" not in titles
    assert titles.count("Season 8.5 Trailer") == 1
    assert "Dev Vision Update" in titles
    assert len(entries) == 2


def test_fetch_listing_prefers_described_full_upload_over_short() -> None:
    # The channel ships the same trailer as a description-less Short (listed first
    # in the feed) AND a full /watch upload that carries the real description. The
    # collapsed entry must keep the described upload so the AI draft has text.
    videos = [
        YouTubeVideo(
            "SHORT", "https://www.youtube.com/shorts/SHORT",
            "18 vs. 18 Bounty Annihilation | #MarvelRivals", "",
            "2026-06-09T12:00:00+00:00", None, "MR",
        ),
        YouTubeVideo(
            "FULL", "https://www.youtube.com/watch?v=FULL",
            "18 vs. 18 Bounty Annihilation | MarvelRivals", "Thirty-six heroes, one arena...",
            "2026-06-09T11:59:00+00:00", None, "MR",
        ),
    ]
    collector = _collector(videos)

    entries = asyncio.run(collector.fetch_listing())

    assert len(entries) == 1  # the Short and the full upload collapse to one trailer
    kept = entries[0].payload
    assert kept.video_id == "FULL"  # the described variant wins regardless of order
    assert kept.description.startswith("Thirty-six heroes")


def test_fetch_listing_keeps_described_upload_when_short_comes_later() -> None:
    # Symmetric case: the described upload is seen first, a description-less Short
    # of the same trailer comes later — the Short must not overwrite it.
    videos = [
        YouTubeVideo(
            "FULL", "https://www.youtube.com/watch?v=FULL",
            "K'un-Lun: Shenloong Arena | MarvelRivals", "The legendary tournament...",
            "2026-06-09T12:00:00+00:00", None, "MR",
        ),
        YouTubeVideo(
            "SHORT", "https://www.youtube.com/shorts/SHORT",
            "K'un-Lun: Shenloong Arena | #MarvelRivals", "",
            "2026-06-09T11:00:00+00:00", None, "MR",
        ),
    ]
    collector = _collector(videos)

    entries = asyncio.run(collector.fetch_listing())

    assert len(entries) == 1
    assert entries[0].payload.video_id == "FULL"


def test_parse_entry_maps_video_to_draft_candidate() -> None:
    video = YouTubeVideo(
        video_id="VID9",
        web_url="https://www.youtube.com/watch?v=VID9",
        title="Season 8.5 Official Trailer",
        description="Опис трейлера.",
        published="2026-06-12T16:00:00+00:00",
        thumbnail_url="https://i.ytimg.com/vi/VID9/hqdefault.jpg",
        channel_name="Marvel Rivals",
    )
    collector = _collector([])
    entry = ListingEntry(dedup_key=_dedup_key(video), payload=video)

    candidate = asyncio.run(collector.parse_entry(entry))

    assert candidate.source_id == entry.dedup_key  # normalised-title key, not the video id
    assert candidate.source_url == video.web_url
    assert candidate.title == "Season 8.5 Official Trailer"
    assert candidate.body_text == "Опис трейлера."
    assert candidate.source_name == "YouTube Marvel Rivals"
    # No attached photo: published as text + a playable link preview of the video.
    assert candidate.has_media is False
    assert candidate.media_url is None
    assert candidate.media_type == "none"
    assert candidate.article_date == "2026-06-12T16:00:00+00:00"
    assert candidate.article_date_display == "2026-06-12"
