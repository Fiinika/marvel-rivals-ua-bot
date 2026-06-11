"""aiogram router for Telegram group-chat moderation.

This router owns the chats listed in ``config.telegram_moderation_chat_ids`` and
mirrors the feature set of the independent Discord module
(``discord_moderation.py``): anti-flood, Telegram invite/scam/link filtering, a
bad-word filter, a persistent warning system with auto-actions, a mod-log, and a
welcome message for new members. All pure rule logic lives in
``services/chat_moderation.py``; this file holds the Telegram I/O only.

Important design points (see the plan):
  * A router-level ``ModeratedChatFilter`` scopes EVERY handler here to the
    allowlisted chats, so non-moderated chats are never touched and ordinary
    group messages never reach the private-only submission flow in ``user.py``.
  * Moderator commands gate on chat admins / creator (cached) OR the bot's
    configured ``ADMIN_USER_IDS``.
  * Every enforcement call goes through a ``_safe_*`` wrapper that swallows
    Telegram errors (missing rights, target is an admin, 48h delete window, …),
    so a permission problem logs once and never crashes the polling loop.
  * Anonymous group admins (``sender_chat``) and channel/bot posts bypass the
    content filters, and the content handler never touches service messages.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict, deque
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from html import escape as html_escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import CallbackQuery, ChatPermissions, Message

from config import Config
from database import Database
from keyboards import ModLogActionCallback, mod_log_action_keyboard
from services.chat_moderation import (
    SPAM_MAX_MESSAGES,
    SPAM_MUTE_SECONDS,
    SPAM_WINDOW_SECONDS,
    WARN_ESCALATION_THRESHOLD,
    WARN_MUTE_THRESHOLDS,
    SpamTracker,
    clamp_mute_seconds,
    has_bad_word,
    has_blocked_invite,
    load_welcome_rules,
    parse_duration,
    suspicious_reason,
)
from services.i18n import t
from services.publisher import DISABLED_LINK_PREVIEW


logger = logging.getLogger(__name__)
router = Router(name="moderation")

# Anti-flood state (per process). Pruned internally so it does not grow unbounded.
_spam_tracker = SpamTracker()

# Cache of chat admin user IDs to avoid a get_chat_member round-trip per message.
# chat_id -> (monotonic_expiry, set_of_admin_ids | None). None = the list could
# not be fetched (negative cache, so a flood doesn't hammer the failing call).
_admin_cache: dict[int, tuple[float, set[int] | None]] = {}
_ADMIN_CACHE_TTL = 300.0
_ADMIN_CACHE_FAILURE_TTL = 30.0

# Warn-once-per-chat about missing bot rights, so logs are not spammed every msg.
_rights_warned: set[int] = set()
_not_admin_warned: set[int] = set()

# Background auto-delete tasks (welcome / transient notices). Kept referenced so
# they are not garbage-collected mid-flight; discarded on completion.
_background_tasks: set[asyncio.Task] = set()

# How long a transient in-chat moderation notice stays before self-deleting.
_NOTICE_DELETE_SECONDS = 15

# Light anti-abuse for /report: at most this many reports per user per window.
_REPORT_WINDOW_SECONDS = 60.0
_REPORT_MAX_PER_WINDOW = 3
_report_times: dict[int, deque[float]] = defaultdict(deque)

# Full mute: every can_send_* flag explicitly False (Telegram does not guarantee a
# full mute from can_send_messages alone when the granular flags are omitted).
MUTED_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
)

# Fallback used to unmute when the chat's default permissions cannot be read.
_UNMUTE_FALLBACK_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_invite_users=True,
)

_NUMERIC_ID_RE = re.compile(r"^-?\d+$")


class ModeratedChatFilter(BaseFilter):
    """Match only the chats listed in TELEGRAM_MODERATION_CHAT_IDS."""

    async def __call__(self, message: Message, config: Config) -> bool:
        return message.chat.id in config.telegram_moderation_chat_ids


# Scope the WHOLE router to moderated chats (message + edited_message observers),
# so it never inspects DMs/admin chat and ordinary group messages never fall
# through to the submission flow.
router.message.filter(ModeratedChatFilter())
router.edited_message.filter(ModeratedChatFilter())


# --------------------------------------------------------------------------- #
# Welcome
# --------------------------------------------------------------------------- #

@router.message(F.new_chat_members)
async def welcome_new_members(message: Message, bot: Bot, config: Config) -> None:
    try:
        me = await bot.me()
        for member in message.new_chat_members or []:
            if member.is_bot or member.id == me.id:
                continue
            await _send_welcome(bot, config, message.chat.id, member)
    except Exception:  # noqa: BLE001 - never let a welcome error reach the loop
        logger.exception("Error while sending a Telegram welcome message.")
    # Keep the chat clean: drop the "X joined" service message.
    await _safe_delete(bot, message.chat.id, message.message_id)


@router.message(F.left_chat_member)
async def delete_leave_service_message(message: Message, bot: Bot) -> None:
    # Keep the chat clean: drop the "X left" service message.
    await _safe_delete(bot, message.chat.id, message.message_id)


async def _send_welcome(bot: Bot, config: Config, chat_id: int, member) -> None:
    rules = load_welcome_rules()
    lines = [
        t("moderation.welcome.title"),
        "",
        t("moderation.welcome.greeting", mention=_user_html(member)),
    ]
    if rules:
        lines.append("")
        lines.append(t("moderation.welcome.rules_header"))
        lines.extend(f"{index}. {html_escape(rule)}" for index, rule in enumerate(rules, start=1))

    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text="\n".join(lines),
            parse_mode="HTML",
            link_preview_options=DISABLED_LINK_PREVIEW,
        )
    except TelegramAPIError:
        logger.info("Could not send welcome message in chat %s", chat_id)
        return

    if config.telegram_welcome_delete_seconds > 0:
        _schedule_delete(bot, chat_id, sent.message_id, config.telegram_welcome_delete_seconds)


# --------------------------------------------------------------------------- #
# Moderator commands (gated inside each handler so non-mods get a clear reply)
# --------------------------------------------------------------------------- #

@router.message(Command("modhelp"))
async def modhelp_command(message: Message, bot: Bot, config: Config) -> None:
    if await _enforce(message, bot, config, run_text_filters=True, count_flood=True):
        return  # command-shaped spam: filtered exactly like any other message
    if not await _is_moderator(bot, config, message):
        await _transient_reply(message, t("moderation.cmd.no_permission"))
        return

    warn_rules = ", ".join(
        f"{count} → {_humanize_duration(seconds)}"
        for count, seconds in sorted(WARN_MUTE_THRESHOLDS.items())
    )
    body = t(
        "moderation.help.body",
        spam_max=SPAM_MAX_MESSAGES,
        spam_window=int(SPAM_WINDOW_SECONDS),
        warn_rules=warn_rules,
        escalation=WARN_ESCALATION_THRESHOLD,
    )
    await _reply(message, f"{t('moderation.help.title')}\n\n{body}", html=True)


@router.message(Command("del"))
async def del_command(message: Message, bot: Bot, config: Config) -> None:
    if await _enforce(message, bot, config, run_text_filters=True, count_flood=True):
        return  # command-shaped spam: filtered exactly like any other message
    if not await _is_moderator(bot, config, message):
        await _transient_reply(message, t("moderation.cmd.no_permission"))
        return
    target_message = message.reply_to_message
    if target_message is None:
        await _reply(message, t("moderation.cmd.reply_required"))
        return

    await _safe_delete(bot, message.chat.id, target_message.message_id)
    await _safe_delete(bot, message.chat.id, message.message_id)
    await _send_mod_log(
        bot,
        config,
        action=t("moderation.action.del"),
        target_html=_user_html(target_message.from_user) if target_message.from_user else "—",
        moderator_html=_actor_html(message),
        reason=t("moderation.reason.none"),
        preview=target_message.text or target_message.caption,
    )


@router.message(Command("mute"))
async def mute_command(message: Message, command: CommandObject, bot: Bot, config: Config) -> None:
    if await _enforce(message, bot, config, run_text_filters=True, count_flood=True):
        return  # command-shaped spam: filtered exactly like any other message
    if not await _is_moderator(bot, config, message):
        await _transient_reply(message, t("moderation.cmd.no_permission"))
        return

    target_id, target_html, rest = _resolve_target(message, command.args)
    if target_id is None:
        await _reply(message, t("moderation.cmd.target_required"))
        return
    if await _is_admin_id(bot, config, message.chat.id, target_id):
        await _reply(message, t("moderation.cmd.cannot_target_moderator"))
        return

    seconds, reason_text = _split_duration_and_reason(rest)
    if seconds is None:
        seconds = 60 * 60  # default mute: 60 minutes
    reason = reason_text or t("moderation.reason.none")

    if not await _safe_mute(bot, message.chat.id, target_id, seconds, reason):
        await _reply(message, t("moderation.cmd.action_failed"))
        return

    if seconds <= 0:
        await _reply(
            message,
            t("moderation.cmd.muted_permanent", target=target_html, reason=html_escape(reason)),
            html=True,
        )
    else:
        await _reply(
            message,
            t(
                "moderation.cmd.muted",
                target=target_html,
                duration=_humanize_duration(seconds),
                reason=html_escape(reason),
            ),
            html=True,
        )
    await _send_mod_log(
        bot,
        config,
        action=f"{t('moderation.action.mute')} — {_humanize_duration(seconds)}",
        target_html=target_html,
        moderator_html=_actor_html(message),
        reason=reason,
    )


@router.message(Command("unmute"))
async def unmute_command(message: Message, command: CommandObject, bot: Bot, config: Config) -> None:
    if await _enforce(message, bot, config, run_text_filters=True, count_flood=True):
        return  # command-shaped spam: filtered exactly like any other message
    if not await _is_moderator(bot, config, message):
        await _transient_reply(message, t("moderation.cmd.no_permission"))
        return

    target_id, target_html, _ = _resolve_target(message, command.args)
    if target_id is None:
        await _reply(message, t("moderation.cmd.target_required"))
        return

    if not await _safe_unmute(bot, message.chat.id, target_id):
        await _reply(message, t("moderation.cmd.action_failed"))
        return

    await _reply(message, t("moderation.cmd.unmuted", target=target_html), html=True)
    await _send_mod_log(
        bot,
        config,
        action=t("moderation.action.unmute"),
        target_html=target_html,
        moderator_html=_actor_html(message),
        reason=t("moderation.reason.none"),
    )


@router.message(Command("ban"))
async def ban_command(message: Message, command: CommandObject, bot: Bot, config: Config) -> None:
    if await _enforce(message, bot, config, run_text_filters=True, count_flood=True):
        return  # command-shaped spam: filtered exactly like any other message
    if not await _is_moderator(bot, config, message):
        await _transient_reply(message, t("moderation.cmd.no_permission"))
        return

    target_id, target_html, rest = _resolve_target(message, command.args)
    if target_id is None:
        await _reply(message, t("moderation.cmd.target_required"))
        return
    if await _is_admin_id(bot, config, message.chat.id, target_id):
        await _reply(message, t("moderation.cmd.cannot_target_moderator"))
        return

    seconds, reason_text = _split_duration_and_reason(rest)
    if seconds is None:
        seconds = 0  # default ban: permanent
    reason = reason_text or t("moderation.reason.none")

    if not await _safe_ban(bot, message.chat.id, target_id, seconds):
        await _reply(message, t("moderation.cmd.action_failed"))
        return

    if seconds > 0:
        confirmation = t(
            "moderation.cmd.banned_temp",
            target=target_html,
            duration=_humanize_duration(seconds),
            reason=html_escape(reason),
        )
    else:
        confirmation = t("moderation.cmd.banned", target=target_html, reason=html_escape(reason))
    await _reply(message, confirmation, html=True)
    await _send_mod_log(
        bot,
        config,
        action=f"{t('moderation.action.ban')} — {_humanize_duration(seconds)}",
        target_html=target_html,
        moderator_html=_actor_html(message),
        reason=reason,
    )


@router.message(Command("unban"))
async def unban_command(message: Message, command: CommandObject, bot: Bot, config: Config) -> None:
    if await _enforce(message, bot, config, run_text_filters=True, count_flood=True):
        return  # command-shaped spam: filtered exactly like any other message
    if not await _is_moderator(bot, config, message):
        await _transient_reply(message, t("moderation.cmd.no_permission"))
        return

    target_id, target_html, _ = _resolve_target(message, command.args)
    if target_id is None:
        await _reply(message, t("moderation.cmd.target_required"))
        return

    if not await _safe_unban(bot, message.chat.id, target_id):
        await _reply(message, t("moderation.cmd.action_failed"))
        return

    await _reply(message, t("moderation.cmd.unbanned", target=target_html), html=True)
    await _send_mod_log(
        bot,
        config,
        action=t("moderation.action.unban"),
        target_html=target_html,
        moderator_html=_actor_html(message),
        reason=t("moderation.reason.none"),
    )


@router.message(Command("kick"))
async def kick_command(message: Message, command: CommandObject, bot: Bot, config: Config) -> None:
    if await _enforce(message, bot, config, run_text_filters=True, count_flood=True):
        return  # command-shaped spam: filtered exactly like any other message
    if not await _is_moderator(bot, config, message):
        await _transient_reply(message, t("moderation.cmd.no_permission"))
        return

    target_id, target_html, _ = _resolve_target(message, command.args)
    if target_id is None:
        await _reply(message, t("moderation.cmd.target_required"))
        return
    if await _is_admin_id(bot, config, message.chat.id, target_id):
        await _reply(message, t("moderation.cmd.cannot_target_moderator"))
        return

    kick_result = await _safe_kick(bot, message.chat.id, target_id)
    if kick_result == "failed":
        await _reply(message, t("moderation.cmd.action_failed"))
        return
    if kick_result == "partial":
        # The unban step failed: the member is still banned and needs a manual /unban.
        await _reply(message, t("moderation.cmd.kick_partial", target=target_html), html=True)
        await _send_mod_log(
            bot,
            config,
            action=t("moderation.action.kick"),
            target_html=target_html,
            moderator_html=_actor_html(message),
            reason=t("moderation.cmd.kick_partial_reason"),
        )
        return

    reason = t("moderation.reason.none")
    await _reply(message, t("moderation.cmd.kicked", target=target_html, reason=reason), html=True)
    await _send_mod_log(
        bot,
        config,
        action=t("moderation.action.kick"),
        target_html=target_html,
        moderator_html=_actor_html(message),
        reason=reason,
    )


@router.message(Command("warn"))
async def warn_command(message: Message, command: CommandObject, bot: Bot, config: Config, db: Database) -> None:
    if await _enforce(message, bot, config, run_text_filters=True, count_flood=True):
        return  # command-shaped spam: filtered exactly like any other message
    if not await _is_moderator(bot, config, message):
        await _transient_reply(message, t("moderation.cmd.no_permission"))
        return

    target_id, target_html, rest = _resolve_target(message, command.args)
    if target_id is None:
        await _reply(message, t("moderation.cmd.target_required"))
        return
    if await _is_admin_id(bot, config, message.chat.id, target_id):
        await _reply(message, t("moderation.cmd.cannot_target_moderator"))
        return

    reason_text = " ".join(rest).strip() or t("moderation.reason.none")
    moderator_id = message.from_user.id if message.from_user else 0

    try:
        count = await db.add_telegram_warning(
            chat_id=message.chat.id,
            user_id=target_id,
            moderator_id=moderator_id,
            reason=reason_text,
        )
    except Exception:  # noqa: BLE001 - history failure must not break the command
        logger.exception("Failed to persist a Telegram warning.")
        await _reply(message, t("moderation.cmd.action_failed"))
        return

    await _reply(
        message,
        t("moderation.cmd.warned", target=target_html, count=count, reason=html_escape(reason_text)),
        html=True,
    )
    await _send_mod_log(
        bot,
        config,
        action=t("moderation.action.warn"),
        target_html=target_html,
        moderator_html=_actor_html(message),
        reason=reason_text,
    )

    # Best-effort DM to the warned member (works only if they started the bot).
    with suppress(TelegramAPIError):
        await bot.send_message(
            chat_id=target_id,
            text=t("moderation.cmd.warn_dm", chat=message.chat.title or "", reason=reason_text),
        )

    await _apply_warning_autoactions(bot, config, message.chat.id, target_id, target_html, count)


@router.message(Command("warnings"))
async def warnings_command(message: Message, command: CommandObject, bot: Bot, config: Config, db: Database) -> None:
    if await _enforce(message, bot, config, run_text_filters=True, count_flood=True):
        return  # command-shaped spam: filtered exactly like any other message
    if not await _is_moderator(bot, config, message):
        await _transient_reply(message, t("moderation.cmd.no_permission"))
        return

    target_id, target_html, _ = _resolve_target(message, command.args)
    if target_id is None:
        await _reply(message, t("moderation.cmd.target_required"))
        return

    try:
        records = await db.list_telegram_warnings(chat_id=message.chat.id, user_id=target_id)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to read Telegram warnings.")
        await _reply(message, t("moderation.cmd.action_failed"))
        return

    if not records:
        await _reply(message, t("moderation.cmd.warnings_empty", target=target_html), html=True)
        return

    lines = [t("moderation.cmd.warnings_header", target=target_html, count=len(records))]
    recent = records[-20:]
    start_index = len(records) - len(recent) + 1
    for offset, record in enumerate(recent):
        lines.append(
            t(
                "moderation.cmd.warnings_line",
                index=start_index + offset,
                when=_format_when(str(record.get("created_at") or "")),
                moderator=_user_html_from_id(int(record.get("moderator_id") or 0)),
                reason=html_escape(str(record.get("reason") or "")),
            )
        )
    if len(records) > len(recent):
        lines.append(t("moderation.cmd.warnings_more", shown=len(recent), total=len(records)))

    await _reply(message, "\n".join(lines), html=True)


@router.message(Command("clearwarnings"))
async def clearwarnings_command(message: Message, command: CommandObject, bot: Bot, config: Config, db: Database) -> None:
    if await _enforce(message, bot, config, run_text_filters=True, count_flood=True):
        return  # command-shaped spam: filtered exactly like any other message
    if not await _is_moderator(bot, config, message):
        await _transient_reply(message, t("moderation.cmd.no_permission"))
        return

    target_id, target_html, _ = _resolve_target(message, command.args)
    if target_id is None:
        await _reply(message, t("moderation.cmd.target_required"))
        return

    try:
        removed = await db.clear_telegram_warnings(chat_id=message.chat.id, user_id=target_id)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to clear Telegram warnings.")
        await _reply(message, t("moderation.cmd.action_failed"))
        return

    await _reply(message, t("moderation.cmd.warnings_cleared", target=target_html, count=removed), html=True)
    await _send_mod_log(
        bot,
        config,
        action=t("moderation.action.clearwarnings"),
        target_html=target_html,
        moderator_html=_actor_html(message),
        reason=t("moderation.reason.none"),
    )


# --------------------------------------------------------------------------- #
# Member commands (available to EVERYONE in the moderated chat)
# --------------------------------------------------------------------------- #

@router.message(Command("report"))
async def report_command(message: Message, command: CommandObject, bot: Bot, config: Config) -> None:
    """Let ANY member report a message to the moderators (mirrors Discord /report).

    Used as a reply to the offending message; the report goes to the mod-log chat.
    The /report message and the confirmation are removed shortly after, so the
    chat stays clean and reporting causes no public drama.
    """
    reporter = message.from_user
    if reporter is None or message.sender_chat is not None:
        return
    if await _enforce(message, bot, config, run_text_filters=True, count_flood=True):
        return  # command-shaped spam: filtered exactly like any other message

    target_message = message.reply_to_message
    if target_message is None or target_message.from_user is None:
        await _transient_reply(message, t("moderation.report.reply_required"))
        return

    reported = target_message.from_user
    if reported.id == reporter.id:
        await _transient_reply(message, t("moderation.report.self"))
        return
    if reported.is_bot:
        await _transient_reply(message, t("moderation.report.bot"))
        return
    if not _report_allowed(reporter.id):
        await _transient_reply(message, t("moderation.report.too_many"))
        return

    reason = (command.args or "").strip() or t("moderation.reason.none")
    await _send_report_log(
        bot,
        config,
        reporter_html=_user_html(reporter),
        reported_html=_user_html(reported),
        reason=reason,
        link=_message_link(message.chat.id, target_message.message_id),
        preview=target_message.text or target_message.caption,
        target_chat_id=message.chat.id,
        reported_id=reported.id,
        reported_message_id=target_message.message_id,
    )

    await _safe_delete(bot, message.chat.id, message.message_id)
    await _send_notice(bot, message.chat.id, t("moderation.report.sent", mention=_user_html(reporter)))


@router.message(Command("rules"))
async def rules_command(message: Message, bot: Bot, config: Config) -> None:
    """Show the chat rules to ANY member. The reply auto-deletes to avoid clutter."""
    if await _enforce(message, bot, config, run_text_filters=True, count_flood=True):
        return  # command-shaped spam: filtered exactly like any other message

    rules = load_welcome_rules()
    if not rules:
        await _transient_reply(message, t("moderation.rules.empty"))
        return

    lines = [t("moderation.rules.header")]
    lines.extend(f"{index}. {html_escape(rule)}" for index, rule in enumerate(rules, start=1))
    try:
        sent = await bot.send_message(
            chat_id=message.chat.id,
            text="\n".join(lines),
            parse_mode="HTML",
            link_preview_options=DISABLED_LINK_PREVIEW,
        )
    except TelegramAPIError:
        logger.info("Could not send rules in chat %s", message.chat.id)
        return

    delay = config.telegram_welcome_delete_seconds
    if delay > 0:
        _schedule_delete(bot, message.chat.id, sent.message_id, delay)
        _schedule_delete(bot, message.chat.id, message.message_id, delay)


def _report_allowed(user_id: int) -> bool:
    now = time.monotonic()
    bucket = _report_times[user_id]
    while bucket and now - bucket[0] > _REPORT_WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= _REPORT_MAX_PER_WINDOW:
        return False
    bucket.append(now)
    return True


def _message_link(chat_id: int, message_id: int) -> str | None:
    """Build a t.me/c/... deep link to a supergroup message, or None for other chats."""
    raw = str(chat_id)
    if not raw.startswith("-100"):
        return None
    return f"https://t.me/c/{raw[4:]}/{message_id}"


# --------------------------------------------------------------------------- #
# Content moderation (registered AFTER commands so commands win)
# --------------------------------------------------------------------------- #

@router.message(F.text | F.caption)
async def moderate_text_message(message: Message, bot: Bot, config: Config) -> None:
    await _enforce(message, bot, config, run_text_filters=True, count_flood=True)


@router.message(
    F.sticker
    | F.photo
    | F.video
    | F.animation
    | F.voice
    | F.video_note
    | F.document
    | F.audio
    | F.poll
    | F.dice
    | F.game
)
async def moderate_media_message(message: Message, bot: Bot, config: Config) -> None:
    # Captionless media: nothing to text-filter, but still counts toward anti-flood.
    await _enforce(message, bot, config, run_text_filters=False, count_flood=True)


@router.edited_message(F.text | F.caption)
async def moderate_edited_message(message: Message, bot: Bot, config: Config) -> None:
    # Anti-evasion: a benign message edited to add a scam link is re-checked.
    # Edits do NOT count toward anti-flood (an edit is not a new send).
    await _enforce(message, bot, config, run_text_filters=True, count_flood=False)


async def _enforce(message: Message, bot: Bot, config: Config, *, run_text_filters: bool, count_flood: bool) -> bool:
    """Run the content filters / flood counter. Returns True when the message was punished."""
    try:
        # Skip non-user senders: channel posts, linked-channel forwards, and
        # anonymous group admins (sender_chat) — they cannot/should not be punished.
        user = message.from_user
        if user is None or message.sender_chat is not None or user.is_bot:
            return False
        # Moderators bypass the content filters. default=True fails OPEN: when the
        # admin list is temporarily unknown (API error on a cold cache) we must
        # not punish someone who may well be an admin.
        if await _is_admin_id(bot, config, message.chat.id, user.id, default=True):
            return False

        content = message.text or message.caption or ""
        # Hyperlinked text hides its URL outside message.text — scan text_link
        # entities too, or "натисни тут" pointing at t.me/scam passes every filter.
        embedded_urls = _embedded_link_urls(message)
        if embedded_urls:
            content = "\n".join([content, *embedded_urls]) if content else "\n".join(embedded_urls)

        if run_text_filters and content:
            if has_blocked_invite(content, config.telegram_link_allowlist):
                await _punish(message, bot, config, action_key="invite_removed", reason=t("moderation.reason.invite"), mute_seconds=0)
                return True
            if suspicious_reason(content):
                await _punish(message, bot, config, action_key="scam_removed", reason=t("moderation.reason.scam"), mute_seconds=0)
                return True
            if has_bad_word(content):
                await _punish(message, bot, config, action_key="badword_removed", reason=t("moderation.reason.badword"), mute_seconds=0)
                return True

        if count_flood and _spam_tracker.register_and_check(message.chat.id, user.id):
            flood_reason = t("moderation.reason.flood", count=SPAM_MAX_MESSAGES, seconds=int(SPAM_WINDOW_SECONDS))
            await _punish(message, bot, config, action_key="flood", reason=flood_reason, mute_seconds=SPAM_MUTE_SECONDS, notice=True)
            return True
    except Exception:  # noqa: BLE001 - moderation must never crash the polling loop
        logger.exception("Error while moderating a Telegram message in chat %s", message.chat.id)
    return False


def _embedded_link_urls(message: Message) -> list[str]:
    """URLs hidden behind text_link entities (the visible text shows no URL)."""
    urls: list[str] = []
    for entity in [*(message.entities or []), *(message.caption_entities or [])]:
        entity_type = getattr(entity.type, "value", entity.type)
        if entity_type == "text_link" and entity.url:
            urls.append(entity.url)
    return urls


async def _punish(
    message: Message,
    bot: Bot,
    config: Config,
    *,
    action_key: str,
    reason: str,
    mute_seconds: int,
    notice: bool = False,
) -> None:
    user = message.from_user
    deleted = await _safe_delete(bot, message.chat.id, message.message_id)

    if mute_seconds > 0 and user is not None:
        await _safe_mute(bot, message.chat.id, user.id, mute_seconds, reason)
        if notice:
            await _send_notice(
                bot,
                message.chat.id,
                t("moderation.notice.flood", mention=_user_html(user), duration=_humanize_duration(mute_seconds)),
            )

    action = t(f"moderation.action.{action_key}")
    # Quick moderator actions right under the log entry; the delete button is
    # offered only when the offending message could not be removed.
    keyboard = None
    if user is not None:
        keyboard = mod_log_action_keyboard(
            message.chat.id,
            user.id,
            message.message_id if not deleted else 0,
        )
    await _send_mod_log(
        bot,
        config,
        action=action if deleted else f"{action} (видалити не вдалося)",
        target_html=_user_html(user) if user is not None else "—",
        moderator_html=t("moderation.log.moderator_auto"),
        reason=reason,
        preview=message.text or message.caption,
        reply_markup=keyboard,
    )


async def _apply_warning_autoactions(
    bot: Bot,
    config: Config,
    chat_id: int,
    user_id: int,
    target_html: str,
    count: int,
) -> None:
    """Mute on warning thresholds; escalate at the top one. Never bans/kicks."""
    seconds = WARN_MUTE_THRESHOLDS.get(count)
    if seconds:
        reason = t("moderation.reason.auto_warn", count=count)
        ok = await _safe_mute(bot, chat_id, user_id, seconds, reason)
        await _send_mod_log(
            bot,
            config,
            action=t("moderation.action.auto_mute"),
            target_html=target_html,
            moderator_html=t("moderation.log.moderator_auto"),
            reason=(
                t("moderation.auto.muted", count=count, duration=_humanize_duration(seconds))
                if ok
                else t("moderation.auto.muted_failed", count=count, duration=_humanize_duration(seconds))
            ),
        )

    if count == WARN_ESCALATION_THRESHOLD:
        await _send_mod_log(
            bot,
            config,
            action=t("moderation.action.review"),
            target_html=target_html,
            moderator_html=t("moderation.log.moderator_auto"),
            # No {target} here: the reason field is html-escaped by _send_mod_log,
            # so an embedded mention would render as raw markup. The log entry
            # already shows the clickable mention on its own line.
            reason=t("moderation.auto.review", count=count),
        )


# --------------------------------------------------------------------------- #
# Mod-log inline action buttons (Delete / Mute / Ban under report & filter logs)
# --------------------------------------------------------------------------- #

# Fixed duration for the one-tap mute button; the /mute command covers custom ones.
_MODLOG_MUTE_SECONDS = 60 * 60

_MODLOG_ACTION_BUTTON_KEYS = {
    "del": "buttons.modlog_delete",
    "mute": "buttons.modlog_mute",
    "ban": "buttons.modlog_ban",
}


@router.callback_query(ModLogActionCallback.filter())
async def mod_log_action_callback(
    callback: CallbackQuery,
    callback_data: ModLogActionCallback,
    bot: Bot,
    config: Config,
) -> None:
    """One-tap moderator actions on a mod-log entry.

    The clicker must moderate the TARGET chat (config admin or a chat admin
    there) — membership in the mod-log chat alone is not enough.
    """
    actor = callback.from_user
    if actor is None or not await _is_admin_id(bot, config, callback_data.chat_id, actor.id):
        await _answer_callback(callback, t("moderation.modlog.not_allowed"), show_alert=True)
        return

    action = callback_data.action
    if action == "del":
        ok = await _safe_delete(bot, callback_data.chat_id, callback_data.message_id)
        toast = t("moderation.cmd.deleted") if ok else t("moderation.modlog.delete_failed")
    elif action == "mute":
        ok = await _safe_mute(
            bot,
            callback_data.chat_id,
            callback_data.user_id,
            _MODLOG_MUTE_SECONDS,
            t("moderation.reason.none"),
        )
        toast = (
            t("moderation.modlog.muted", duration=_humanize_duration(_MODLOG_MUTE_SECONDS))
            if ok
            else t("moderation.modlog.mute_failed")
        )
    elif action == "ban":
        ok = await _safe_ban(bot, callback_data.chat_id, callback_data.user_id, 0)
        toast = t("moderation.modlog.banned") if ok else t("moderation.modlog.ban_failed")
    else:
        await _answer_callback(callback, t("moderation.modlog.unknown_action"), show_alert=True)
        return

    await _answer_callback(callback, toast, show_alert=not ok)
    await _append_modlog_note(callback, action, ok)


async def _append_modlog_note(callback: CallbackQuery, action: str, ok: bool) -> None:
    """Record who did what right inside the log entry; keep the buttons usable."""
    message = callback.message
    if not isinstance(message, Message) or callback.from_user is None:
        return
    note = t(
        "moderation.modlog.done_line",
        action=t(_MODLOG_ACTION_BUTTON_KEYS[action]),
        result="✅" if ok else "❌",
        moderator=_user_html(callback.from_user),
    )
    try:
        await message.edit_text(
            f"{message.html_text}\n{note}",
            parse_mode="HTML",
            reply_markup=message.reply_markup,
            link_preview_options=DISABLED_LINK_PREVIEW,
        )
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.info("Could not update mod-log entry %s: %s", message.message_id, exc)
    except TelegramAPIError:
        logger.info("Could not update mod-log entry %s", message.message_id)


async def _answer_callback(callback: CallbackQuery, text: str | None = None, *, show_alert: bool = False) -> None:
    try:
        await callback.answer(text, show_alert=show_alert)
    except TelegramAPIError:
        logger.info("Failed to answer mod-log callback %s", callback.id)


# --------------------------------------------------------------------------- #
# Permission helpers
# --------------------------------------------------------------------------- #

async def _is_moderator(bot: Bot, config: Config, message: Message) -> bool:
    """True if the command issuer may moderate this chat."""
    # Anonymous group admins post as the chat itself.
    if message.sender_chat is not None and message.sender_chat.id == message.chat.id:
        return True
    user = message.from_user
    if user is None:
        return False
    return await _is_admin_id(bot, config, message.chat.id, user.id)


async def _is_admin_id(bot: Bot, config: Config, chat_id: int, user_id: int, *, default: bool = False) -> bool:
    """True if the user moderates the chat. `default` applies while the admin list
    is unknown (API failure): filters pass default=True (fail open — never punish a
    possible admin), command gates keep default=False (fail closed)."""
    if user_id in config.admin_user_ids:
        return True
    admin_ids = await _get_chat_admin_ids(bot, chat_id)
    if admin_ids is None:
        return default
    return user_id in admin_ids


async def _get_chat_admin_ids(bot: Bot, chat_id: int) -> set[int] | None:
    """Cached admin ids for a chat, or None while the list cannot be fetched."""
    now = time.monotonic()
    cached = _admin_cache.get(chat_id)
    if cached is not None and now < cached[0]:
        return cached[1]

    try:
        admins = await bot.get_chat_administrators(chat_id)
    except TelegramAPIError:
        if cached is not None and cached[1] is not None:
            return cached[1]  # a stale admin list beats no list at all
        _admin_cache[chat_id] = (now + _ADMIN_CACHE_FAILURE_TTL, None)
        return None

    ids = {member.user.id for member in admins if member.user is not None}
    _admin_cache[chat_id] = (now + _ADMIN_CACHE_TTL, ids)

    # Loud one-time warning if the bot itself is not an admin here: without admin
    # rights, group privacy hides messages and the filters silently do nothing.
    with suppress(TelegramAPIError):
        me = await bot.me()
        if me.id not in ids and chat_id not in _not_admin_warned:
            logger.warning(
                "Bot is not an administrator in moderated chat %s; filters and "
                "commands will not work until it is promoted (Delete + Restrict/Ban).",
                chat_id,
            )
            _not_admin_warned.add(chat_id)

    return ids


# --------------------------------------------------------------------------- #
# Safe Telegram enforcement wrappers (mirror discord_moderation._safe_*)
# --------------------------------------------------------------------------- #

async def _safe_delete(bot: Bot, chat_id: int, message_id: int) -> bool:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except TelegramBadRequest as exc:
        if "not found" in str(exc).lower():
            return True  # already gone
        logger.info("Could not delete message %s in chat %s: %s", message_id, chat_id, exc)
        return False
    except TelegramForbiddenError:
        _warn_rights_once(chat_id, "cannot delete messages")
        return False
    except TelegramAPIError:
        logger.info("Could not delete message %s in chat %s", message_id, chat_id)
        return False


# BadRequest texts that genuinely mean the BOT lacks an admin right. Other
# BadRequests (target is an admin, bogus user id, ...) must NOT poison the
# warn-once flag, or the real missing-rights diagnostic is silenced forever.
_RIGHTS_ERROR_MARKERS = ("not enough rights", "need administrator rights", "chat_admin_required")


def _handle_enforcement_error(exc: Exception, chat_id: int, what: str, detail: str) -> None:
    if isinstance(exc, TelegramForbiddenError):
        _warn_rights_once(chat_id, f"{what} (Forbidden)")
        return
    message_text = str(exc).lower()
    if any(marker in message_text for marker in _RIGHTS_ERROR_MARKERS):
        _warn_rights_once(chat_id, f"{what} (missing admin right)")
        return
    logger.info("%s in chat %s: %s", detail, chat_id, exc)


async def _safe_mute(bot: Bot, chat_id: int, user_id: int, seconds: int, reason: str) -> bool:
    until_date = None
    if seconds and seconds > 0:
        until_date = datetime.now(timezone.utc) + timedelta(seconds=clamp_mute_seconds(seconds))
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=MUTED_PERMISSIONS,
            until_date=until_date,
        )
        return True
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        _handle_enforcement_error(exc, chat_id, "cannot restrict members", f"Could not mute user {user_id}")
        return False
    except TelegramAPIError:
        logger.exception("Failed to mute user %s in chat %s", user_id, chat_id)
        return False


async def _safe_unmute(bot: Bot, chat_id: int, user_id: int) -> bool:
    permissions = _UNMUTE_FALLBACK_PERMISSIONS
    with suppress(TelegramAPIError):
        chat = await bot.get_chat(chat_id)
        if chat.permissions is not None:
            permissions = chat.permissions
    try:
        await bot.restrict_chat_member(chat_id=chat_id, user_id=user_id, permissions=permissions)
        return True
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        _handle_enforcement_error(exc, chat_id, "cannot restrict members", f"Could not unmute user {user_id}")
        return False
    except TelegramAPIError:
        logger.exception("Failed to unmute user %s in chat %s", user_id, chat_id)
        return False


async def _safe_ban(bot: Bot, chat_id: int, user_id: int, seconds: int) -> bool:
    until_date = None
    if seconds and seconds > 0:
        until_date = datetime.now(timezone.utc) + timedelta(seconds=clamp_mute_seconds(seconds))
    try:
        await bot.ban_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            until_date=until_date,
            revoke_messages=False,
        )
        return True
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        _handle_enforcement_error(exc, chat_id, "cannot ban members", f"Could not ban user {user_id}")
        return False
    except TelegramAPIError:
        logger.exception("Failed to ban user %s in chat %s", user_id, chat_id)
        return False


async def _safe_unban(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
        return True
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        _handle_enforcement_error(exc, chat_id, "cannot unban members", f"Could not unban user {user_id}")
        return False
    except TelegramAPIError:
        logger.exception("Failed to unban user %s in chat %s", user_id, chat_id)
        return False


async def _safe_kick(bot: Bot, chat_id: int, user_id: int) -> str:
    """Remove a member without a lasting ban. Returns "ok", "partial", or "failed".

    "partial" means the ban succeeded but the unban step kept failing even after a
    retry: the member is left BANNED and a moderator must run /unban to finish the
    kick. Reporting that distinctly matters — a plain "failed" would suggest
    nothing happened at all.
    """
    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id, revoke_messages=False)
    except (TelegramBadRequest, TelegramForbiddenError) as exc:
        _handle_enforcement_error(exc, chat_id, "cannot kick members", f"Could not kick user {user_id}")
        return "failed"
    except TelegramAPIError:
        logger.exception("Failed to kick user %s in chat %s", user_id, chat_id)
        return "failed"

    for attempt in (1, 2):
        try:
            await bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
            return "ok"
        except TelegramAPIError:
            logger.warning(
                "Kick of user %s in chat %s: unban step failed (attempt %s)", user_id, chat_id, attempt
            )
    return "partial"


async def _send_mod_log(
    bot: Bot,
    config: Config,
    *,
    action: str,
    target_html: str,
    moderator_html: str,
    reason: str,
    preview: str | None = None,
    reply_markup=None,
) -> None:
    chat_id = config.telegram_mod_log_chat_id or config.admin_chat_id
    text = t(
        "moderation.log.entry",
        action=action,
        target=target_html,
        moderator=moderator_html,
        reason=html_escape(reason) if reason else t("moderation.reason.none"),
    )
    if preview:
        safe = html_escape(preview)[:200]
        if safe.strip():
            text += "\n" + t("moderation.log.preview", preview=safe)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
            link_preview_options=DISABLED_LINK_PREVIEW,
        )
    except TelegramAPIError:
        logger.info("Could not send moderation log to chat %s", chat_id)


async def _send_report_log(
    bot: Bot,
    config: Config,
    *,
    reporter_html: str,
    reported_html: str,
    reason: str,
    link: str | None,
    preview: str | None,
    target_chat_id: int,
    reported_id: int,
    reported_message_id: int,
) -> None:
    chat_id = config.telegram_mod_log_chat_id or config.admin_chat_id
    text = t(
        "moderation.report.log",
        reporter=reporter_html,
        reported=reported_html,
        reason=html_escape(reason),
    )
    if preview:
        safe = html_escape(preview)[:200]
        if safe.strip():
            text += "\n" + t("moderation.log.preview", preview=safe)
    if link:
        text += "\n" + t("moderation.report.log_link", link=link)
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=mod_log_action_keyboard(target_chat_id, reported_id, reported_message_id),
            link_preview_options=DISABLED_LINK_PREVIEW,
        )
    except TelegramAPIError:
        logger.info("Could not send report log to chat %s", chat_id)


async def _send_notice(bot: Bot, chat_id: int, text: str) -> None:
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            link_preview_options=DISABLED_LINK_PREVIEW,
        )
    except TelegramAPIError:
        return
    _schedule_delete(bot, chat_id, sent.message_id, _NOTICE_DELETE_SECONDS)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _schedule_delete(bot: Bot, chat_id: int, message_id: int, delay: int) -> None:
    task = asyncio.create_task(_delete_after(bot, chat_id, message_id, delay))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _delete_after(bot: Bot, chat_id: int, message_id: int, delay: int) -> None:
    with suppress(asyncio.CancelledError):
        await asyncio.sleep(delay)
        await _safe_delete(bot, chat_id, message_id)


def _warn_rights_once(chat_id: int, what: str) -> None:
    if chat_id in _rights_warned:
        return
    logger.warning("Missing rights in chat %s: %s. Check the bot's admin permissions.", chat_id, what)
    _rights_warned.add(chat_id)


async def _reply(message: Message, text: str, *, html: bool = False) -> None:
    try:
        await message.reply(
            text,
            parse_mode="HTML" if html else None,
            link_preview_options=DISABLED_LINK_PREVIEW,
        )
    except TelegramAPIError:
        logger.info("Could not reply in chat %s", message.chat.id)


async def _transient_reply(message: Message, text: str) -> None:
    """Reply, then auto-delete both the reply and the triggering message."""
    try:
        sent = await message.reply(text, link_preview_options=DISABLED_LINK_PREVIEW)
    except TelegramAPIError:
        logger.info("Could not reply in chat %s", message.chat.id)
        return
    _schedule_delete(message.bot, message.chat.id, sent.message_id, _NOTICE_DELETE_SECONDS)
    _schedule_delete(message.bot, message.chat.id, message.message_id, _NOTICE_DELETE_SECONDS)


def _resolve_target(message: Message, args: str | None) -> tuple[int | None, str, list[str]]:
    """Resolve the command target from a reply or a leading numeric ID.

    Returns (user_id | None, display_html, remaining_arg_tokens). When the command
    is a reply, all arg tokens are returned as the remainder (duration/reason);
    otherwise the first token must be a numeric user ID and is consumed.

    Replies to anonymous-admin or linked-channel posts are NOT resolvable targets:
    their from_user is a shared service account (GroupAnonymousBot / Telegram), so
    punishing it would hit the wrong entity in every chat at once.
    """
    tokens = (args or "").split()
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None and reply.sender_chat is None:
        return reply.from_user.id, _user_html(reply.from_user), tokens
    if tokens and _NUMERIC_ID_RE.match(tokens[0]):
        user_id = int(tokens[0])
        return user_id, _user_html_from_id(user_id), tokens[1:]
    return None, "", tokens


def _split_duration_and_reason(tokens: list[str]) -> tuple[int | None, str]:
    """Split command args into (duration_seconds, reason_text).

    Only the FIRST token is tried as a duration, so "/mute 2h спам" keeps the 2h
    and the rest becomes the reason. parse_duration is anchored, so joining all
    tokens would silently drop the duration whenever a reason follows it (for
    /ban that meant a PERMANENT ban instead of the requested one). When the first
    token does not parse, the whole text is the reason and the caller applies its
    own default duration.
    """
    if not tokens:
        return None, ""
    seconds = parse_duration(tokens[0])
    if seconds is not None:
        return seconds, " ".join(tokens[1:]).strip()
    return None, " ".join(tokens).strip()


def _user_html(user) -> str:
    name = html_escape(user.full_name or str(user.id))
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def _user_html_from_id(user_id: int, name: str | None = None) -> str:
    label = html_escape(name) if name else str(user_id)
    return f'<a href="tg://user?id={user_id}">{label}</a>'


def _actor_html(message: Message) -> str:
    if message.from_user is not None:
        return _user_html(message.from_user)
    if message.sender_chat is not None:
        return html_escape(message.sender_chat.title or str(message.sender_chat.id))
    return "—"


def _humanize_duration(seconds: int) -> str:
    if seconds <= 0:
        return t("moderation.duration.permanent")
    if seconds % 86400 == 0:
        return t("moderation.duration.days", n=seconds // 86400)
    if seconds % 3600 == 0:
        return t("moderation.duration.hours", n=seconds // 3600)
    if seconds % 60 == 0:
        return t("moderation.duration.minutes", n=seconds // 60)
    return t("moderation.duration.seconds", n=seconds)


def _format_when(raw: str) -> str:
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return raw or "—"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.strftime("%Y-%m-%d %H:%M UTC")
