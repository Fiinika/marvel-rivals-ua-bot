from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil import parser as date_parser

from services.i18n import t


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParsedArticleDate:
    original: str
    article_date: str
    article_date_display: str
    has_time: bool


def parse_article_date(value: str | None, target_timezone: str) -> ParsedArticleDate | None:
    if not value or not value.strip():
        return None

    raw_value = _clean_date_text(value)
    if not raw_value:
        return None

    try:
        parsed = date_parser.parse(raw_value, fuzzy=True)
    except (ValueError, OverflowError) as exc:
        logger.warning("Could not parse article date %r: %s", value, exc)
        return None

    has_time = _looks_like_datetime(raw_value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    converted = parsed.astimezone(_load_timezone(target_timezone))
    if has_time:
        machine_value = converted.isoformat(timespec="minutes")
    else:
        machine_value = converted.date().isoformat()

    return ParsedArticleDate(
        original=raw_value,
        article_date=machine_value,
        article_date_display=format_ukrainian_article_date(converted, has_time, target_timezone),
        has_time=has_time,
    )


def format_ukrainian_article_date(value: datetime, has_time: bool, target_timezone: str) -> str:
    month = t(f"date.months.{value.month}")
    date_part = f"{value.day} {month} {value.year}"

    if not has_time:
        return t("date.published_date", date=date_part)

    zone_label = _timezone_label(target_timezone)
    return t("date.published_datetime", date=date_part, time=f"{value:%H:%M}", timezone_label=zone_label)


def _clean_date_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _looks_like_datetime(value: str) -> bool:
    return bool(re.search(r"\d{1,2}:\d{2}", value) or "T" in value)


def _load_timezone(target_timezone: str) -> tzinfo:
    try:
        return ZoneInfo(target_timezone)
    except ZoneInfoNotFoundError:
        logger.warning("Unknown ARTICLE_TIMEZONE %r. Falling back to Europe/Kyiv.", target_timezone)
        try:
            return ZoneInfo("Europe/Kyiv")
        except ZoneInfoNotFoundError:
            logger.warning("Europe/Kyiv timezone data is unavailable. Falling back to UTC.")
            return timezone.utc


def _timezone_label(target_timezone: str) -> str:
    if target_timezone == "Europe/Kyiv":
        return t("date.timezone_kyiv")

    return t("date.timezone_generic", timezone=target_timezone)
