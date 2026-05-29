from __future__ import annotations

from html import escape
from typing import Any


MAX_PREVIEW_TEXT_LENGTH = 1200


def format_admin_preview(submission: dict[str, Any]) -> str:
    username = submission.get("username")
    username_text = f"@{escape(username)}" if username else "немає"
    original_text = _format_block(submission.get("original_text"))
    draft_text = _format_block(submission.get("draft_text"))

    return "\n".join(
        [
            f"<b>Заявка #{submission['id']}</b>",
            f"<b>Автор:</b> {username_text}",
            f"<b>User ID:</b> <code>{submission['user_id']}</code>",
            f"<b>Тип:</b> <code>{escape(str(submission['message_type']))}</code>",
            f"<b>Файл:</b> {_format_file_id(submission.get('file_id'))}",
            f"<b>Медіа-повідомлення:</b> {_format_message_id(submission.get('admin_media_message_id'))}",
            "",
            "<b>Оригінальний текст / підпис:</b>",
            original_text,
            "",
            "<b>Поточна чернетка:</b>",
            draft_text,
            "",
            f"<b>Статус:</b> <code>{escape(str(submission['status']))}</code>",
            f"<b>Створено:</b> <code>{escape(str(submission['created_at']))}</code>",
            f"<b>Оновлено:</b> <code>{escape(str(submission['updated_at']))}</code>",
            f"<b>Опубліковано:</b> <code>{escape(str(submission.get('published_at') or '—'))}</code>",
        ]
    )


def _format_block(value: str | None) -> str:
    if not value:
        return "<i>порожньо</i>"

    text = _truncate(value, MAX_PREVIEW_TEXT_LENGTH)
    return escape(text)


def _format_file_id(file_id: str | None) -> str:
    if not file_id:
        return "<i>немає</i>"

    return f"<code>{escape(_short_file_id(file_id))}</code>"


def _format_message_id(message_id: object | None) -> str:
    if message_id is None:
        return "<i>немає</i>"

    return f"<code>{escape(str(message_id))}</code>"


def _short_file_id(file_id: str) -> str:
    if len(file_id) <= 24:
        return file_id

    return f"{file_id[:10]}…{file_id[-10:]}"


def _truncate(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[: max_length - 1].rstrip() + "…"
