from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(ValueError):
    """Raised when required environment configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_chat_id: int
    publish_chat_id: int
    admin_user_ids: frozenset[int]
    database_path: str = "bot.db"
    submission_cooldown_seconds: int = 120
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    official_news_url: str = "https://www.marvelrivals.com/news/"
    news_check_interval_minutes: int | None = 30
    article_timezone: str = "Europe/Kyiv"
    # Discord moderation module (optional, fully independent from the Telegram flow).
    enable_discord_moderation: bool = False
    discord_bot_token: str | None = None
    discord_mod_log_channel_id: int | None = None
    discord_allowed_invites: frozenset[str] = frozenset()
    discord_guild_id: int | None = None
    # Optional welcome system + channels referenced in the welcome message.
    discord_welcome_channel_id: int | None = None
    discord_rules_channel_id: int | None = None
    discord_chat_channel_id: int | None = None
    discord_lft_channel_id: int | None = None


def load_config() -> Config:
    load_dotenv()

    bot_token = _required("BOT_TOKEN")
    admin_chat_id = _required_int("ADMIN_CHAT_ID")
    publish_chat_id = _required_int("PUBLISH_CHAT_ID")
    admin_user_ids = _parse_admin_user_ids(_required("ADMIN_USER_IDS"))
    database_path = os.getenv("DATABASE_PATH", "bot.db").strip() or "bot.db"
    submission_cooldown_seconds = _optional_non_negative_int("SUBMISSION_COOLDOWN_SECONDS", 120)
    gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip() or None
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
    official_news_url = os.getenv("OFFICIAL_NEWS_URL", "https://www.marvelrivals.com/news/").strip()
    official_news_url = official_news_url or "https://www.marvelrivals.com/news/"
    news_check_interval_minutes = _optional_positive_int_or_none("NEWS_CHECK_INTERVAL_MINUTES", 30)
    article_timezone = os.getenv("ARTICLE_TIMEZONE", "Europe/Kyiv").strip() or "Europe/Kyiv"

    # Discord values are parsed leniently: a misconfigured Discord setting must never
    # raise ConfigError, because that would also stop the Telegram bot from starting.
    enable_discord_moderation = _optional_bool("ENABLE_DISCORD_MODERATION", False)
    discord_bot_token = os.getenv("DISCORD_BOT_TOKEN", "").strip() or None
    discord_mod_log_channel_id = _optional_int_or_none("DISCORD_MOD_LOG_CHANNEL_ID")
    discord_allowed_invites = _parse_invite_allowlist("DISCORD_ALLOWED_INVITES")
    discord_guild_id = _optional_int_or_none("DISCORD_GUILD_ID")
    discord_welcome_channel_id = _optional_int_or_none("DISCORD_WELCOME_CHANNEL_ID")
    discord_rules_channel_id = _optional_int_or_none("DISCORD_RULES_CHANNEL_ID")
    discord_chat_channel_id = _optional_int_or_none("DISCORD_CHAT_CHANNEL_ID")
    discord_lft_channel_id = _optional_int_or_none("DISCORD_LFT_CHANNEL_ID")

    return Config(
        bot_token=bot_token,
        admin_chat_id=admin_chat_id,
        publish_chat_id=publish_chat_id,
        admin_user_ids=admin_user_ids,
        database_path=database_path,
        submission_cooldown_seconds=submission_cooldown_seconds,
        gemini_api_key=gemini_api_key,
        gemini_model=gemini_model,
        official_news_url=official_news_url,
        news_check_interval_minutes=news_check_interval_minutes,
        article_timezone=article_timezone,
        enable_discord_moderation=enable_discord_moderation,
        discord_bot_token=discord_bot_token,
        discord_mod_log_channel_id=discord_mod_log_channel_id,
        discord_allowed_invites=discord_allowed_invites,
        discord_guild_id=discord_guild_id,
        discord_welcome_channel_id=discord_welcome_channel_id,
        discord_rules_channel_id=discord_rules_channel_id,
        discord_chat_channel_id=discord_chat_channel_id,
        discord_lft_channel_id=discord_lft_channel_id,
    )


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ConfigError(f"Missing required environment variable: {name}")
    return value.strip()


def _required_int(name: str) -> int:
    value = _required(name)
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _optional_non_negative_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default

    try:
        parsed = int(value.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc

    if parsed < 0:
        raise ConfigError(f"{name} must be 0 or greater")

    return parsed


def _optional_positive_int_or_none(name: str, default: int | None) -> int | None:
    value = os.getenv(name)
    if value is None:
        return default

    if not value.strip():
        return None

    try:
        parsed = int(value.strip())
    except ValueError:
        return None

    if parsed <= 0:
        return None

    return parsed


_TRUE_VALUES = {"true", "1", "yes", "on"}


def _optional_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in _TRUE_VALUES


def _optional_int_or_none(name: str) -> int | None:
    """Parse an optional integer, returning None when missing, empty, or invalid.

    Used for Discord settings so a typo cannot crash the Telegram bot at startup.
    """
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def _parse_invite_allowlist(name: str) -> frozenset[str]:
    """Normalise DISCORD_ALLOWED_INVITES into a set of lowercase invite codes.

    Accepts bare codes (``abc123``) or full URLs (``https://discord.gg/abc123``)
    and keeps only the trailing code. An empty value means "block every invite".
    """
    raw = os.getenv(name, "")
    codes: set[str] = set()
    for part in raw.split(","):
        token = part.strip().rstrip("/")
        if not token:
            continue
        if "/" in token:
            token = token.rsplit("/", 1)[-1]
        if token:
            codes.add(token.lower())
    return frozenset(codes)


def _parse_admin_user_ids(value: str) -> frozenset[int]:
    raw_ids = [part.strip() for part in value.split(",") if part.strip()]
    if not raw_ids:
        raise ConfigError("ADMIN_USER_IDS must contain at least one Telegram user ID")

    admin_ids: set[int] = set()
    for raw_id in raw_ids:
        try:
            admin_ids.add(int(raw_id))
        except ValueError as exc:
            raise ConfigError("ADMIN_USER_IDS must be comma-separated integers") from exc

    return frozenset(admin_ids)
