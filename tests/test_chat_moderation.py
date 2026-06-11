"""Unit tests for the pure moderation logic in services/chat_moderation.py.

Everything tested here is synchronous and side-effect free, so the tests need
no aiogram, no event loop, and no network. File-backed loaders are pointed at
tmp_path copies; the word-list regex is rebuilt from a fixed list so the tests
never depend on the repo's editable telegram_badwords.txt.
"""

from __future__ import annotations

import pytest

from services import chat_moderation as cm


# --------------------------------------------------------------------------- #
# suspicious_reason
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "text",
    [
        "FREE NITRO for everyone, hurry up",
        "get free discord nitro here",
        "nitro for free!!!",
        "free telegram premium на місяць",
        "telegram premium for free",
        "we will double your crypto in 24h",
        "2x your BTC guaranteed",
        "crypto airdrop just started",
        "join my pump group",
        "insider signals from the exchange",
        "earn $500 per day from home",
        "earn 100 a day easily",
        "login via dlscord.com to claim",
        "steamcommunlty.com/trade/123",
        "check https://grabify.link/abc123 now",
    ],
)
def test_suspicious_reason_flags_scam(text: str) -> None:
    assert cm.suspicious_reason(text) == "scam"


def test_linked_scam_patterns_require_a_link() -> None:
    # Normal gaming chat: free skins / steam gift talk without a URL is fine…
    assert cm.suspicious_reason("в івенті роздають free skins всім") is None
    assert cm.suspicious_reason("хто хоче steam gift card на день народження?") is None
    # …but the same phrases plus any link smell like scam.
    assert cm.suspicious_reason("free skins тут: https://example.com/skins") == "scam"
    assert cm.suspicious_reason("steam gift забирай www.totally-legit.com") == "scam"
    assert cm.suspicious_reason("free robux t.me/grab_it") == "scam"


def test_shortener_is_suspicious_only_with_scam_keyword() -> None:
    assert cm.suspicious_reason("https://bit.ly/3xYz claim your free gift") == "shortener"
    assert cm.suspicious_reason("ось патчноути https://bit.ly/3xYz") is None
    # Keyword without a shortener is not enough either.
    assert cm.suspicious_reason("хто виграв giveaway вчора?") is None


@pytest.mark.parametrize(
    "text",
    [
        "хто грає ввечері? го стак",
        "новий патч поламав Джеффа, граємо?",
        "https://www.marvelrivals.com/news/ читайте патчноути",
        "купив нову відеокарту, fps виріс удвічі",
    ],
)
def test_suspicious_reason_passes_normal_chat(text: str) -> None:
    assert cm.suspicious_reason(text) is None


# --------------------------------------------------------------------------- #
# has_blocked_invite
# --------------------------------------------------------------------------- #

EMPTY: frozenset[str] = frozenset()


def test_invite_blocked_when_not_allowlisted() -> None:
    assert cm.has_blocked_invite("заходьте t.me/SpamChannel", EMPTY) is True
    assert cm.has_blocked_invite("https://t.me/joinchat/AbCdEf", EMPTY) is True
    assert cm.has_blocked_invite("telegram.me/another_group", EMPTY) is True
    assert cm.has_blocked_invite("telegram.dog/SpamGroup", EMPTY) is True
    assert cm.has_blocked_invite("https://t.me/+PrivateHash123", EMPTY) is True


def test_invite_allowed_when_allowlisted() -> None:
    allow = frozenset({"uamarvelrivals", "marvelrivalsuabot"})
    assert cm.has_blocked_invite("наш канал https://t.me/UAMarvelRivals", allow) is False
    assert cm.has_blocked_invite("пишіть боту t.me/MarvelRivalsUABot", allow) is False
    # Mixed message: one allowlisted + one foreign link must still block.
    assert (
        cm.has_blocked_invite("t.me/UAMarvelRivals і ще t.me/SpamChannel", allow) is True
    )


def test_invite_allowlist_accepts_plus_prefixed_hash() -> None:
    # The config parser keeps the "+" from t.me/+hash URLs; the regex strips it.
    allow = frozenset({"+abcdef123"})
    assert cm.has_blocked_invite("https://t.me/+AbCdEf123", allow) is False


def test_non_invite_tme_paths_are_ignored() -> None:
    assert cm.has_blocked_invite("стікери: t.me/addstickers/CoolPack", EMPTY) is False
    assert cm.has_blocked_invite("t.me/share/url?url=x", EMPTY) is False
    assert cm.has_blocked_invite("цитата t.me/c/1234567/89", EMPTY) is False


def test_other_domains_do_not_match() -> None:
    assert cm.has_blocked_invite("моє портфоліо about.me/john", EMPTY) is False
    assert cm.has_blocked_invite("пишіть на support.me/help", EMPTY) is False
    assert cm.has_blocked_invite("повідомлення без посилань", EMPTY) is False


# --------------------------------------------------------------------------- #
# Bad-word filter (regex rebuilt from a fixed list, independent of the repo file)
# --------------------------------------------------------------------------- #

@pytest.fixture()
def fixed_bad_words(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cm, "BAD_WORDS", ("badword", "хейтслово"))
    monkeypatch.setattr(cm, "_BAD_WORD_RE", cm._build_bad_word_re())


def test_bad_word_matches_whole_words_only(fixed_bad_words: None) -> None:
    assert cm.has_bad_word("this has badword inside") is True
    assert cm.has_bad_word("BADWORD!!!") is True
    assert cm.has_bad_word("embedbadwordx is fine") is False


def test_bad_word_unicode_boundaries(fixed_bad_words: None) -> None:
    assert cm.has_bad_word("ну ти й хейтслово,") is True
    # Substring inside a longer Ukrainian word must NOT match.
    assert cm.has_bad_word("обмінхейтсловом не рахується") is False


def test_load_bad_words_parses_comments_dupes_and_case(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    words_file = tmp_path / "badwords.txt"
    words_file.write_text("# comment\nFoo\n\nbar\nfoo\n", encoding="utf-8")
    monkeypatch.setattr(cm, "_BAD_WORDS_FILE", words_file)
    assert cm._load_bad_words() == ("foo", "bar")


def test_load_bad_words_falls_back_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(cm, "_BAD_WORDS_FILE", tmp_path / "absent.txt")
    assert cm._load_bad_words() == cm._DEFAULT_BAD_WORDS


def test_load_welcome_rules(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    rules_file = tmp_path / "rules.txt"
    rules_file.write_text("# заголовок\nПравило один\n\nПравило два\n", encoding="utf-8")
    monkeypatch.setattr(cm, "_RULES_FILE", rules_file)
    assert cm.load_welcome_rules() == ("Правило один", "Правило два")

    monkeypatch.setattr(cm, "_RULES_FILE", tmp_path / "absent.txt")
    assert cm.load_welcome_rules() == ()


# --------------------------------------------------------------------------- #
# SpamTracker
# --------------------------------------------------------------------------- #

class FakeClock:
    """Injected via SpamTracker's clock parameter — no global time patching."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_spam_tracker_triggers_once_per_burst() -> None:
    tracker = cm.SpamTracker(window_seconds=7.0, max_messages=3, clock=FakeClock())
    assert [tracker.register_and_check(1, 42) for _ in range(3)] == [False] * 3
    assert tracker.register_and_check(1, 42) is True
    # The bucket was reset: the very next message does not re-trigger.
    assert tracker.register_and_check(1, 42) is False


def test_spam_tracker_isolates_chats_and_users() -> None:
    tracker = cm.SpamTracker(window_seconds=7.0, max_messages=2, clock=FakeClock())
    assert tracker.register_and_check(1, 42) is False
    assert tracker.register_and_check(2, 42) is False  # same user, other chat
    assert tracker.register_and_check(1, 43) is False  # other user, same chat
    assert tracker.register_and_check(1, 42) is False
    assert tracker.register_and_check(1, 42) is True


def test_spam_tracker_window_expiry() -> None:
    clock = FakeClock()
    tracker = cm.SpamTracker(window_seconds=7.0, max_messages=2, clock=clock)
    assert tracker.register_and_check(1, 42) is False
    assert tracker.register_and_check(1, 42) is False
    clock.now += 8.0  # the earlier messages fall out of the window
    assert tracker.register_and_check(1, 42) is False


def test_spam_tracker_sweep_drops_stale_buckets() -> None:
    clock = FakeClock()
    tracker = cm.SpamTracker(window_seconds=7.0, max_messages=5, clock=clock)
    tracker.register_and_check(1, 42)
    tracker.register_and_check(2, 43)
    clock.now += 100.0
    tracker.register_and_check(3, 44)
    tracker.sweep()
    assert set(tracker._buckets) == {(3, 44)}


# --------------------------------------------------------------------------- #
# Duration parsing and clamping
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30", 30 * 60),  # bare number = minutes
        ("45s", 45),
        ("2h", 2 * 60 * 60),
        ("1d", 24 * 60 * 60),
        ("1w", 7 * 24 * 60 * 60),
        ("10 m", 10 * 60),  # space before the unit is fine
        ("2H", 2 * 60 * 60),  # unit is case-insensitive
        ("0", 0),  # explicit permanent
        ("0s", 0),
        ("perm", 0),
        ("forever", 0),
        ("назавжди", 0),
        (None, None),
        ("", None),
        ("   ", None),
        ("abc", None),
        ("-5", None),
        ("5x", None),
        ("1.5h", None),
    ],
)
def test_parse_duration(text: str | None, expected: int | None) -> None:
    assert cm.parse_duration(text) == expected


def test_clamp_mute_seconds() -> None:
    assert cm.clamp_mute_seconds(1) == cm.MIN_MUTE_SECONDS
    assert cm.clamp_mute_seconds(cm.MIN_MUTE_SECONDS) == cm.MIN_MUTE_SECONDS
    assert cm.clamp_mute_seconds(3600) == 3600
    assert cm.clamp_mute_seconds(cm.MAX_MUTE_SECONDS + 1) == cm.MAX_MUTE_SECONDS
