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


def load_config() -> Config:
    load_dotenv()

    bot_token = _required("BOT_TOKEN")
    admin_chat_id = _required_int("ADMIN_CHAT_ID")
    publish_chat_id = _required_int("PUBLISH_CHAT_ID")
    admin_user_ids = _parse_admin_user_ids(_required("ADMIN_USER_IDS"))
    database_path = os.getenv("DATABASE_PATH", "bot.db").strip() or "bot.db"
    submission_cooldown_seconds = _optional_non_negative_int("SUBMISSION_COOLDOWN_SECONDS", 120)

    return Config(
        bot_token=bot_token,
        admin_chat_id=admin_chat_id,
        publish_chat_id=publish_chat_id,
        admin_user_ids=admin_user_ids,
        database_path=database_path,
        submission_cooldown_seconds=submission_cooldown_seconds,
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
