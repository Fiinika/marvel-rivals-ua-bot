from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil import parser as date_parser

from services.i18n import t


logger = logging.getLogger(__name__)

UTC_TIME_PATTERN = re.compile(
    r"\b(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)\s*"
    r"(?P<zone>UTC|GMT)(?P<offset>\s*[+-]\s*\d{1,2}(?::?[0-5]\d)?)?\b",
    re.IGNORECASE,
)
DATE_CONTEXT_PATTERN = re.compile(
    r"\b(?:"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+\d{1,2}(?:,\s*\d{4})?"
    r"|"
    r"\d{1,2}\s+"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"(?:\s+\d{4})?"
    r"|"
    r"\d{4}-\d{1,2}-\d{1,2}"
    r"|"
    r"\d{1,2}/\d{1,2}/\d{2,4}"
    r")\b",
    re.IGNORECASE,
)


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
        return ParsedArticleDate(
            original=raw_value,
            article_date=parsed.date().isoformat(),
            article_date_display=format_ukrainian_article_date(
                datetime.combine(parsed.date(), time.min),
                False,
                target_timezone,
            ),
            has_time=False,
        )

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

    zone_label = _timezone_label(target_timezone, value)
    return t("date.published_datetime", date=date_part, time=f"{value:%H:%M}", timezone_label=zone_label)


def build_utc_time_conversion_notes(
    text: str,
    *,
    article_date: str | None,
    target_timezone: str,
) -> str:
    if not text or not UTC_TIME_PATTERN.search(text):
        return ""

    target_tz = _load_timezone(target_timezone)
    base_date = _machine_date_to_date(article_date)
    notes: list[str] = []
    seen: set[str] = set()

    for match in UTC_TIME_PATTERN.finditer(text):
        hour = int(match.group("hour"))
        minute = int(match.group("minute"))
        source_date = _find_context_date(text, match.start(), base_date) or base_date
        original_time = match.group(0)

        if source_date is None:
            note = (
                f"{original_time}: дата не знайдена, тому конвертація в Europe/Kyiv ненадійна. "
                "Не вигадуй час; якщо згадуєш його, залиш оригінальний UTC/GMT."
            )
        else:
            source_dt = datetime.combine(
                source_date,
                time(hour, minute),
                tzinfo=_timezone_from_utc_offset(match.group("offset")),
            )
            converted = source_dt.astimezone(target_tz)
            converted_label = _format_converted_time_label(converted, target_timezone, source_date)
            note = f"{original_time} ({source_date.isoformat()}) -> {converted_label}"

        if note in seen:
            continue

        seen.add(note)
        notes.append(note)
        if len(notes) >= 12:
            break

    if not notes:
        return ""

    return "\n".join(f"- {note}" for note in notes)


def _clean_date_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _looks_like_datetime(value: str) -> bool:
    return bool(
        re.search(r"\d{1,2}:\d{2}", value)
        or re.search(r"\b\d{1,2}\s*(?:AM|PM)\b", value, flags=re.IGNORECASE)
        or "T" in value
    )


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


def _timezone_label(target_timezone: str, value: datetime) -> str:
    offset = _utc_offset_label(value)
    if target_timezone == "Europe/Kyiv":
        return t("date.timezone_kyiv")

    return t("date.timezone_generic", timezone=target_timezone, offset=offset)


def _machine_date_to_date(value: str | None) -> date | None:
    if not value:
        return None

    try:
        if "T" in value:
            return datetime.fromisoformat(value).date()
        return date.fromisoformat(value)
    except ValueError:
        return None


def _find_context_date(text: str, time_position: int, fallback_date: date | None) -> date | None:
    line_start = text.rfind("\n", 0, time_position) + 1
    line_end = text.find("\n", time_position)
    if line_end == -1:
        line_end = len(text)

    window = text[line_start:line_end]
    relative_time_position = time_position - line_start
    candidates: list[tuple[int, date]] = []
    for match in DATE_CONTEXT_PATTERN.finditer(window):
        parsed = _parse_context_date(match.group(0), fallback_date)
        if parsed is not None:
            candidates.append((match.start(), parsed))

    if not candidates:
        return None

    preceding_candidates = [candidate for candidate in candidates if candidate[0] <= relative_time_position]
    if preceding_candidates:
        return preceding_candidates[-1][1]

    return candidates[0][1]


def _parse_context_date(value: str, fallback_date: date | None) -> date | None:
    has_year = bool(re.search(r"\b\d{4}\b", value))
    if not has_year and fallback_date is None:
        return None

    default = datetime((fallback_date or date.today()).year, 1, 1)
    try:
        parsed = date_parser.parse(value, fuzzy=True, default=default)
    except (ValueError, OverflowError):
        return None

    return parsed.date()


def _timezone_from_utc_offset(value: str | None) -> tzinfo:
    if not value or not value.strip():
        return timezone.utc

    normalized = value.replace(" ", "")
    sign = 1
    if normalized.startswith("-"):
        sign = -1
        normalized = normalized[1:]
    elif normalized.startswith("+"):
        normalized = normalized[1:]

    if ":" in normalized:
        raw_hours, raw_minutes = normalized.split(":", 1)
    elif len(normalized) > 2:
        raw_hours, raw_minutes = normalized[:-2], normalized[-2:]
    else:
        raw_hours, raw_minutes = normalized, "0"

    try:
        hours = int(raw_hours)
        minutes = int(raw_minutes)
    except ValueError:
        return timezone.utc

    if hours > 23 or minutes > 59:
        return timezone.utc

    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def _format_converted_time_label(value: datetime, target_timezone: str, source_date: date) -> str:
    if target_timezone == "Europe/Kyiv":
        label = f"{value:%H:%M} за Києвом"
    else:
        label = f"{value:%H:%M} за часовим поясом {target_timezone}"

    if value.date() != source_date:
        return f"{label} ({_format_ukrainian_date_only(value)})"

    return label


def _format_ukrainian_date_only(value: datetime) -> str:
    month = t(f"date.months.{value.month}")
    return f"{value.day} {month} {value.year}"


def _utc_offset_label(value: datetime) -> str:
    offset = value.utcoffset()
    if offset is None:
        return "UTC"

    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    if minutes == 0:
        return f"UTC{sign}{hours}"

    return f"UTC{sign}{hours}:{minutes:02d}"
