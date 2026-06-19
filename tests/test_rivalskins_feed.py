"""Tests for the rivalskins.com leaks collector: WordPress-RSS parsing, picking the
skin-render image (skipping the recurring banner, rejecting off-site hosts), pubDate
normalisation, and the collector → DraftCandidate mapping (a rumour source)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from services.collectors.base import ListingEntry
from services.collectors.rivalskins.collector import RivalSkinsCollector
from services.collectors.rivalskins.feed_fetcher import (
    RivalSkinsPost,
    _normalize_pubdate,
    parse_feed,
)
from services.gemini import RUMOR_SOURCE_TYPES


HERO = "https://rivalskins.com/wp-content/uploads/2026/06/Phoenix-White-Crown-Phoenix.jpg"
BANNER = "https://rivalskins.com/wp-content/uploads/2026/06/s85-launch-skins-1024x576.jpg"
OFFSITE = "https://evil.example.com/x.jpg"


def _item(
    title: str,
    *,
    post_id: str,
    link: str = "https://rivalskins.com/leaks/phoenix-white-crown-phoenix/",
    pubdate: str = "Tue, 09 Jun 2026 20:07:38 +0000",
    imgs: tuple[str, ...] = (HERO,),
    body: str = "Новий скін Фенікс.",
) -> str:
    img_html = "".join(f'<img src="{u}"/>' for u in imgs)
    return (
        "<item>"
        f"<title>{title}</title>"
        f"<link>{link}</link>"
        f"<pubDate>{pubdate}</pubDate>"
        f'<guid isPermaLink="false">{post_id}</guid>'
        f"<content:encoded><![CDATA[<div>{img_html}<p>{body}</p></div>]]></content:encoded>"
        "</item>"
    )


def _feed(*items: str) -> str:
    body = "".join(items)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        f"<channel><title>RivalSkins</title>{body}</channel></rss>"
    )


def test_parse_feed_extracts_fields_and_hero_image() -> None:
    feed = _feed(_item("Phoenix: White Crown Phoenix", post_id="https://rivalskins.com/?p=4798"))
    posts = parse_feed(feed)

    assert len(posts) == 1
    post = posts[0]
    assert isinstance(post, RivalSkinsPost)
    assert post.title == "Phoenix: White Crown Phoenix"
    assert post.post_id == "https://rivalskins.com/?p=4798"
    assert post.web_url == "https://rivalskins.com/leaks/phoenix-white-crown-phoenix/"
    assert post.image_url == HERO
    assert "Новий скін Фенікс." in post.body_text
    assert post.created_at == "2026-06-09T20:07:38+00:00"


def test_parse_feed_skips_recurring_banner_and_picks_render() -> None:
    # Banner listed FIRST must be skipped in favour of the unique skin render.
    feed = _feed(_item("Magik Skin", post_id="p1", imgs=(BANNER, HERO)))
    assert parse_feed(feed)[0].image_url == HERO


def test_parse_feed_rejects_offsite_image_host() -> None:
    feed = _feed(_item("Skin", post_id="p2", imgs=(OFFSITE,)))
    assert parse_feed(feed)[0].image_url is None


def test_parse_feed_dedups_by_guid() -> None:
    feed = _feed(
        _item("First", post_id="same"),
        _item("Dup", post_id="same"),
    )
    assert len(parse_feed(feed)) == 1


def test_parse_feed_invalid_xml_returns_empty() -> None:
    assert parse_feed("not xml") == []
    assert parse_feed("") == []


def test_parse_feed_rejects_entity_expansion_bomb() -> None:
    # The feed is untrusted: a DTD with nested entities (billion-laughs) must be
    # refused by the defused parser, not expanded — parse_feed returns [].
    bomb = (
        '<?xml version="1.0"?>'
        "<!DOCTYPE rss [<!ENTITY a \"aaaaaaaaaa\">"
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>'
        '<rss version="2.0"><channel><item><title>&c;</title></item></channel></rss>'
    )
    assert parse_feed(bomb) == []


def test_normalize_pubdate_rfc822_to_utc_iso() -> None:
    assert _normalize_pubdate("Tue, 09 Jun 2026 20:07:38 +0000") == "2026-06-09T20:07:38+00:00"
    # A non-UTC offset is converted to UTC.
    assert _normalize_pubdate("Tue, 09 Jun 2026 23:07:38 +0300") == "2026-06-09T20:07:38+00:00"
    assert _normalize_pubdate("") is None
    assert _normalize_pubdate("garbage") is None


def test_rivalskins_is_a_rumour_source() -> None:
    assert "rivalskins" in RUMOR_SOURCE_TYPES


def test_collector_maps_post_to_rumour_photo_candidate() -> None:
    post = RivalSkinsPost(
        post_id="https://rivalskins.com/?p=1",
        web_url="https://rivalskins.com/leaks/x/",
        title="Magik: Soulless Sword",
        body_text="Опис скіна Magik.",
        created_at="2026-06-09T20:07:38+00:00",
        image_url=HERO,
    )
    collector = RivalSkinsCollector(
        config=SimpleNamespace(rivalskins_feed_url="https://rivalskins.com/category/leaks/feed/"),
        db=None,
        bot=None,
    )

    candidate = asyncio.run(collector.parse_entry(ListingEntry(dedup_key=post.post_id, payload=post)))

    assert candidate.source_id == post.post_id
    assert candidate.source_url == post.web_url
    assert candidate.title == "Magik: Soulless Sword"
    assert candidate.has_media is True
    assert candidate.media_url == HERO
    assert candidate.media_type == "photo"
    assert candidate.article_date_display == "2026-06-09"
    assert candidate.source_name == "RivalSkins"
