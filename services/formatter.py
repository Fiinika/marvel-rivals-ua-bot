from __future__ import annotations

from html import escape
from typing import Any

from services.i18n import t, t_optional
from services.post_footer import format_post_html


MAX_PREVIEW_TEXT_LENGTH = 1200
MAX_SOURCE_DRAFT_PREVIEW_LENGTH = 800


def format_admin_preview(submission: dict[str, Any]) -> str:
    if submission.get("source_type"):
        return _format_source_admin_preview(submission)

    username = submission.get("username")
    username_text = f"@{escape(username)}" if username else t("formatter.none")
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
            f"<b>{escape(t('formatter.labels.tags'))}:</b> {_format_tags(submission.get('tags'))}",
            (
                f"<b>{escape(t('formatter.labels.media_message'))}:</b> "
                f"{_format_message_id(submission.get('admin_media_message_id'))}"
            ),
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


def _format_source_admin_preview(submission: dict[str, Any]) -> str:
    status = str(submission["status"])
    status_text = t_optional(f"status.{status}", status)
    title = _extract_article_title(str(submission.get("original_text") or ""))
    source_label = _format_source_label(str(submission.get("source_type") or ""))
    category = _detect_source_category(submission.get("tags"))

    return "\n".join(
        [
            f"<b>{escape(t('formatter.labels.title', submission_id=submission['id']))}</b>",
            f"<b>Джерело:</b> <code>{escape(source_label)}</code>",
            f"<b>Заголовок:</b> {_format_inline_plain(title)}",
            f"<b>Тип:</b> {_format_inline_plain(category)}",
            (
                f"<b>{escape(t('formatter.labels.article_date'))}:</b> "
                f"{_format_inline_value(submission.get('article_date_display'))}"
            ),
            "",
            "<b>Чернетка:</b>",
            _format_source_public_draft_block(submission),
            "",
            f"<b>{escape(t('formatter.labels.source_url'))}:</b> {_format_inline_value(submission.get('source_url'))}",
            f"<b>{escape(t('formatter.labels.media_url'))}:</b> {_format_inline_value(submission.get('media_url'))}",
            f"<b>{escape(t('formatter.labels.tags'))}:</b> {_format_tags(submission.get('tags'))}",
            f"<b>{escape(t('formatter.labels.status'))}:</b> <code>{escape(status_text)}</code>",
        ]
    )


def _format_block(value: str | None, *, max_length: int = MAX_PREVIEW_TEXT_LENGTH) -> str:
    if not value:
        return f"<i>{escape(t('formatter.empty'))}</i>"

    text = _truncate(value, max_length)
    return escape(text)


def _format_source_public_draft_block(submission: dict[str, Any]) -> str:
    return format_post_html(
        str(submission.get("draft_text") or ""),
        source_url=str(submission.get("source_url") or ""),
        allow_source_link=True,
        include_community_footer=True,
    )


def _format_file_id(file_id: str | None) -> str:
    if not file_id:
        return f"<i>{escape(t('formatter.none'))}</i>"

    return f"<code>{escape(_short_file_id(file_id))}</code>"


def _format_inline_value(value: object | None) -> str:
    if value is None or value == "":
        return f"<i>{escape(t('formatter.none'))}</i>"

    return f"<code>{escape(_truncate(str(value), 160))}</code>"


def _format_inline_plain(value: object | None) -> str:
    if value is None or value == "":
        return f"<i>{escape(t('formatter.none'))}</i>"

    return escape(_truncate(str(value), 180))


def _format_message_id(message_id: object | None) -> str:
    if message_id is None:
        return f"<i>{escape(t('formatter.none'))}</i>"

    return f"<code>{escape(str(message_id))}</code>"


def _format_tags(tags: object | None) -> str:
    if not tags:
        return f"<i>{escape(t('formatter.none'))}</i>"

    if isinstance(tags, list):
        values = [str(tag) for tag in tags if str(tag).strip()]
    else:
        values = [str(tags)]

    if not values:
        return f"<i>{escape(t('formatter.none'))}</i>"

    return ", ".join(f"<code>{escape(value)}</code>" for value in values)


def _short_file_id(file_id: str) -> str:
    if len(file_id) <= 24:
        return file_id

    return f"{file_id[:10]}…{file_id[-10:]}"


def _extract_article_title(original_text: str) -> str | None:
    prefix = t("collectors.common.original_text.article_title", value="")
    for line in original_text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()

    return None


def _format_source_label(source_type: str) -> str:
    if source_type == "official_marvel_rivals":
        return "Official Marvel Rivals"

    return source_type or t("formatter.none")


def _detect_source_category(tags: object | None) -> str:
    if isinstance(tags, list):
        normalized_tags = {str(tag).strip().lower() for tag in tags}
    elif tags:
        normalized_tags = {str(tags).strip().lower()}
    else:
        normalized_tags = set()

    category_rules = [
        ({"патч"}, "Patch notes / game update"),
        ({"магазин", "скіни"}, "Shop / skins / bundles"),
        ({"івент"}, "Event / rewards"),
        ({"трейлер"}, "Trailer / teaser"),
        ({"голосування"}, "Vote / community choice"),
        ({"карта", "геймплей"}, "Map / gameplay"),
        ({"технічніроботи"}, "Maintenance"),
        ({"рейтинг"}, "Ranked / competitive"),
        ({"кіберспорт"}, "Esports"),
        ({"анонс"}, "Short announcement"),
    ]
    for rule_tags, label in category_rules:
        if normalized_tags & rule_tags:
            return label

    return t("formatter.none")


def _truncate(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[: max_length - 1].rstrip() + "…"
