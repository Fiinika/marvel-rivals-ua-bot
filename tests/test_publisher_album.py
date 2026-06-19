"""Tests for the album publish helpers: the index Telegram blames in a media-group
error (used to drop one rejected image and retry the otherwise-atomic album)."""

from __future__ import annotations

from services.publisher import _failing_media_index, album_caption_html, link_preview_options_for


def test_link_preview_enabled_for_youtube_only() -> None:
    yt = link_preview_options_for({"source_type": "youtube", "source_url": "https://www.youtube.com/watch?v=x"})
    assert yt.is_disabled is False
    assert yt.url == "https://www.youtube.com/watch?v=x"
    assert yt.prefer_large_media is True

    # Other sources keep previews disabled, and youtube without a URL stays disabled.
    assert link_preview_options_for({"source_type": "bluesky", "source_url": "https://x"}).is_disabled is True
    assert link_preview_options_for({"source_type": "youtube", "source_url": ""}).is_disabled is True


def test_album_caption_html_prerendered_vs_formatted() -> None:
    # Digest captions are already HTML and pass through untouched.
    raw = album_caption_html({"source_type": "reddit_fanart", "draft_text": '<a href="u">x</a>'})
    assert raw == '<a href="u">x</a>'

    # Collector-album captions are plain text formatted through the post formatter
    # (the "Джерело:" line is linkified, the body escaped).
    formatted = album_caption_html(
        {"source_type": "bluesky", "draft_text": "Текст.\n\nДжерело: MR", "source_url": "https://x"}
    )
    assert '<a href="https://x">MR</a>' in formatted


def test_failing_media_index_parses_message_number() -> None:
    assert _failing_media_index("Bad Request: failed to send message #2 with the error ...", 5) == 1
    assert _failing_media_index("Bad Request: failed to send message #1 ...", 5) == 0


def test_failing_media_index_falls_back_to_zero() -> None:
    assert _failing_media_index("Bad Request: PHOTO_INVALID_DIMENSIONS", 5) == 0  # no "message #N"
    assert _failing_media_index("failed to send message #99", 3) == 0  # out of range
    assert _failing_media_index("", 1) == 0
