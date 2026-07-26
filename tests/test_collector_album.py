"""Tests for the collector album branch: a single-part draft with 2+ photos becomes
one grouped album submission; single-image / text / multi-part stay as before."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import services.collectors.runner as runner_module
from services.collectors.base import CollectionStats, CollectorDefinition, DraftCandidate
from services.collectors.runner import BaseNewsCollector, _album_images
from services.gemini import GeminiDraftPackage


DEFN = CollectorDefinition(collector_id="bluesky", source_type="bluesky", title_key="x", button_key="y")


def _candidate(images: list[str]) -> DraftCandidate:
    return DraftCandidate(
        source_id="s1",
        source_url="https://example.com/p",
        title="t",
        body_text="b",
        source_name="MR",
        username="u",
        original_text="o",
        has_media=bool(images),
        media_url=images[0] if images else None,
        media_type="photo" if images else "none",
        additional_media_urls=images[1:] if len(images) > 1 else None,
    )


class _FakeDB:
    def __init__(self) -> None:
        self.album: dict | None = None
        self.single: dict | None = None
        self.marked: list[dict] = []

    async def create_album_submission(self, **kwargs) -> int:
        self.album = kwargs
        return 1

    async def create_ai_news_submission(self, **kwargs) -> int:
        self.single = kwargs
        return 2

    async def mark_source_seen(self, **kwargs) -> None:
        self.marked.append(kwargs)


class _FakeGen:
    def __init__(self, parts: list[str]) -> None:
        self.parts = parts

    async def generate_draft_package(self, draft_input, *, max_part_length) -> GeminiDraftPackage:
        return GeminiDraftPackage(draft_parts=self.parts, tags=["t"])


class _Collector(BaseNewsCollector):
    definition = DEFN

    async def fetch_listing(self):
        return []

    async def parse_entry(self, entry):  # pragma: no cover - unused
        raise NotImplementedError

    def missing_gemini_warning(self) -> str:
        return ""


def _run(candidate: DraftCandidate, parts: list[str], monkeypatch) -> tuple[_FakeDB, list[int]]:
    db = _FakeDB()
    collector = _Collector(config=SimpleNamespace(article_timezone="Europe/Kyiv"), db=db, bot=None)
    sent: list[int] = []

    async def _send(bot, config, db_, submission_id):
        sent.append(submission_id)

    monkeypatch.setattr(runner_module, "send_submission_to_moderation", _send)
    stats = CollectionStats(collector_id="bluesky", source_type="bluesky", source_title="x")
    asyncio.run(collector._create_moderation_submissions(candidate, _FakeGen(parts), stats))
    return db, sent


def _video_candidate() -> DraftCandidate:
    return DraftCandidate(
        source_id="v1",
        source_url="https://bsky.app/profile/x/post/1",
        title="t",
        body_text="b",
        source_name="Bluesky",
        username="u",
        original_text="o",
        has_media=True,
        media_url="https://pds.bsky.network/xrpc/com.atproto.sync.getBlob?did=d&cid=c",
        media_type="video",
        additional_media_urls=None,
    )


def test_video_candidate_is_stored_as_a_video_part(monkeypatch) -> None:
    # Both the moderation preview and the publisher route on message_type, and the
    # native-video download path is keyed on "video". Stored as "photo" (the old
    # behaviour) a Bluesky MP4 went to send_photo and could only degrade to text.
    db, _sent = _run(_video_candidate(), ["draft"], monkeypatch)

    assert db.album is None and db.single is not None
    assert db.single["message_type"] == "video"
    assert db.single["media_type"] == "video"


def test_video_part_reaches_the_native_download_path(monkeypatch) -> None:
    from services.publisher import needs_external_video_download

    db, _sent = _run(_video_candidate(), ["draft"], monkeypatch)
    part = {
        "message_type": db.single["message_type"],
        "source_type": db.single["source_type"],
        "media_url": db.single["media_url"],
        "file_id": None,
    }

    assert needs_external_video_download(part) is True


def test_photo_candidate_is_still_stored_as_a_photo_part(monkeypatch) -> None:
    db, _sent = _run(_candidate(["https://cdn.bsky.app/a.jpg"]), ["draft"], monkeypatch)

    assert db.single["message_type"] == "photo"


def test_text_candidate_is_still_stored_as_text(monkeypatch) -> None:
    db, _sent = _run(_candidate([]), ["draft"], monkeypatch)

    assert db.single["message_type"] == "text"


def test_two_photos_single_part_creates_album(monkeypatch) -> None:
    db, sent = _run(_candidate(["a", "b", "c"]), ["caption"], monkeypatch)
    assert db.album is not None and db.single is None
    assert db.album["image_urls"] == ["a", "b", "c"]
    assert db.album["caption"] == "caption"
    assert sent == [1]
    assert db.marked  # marked seen after a successful send


def test_single_photo_uses_normal_submission(monkeypatch) -> None:
    db, sent = _run(_candidate(["a"]), ["caption"], monkeypatch)
    assert db.single is not None and db.album is None
    assert sent == [2]


def test_text_only_uses_normal_submission(monkeypatch) -> None:
    db, sent = _run(_candidate([]), ["caption"], monkeypatch)
    assert db.single is not None and db.album is None


def test_multipart_draft_is_not_albumed(monkeypatch) -> None:
    # A multi-part article keeps the per-part image mapping, never an album.
    db, _ = _run(_candidate(["a", "b"]), ["part1", "part2"], monkeypatch)
    assert db.single is not None and db.album is None


def test_album_images_helper() -> None:
    assert _album_images(_candidate(["a", "b"])) == ["a", "b"]
    assert _album_images(_candidate(["a"])) == ["a"]
    assert _album_images(_candidate([])) == []
