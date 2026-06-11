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
    # (Rules are shown inline from discord_rules.txt, not via a channel link.)
    discord_welcome_channel_id: int | None = None
    discord_chat_channel_id: int | None = None
    discord_lft_channel_id: int | None = None
    # Telegram group-chat moderation module (optional, runs in the same process).
    # Like the Discord block, every value is parsed leniently so a typo can never
    # raise ConfigError and stop the bot from starting.
    enable_telegram_moderation: bool = False
    telegram_moderation_chat_ids: frozenset[int] = frozenset()
    # Where moderation actions are logged. Falls back to admin_chat_id at use-time.
    telegram_mod_log_chat_id: int | None = None
    # Permitted t.me link codes/usernames (lowercased). Empty = block all t.me links.
    telegram_link_allowlist: frozenset[str] = frozenset()
    # Seconds before the welcome message auto-deletes. 0 disables auto-delete.
    telegram_welcome_delete_seconds: int = 60
    # Nightly backup: the SQLite file is snapshotted once a day into backups/
    # next to the database; the newest N snapshots are kept. Parsed leniently —
    # a typo never stops the bot.
    enable_database_backup: bool = True
    database_backup_hour: int = 4
    database_backup_keep: int = 14


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
    discord_chat_channel_id = _optional_int_or_none("DISCORD_CHAT_CHANNEL_ID")
    discord_lft_channel_id = _optional_int_or_none("DISCORD_LFT_CHANNEL_ID")

    # Telegram moderation values are parsed leniently for the same reason as Discord:
    # a misconfigured moderation setting must never stop the whole bot from starting.
    enable_telegram_moderation = _optional_bool("ENABLE_TELEGRAM_MODERATION", False)
    telegram_moderation_chat_ids = _parse_int_id_set("TELEGRAM_MODERATION_CHAT_IDS")
    telegram_mod_log_chat_id = _optional_int_or_none("TELEGRAM_MOD_LOG_CHAT_ID")
    # The link allowlist reuses the invite parser: it strips full URLs down to the
    # trailing code and lowercases it, so "https://t.me/UAMarvelRivalsChat" -> "uamarvelrivalschat".
    telegram_link_allowlist = _parse_invite_allowlist("TELEGRAM_LINK_ALLOWLIST")
    telegram_welcome_delete_seconds = _optional_lenient_non_negative_int("TELEGRAM_WELCOME_DELETE_SECONDS", 60)

    enable_database_backup = _optional_bool("ENABLE_DATABASE_BACKUP", True)
    database_backup_hour = _optional_hour("DATABASE_BACKUP_HOUR", 4)
    # `or 14`: zero would mean "keep nothing", which can only be a mistake.
    database_backup_keep = _optional_lenient_non_negative_int("DATABASE_BACKUP_KEEP", 14) or 14

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
        discord_chat_channel_id=discord_chat_channel_id,
        discord_lft_channel_id=discord_lft_channel_id,
        enable_telegram_moderation=enable_telegram_moderation,
        telegram_moderation_chat_ids=telegram_moderation_chat_ids,
        telegram_mod_log_chat_id=telegram_mod_log_chat_id,
        telegram_link_allowlist=telegram_link_allowlist,
        telegram_welcome_delete_seconds=telegram_welcome_delete_seconds,
        enable_database_backup=enable_database_backup,
        database_backup_hour=database_backup_hour,
        database_backup_keep=database_backup_keep,
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
_FALSE_VALUES = {"false", "0", "no", "off"}


def _optional_bool(name: str, default: bool) -> bool:
    """Parse an optional boolean, falling back to the default on typos.

    Unrecognized values keep the default (like the other lenient parsers) so a
    misspelt value can never silently flip a default-on feature like the
    nightly backup off — explicit "false"/"0"/"no"/"off" is required for that.
    """
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    token = value.strip().lower()
    if token in _TRUE_VALUES:
        return True
    if token in _FALSE_VALUES:
        return False
    return default


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


def _optional_lenient_non_negative_int(name: str, default: int) -> int:
    """Parse an optional non-negative int, returning the default on missing/invalid.

    Unlike _optional_non_negative_int, this never raises ConfigError — used for
    Telegram moderation settings so a typo cannot stop the whole bot from starting.
    """
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


def _optional_hour(name: str, default: int) -> int:
    """Parse an hour of day (0-23), returning the default on missing/invalid.

    Lenient for the same reason as the other backup/moderation settings: a typo
    must never stop the whole bot from starting.
    """
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        return default
    return parsed if 0 <= parsed <= 23 else default


def _parse_int_id_set(name: str) -> frozenset[int]:
    """Parse a comma-separated list of chat IDs leniently into a frozenset[int].

    Blank or non-integer entries are skipped rather than raising, so one bad value
    in TELEGRAM_MODERATION_CHAT_IDS cannot stop the bot from starting.
    """
    raw = os.getenv(name, "")
    ids: set[int] = set()
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            ids.add(int(token))
        except ValueError:
            continue
    return frozenset(ids)


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
