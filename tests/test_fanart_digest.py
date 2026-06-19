"""Tests for the weekly fan-art digest: album submission storage, the run-once
build/dedup logic, the weekly schedule helper, and album image extraction."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from database import Database
from services.collectors.reddit.feed_fetcher import RedditPost
from services.digests import fanart
from services.digests.fanart import (
    _author_handle,
    _is_direct_image,
    _iso_week_key,
    next_weekly_run_at,
    run_fanart_digest_once,
)
from services.publisher import album_image_urls


_TZ = ZoneInfo("Europe/Kyiv")


def _post(post_id: str, title: str, image: str | None, author: str = "/u/artist") -> RedditPost:
    return RedditPost(
        post_id=post_id,
        web_url=f"https://www.reddit.com/r/MarvelRivals/comments/{post_id[3:]}/x/",
        title=title,
        body_text="",
        created_at="2026-06-12T10:00:00+00:00",
        image_url=image,
        author=author,
    )


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        article_timezone="Europe/Kyiv",
        fanart_subreddit="MarvelRivals",
        fanart_flair="Fan Art",
        fanart_digest_count=10,
    )


# --- DB album submission ----------------------------------------------------------


def test_create_album_submission_stores_one_part_per_image(tmp_path) -> None:
    db = Database(str(tmp_path / "bot.db"))
    asyncio.run(db.init())

    sid = asyncio.run(
        db.create_album_submission(
            username="fanart_digest",
            original_text="orig",
            caption="CAPTION TEXT",
            image_urls=["https://i.redd.it/a.jpg", "https://i.redd.it/b.jpg", "https://i.redd.it/a.jpg"],
            source_type="reddit_fanart",
            source_id="2026-W24",
            source_url="https://www.reddit.com/r/MarvelRivals/",
        )
    )
    submission = asyncio.run(db.get_submission(sid))

    assert submission["message_type"] == "album"
    assert submission["media_url"] == "https://i.redd.it/a.jpg"
    parts = submission["parts"]
    assert [p["media_url"] for p in parts] == ["https://i.redd.it/a.jpg", "https://i.redd.it/b.jpg"]  # dedup
    assert all(str(p["message_type"]) == "album" for p in parts)
    assert parts[0]["text"] == "CAPTION TEXT"
    assert not parts[1]["text"]


def test_create_album_submission_caps_at_ten_images(tmp_path) -> None:
    db = Database(str(tmp_path / "bot.db"))
    asyncio.run(db.init())

    sid = asyncio.run(
        db.create_album_submission(
            username="fanart_digest",
            original_text="orig",
            caption="c",
            image_urls=[f"https://i.redd.it/{i}.jpg" for i in range(15)],
            source_type="reddit_fanart",
            source_id="2026-W25",
            source_url="https://www.reddit.com/r/MarvelRivals/",
        )
    )
    submission = asyncio.run(db.get_submission(sid))
    assert len(submission["parts"]) == 10


# --- run-once build / dedup -------------------------------------------------------


class _FakeDB:
    def __init__(self, *, seen: bool = False) -> None:
        self._seen = seen
        self.created: dict | None = None
        self.marked: list[dict] = []

    async def is_source_seen(self, source_type: str, source_id: str) -> bool:
        return self._seen

    async def create_album_submission(self, **kwargs) -> int:
        self.created = kwargs
        return 42

    async def mark_source_seen(self, **kwargs) -> None:
        self.marked.append(kwargs)


def _patch_fetch(monkeypatch, posts: list[RedditPost]) -> None:
    class _Fetcher:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def fetch_recent_posts(self) -> list[RedditPost]:
            return posts

    monkeypatch.setattr(fanart, "RedditSearchFetcher", _Fetcher)


def test_run_once_builds_album_from_image_posts_and_marks_week(monkeypatch) -> None:
    posts = [
        _post("t3_1", "Art One", "https://i.redd.it/1.jpg", "/u/alice"),
        _post("t3_2", "Art Two", "https://i.redd.it/2.jpg", "u/bob"),
        _post("t3_3", "No image", None, "/u/carol"),  # excluded: no image
    ]
    _patch_fetch(monkeypatch, posts)
    sent: list[int] = []

    async def _send(bot, config, db, submission_id):
        sent.append(submission_id)

    monkeypatch.setattr(fanart, "send_submission_to_moderation", _send)
    db = _FakeDB()
    now = datetime(2026, 6, 12, 18, 0, tzinfo=_TZ)

    created = asyncio.run(run_fanart_digest_once(bot=None, config=_config(), db=db, now=now))

    assert created is True
    assert sent == [42]
    assert db.created["image_urls"] == ["https://i.redd.it/1.jpg", "https://i.redd.it/2.jpg"]
    caption = db.created["caption"]
    # Author nicks are hyperlinked to their posts; post titles are NOT shown.
    assert '<a href="https://www.reddit.com/r/MarvelRivals/comments/1/x/">u/alice</a>' in caption
    assert "u/bob" in caption
    assert "Art One" not in caption and "Art Two" not in caption
    assert "сподобався" in caption  # engagement prompt
    assert "#ФанАрт" in caption  # tags
    assert db.marked and db.marked[0]["source_id"] == _iso_week_key(now)


def test_run_once_skips_when_week_already_seen(monkeypatch) -> None:
    _patch_fetch(monkeypatch, [_post("t3_1", "Art", "https://i.redd.it/1.jpg")])
    db = _FakeDB(seen=True)
    now = datetime(2026, 6, 12, 18, 0, tzinfo=_TZ)

    created = asyncio.run(run_fanart_digest_once(bot=None, config=_config(), db=db, now=now))

    assert created is False
    assert db.created is None
    assert db.marked == []


def test_run_once_force_bypasses_week_guard(monkeypatch) -> None:
    # /fanartdigest force must build the digest even when the week is already seen.
    _patch_fetch(monkeypatch, [_post("t3_1", "Art", "https://i.redd.it/1.jpg", "u/alice")])
    sent: list[int] = []

    async def _send(bot, config, db, submission_id):
        sent.append(submission_id)

    monkeypatch.setattr(fanart, "send_submission_to_moderation", _send)
    db = _FakeDB(seen=True)
    now = datetime(2026, 6, 12, 18, 0, tzinfo=_TZ)

    created = asyncio.run(run_fanart_digest_once(bot=None, config=_config(), db=db, now=now, force=True))

    assert created is True
    assert sent == [42]
    assert db.created is not None


def test_run_once_skips_non_direct_image_posts(monkeypatch) -> None:
    # Gallery/video/external posts expose only a signed preview URL Telegram can't
    # fetch in an album, so they must be dropped (not break the whole digest).
    posts = [
        _post("t3_1", "Gallery", "https://preview.redd.it/x.jpg?s=sig"),
        _post("t3_2", "Direct", "https://i.redd.it/2.jpg"),
    ]
    _patch_fetch(monkeypatch, posts)

    async def _send(bot, config, db, submission_id):
        pass

    monkeypatch.setattr(fanart, "send_submission_to_moderation", _send)
    db = _FakeDB()
    created = asyncio.run(
        run_fanart_digest_once(bot=None, config=_config(), db=db, now=datetime(2026, 6, 12, 18, 0, tzinfo=_TZ))
    )

    assert created is True
    assert db.created["image_urls"] == ["https://i.redd.it/2.jpg"]


def test_is_direct_image_only_accepts_iredd_still_images() -> None:
    assert _is_direct_image("https://i.redd.it/a.jpg") is True
    assert _is_direct_image("https://i.redd.it/a.PNG") is True
    assert _is_direct_image("https://preview.redd.it/a.jpg?s=x") is False
    assert _is_direct_image("https://external-preview.redd.it/a.jpg") is False
    assert _is_direct_image("https://i.redd.it.attacker.com/a.jpg") is False
    assert _is_direct_image("https://i.redd.it/a.gif") is False  # animated -> album would skip it
    assert _is_direct_image("https://i.redd.it/a") is False  # no still-image extension
    assert _is_direct_image(None) is False


def test_run_once_does_not_mark_week_when_no_arts(monkeypatch) -> None:
    _patch_fetch(monkeypatch, [_post("t3_3", "No image", None)])
    db = _FakeDB()
    now = datetime(2026, 6, 12, 18, 0, tzinfo=_TZ)

    created = asyncio.run(run_fanart_digest_once(bot=None, config=_config(), db=db, now=now))

    # Empty week: don't mark seen, so a later manual run can still post.
    assert created is False
    assert db.created is None
    assert db.marked == []


# --- helpers ----------------------------------------------------------------------


def test_next_weekly_run_at_lands_on_target_weekday() -> None:
    now = datetime(2026, 6, 10, 10, 0, tzinfo=_TZ)  # a Wednesday
    nr = next_weekly_run_at(now, 4, 18)  # Friday 18:00
    assert nr.weekday() == 4 and nr.hour == 18
    assert now < nr <= now + timedelta(days=7)


def test_next_weekly_run_at_same_weekday_before_and_after_hour() -> None:
    base = datetime(2026, 6, 10, 0, 0, tzinfo=_TZ)
    friday = (base + timedelta(days=(4 - base.weekday()) % 7)).replace(hour=10)
    assert next_weekly_run_at(friday, 4, 18).date() == friday.date()  # before 18:00 -> today
    after = friday.replace(hour=19)
    assert next_weekly_run_at(after, 4, 18).date() == (friday + timedelta(days=7)).date()


def test_author_handle_normalizes() -> None:
    assert _author_handle("/u/Alice") == "u/Alice"
    assert _author_handle("u/Bob") == "u/Bob"
    assert _author_handle("Carol") == "u/Carol"
    assert _author_handle("") == ""


def test_album_image_urls_dedups_and_caps() -> None:
    sub = {"parts": [{"media_url": "a"}, {"media_url": "b"}, {"media_url": "a"}, {"media_url": ""}], "media_url": "x"}
    assert album_image_urls(sub) == ["a", "b"]
    assert album_image_urls({"parts": [], "media_url": "only"}) == ["only"]
    many = {"parts": [{"media_url": f"u{i}"} for i in range(12)]}
    assert album_image_urls(many) == [f"u{i}" for i in range(10)]
