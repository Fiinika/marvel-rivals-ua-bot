"""Tests for the user-submission anti-spam filter: too-short plain-text tips are
bounced before they reach the moderation queue, while links, media and admins are
exempt."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import handlers.user as user
from services.i18n import t


def _config(*, words: int = 3, chars: int = 10, admins: frozenset[int] = frozenset()) -> SimpleNamespace:
    return SimpleNamespace(
        min_submission_text_words=words,
        min_submission_text_chars=chars,
        admin_user_ids=admins,
    )


class _FakeMessage:
    def __init__(self, text: str, *, user_id: int = 999, entities=None) -> None:
        self.text = text
        self.caption = None
        self.from_user = SimpleNamespace(id=user_id, username="tester")
        self.entities = entities or []
        self.chat = SimpleNamespace(id=123, type="private")
        self.answers: list[str] = []

    async def answer(self, text: str) -> None:
        self.answers.append(text)


# --- the pure predicate ---------------------------------------------------------


def test_one_word_is_too_short() -> None:
    assert user._is_too_short_text(_FakeMessage("ы"), _config()) is True


def test_two_words_is_too_short() -> None:
    assert user._is_too_short_text(_FakeMessage("новий патч"), _config()) is True


def test_three_real_words_passes() -> None:
    assert user._is_too_short_text(_FakeMessage("Новий герой виходить завтра"), _config()) is False


def test_three_tiny_words_fail_on_char_floor() -> None:
    # 3 words but only 5 characters -> still junk, caught by the char floor.
    assert user._is_too_short_text(_FakeMessage("a a a"), _config()) is True


def test_admin_is_exempt() -> None:
    cfg = _config(admins=frozenset({7}))
    assert user._is_too_short_text(_FakeMessage("ы", user_id=7), cfg) is False


def test_disabled_when_both_thresholds_zero() -> None:
    assert user._is_too_short_text(_FakeMessage("ы"), _config(words=0, chars=0)) is False


def test_only_word_floor_active() -> None:
    cfg = _config(words=3, chars=0)
    assert user._is_too_short_text(_FakeMessage("a a a"), cfg) is False  # char floor off
    assert user._is_too_short_text(_FakeMessage("hi there"), cfg) is True  # 2 words


# --- the handler ----------------------------------------------------------------


def _run_submit(message: _FakeMessage, cfg: SimpleNamespace, monkeypatch) -> list:
    created: list = []

    async def _fake_create(**kwargs) -> None:
        created.append(kwargs)

    monkeypatch.setattr(user, "_create_and_send_submission", _fake_create)
    asyncio.run(user.submit_text(message, bot=None, config=cfg, db=None))
    return created


def test_handler_rejects_short_text(monkeypatch) -> None:
    message = _FakeMessage("ы")
    created = _run_submit(message, _config(), monkeypatch)

    assert created == []  # never queued
    assert message.answers == [t("user.too_short")]


def test_handler_accepts_long_text(monkeypatch) -> None:
    message = _FakeMessage("Новий герой Маг виходить наступного тижня")
    created = _run_submit(message, _config(), monkeypatch)

    assert len(created) == 1
    assert created[0]["message_type"] == "text"


def test_handler_accepts_short_link(monkeypatch) -> None:
    # A bare link is short on words but carries content -> must pass through.
    message = _FakeMessage("https://marvelrivals.com/x")
    created = _run_submit(message, _config(), monkeypatch)

    assert len(created) == 1
    assert created[0]["message_type"] == "link"
    assert message.answers == []


def test_handler_lets_admin_post_short_text(monkeypatch) -> None:
    message = _FakeMessage("тест", user_id=7)
    created = _run_submit(message, _config(admins=frozenset({7})), monkeypatch)

    assert len(created) == 1
