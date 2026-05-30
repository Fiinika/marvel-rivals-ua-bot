from __future__ import annotations

from html import escape
from typing import Any

from services.i18n import t, t_optional


MAX_PREVIEW_TEXT_LENGTH = 1200


def format_admin_preview(submission: dict[str, Any]) -> str:
    username = submission.get("username")
    username_text = f"@{escape(username)}" if username else t("formatter.none")
    original_text = _format_block(submission.get("original_text"))
    draft_text = _format_block(submission.get("draft_text"))
    status = str(submission["status"])
    status_text = t_optional(f"status.{status}", status)

    return "\n".join(
        [
            f"<b>{escape(t('formatter.labels.title', submission_id=submission['id']))}</b>",
            f"<b>{escape(t('formatter.labels.author'))}:</b> {username_text}",
            f"<b>{escape(t('formatter.labels.user_id'))}:</b> <code>{submission['user_id']}</code>",
            f"<b>{escape(t('formatter.labels.type'))}:</b> <code>{escape(str(submission['message_type']))}</code>",
            f"<b>{escape(t('formatter.labels.file'))}:</b> {_format_file_id(submission.get('file_id'))}",
            f"<b>{escape(t('formatter.labels.media_url'))}:</b> {_format_inline_value(submission.get('media_url'))}",
            f"<b>{escape(t('formatter.labels.source_url'))}:</b> {_format_inline_value(submission.get('source_url'))}",
            (
                f"<b>{escape(t('formatter.labels.article_date'))}:</b> "
                f"{_format_inline_value(submission.get('article_date_display'))}"
            ),
            (
                f"<b>{escape(t('formatter.labels.media_message'))}:</b> "
                f"{_format_message_id(submission.get('admin_media_message_id'))}"
            ),
            "",
            f"<b>{escape(t('formatter.labels.original_text'))}:</b>",
            original_text,
            "",
            f"<b>{escape(t('formatter.labels.draft_text'))}:</b>",
            draft_text,
            "",
            f"<b>{escape(t('formatter.labels.status'))}:</b> <code>{escape(status_text)}</code>",
            f"<b>{escape(t('formatter.labels.created_at'))}:</b> <code>{escape(str(submission['created_at']))}</code>",
            f"<b>{escape(t('formatter.labels.updated_at'))}:</b> <code>{escape(str(submission['updated_at']))}</code>",
            (
                f"<b>{escape(t('formatter.labels.published_at'))}:</b> "
                f"<code>{escape(str(submission.get('published_at') or t('formatter.dash')))}</code>"
            ),
        ]
    )


def _format_block(value: str | None) -> str:
    if not value:
        return f"<i>{escape(t('formatter.empty'))}</i>"

    text = _truncate(value, MAX_PREVIEW_TEXT_LENGTH)
    return escape(text)


def _format_file_id(file_id: str | None) -> str:
    if not file_id:
        return f"<i>{escape(t('formatter.none'))}</i>"

    return f"<code>{escape(_short_file_id(file_id))}</code>"


def _format_inline_value(value: object | None) -> str:
    if value is None or value == "":
        return f"<i>{escape(t('formatter.none'))}</i>"

    return f"<code>{escape(_truncate(str(value), 160))}</code>"


def _format_message_id(message_id: object | None) -> str:
    if message_id is None:
        return f"<i>{escape(t('formatter.none'))}</i>"

    return f"<code>{escape(str(message_id))}</code>"


def _short_file_id(file_id: str) -> str:
    if len(file_id) <= 24:
        return file_id

    return f"{file_id[:10]}…{file_id[-10:]}"


def _truncate(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[: max_length - 1].rstrip() + "…"
