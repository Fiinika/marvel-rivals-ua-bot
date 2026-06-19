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
    download_external_video,
    link_preview_options_for,
    needs_external_video_download,
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


# --- external (Bluesky) video download routing + guards ------------------------


def test_needs_external_video_download_only_for_external_source_videos() -> None:
    assert needs_external_video_download(
        {"message_type": "video", "source_type": "bluesky", "media_url": "https://pds.host.bsky.network/x"}
    ) is True
    # A user video carries a Telegram file_id (sent directly) -> not downloaded.
    assert needs_external_video_download(
        {"message_type": "video", "source_type": "bluesky", "file_id": "abc", "media_url": "https://x"}
    ) is False
    # Non-download source / non-video / non-http URL -> not downloaded.
    assert needs_external_video_download(
        {"message_type": "video", "source_type": "reddit", "media_url": "https://x"}
    ) is False
    assert needs_external_video_download(
        {"message_type": "photo", "source_type": "bluesky", "media_url": "https://x"}
    ) is False
    assert needs_external_video_download(
        {"message_type": "video", "source_type": "bluesky", "media_url": "ftp://x"}
    ) is False
    # http is rejected too — only https is fetchable, so routing must match.
    assert needs_external_video_download(
        {"message_type": "video", "source_type": "bluesky", "media_url": "http://pds.host.bsky.network/x"}
    ) is False


def _patch_async_client(monkeypatch, handler) -> None:
    """Route download_external_video's own AsyncClient through a MockTransport."""
    orig = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: orig(transport=httpx.MockTransport(handler), follow_redirects=False)
    )


def test_download_external_video_accepts_mp4_within_cap(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"\x00\x00\x00 ftypisom")

    _patch_async_client(monkeypatch, handler)
    result = asyncio.run(download_external_video("https://pds.host.bsky.network/x", max_bytes=10_000_000))

    assert result is not None
    data, filename = result
    assert data.startswith(b"\x00\x00\x00 ftyp")
    assert filename == "video.mp4"


def test_download_external_video_rejects_internal_url_without_fetching(monkeypatch) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, headers={"content-type": "video/mp4"}, content=b"x")

    _patch_async_client(monkeypatch, handler)
    result = asyncio.run(download_external_video("https://169.254.169.254/x.mp4", max_bytes=10_000_000))

    assert result is None
    assert calls == []  # SSRF guard short-circuits before any request


def test_download_external_video_rejects_non_video_content_type(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>")

    _patch_async_client(monkeypatch, handler)
    result = asyncio.run(download_external_video("https://pds.host.bsky.network/x", max_bytes=10_000_000))

    assert result is None
