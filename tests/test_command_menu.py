"""Tests for the admin-chat command menu: the content commands are advertised in
the admin chat only, never in a chat whose menu belongs to moderation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommandScopeChat

import main
from services.i18n import t


class _FakeBot:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[list, object]] = []
        self._fail = fail

    async def set_my_commands(self, commands, scope=None) -> None:
        if self._fail:
            raise TelegramAPIError(method=None, message="forbidden")
        self.calls.append((commands, scope))


def _config(*, admin_chat_id: int = -100, moderated: frozenset[int] = frozenset()) -> SimpleNamespace:
    return SimpleNamespace(admin_chat_id=admin_chat_id, telegram_moderation_chat_ids=moderated)


def test_admin_commands_are_scoped_to_the_admin_chat() -> None:
    bot = _FakeBot()
    asyncio.run(main._apply_admin_chat_commands(bot, _config(admin_chat_id=-4242)))

    assert len(bot.calls) == 1
    commands, scope = bot.calls[0]
    assert isinstance(scope, BotCommandScopeChat)
    assert scope.chat_id == -4242
    assert [c.command for c in commands] == [
        "fetch_news",
        "fanartdigest",
        "wikifact",
        "cleanup",
        "cancel",
    ]
    # Descriptions come from the locale, not a placeholder key.
    assert [c.description for c in commands] == [
        t("commands.fetch_news"),
        t("commands.fanartdigest"),
        t("commands.wikifact"),
        t("commands.cleanup"),
        t("commands.cancel"),
    ]
    assert all(c.description and not c.description.startswith("commands.") for c in commands)


def test_admin_commands_skipped_when_the_admin_chat_is_moderated() -> None:
    # That chat's chat-scope menu carries /report + /rules for ordinary members;
    # overwriting it would advertise admin commands to everyone in the chat.
    bot = _FakeBot()
    asyncio.run(main._apply_admin_chat_commands(bot, _config(admin_chat_id=-7, moderated=frozenset({-7}))))

    assert bot.calls == []


def test_admin_commands_survive_a_telegram_error() -> None:
    bot = _FakeBot(fail=True)
    # Must not raise: a failed menu update never blocks startup.
    asyncio.run(main._apply_admin_chat_commands(bot, _config()))

    assert bot.calls == []
