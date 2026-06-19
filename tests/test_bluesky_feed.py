"""Tests for Bluesky author-feed parsing: reposts/replies are dropped, image
posts expose full-size URLs and a public web URL, video posts are flagged but
carry no media, and nanosecond timestamps are normalized for date-gating."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

from services.collectors.base import ListingEntry
from services.collectors.bluesky.collector import BlueskyCollector
from services.collectors.bluesky.feed_fetcher import (
    BlueskyPost,
    _normalize_timestamp,
    parse_author_feed,
)


def _image_post(uri: str, text: str, *, handle: str = "marvelrivalsglobal.bsky.social") -> dict:
    return {
        "post": {
            "uri": f"at://did:plc:abc/app.bsky.feed.post/{uri}",
            "author": {"handle": handle},
            "record": {"text": text, "createdAt": "2026-06-12T16:00:37.994523881Z"},
            "embed": {
                "$type": "app.bsky.embed.images#view",
                "images": [
                    {"fullsize": "https://cdn.bsky.app/img/feed_fullsize/a.jpg", "alt": ""},
                    {"fullsize": "https://cdn.bsky.app/img/feed_fullsize/b.jpg", "alt": ""},
                ],
            },
        }
    }


_VIDEO_CID = "bafkreieyoi3vyluyzrqqzctywn2we7zwdpl6nkpcd5hz43tuprkgy6odke"


def _video_post(uri: str, text: str, *, cid: str | None = _VIDEO_CID) -> dict:
    embed = {"$type": "app.bsky.embed.video#view", "playlist": "https://video.bsky.app/x.m3u8"}
    if cid is not None:
        embed["cid"] = cid
    return {
        "post": {
            "uri": f"at://did:plc:abc/app.bsky.feed.post/{uri}",
            "author": {"handle": "marvelrivalsglobal.bsky.social"},
            "record": {"text": text, "createdAt": "2026-06-11T10:00:00.000Z"},
            "embed": embed,
        }
    }


def _text_post(uri: str, text: str) -> dict:
    return {
        "post": {
            "uri": f"at://did:plc:abc/app.bsky.feed.post/{uri}",
            "author": {"handle": "marvelrivalsglobal.bsky.social"},
            "record": {"text": text, "createdAt": "2026-06-10T09:00:00Z"},
        }
    }


def test_parse_author_feed_extracts_images_and_web_url() -> None:
    feed = {"feed": [_image_post("aaa", "New event!")]}
    posts = parse_author_feed(feed, actor="marvelrivalsglobal.bsky.social")

    assert len(posts) == 1
    post = posts[0]
    assert isinstance(post, BlueskyPost)
    assert post.text == "New event!"
    assert post.image_urls == (
        "https://cdn.bsky.app/img/feed_fullsize/a.jpg",
        "https://cdn.bsky.app/img/feed_fullsize/b.jpg",
    )
    assert post.has_video is False
    assert post.web_url == "https://bsky.app/profile/marvelrivalsglobal.bsky.social/post/aaa"


def test_image_urls_outside_bluesky_cdn_are_dropped() -> None:
    # A tampered fullsize value must never become a media URL: only Bluesky's own
    # image CDN (https cdn.bsky.app) is trusted; anything else (an internal SSRF
    # target, a non-CDN host, or http) is dropped so it is never fetched bot-side
    # or handed to Telegram.
    post = {
        "post": {
            "uri": "at://did:plc:abc/app.bsky.feed.post/evil",
            "author": {"handle": "marvelrivalsglobal.bsky.social"},
            "record": {"text": "Pic", "createdAt": "2026-06-12T16:00:00Z"},
            "embed": {
                "$type": "app.bsky.embed.images#view",
                "images": [
                    {"fullsize": "https://cdn.bsky.app/img/feed_fullsize/ok.jpg", "alt": ""},
                    {"fullsize": "http://169.254.169.254/latest/meta-data/", "alt": ""},
                    {"fullsize": "https://evil.example.com/x.jpg", "alt": ""},
                    {"fullsize": "https://cdn.bsky.app.attacker.com/x.jpg", "alt": ""},
                ],
            },
        }
    }
    posts = parse_author_feed({"feed": [post]}, actor="marvelrivalsglobal.bsky.social")

    assert posts[0].image_urls == ("https://cdn.bsky.app/img/feed_fullsize/ok.jpg",)


def test_parse_author_feed_skips_reposts() -> None:
    repost = _image_post("ccc", "Reposted")
    repost["reason"] = {"$type": "app.bsky.feed.defs#reasonRepost"}
    feed = {"feed": [repost, _text_post("ddd", "Own post")]}

    posts = parse_author_feed(feed, actor="marvelrivalsglobal.bsky.social")

    assert [p.text for p in posts] == ["Own post"]


def test_video_post_exposes_blob_cid_and_author_did() -> None:
    feed = {"feed": [_video_post("vvv", "Watch the trailer")]}
    posts = parse_author_feed(feed, actor="marvelrivalsglobal.bsky.social")

    assert len(posts) == 1
    assert posts[0].has_video is True
    assert posts[0].image_urls == ()
    assert posts[0].video_cid == _VIDEO_CID
    assert posts[0].author_did == "did:plc:abc"  # owns the blob, used for getBlob


def test_video_post_without_cid_is_not_flagged_as_video() -> None:
    # No CID -> the original MP4 cannot be fetched, so the post is not a video post
    # (it falls back to a text post downstream).
    feed = {"feed": [_video_post("nocid", "Trailer", cid=None)]}
    posts = parse_author_feed(feed, actor="marvelrivalsglobal.bsky.social")

    assert posts[0].has_video is False
    assert posts[0].video_cid is None


def test_video_post_with_malformed_cid_is_rejected() -> None:
    feed = {"feed": [_video_post("badcid", "Trailer", cid="not-a-valid-cid!!")]}
    posts = parse_author_feed(feed, actor="marvelrivalsglobal.bsky.social")

    assert posts[0].has_video is False
    assert posts[0].video_cid is None


def test_duplicate_uris_collapse() -> None:
    feed = {"feed": [_text_post("same", "First"), _text_post("same", "Dup")]}
    posts = parse_author_feed(feed, actor="marvelrivalsglobal.bsky.social")

    assert len(posts) == 1


def test_malformed_entries_are_skipped() -> None:
    feed = {"feed": [None, {}, {"post": {"author": {"handle": "x"}}}, _text_post("ok", "Good")]}
    posts = parse_author_feed(feed, actor="marvelrivalsglobal.bsky.social")

    assert [p.text for p in posts] == ["Good"]


def test_non_dict_record_or_author_does_not_drop_valid_posts() -> None:
    # A truthy non-dict record/author (partial/garbage payload) must be tolerated,
    # not raise AttributeError and abort the whole feed — which would drop every
    # other valid post collected in that run.
    bad_record = {
        "post": {
            "uri": "at://did:plc:abc/app.bsky.feed.post/b1",
            "author": {"handle": "h"},
            "record": "oops-a-string",
        }
    }
    bad_author = {
        "post": {
            "uri": "at://did:plc:abc/app.bsky.feed.post/b2",
            "author": ["not", "a", "dict"],
            "record": {"text": "Валідний текст"},
        }
    }
    feed = {"feed": [bad_record, bad_author, _text_post("ok", "Good")]}

    texts = [p.text for p in parse_author_feed(feed, actor="marvelrivalsglobal.bsky.social")]

    assert "Good" in texts
    assert "Валідний текст" in texts  # bad_author still has a valid record


def test_non_dict_payload_returns_empty() -> None:
    # A 200 with a valid but non-object JSON body must not crash the parser.
    for payload in ([], None, "oops", 42, {"feed": "not-a-list"}):
        assert parse_author_feed(payload, actor="x") == []


def test_normalize_timestamp_handles_nanoseconds_and_z() -> None:
    normalized = _normalize_timestamp("2026-06-12T16:00:37.994523881Z")
    # Trimmed to microseconds, Z -> +00:00, and parseable by datetime.fromisoformat.
    assert normalized == "2026-06-12T16:00:37.994523+00:00"
    assert datetime.fromisoformat(normalized).year == 2026


def test_normalize_timestamp_plain_utc() -> None:
    assert _normalize_timestamp("2026-06-10T09:00:00Z") == "2026-06-10T09:00:00+00:00"


def test_normalize_timestamp_none_for_empty() -> None:
    assert _normalize_timestamp("") is None
    assert _normalize_timestamp(None) is None


def test_collector_maps_post_to_draft_candidate() -> None:
    post = BlueskyPost(
        uri="at://did:plc:abc/app.bsky.feed.post/zzz",
        web_url="https://bsky.app/profile/h/post/zzz",
        text="Великий анонс нового сезону!\nДеталі нижче.",
        created_at="2026-06-12T16:00:37.994523+00:00",
        image_urls=("https://cdn/a.jpg", "https://cdn/b.jpg"),
        has_video=False,
    )
    collector = BlueskyCollector(
        config=SimpleNamespace(bluesky_actor="marvelrivalsglobal.bsky.social"),
        db=None,
        bot=None,
    )

    candidate = asyncio.run(collector.parse_entry(ListingEntry(dedup_key=post.uri, payload=post)))

    assert candidate.source_id == post.uri
    assert candidate.source_url == post.web_url
    assert candidate.title == "Великий анонс нового сезону!"  # first line, no media/tags
    assert candidate.body_text == post.text
    assert candidate.has_media is True
    assert candidate.media_url == "https://cdn/a.jpg"
    assert candidate.additional_media_urls == ["https://cdn/b.jpg"]
    assert candidate.source_name == "Bluesky Marvel Rivals"
    assert candidate.article_date == "2026-06-12T16:00:37.994523+00:00"
    assert candidate.article_date_display == "2026-06-12"


def test_collector_resolves_video_to_native_video_candidate(monkeypatch) -> None:
    import services.collectors.bluesky.collector as bc

    async def fake_resolve(did: str, cid: str) -> str:
        return f"https://pds.host.bsky.network/xrpc/com.atproto.sync.getBlob?did={did}&cid={cid}"

    monkeypatch.setattr(bc, "resolve_video_blob_url", fake_resolve)

    post = BlueskyPost(
        uri="at://did:plc:abc/app.bsky.feed.post/vid",
        web_url="https://bsky.app/profile/h/post/vid",
        text="Новий трейлер!",
        created_at="2026-06-11T10:00:00+00:00",
        image_urls=(),
        has_video=True,
        video_cid=_VIDEO_CID,
    )
    collector = BlueskyCollector(
        config=SimpleNamespace(bluesky_actor="x", enable_bluesky_video_download=True),
        db=None,
        bot=None,
    )

    candidate = asyncio.run(collector.parse_entry(ListingEntry(dedup_key=post.uri, payload=post)))

    assert candidate.has_media is True
    assert candidate.media_type == "video"
    assert "com.atproto.sync.getBlob" in candidate.media_url
    assert candidate.additional_media_urls is None


def test_collector_video_falls_back_to_text_when_download_disabled() -> None:
    post = BlueskyPost(
        uri="at://did:plc:abc/app.bsky.feed.post/vid",
        web_url="https://bsky.app/profile/h/post/vid",
        text="Новий трейлер!",
        created_at=None,
        image_urls=(),
        has_video=True,
        video_cid=_VIDEO_CID,
    )
    collector = BlueskyCollector(
        config=SimpleNamespace(bluesky_actor="x", enable_bluesky_video_download=False),
        db=None,
        bot=None,
    )

    candidate = asyncio.run(collector.parse_entry(ListingEntry(dedup_key=post.uri, payload=post)))

    assert candidate.has_media is False
    assert candidate.media_type == "none"


def test_collector_video_falls_back_to_text_when_unresolvable(monkeypatch) -> None:
    import services.collectors.bluesky.collector as bc

    async def fail_resolve(_did: str, _cid: str):
        return None

    monkeypatch.setattr(bc, "resolve_video_blob_url", fail_resolve)

    post = BlueskyPost(
        uri="at://did:plc:abc/app.bsky.feed.post/vid",
        web_url="https://bsky.app/profile/h/post/vid",
        text="Трейлер!",
        created_at=None,
        image_urls=(),
        has_video=True,
        video_cid=_VIDEO_CID,
    )
    collector = BlueskyCollector(
        config=SimpleNamespace(bluesky_actor="x", enable_bluesky_video_download=True),
        db=None,
        bot=None,
    )

    candidate = asyncio.run(collector.parse_entry(ListingEntry(dedup_key=post.uri, payload=post)))

    assert candidate.has_media is False
    assert candidate.media_type == "none"


def test_collector_title_fallback_for_image_only_post() -> None:
    post = BlueskyPost(
        uri="at://did:plc:abc/app.bsky.feed.post/img",
        web_url="https://bsky.app/profile/h/post/img",
        text="",
        created_at=None,
        image_urls=("https://cdn/a.jpg",),
        has_video=False,
    )
    collector = BlueskyCollector(
        config=SimpleNamespace(bluesky_actor="marvelrivalsglobal.bsky.social"),
        db=None,
        bot=None,
    )

    candidate = asyncio.run(collector.parse_entry(ListingEntry(dedup_key=post.uri, payload=post)))

    assert candidate.title  # non-empty fallback so cross-source dedup has something to compare
    assert candidate.has_media is True
    assert candidate.article_date_display is None
