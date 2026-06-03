from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite


Submission = dict[str, Any]
SubmissionPart = dict[str, Any]
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
                CREATE TABLE IF NOT EXISTS submission_parts (
                    submission_id INTEGER NOT NULL,
                    part_index INTEGER NOT NULL,
                    message_type TEXT NOT NULL,
                    text TEXT NOT NULL,
                    file_id TEXT,
                    media_url TEXT,
                    media_type TEXT,
                    admin_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (submission_id, part_index)
                )
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_submission_parts_submission
                ON submission_parts (submission_id)
                """
            )
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
                    mode TEXT NOT NULL DEFAULT 'edit',
                    part_index INTEGER,
                    draft_message_id INTEGER,
                    created_at TEXT NOT NULL
                )
                """
            )
            await self._ensure_column(db, "admin_edit_states", "mode", "TEXT NOT NULL DEFAULT 'edit'")
            await self._ensure_column(db, "admin_edit_states", "part_index", "INTEGER")
            await self._ensure_column(db, "admin_edit_states", "draft_message_id", "INTEGER")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS tags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS submission_tags (
                    submission_id INTEGER NOT NULL,
                    tag_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (submission_id, tag_id)
                )
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_submission_tags_submission
                ON submission_tags (submission_id)
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_submission_tags_tag
                ON submission_tags (tag_id)
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
            submission_id = int(cursor.lastrowid)
            await self._replace_submission_parts(
                db,
                submission_id,
                [
                    {
                        "message_type": message_type,
                        "text": draft_text,
                        "file_id": file_id,
                        "media_url": None,
                        "media_type": _media_type_for(message_type),
                    }
                ],
                now,
            )
            await db.commit()
            return submission_id

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
        tags: list[str] | None = None,
        draft_parts: list[str] | None = None,
        additional_media_urls: list[str] | None = None,
    ) -> int:
        now = utc_now()
        normalized_draft_parts = _normalize_text_parts(draft_parts or [draft_text])
        stored_draft_text = _parts_to_draft_text(normalized_draft_parts)
        media_urls = _normalize_media_urls(media_url, additional_media_urls or [], media_type)
        primary_media_url = media_urls[0] if media_urls else None
        stored_media_type = media_type if primary_media_url else "none"
        stored_message_type = message_type if primary_media_url else "text"

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
                    stored_message_type,
                    original_text,
                    stored_draft_text,
                    None,
                    primary_media_url,
                    stored_media_type,
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
            submission_id = int(cursor.lastrowid)
            part_rows = []
            for index, part_text in enumerate(normalized_draft_parts, start=1):
                part_media_url = media_urls[index - 1] if index <= len(media_urls) else None
                has_part_media = bool(part_media_url and stored_media_type != "none")
                part_message_type = stored_message_type if has_part_media else "text"
                part_rows.append(
                    {
                        "message_type": part_message_type,
                        "text": part_text,
                        "file_id": None,
                        "media_url": part_media_url if has_part_media else None,
                        "media_type": stored_media_type if has_part_media else "none",
                    }
                )
            await self._replace_submission_parts(db, submission_id, part_rows, now)
            await self._set_submission_tags(db, submission_id, tags or [], now)
            await db.commit()
            return submission_id

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
            if row is None:
                return None

            submission = dict(row)
            submission["tags"] = await self._get_submission_tags(db, submission_id)
            submission["parts"] = await self._get_submission_parts(db, submission)
            return submission

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

    async def set_submission_tags(self, submission_id: int, tag_names: list[str]) -> None:
        async with self._connect() as db:
            await self._set_submission_tags(db, submission_id, tag_names, utc_now())
            await db.commit()

    async def get_submission_tags(self, submission_id: int) -> list[str]:
        async with self._connect() as db:
            return await self._get_submission_tags(db, submission_id)

    async def get_all_tags(self) -> list[str]:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT name
                FROM tags
                ORDER BY name
                """
            )
            rows = await cursor.fetchall()
            return [str(row["name"]) for row in rows]

    async def update_draft_text(self, submission_id: int, draft_text: str) -> None:
        now = utc_now()
        async with self._connect() as db:
            await db.execute(
                """
                UPDATE submissions
                SET draft_text = ?, updated_at = ?
                WHERE id = ?
                """,
                (draft_text, now, submission_id),
            )
            await self._replace_submission_parts(
                db,
                submission_id,
                [
                    {
                        "message_type": "text",
                        "text": draft_text,
                        "file_id": None,
                        "media_url": None,
                        "media_type": "none",
                    }
                ],
                now,
            )
            await db.commit()

    async def get_submission_part(self, submission_id: int, part_index: int) -> SubmissionPart | None:
        submission = await self.get_submission(submission_id)
        if submission is None:
            return None

        for part in submission.get("parts", []):
            if int(part["part_index"]) == part_index:
                return part

        return None

    async def set_submission_part_admin_message_id(
        self,
        submission_id: int,
        part_index: int,
        admin_message_id: int,
    ) -> None:
        await self._execute(
            """
            UPDATE submission_parts
            SET admin_message_id = ?, updated_at = ?
            WHERE submission_id = ? AND part_index = ?
            """,
            (admin_message_id, utc_now(), submission_id, part_index),
        )

    async def add_submission_part(
        self,
        submission_id: int,
        *,
        message_type: str,
        text: str,
        file_id: str | None,
        media_url: str | None,
        media_type: str | None,
        admin_message_id: int | None,
    ) -> int:
        now = utc_now()
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT COALESCE(MAX(part_index), 0) + 1 AS next_part_index
                FROM submission_parts
                WHERE submission_id = ?
                """,
                (submission_id,),
            )
            row = await cursor.fetchone()
            part_index = int(row["next_part_index"]) if row is not None else 1
            await db.execute(
                """
                INSERT INTO submission_parts (
                    submission_id,
                    part_index,
                    message_type,
                    text,
                    file_id,
                    media_url,
                    media_type,
                    admin_message_id,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    submission_id,
                    part_index,
                    message_type,
                    text,
                    file_id,
                    media_url,
                    media_type or _media_type_for(message_type),
                    admin_message_id,
                    now,
                    now,
                ),
            )
            await self._sync_submission_draft_text(db, submission_id, now)
            await db.commit()
            return part_index

    async def update_submission_part_text(self, submission_id: int, part_index: int, text: str) -> None:
        now = utc_now()
        async with self._connect() as db:
            await db.execute(
                """
                UPDATE submission_parts
                SET text = ?, updated_at = ?
                WHERE submission_id = ? AND part_index = ?
                """,
                (text, now, submission_id, part_index),
            )
            await self._sync_submission_draft_text(db, submission_id, now)
            await db.commit()

    async def update_submission_content(
        self,
        submission_id: int,
        *,
        message_type: str,
        draft_text: str,
        file_id: str | None,
        admin_media_message_id: int | None,
    ) -> None:
        now = utc_now()
        async with self._connect() as db:
            await db.execute(
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
                (message_type, draft_text, file_id, admin_media_message_id, message_type, now, submission_id),
            )
            await self._replace_submission_parts(
                db,
                submission_id,
                [
                    {
                        "message_type": message_type,
                        "text": draft_text,
                        "file_id": file_id,
                        "media_url": None,
                        "media_type": _media_type_for(message_type),
                        "admin_message_id": admin_media_message_id if message_type in {"photo", "video", "document"} else None,
                    }
                ],
                now,
            )
            await db.commit()

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

    async def set_admin_edit_state(
        self,
        admin_id: int,
        submission_id: int,
        *,
        mode: str = "edit",
        part_index: int | None = None,
        draft_message_id: int | None = None,
    ) -> None:
        await self._execute(
            """
            INSERT OR REPLACE INTO admin_edit_states (
                admin_id,
                submission_id,
                mode,
                part_index,
                draft_message_id,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (admin_id, submission_id, mode, part_index, draft_message_id, utc_now()),
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

    async def _set_submission_tags(
        self,
        db: aiosqlite.Connection,
        submission_id: int,
        tag_names: list[str],
        now: str,
    ) -> None:
        normalized_tags = _normalize_tag_names(tag_names)
        await db.execute("DELETE FROM submission_tags WHERE submission_id = ?", (submission_id,))
        for tag_name in normalized_tags:
            await db.execute(
                """
                INSERT OR IGNORE INTO tags (name, created_at)
                VALUES (?, ?)
                """,
                (tag_name, now),
            )
            cursor = await db.execute("SELECT id FROM tags WHERE name = ?", (tag_name,))
            row = await cursor.fetchone()
            if row is None:
                continue

            await db.execute(
                """
                INSERT OR IGNORE INTO submission_tags (submission_id, tag_id, created_at)
                VALUES (?, ?, ?)
                """,
                (submission_id, int(row["id"]), now),
            )

    async def _get_submission_tags(self, db: aiosqlite.Connection, submission_id: int) -> list[str]:
        cursor = await db.execute(
            """
            SELECT tags.name
            FROM submission_tags
            JOIN tags ON tags.id = submission_tags.tag_id
            WHERE submission_tags.submission_id = ?
            ORDER BY tags.name
            """,
            (submission_id,),
        )
        rows = await cursor.fetchall()
        return [str(row["name"]) for row in rows]

    async def _replace_submission_parts(
        self,
        db: aiosqlite.Connection,
        submission_id: int,
        parts: list[dict[str, Any]],
        now: str,
    ) -> None:
        await db.execute("DELETE FROM submission_parts WHERE submission_id = ?", (submission_id,))
        for index, part in enumerate(parts, start=1):
            await db.execute(
                """
                INSERT INTO submission_parts (
                    submission_id,
                    part_index,
                    message_type,
                    text,
                    file_id,
                    media_url,
                    media_type,
                    admin_message_id,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    submission_id,
                    index,
                    str(part.get("message_type") or "text"),
                    str(part.get("text") or ""),
                    part.get("file_id"),
                    part.get("media_url"),
                    part.get("media_type") or _media_type_for(str(part.get("message_type") or "text")),
                    part.get("admin_message_id"),
                    now,
                    now,
                ),
            )

    async def _get_submission_parts(
        self,
        db: aiosqlite.Connection,
        submission: Submission,
    ) -> list[SubmissionPart]:
        submission_id = int(submission["id"])
        cursor = await db.execute(
            """
            SELECT *
            FROM submission_parts
            WHERE submission_id = ?
            ORDER BY part_index
            """,
            (submission_id,),
        )
        rows = await cursor.fetchall()
        if rows:
            parts = [dict(row) for row in rows]
            for part in parts:
                part["admin_media_message_id"] = _admin_media_message_id_for_part(part, submission)
                part["source_url"] = submission.get("source_url")
                part["source_type"] = submission.get("source_type")
            return parts

        return [_fallback_submission_part(submission)]

    async def _sync_submission_draft_text(
        self,
        db: aiosqlite.Connection,
        submission_id: int,
        now: str,
    ) -> None:
        cursor = await db.execute(
            """
            SELECT text
            FROM submission_parts
            WHERE submission_id = ?
            ORDER BY part_index
            """,
            (submission_id,),
        )
        rows = await cursor.fetchall()
        draft_text = _parts_to_draft_text([str(row["text"] or "") for row in rows])
        await db.execute(
            """
            UPDATE submissions
            SET draft_text = ?, updated_at = ?
            WHERE id = ?
            """,
            (draft_text, now, submission_id),
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_tag_names(tag_names: list[str]) -> list[str]:
    normalized_tags: list[str] = []
    seen: set[str] = set()
    for tag_name in tag_names:
        normalized = " ".join(str(tag_name).strip().lower().split())
        normalized = normalized.strip("#,.;:!?'\"()[]{}")
        if not normalized or normalized in seen or len(normalized) > 40:
            continue

        seen.add(normalized)
        normalized_tags.append(normalized)

        if len(normalized_tags) >= 12:
            break

    return normalized_tags


def _normalize_text_parts(parts: list[str]) -> list[str]:
    normalized = [str(part or "").strip() for part in parts]
    normalized = [part for part in normalized if part]
    return normalized or [""]


def _parts_to_draft_text(parts: list[str]) -> str:
    return "\n\n".join(part for part in parts if part).strip()


def _normalize_media_urls(primary_media_url: str | None, additional_media_urls: list[str], media_type: str) -> list[str]:
    if media_type == "none":
        return []

    normalized_urls: list[str] = []
    seen: set[str] = set()
    for value in [primary_media_url, *additional_media_urls]:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue

        seen.add(normalized)
        normalized_urls.append(normalized)

        if len(normalized_urls) >= 4:
            break

    return normalized_urls


def _media_type_for(message_type: str) -> str:
    return message_type if message_type in {"photo", "video", "document"} else "none"


def _admin_media_message_id_for_part(part: SubmissionPart, submission: Submission) -> object | None:
    message_type = str(part.get("message_type") or "text")
    if message_type not in {"photo", "video", "document"}:
        return None

    if int(part.get("part_index") or 0) == 1:
        return submission.get("admin_media_message_id")

    return part.get("admin_message_id")


def _fallback_submission_part(submission: Submission) -> SubmissionPart:
    message_type = str(submission.get("message_type") or "text")
    return {
        "submission_id": int(submission["id"]),
        "part_index": 1,
        "message_type": message_type,
        "text": str(submission.get("draft_text") or submission.get("original_text") or ""),
        "file_id": submission.get("file_id"),
        "media_url": submission.get("media_url"),
        "media_type": submission.get("media_type") or _media_type_for(message_type),
        "admin_message_id": submission.get("admin_media_message_id"),
        "admin_media_message_id": submission.get("admin_media_message_id"),
        "source_url": submission.get("source_url"),
        "source_type": submission.get("source_type"),
        "created_at": submission.get("created_at"),
        "updated_at": submission.get("updated_at"),
    }
