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
    enable_community_footer: bool = True


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
    enable_community_footer = _optional_bool("ENABLE_COMMUNITY_FOOTER", True)

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
        enable_community_footer=enable_community_footer,
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


def _optional_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False

    raise ConfigError(f"{name} must be true or false")


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
