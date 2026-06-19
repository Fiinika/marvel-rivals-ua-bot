"""Tests for the album publish helpers: the index Telegram blames in a media-group
error (used to drop one rejected image and retry the otherwise-atomic album), and
the SSRF/size guards on the bot-side album image downloader."""

from __future__ import annotations

import asyncio

import httpx

import services.publisher as publisher
from services.publisher import (
    _download_album_photo,
    _failing_media_index,
    _is_fetchable_media_url,
    album_caption_html,
    link_preview_options_for,
)


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)


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


# --- SSRF / size guards on the bot-side album downloader -----------------------


def test_is_fetchable_media_url_allows_public_https_only() -> None:
    assert _is_fetchable_media_url("https://cdn.bsky.app/img/a.jpg") is True
    assert _is_fetchable_media_url("https://i.redd.it/a.jpg") is True
    assert _is_fetchable_media_url("https://8.8.8.8/a.jpg") is True  # public IP literal is fine


def test_is_fetchable_media_url_blocks_ssrf_and_non_https() -> None:
    assert _is_fetchable_media_url("http://cdn.bsky.app/a.jpg") is False  # not https
    assert _is_fetchable_media_url("ftp://cdn.bsky.app/a.jpg") is False
    assert _is_fetchable_media_url("https:///a.jpg") is False  # no host
    # Internal / metadata IP literals must be rejected.
    assert _is_fetchable_media_url("https://169.254.169.254/latest/meta-data/") is False
    assert _is_fetchable_media_url("https://127.0.0.1/a.jpg") is False
    assert _is_fetchable_media_url("https://10.0.0.5/a.jpg") is False
    assert _is_fetchable_media_url("https://192.168.1.1/a.jpg") is False


def test_download_album_photo_rejects_internal_url_without_fetching() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"x")

    async def run():
        async with _mock_client(handler) as client:
            return await _download_album_photo(client, "https://169.254.169.254/x.jpg", 0)

    assert asyncio.run(run()) is None
    assert calls == []  # the guard short-circuits before any request is sent


def test_download_album_photo_rejects_oversized_declared_length() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # content sets Content-Length; make it exceed the cap so the precheck fires.
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"x" * (publisher._ALBUM_MAX_IMAGE_BYTES + 1))

    async def run():
        async with _mock_client(handler) as client:
            return await _download_album_photo(client, "https://cdn.bsky.app/big.jpg", 0)

    assert asyncio.run(run()) is None


def test_download_album_photo_aborts_oversized_stream_without_content_length(monkeypatch) -> None:
    monkeypatch.setattr(publisher, "_ALBUM_MAX_IMAGE_BYTES", 8)

    async def _async_body():
        # Many small chunks, no Content-Length -> the precheck passes and the
        # streaming loop must abort once the running total exceeds the cap.
        for _ in range(100):
            yield b"xxxxx"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=_async_body())

    async def run():
        async with _mock_client(handler) as client:
            return await _download_album_photo(client, "https://cdn.bsky.app/drip.jpg", 0)

    assert asyncio.run(run()) is None


def test_download_album_photo_rejects_non_image_content_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>internal</html>")

    async def run():
        async with _mock_client(handler) as client:
            return await _download_album_photo(client, "https://cdn.bsky.app/x", 0)

    assert asyncio.run(run()) is None


def test_download_album_photo_accepts_valid_image() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/png"}, content=b"\x89PNG-data")

    async def run():
        async with _mock_client(handler) as client:
            return await _download_album_photo(client, "https://cdn.bsky.app/a.png", 2)

    result = asyncio.run(run())
    assert result is not None
    data, filename = result
    assert data == b"\x89PNG-data"
    assert filename == "art_3.png"
