"""Download a YouTube video so it can be re-uploaded as a NATIVE Telegram video
(plays inline) instead of a tap-to-open link preview.

yt-dlp is imported lazily so the bot still starts (and the link-preview fallback
still works) if it is not installed. Only a single progressive MP4 (audio+video
in one stream) is selected, so no ffmpeg merge is needed; videos over the byte
limit are skipped and the caller falls back to the link preview.
"""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
from pathlib import Path


logger = logging.getLogger(__name__)

# Progressive (combined audio+video) MP4 only — no ffmpeg merge required.
_FORMAT = "best[ext=mp4][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none]"


async def download_youtube_video(url: str, *, max_bytes: int) -> tuple[bytes, str] | None:
    """Return (video bytes, filename) for ``url`` within ``max_bytes``, or None when
    yt-dlp is missing, the video is too large, or the download fails."""
    if not url or not url.strip():
        return None
    return await asyncio.to_thread(_download_sync, url.strip(), max_bytes)


def _download_sync(url: str, max_bytes: int) -> tuple[bytes, str] | None:
    try:
        import yt_dlp
    except ImportError:
        logger.warning("yt-dlp is not installed; cannot download YouTube video for %s", url)
        return None

    with tempfile.TemporaryDirectory(prefix="ytvideo_") as tmp:
        options = {
            "format": _FORMAT,
            "outtmpl": str(Path(tmp) / "%(id)s.%(ext)s"),
            "max_filesize": max_bytes,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "retries": 2,
            "socket_timeout": 30,
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)
                filename = ydl.prepare_filename(info)
        except Exception as exc:  # yt-dlp raises DownloadError (incl. max_filesize abort)
            logger.warning("yt-dlp could not download %s: %s", url, exc)
            return None

        path = Path(filename)
        if not path.exists():
            logger.info("No YouTube video within the %s-byte limit for %s", max_bytes, url)
            return None

        data = path.read_bytes()
        if not data or len(data) > max_bytes:
            logger.info("YouTube video for %s exceeds the byte limit (%s); using link preview", url, len(data))
            return None

        title = str((info or {}).get("title") or "video")
        return data, f"{_safe_filename(title)}.mp4"


def _safe_filename(title: str) -> str:
    cleaned = re.sub(r"[^\w\-]+", "_", title, flags=re.UNICODE).strip("_")
    return (cleaned or "video")[:60]
