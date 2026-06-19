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


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Stub load_dotenv so a developer's local .env can never leak values into the
    # config tests (these assert behaviour for explicit/absent env vars only).
    monkeypatch.setattr(cfg, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("BOT_TOKEN", "token")
    monkeypatch.setenv("ADMIN_CHAT_ID", "1")
    monkeypatch.setenv("PUBLISH_CHAT_ID", "2")
    monkeypatch.setenv("ADMIN_USER_IDS", "3")


def test_submission_limits_and_send_interval_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    for key in (
        "MIN_SUBMISSION_TEXT_WORDS",
        "MIN_SUBMISSION_TEXT_CHARS",
        "MODERATION_SEND_INTERVAL_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)

    config = cfg.load_config()

    assert config.min_submission_text_words == 3
    assert config.min_submission_text_chars == 10
    assert config.moderation_send_interval_seconds == 5


def test_submission_limits_and_send_interval_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MIN_SUBMISSION_TEXT_WORDS", "5")
    monkeypatch.setenv("MIN_SUBMISSION_TEXT_CHARS", "0")  # 0 disables the char floor
    monkeypatch.setenv("MODERATION_SEND_INTERVAL_SECONDS", "8")

    config = cfg.load_config()

    assert config.min_submission_text_words == 5
    assert config.min_submission_text_chars == 0
    assert config.moderation_send_interval_seconds == 8


def test_send_interval_typo_keeps_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # Parsed leniently (like the other scheduler settings): a typo must never stop
    # the bot, it just keeps the default pacing.
    _set_required_env(monkeypatch)
    monkeypatch.setenv("MODERATION_SEND_INTERVAL_SECONDS", "soon")

    assert cfg.load_config().moderation_send_interval_seconds == 5


def test_cross_source_dedup_defaults_on(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.delenv("ENABLE_CROSS_SOURCE_DEDUP", raising=False)
    monkeypatch.delenv("CROSS_SOURCE_DEDUP_TITLE_LIMIT", raising=False)

    config = cfg.load_config()

    assert config.enable_cross_source_dedup is True
    assert config.cross_source_dedup_title_limit == 200


def test_cross_source_dedup_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ENABLE_CROSS_SOURCE_DEDUP", "false")
    monkeypatch.setenv("CROSS_SOURCE_DEDUP_TITLE_LIMIT", "50")

    config = cfg.load_config()

    assert config.enable_cross_source_dedup is False
    assert config.cross_source_dedup_title_limit == 50


def test_youtube_source_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    for key in (
        "ENABLE_YOUTUBE_SOURCE",
        "YOUTUBE_CHANNEL_ID",
        "YOUTUBE_EXCLUDE_KEYWORDS",
        "ENABLE_YOUTUBE_VIDEO_DOWNLOAD",
        "YOUTUBE_VIDEO_MAX_MB",
    ):
        monkeypatch.delenv(key, raising=False)

    config = cfg.load_config()

    assert config.enable_youtube_source is False
    assert config.youtube_channel_id == cfg.DEFAULT_YOUTUBE_CHANNEL_ID
    assert config.youtube_exclude_keywords == cfg.DEFAULT_YOUTUBE_EXCLUDE_KEYWORDS
    assert config.enable_youtube_video_download is True
    assert config.youtube_video_max_mb == 48


def test_youtube_video_download_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ENABLE_YOUTUBE_VIDEO_DOWNLOAD", "false")
    monkeypatch.setenv("YOUTUBE_VIDEO_MAX_MB", "20")

    config = cfg.load_config()

    assert config.enable_youtube_video_download is False
    assert config.youtube_video_max_mb == 20


def test_youtube_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ENABLE_YOUTUBE_SOURCE", "true")
    monkeypatch.setenv("YOUTUBE_CHANNEL_ID", "UCcustom")
    monkeypatch.setenv("YOUTUBE_EXCLUDE_KEYWORDS", "Foo, BAR ,baz")

    config = cfg.load_config()

    assert config.enable_youtube_source is True
    assert config.youtube_channel_id == "UCcustom"
    assert config.youtube_exclude_keywords == frozenset({"foo", "bar", "baz"})


def test_youtube_exclude_keywords_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("YOUTUBE_EXCLUDE_KEYWORDS", "-")

    config = cfg.load_config()

    assert config.youtube_exclude_keywords == frozenset()


def test_bluesky_video_defaults_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    for key in ("ENABLE_BLUESKY_VIDEO_DOWNLOAD", "BLUESKY_VIDEO_MAX_MB"):
        monkeypatch.delenv(key, raising=False)
    config = cfg.load_config()
    assert config.enable_bluesky_video_download is True
    assert config.bluesky_video_max_mb == 48

    monkeypatch.setenv("ENABLE_BLUESKY_VIDEO_DOWNLOAD", "false")
    monkeypatch.setenv("BLUESKY_VIDEO_MAX_MB", "20")
    config = cfg.load_config()
    assert config.enable_bluesky_video_download is False
    assert config.bluesky_video_max_mb == 20


def test_reddit_source_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    for key in ("ENABLE_REDDIT_SOURCE", "REDDIT_SUBREDDIT", "REDDIT_FLAIRS", "REDDIT_EXCLUDE_KEYWORDS"):
        monkeypatch.delenv(key, raising=False)

    config = cfg.load_config()

    assert config.enable_reddit_source is False
    assert config.reddit_subreddit == cfg.DEFAULT_REDDIT_SUBREDDIT
    assert config.reddit_flairs == cfg.DEFAULT_REDDIT_FLAIRS
    assert config.reddit_exclude_keywords == cfg.DEFAULT_REDDIT_EXCLUDE_KEYWORDS


def test_reddit_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ENABLE_REDDIT_SOURCE", "true")
    monkeypatch.setenv("REDDIT_SUBREDDIT", "MarvelRivals")
    monkeypatch.setenv("REDDIT_FLAIRS", "Patch Notes, Esports")

    config = cfg.load_config()

    assert config.enable_reddit_source is True
    assert config.reddit_subreddit == "MarvelRivals"
    assert config.reddit_flairs == ("Patch Notes", "Esports")


def test_fanart_digest_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    for key in (
        "ENABLE_FANART_DIGEST",
        "FANART_SUBREDDIT",
        "FANART_FLAIR",
        "FANART_DIGEST_WEEKDAY",
        "FANART_DIGEST_HOUR",
        "FANART_DIGEST_COUNT",
    ):
        monkeypatch.delenv(key, raising=False)

    config = cfg.load_config()

    assert config.enable_fanart_digest is False
    assert config.fanart_subreddit == cfg.DEFAULT_FANART_SUBREDDIT
    assert config.fanart_flair == cfg.DEFAULT_FANART_FLAIR
    assert config.fanart_digest_weekday == cfg.DEFAULT_FANART_DIGEST_WEEKDAY
    assert config.fanart_digest_hour == cfg.DEFAULT_FANART_DIGEST_HOUR
    assert config.fanart_digest_count == cfg.DEFAULT_FANART_DIGEST_COUNT


def test_fanart_digest_count_clamped_to_ten(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ENABLE_FANART_DIGEST", "true")
    monkeypatch.setenv("FANART_DIGEST_COUNT", "50")
    monkeypatch.setenv("FANART_DIGEST_WEEKDAY", "6")

    config = cfg.load_config()

    assert config.enable_fanart_digest is True
    assert config.fanart_digest_count == 10  # capped at Telegram's media-group max
    assert config.fanart_digest_weekday == 6


def test_fanart_digest_invalid_weekday_keeps_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("FANART_DIGEST_WEEKDAY", "9")  # out of 0..6

    config = cfg.load_config()

    assert config.fanart_digest_weekday == cfg.DEFAULT_FANART_DIGEST_WEEKDAY
