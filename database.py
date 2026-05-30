from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite


Submission = dict[str, Any]
EditState = dict[str, Any]

STATUS_PENDING = "pending"
STATUS_PUBLISHED = "published"
STATUS_REJECTED = "rejected"


class Database:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    async def init(self) -> None:
        if self.path.parent != Path("."):
            self.path.parent.mkdir(parents=True, exist_ok=True)

        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    message_type TEXT NOT NULL,
                    original_text TEXT,
                    draft_text TEXT,
                    file_id TEXT,
                    admin_message_id INTEGER,
                    admin_media_message_id INTEGER,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    published_at TEXT
                )
                """
            )
            await self._ensure_column(db, "submissions", "admin_media_message_id", "INTEGER")
            await self._ensure_column(db, "submissions", "media_url", "TEXT")
            await self._ensure_column(db, "submissions", "media_type", "TEXT")
            await self._ensure_column(db, "submissions", "source_url", "TEXT")
            await self._ensure_column(db, "submissions", "source_type", "TEXT")
            await self._ensure_column(db, "submissions", "source_id", "TEXT")
            await self._ensure_column(db, "submissions", "article_date", "TEXT")
            await self._ensure_column(db, "submissions", "article_date_display", "TEXT")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_type TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    title TEXT,
                    article_date TEXT,
                    first_seen_at TEXT NOT NULL
                )
                """
            )
            await self._ensure_column(db, "seen_sources", "article_date", "TEXT")
            await db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_seen_sources_source
                ON seen_sources (source_type, source_id)
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_edit_states (
                    admin_id INTEGER PRIMARY KEY,
                    submission_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            await db.commit()

    async def _ensure_column(
        self,
        db: aiosqlite.Connection,
        table_name: str,
        column_name: str,
        column_type: str,
    ) -> None:
        cursor = await db.execute(f"PRAGMA table_info({table_name})")
        columns = await cursor.fetchall()
        if any(column[1] == column_name for column in columns):
            return

        await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")

    async def create_submission(
        self,
        *,
        user_id: int,
        username: str | None,
        message_type: str,
        original_text: str | None,
        file_id: str | None,
    ) -> int:
        now = utc_now()
        draft_text = original_text or ""

        async with self._connect() as db:
            cursor = await db.execute(
                """
                INSERT INTO submissions (
                    user_id,
                    username,
                    message_type,
                    original_text,
                    draft_text,
                    file_id,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    username,
                    message_type,
                    original_text,
                    draft_text,
                    file_id,
                    STATUS_PENDING,
                    now,
                    now,
                ),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def create_ai_news_submission(
        self,
        *,
        username: str,
        original_text: str,
        draft_text: str,
        message_type: str,
        media_url: str | None,
        media_type: str,
        source_type: str,
        source_id: str,
        source_url: str,
        article_date: str | None,
        article_date_display: str | None,
    ) -> int:
        now = utc_now()

        async with self._connect() as db:
            cursor = await db.execute(
                """
                INSERT INTO submissions (
                    user_id,
                    username,
                    message_type,
                    original_text,
                    draft_text,
                    file_id,
                    media_url,
                    media_type,
                    source_url,
                    source_type,
                    source_id,
                    article_date,
                    article_date_display,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    0,
                    username,
                    message_type,
                    original_text,
                    draft_text,
                    None,
                    media_url,
                    media_type,
                    source_url,
                    source_type,
                    source_id,
                    article_date,
                    article_date_display,
                    STATUS_PENDING,
                    now,
                    now,
                ),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def set_admin_message_id(self, submission_id: int, admin_message_id: int) -> None:
        await self._execute(
            """
            UPDATE submissions
            SET admin_message_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (admin_message_id, utc_now(), submission_id),
        )

    async def set_admin_media_message_id(self, submission_id: int, admin_media_message_id: int | None) -> None:
        await self._execute(
            """
            UPDATE submissions
            SET admin_media_message_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (admin_media_message_id, utc_now(), submission_id),
        )

    async def get_submission(self, submission_id: int) -> Submission | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT * FROM submissions WHERE id = ?",
                (submission_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_latest_user_submission(self, user_id: int) -> Submission | None:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT *
                FROM submissions
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def is_source_seen(self, source_type: str, source_id: str) -> bool:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT 1
                FROM seen_sources
                WHERE source_type = ? AND source_id = ?
                LIMIT 1
                """,
                (source_type, source_id),
            )
            row = await cursor.fetchone()
            return row is not None

    async def mark_source_seen(
        self,
        *,
        source_type: str,
        source_id: str,
        source_url: str,
        title: str | None,
        article_date: str | None,
    ) -> None:
        await self._execute(
            """
            INSERT OR IGNORE INTO seen_sources (
                source_type,
                source_id,
                source_url,
                title,
                article_date,
                first_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (source_type, source_id, source_url, title, article_date, utc_now()),
        )

    async def get_latest_seen_article_date(self, source_type: str) -> str | None:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT article_date
                FROM seen_sources
                WHERE source_type = ? AND article_date IS NOT NULL AND article_date != ''
                ORDER BY article_date DESC
                LIMIT 1
                """,
                (source_type,),
            )
            row = await cursor.fetchone()
            return str(row["article_date"]) if row else None

    async def update_draft_text(self, submission_id: int, draft_text: str) -> None:
        await self._execute(
            """
            UPDATE submissions
            SET draft_text = ?, updated_at = ?
            WHERE id = ?
            """,
            (draft_text, utc_now(), submission_id),
        )

    async def update_submission_content(
        self,
        submission_id: int,
        *,
        message_type: str,
        draft_text: str,
        file_id: str | None,
        admin_media_message_id: int | None,
    ) -> None:
        await self._execute(
            """
            UPDATE submissions
            SET message_type = ?,
                draft_text = ?,
                file_id = ?,
                admin_media_message_id = ?,
                media_url = NULL,
                media_type = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (message_type, draft_text, file_id, admin_media_message_id, message_type, utc_now(), submission_id),
        )

    async def mark_published(self, submission_id: int) -> None:
        now = utc_now()
        await self._execute(
            """
            UPDATE submissions
            SET status = ?, updated_at = ?, published_at = ?
            WHERE id = ?
            """,
            (STATUS_PUBLISHED, now, now, submission_id),
        )

    async def mark_rejected(self, submission_id: int) -> None:
        await self._execute(
            """
            UPDATE submissions
            SET status = ?, updated_at = ?
            WHERE id = ?
            """,
            (STATUS_REJECTED, utc_now(), submission_id),
        )

    async def set_admin_edit_state(self, admin_id: int, submission_id: int) -> None:
        await self._execute(
            """
            INSERT OR REPLACE INTO admin_edit_states (admin_id, submission_id, created_at)
            VALUES (?, ?, ?)
            """,
            (admin_id, submission_id, utc_now()),
        )

    async def get_admin_edit_state(self, admin_id: int) -> EditState | None:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT * FROM admin_edit_states WHERE admin_id = ?",
                (admin_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_latest_admin_edit_state(self) -> EditState | None:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT *
                FROM admin_edit_states
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def clear_admin_edit_state(self, admin_id: int) -> None:
        await self._execute(
            "DELETE FROM admin_edit_states WHERE admin_id = ?",
            (admin_id,),
        )

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(self.path) as connection:
            connection.row_factory = aiosqlite.Row
            yield connection

    async def _execute(self, query: str, params: tuple[Any, ...]) -> None:
        async with self._connect() as db:
            await db.execute(query, params)
            await db.commit()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
