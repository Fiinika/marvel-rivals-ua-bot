"""Tests for native YouTube video publishing: the filename sanitiser and the
send_youtube_post branch — native upload when a video is downloaded, link-preview
fallback when it is not."""

from __future__ import annotations

import asyncio

import services.publisher as publisher
from services.publisher import DISABLED_LINK_PREVIEW, send_youtube_post
from services.youtube_video import _safe_filename


class _FakeBot:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def send_video(self, **kwargs):
        self.calls.append("send_video")
        return None

    async def send_message(self, **kwargs):
        self.calls.append("send_message")
        return None


def test_safe_filename_sanitises() -> None:
    assert _safe_filename("Trailer: S8.5!") == "Trailer_S8_5"
    assert _safe_filename("") == "video"
    assert len(_safe_filename("x" * 200)) <= 60


def test_send_youtube_post_uploads_native_video(monkeypatch) -> None:
    async def _fake_download(url, *, max_bytes):
        return (b"video-bytes", "clip.mp4")

    monkeypatch.setattr(publisher, "download_youtube_video", _fake_download)
    bot = _FakeBot()

    asyncio.run(
        send_youtube_post(
            bot, 1, "https://youtu.be/x", "caption", parse_mode="HTML",
            link_preview=DISABLED_LINK_PREVIEW, max_bytes=1_000_000,
        )
    )

    assert "send_video" in bot.calls
    assert "send_message" not in bot.calls  # short caption rides on the video


def test_send_youtube_post_falls_back_to_link_preview(monkeypatch) -> None:
    async def _fake_download(url, *, max_bytes):
        return None  # too large / unavailable

    monkeypatch.setattr(publisher, "download_youtube_video", _fake_download)
    bot = _FakeBot()

    asyncio.run(
        send_youtube_post(
            bot, 1, "https://youtu.be/x", "caption", parse_mode="HTML",
            link_preview=DISABLED_LINK_PREVIEW, max_bytes=1_000_000,
        )
    )

    assert "send_message" in bot.calls
    assert "send_video" not in bot.calls


def test_send_youtube_post_falls_back_when_upload_rejected(monkeypatch) -> None:
    from aiogram.exceptions import TelegramBadRequest

    async def _fake_download(url, *, max_bytes):
        return (b"video-bytes", "clip.mp4")

    class _RejectingBot(_FakeBot):
        async def send_video(self, **kwargs):
            self.calls.append("send_video")
            raise TelegramBadRequest(method=None, message="Request Entity Too Large")

    monkeypatch.setattr(publisher, "download_youtube_video", _fake_download)
    bot = _RejectingBot()

    asyncio.run(
        send_youtube_post(
            bot, 1, "https://youtu.be/x", "caption", parse_mode="HTML",
            link_preview=DISABLED_LINK_PREVIEW, max_bytes=1_000_000,
        )
    )

    # Upload rejected -> still posts as a link-preview text.
    assert "send_video" in bot.calls
    assert "send_message" in bot.calls
