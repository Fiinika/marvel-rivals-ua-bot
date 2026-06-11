"""Unit tests for the lenient env parsers in config.py that moderation and the
nightly backup rely on. Parsers read os.environ, so each test sets variables
through monkeypatch and never touches the real environment permanently."""

from __future__ import annotations

import pytest

import config as cfg


def test_parse_invite_allowlist_normalises_urls_and_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ALLOWLIST",
        "https://t.me/UAMarvelRivals, MarvelRivalsUABot, https://discord.gg/U8HvUB7NFt/,"
        " t.me/+AbCdEf123,, ",
    )
    assert cfg._parse_invite_allowlist("ALLOWLIST") == frozenset(
        {"uamarvelrivals", "marvelrivalsuabot", "u8hvub7nft", "+abcdef123"}
    )


def test_parse_invite_allowlist_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOWLIST", raising=False)
    assert cfg._parse_invite_allowlist("ALLOWLIST") == frozenset()


def test_parse_int_id_set_skips_bad_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IDS", "-1003984455160, oops, , -42")
    assert cfg._parse_int_id_set("IDS") == frozenset({-1003984455160, -42})


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        ("true", False, True),
        ("TRUE", False, True),
        ("1", False, True),
        ("yes", False, True),
        ("on", False, True),
        ("false", True, False),
        ("FALSE", True, False),
        ("0", True, False),
        ("no", True, False),
        ("off", True, False),
        ("nonsense", True, True),  # typo -> default, never silently flips a flag
        ("nonsense", False, False),
        ("", True, True),  # empty -> default
        (None, True, True),  # missing -> default
        (None, False, False),
    ],
)
def test_optional_bool(
    monkeypatch: pytest.MonkeyPatch, value: str | None, default: bool, expected: bool
) -> None:
    if value is None:
        monkeypatch.delenv("FLAG", raising=False)
    else:
        monkeypatch.setenv("FLAG", value)
    assert cfg._optional_bool("FLAG", default) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", 0),
        ("4", 4),
        ("23", 23),
        ("24", 4),  # out of range -> default
        ("-1", 4),
        ("abc", 4),
        ("", 4),
        (None, 4),
    ],
)
def test_optional_hour(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: int
) -> None:
    if value is None:
        monkeypatch.delenv("HOUR", raising=False)
    else:
        monkeypatch.setenv("HOUR", value)
    assert cfg._optional_hour("HOUR", 4) == expected
