"""Tests for /cleanup: only finished submissions go, pending and the seen-sources
memory always stay, and nothing is deleted without an explicit confirm."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import handlers.admin as admin
from database import STATUS_PUBLISHED, STATUS_REJECTED, Database
from services.i18n import t


def _old(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


async def _seed(path) -> Database:
    db = Database(str(path))
    await db.init()

    async def _age(submission_id: int, age_days: int) -> None:
        # Backdate the row so the age filter has something to work with.
        async with db._connect() as conn:  # noqa: SLF001 - test needs to age a row
            await conn.execute(
                "UPDATE submissions SET updated_at = ? WHERE id = ?",
                (_old(age_days), submission_id),
            )
            await conn.commit()

    async def add(status: str, age_days: int) -> int:
        submission_id = await db.create_ai_news_submission(
            username="collector",
            original_text="o",
            draft_text="d",
            message_type="text",
            media_url=None,
            media_type="none",
            source_type="bluesky",
            source_id=f"at://{status}{age_days}",
            source_url="https://bsky.app/x",
            article_date=None,
            article_date_display=None,
            tags=["патч"],
        )
        if status == STATUS_PUBLISHED:
            await db.mark_published(submission_id)
        else:
            await db.mark_rejected(submission_id)
        await _age(submission_id, age_days)
        return submission_id

    await add(STATUS_PUBLISHED, 90)
    await add(STATUS_REJECTED, 60)
    await add(STATUS_PUBLISHED, 1)
    pending_id = await db.create_submission(
        user_id=1, username="u", message_type="text", original_text="o", file_id=None,
    )
    await _age(pending_id, 365)

    await db.mark_source_seen(
        source_type="bluesky",
        source_id="at://x",
        source_url="https://bsky.app/x",
        title="Old news",
        article_date="2026-01-01",
    )
    return db


def test_counts_only_finished_submissions_past_the_cutoff(tmp_path) -> None:
    db = asyncio.run(_seed(tmp_path / "bot.db"))

    assert asyncio.run(db.count_processed_submissions(older_than_days=30)) == 2
    assert asyncio.run(db.count_processed_submissions(older_than_days=365)) == 0
    # 0 days means "everything already processed", including today's.
    assert asyncio.run(db.count_processed_submissions(older_than_days=0)) == 3


def test_delete_keeps_pending_and_recent_rows(tmp_path) -> None:
    db = asyncio.run(_seed(tmp_path / "bot.db"))

    assert asyncio.run(db.delete_processed_submissions(older_than_days=30)) == 2

    async def statuses() -> list[str]:
        async with db._connect() as conn:  # noqa: SLF001
            rows = await (await conn.execute("SELECT status FROM submissions ORDER BY id")).fetchall()
            return [str(row["status"]) for row in rows]

    # The one-day-old published row and the year-old PENDING row both survive.
    assert asyncio.run(statuses()) == [STATUS_PUBLISHED, "pending"]


def test_delete_never_touches_the_seen_sources_memory(tmp_path) -> None:
    # Dropping this is what would make the bot re-queue old news.
    db = asyncio.run(_seed(tmp_path / "bot.db"))

    asyncio.run(db.delete_processed_submissions(older_than_days=0))

    assert asyncio.run(db.is_source_seen("bluesky", "at://x")) is True


def test_delete_removes_the_parts_and_tag_links(tmp_path) -> None:
    db = asyncio.run(_seed(tmp_path / "bot.db"))
    asyncio.run(db.delete_processed_submissions(older_than_days=30))

    async def orphans() -> tuple[int, int]:
        async with db._connect() as conn:  # noqa: SLF001
            parts = await (await conn.execute(
                "SELECT COUNT(*) c FROM submission_parts WHERE submission_id NOT IN (SELECT id FROM submissions)"
            )).fetchone()
            links = await (await conn.execute(
                "SELECT COUNT(*) c FROM submission_tags WHERE submission_id NOT IN (SELECT id FROM submissions)"
            )).fetchone()
            return int(parts["c"]), int(links["c"])

    assert asyncio.run(orphans()) == (0, 0)


def test_delete_is_a_no_op_when_nothing_matches(tmp_path) -> None:
    db = asyncio.run(_seed(tmp_path / "bot.db"))

    assert asyncio.run(db.delete_processed_submissions(older_than_days=1000)) == 0


def test_include_pending_wipes_every_status(tmp_path) -> None:
    db = asyncio.run(_seed(tmp_path / "bot.db"))

    deleted = asyncio.run(db.delete_processed_submissions(older_than_days=0, include_pending=True))

    assert deleted == 4  # 2 published + 1 rejected + 1 pending
    assert asyncio.run(db.count_submissions_by_status()) == {}
    # Even a full wipe keeps the dedup memory, so old news does not come back.
    assert asyncio.run(db.is_source_seen("bluesky", "at://x")) is True


def test_status_breakdown_counts_every_state(tmp_path) -> None:
    db = asyncio.run(_seed(tmp_path / "bot.db"))

    assert asyncio.run(db.count_submissions_by_status()) == {
        "published": 2,
        "rejected": 1,
        "pending": 1,
    }


# --- the command ---------------------------------------------------------------


class _FakeMessage:
    def __init__(self, *, user_id: int = 7, chat_id: int = 100, chat_type: str = "private") -> None:
        self.from_user = SimpleNamespace(id=user_id, username="admin")
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.answers: list[str] = []

    async def answer(self, text: str) -> None:
        self.answers.append(text)


class _FakeDB:
    def __init__(self, total: int = 5, breakdown: dict[str, int] | None = None) -> None:
        self.total = total
        self.breakdown = breakdown or {"pending": 3}
        self.deleted_with: list[tuple[int, bool]] = []
        self.vacuumed = False

    async def count_processed_submissions(self, *, older_than_days: int, include_pending: bool = False) -> int:
        return self.total

    async def count_submissions_by_status(self) -> dict[str, int]:
        return self.breakdown

    async def delete_processed_submissions(self, *, older_than_days: int, include_pending: bool = False) -> int:
        self.deleted_with.append((older_than_days, include_pending))
        return self.total

    async def vacuum(self) -> None:
        self.vacuumed = True

    def size_bytes(self) -> int:
        return 0


def _config() -> SimpleNamespace:
    return SimpleNamespace(admin_user_ids=frozenset({7}), admin_chat_id=100)


def _run(message, db, args: str | None):
    asyncio.run(
        admin.cleanup_command(
            message, command=SimpleNamespace(args=args), bot=None, config=_config(), db=db
        )
    )


def test_without_confirm_nothing_is_deleted() -> None:
    message, db = _FakeMessage(), _FakeDB(total=5)
    _run(message, db, None)

    assert db.deleted_with == []
    assert "5" in message.answers[0] and "confirm" in message.answers[0]


def test_confirm_deletes_and_vacuums() -> None:
    message, db = _FakeMessage(), _FakeDB(total=5)
    _run(message, db, "confirm")

    assert db.deleted_with == [(30, False)]  # default window, finished rows only
    assert db.vacuumed is True


def test_a_custom_window_is_honoured() -> None:
    message, db = _FakeMessage(), _FakeDB(total=2)
    _run(message, db, "7 confirm")

    assert db.deleted_with == [(7, False)]


def test_arguments_may_come_in_any_order() -> None:
    message, db = _FakeMessage(), _FakeDB(total=2)
    _run(message, db, "confirm 7")

    assert db.deleted_with == [(7, False)]


def test_all_includes_pending_and_drops_the_age_window() -> None:
    # "/cleanup all" is the wipe-the-queue case: every status, any age.
    message, db = _FakeMessage(), _FakeDB(total=9)
    _run(message, db, "all confirm")

    assert db.deleted_with == [(0, True)]


def test_all_can_still_be_combined_with_an_age_window() -> None:
    message, db = _FakeMessage(), _FakeDB(total=4)
    _run(message, db, "all 7 confirm")

    assert db.deleted_with == [(7, True)]


def test_all_without_confirm_warns_about_pending() -> None:
    message, db = _FakeMessage(), _FakeDB(total=9)
    _run(message, db, "all")

    assert db.deleted_with == []
    assert message.answers[0] == t("admin.cleanup.preview_all", count=9, days=0)


def test_an_unknown_argument_is_rejected_without_deleting() -> None:
    message, db = _FakeMessage(), _FakeDB(total=5)
    _run(message, db, "wipe-everything")

    assert db.deleted_with == []
    assert "wipe-everything" in message.answers[0]


def test_nothing_to_clean_shows_what_is_actually_in_the_database() -> None:
    # The confusing case from real use: the moderation chat is full, but every
    # draft is still pending, so the default filter matches none of them.
    message, db = _FakeMessage(), _FakeDB(total=0, breakdown={"pending": 12})
    _run(message, db, "confirm")

    assert db.deleted_with == []
    answer = message.answers[0]
    assert "12" in answer  # the pending count is spelled out
    assert "/cleanup all" in answer  # and the way to remove them is offered


def test_non_admins_are_refused() -> None:
    message, db = _FakeMessage(user_id=999), _FakeDB()
    _run(message, db, "confirm")

    assert db.deleted_with == []
    assert message.answers == [t("admin.cleanup.no_permission")]


def test_a_public_group_is_refused() -> None:
    message, db = _FakeMessage(chat_id=555, chat_type="supergroup"), _FakeDB()
    _run(message, db, "confirm")

    assert db.deleted_with == []
    assert message.answers == [t("admin.cleanup.wrong_chat")]
