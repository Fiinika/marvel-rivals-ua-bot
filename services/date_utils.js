import { DateTime, FixedOffsetZone, IANAZone } from "luxon";

import { t } from "./i18n.js";
import { getLogger } from "./logger.js";
import { collapseWhitespace, WORD } from "./pyutils.js";

const logger = getLogger("services.date_utils");

// The Python patterns used `\b`, which is Unicode-aware for `str` in Python but
// ASCII-only in JavaScript: an English month name butting against a Cyrillic
// letter would match here where it did not there. The explicit word-character
// lookarounds restore Python's meaning.
const NOT_WORD_BEFORE = `(?<!${WORD})`;
const NOT_WORD_AFTER = `(?!${WORD})`;

export const UTC_TIME_PATTERN = new RegExp(
  `${NOT_WORD_BEFORE}(?<hour>[01]?\\d|2[0-3]):(?<minute>[0-5]\\d)\\s*` +
    `(?<zone>UTC|GMT)(?<offset>\\s*[+-]\\s*\\d{1,2}(?::?[0-5]\\d)?)?${NOT_WORD_AFTER}`,
  "giu",
);

const MONTH_ALTERNATION =
  "Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|" +
  "Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?";

export const DATE_CONTEXT_PATTERN = new RegExp(
  `${NOT_WORD_BEFORE}(?:` +
    `(?:${MONTH_ALTERNATION})\\s+\\d{1,2}(?:,\\s*\\d{4})?` +
    "|" +
    `\\d{1,2}\\s+(?:${MONTH_ALTERNATION})(?:\\s+\\d{4})?` +
    "|" +
    "\\d{4}-\\d{1,2}-\\d{1,2}" +
    "|" +
    "\\d{1,2}/\\d{1,2}/\\d{2,4}" +
    `)${NOT_WORD_AFTER}`,
  "giu",
);

/**
 * @typedef {{original: string, article_date: string, article_date_display: string, has_time: boolean}} ParsedArticleDate
 */

/** @returns {ParsedArticleDate | null} */
export function parseArticleDate(value, targetTimezone) {
  if (!value || !String(value).trim()) {
    return null;
  }

  const rawValue = cleanDateText(String(value));
  if (!rawValue) {
    return null;
  }

  const parsed = parseFuzzyDateTime(rawValue);
  if (parsed === null) {
    logger.warning(`Could not parse article date ${JSON.stringify(value)}`);
    return null;
  }

  const hasTime = looksLikeDatetime(rawValue);
  if (parsed.offsetMinutes === null) {
    return {
      original: rawValue,
      article_date: isoDate(parsed),
      article_date_display: formatUkrainianArticleDate(
        DateTime.fromObject(
          { year: parsed.year, month: parsed.month, day: parsed.day, hour: 0, minute: 0, second: 0 },
          { zone: "utc" },
        ),
        false,
        targetTimezone,
      ),
      has_time: false,
    };
  }

  const converted = toDateTime(parsed).setZone(loadTimezone(targetTimezone));
  const machineValue = hasTime ? isoMinutes(converted) : converted.toFormat("yyyy-MM-dd");

  return {
    original: rawValue,
    article_date: machineValue,
    article_date_display: formatUkrainianArticleDate(converted, hasTime, targetTimezone),
    has_time: hasTime,
  };
}

export function formatUkrainianArticleDate(value, hasTime, targetTimezone) {
  const month = t(`date.months.${value.month}`);
  const datePart = `${value.day} ${month} ${value.year}`;

  if (!hasTime) {
    return t("date.published_date", { date: datePart });
  }

  const zoneLabel = timezoneLabel(targetTimezone, value);
  return t("date.published_datetime", {
    date: datePart,
    time: value.toFormat("HH:mm"),
    timezone_label: zoneLabel,
  });
}

export function buildUtcTimeConversionNotes(text, { article_date, target_timezone }) {
  UTC_TIME_PATTERN.lastIndex = 0;
  if (!text || !UTC_TIME_PATTERN.test(text)) {
    return "";
  }

  const targetTz = loadTimezone(target_timezone);
  const baseDate = machineDateToDate(article_date);
  const notes = [];
  const seen = new Set();

  UTC_TIME_PATTERN.lastIndex = 0;
  for (const match of text.matchAll(UTC_TIME_PATTERN)) {
    const hour = Number(match.groups.hour);
    const minute = Number(match.groups.minute);
    const sourceDate = findContextDate(text, match.index, baseDate) ?? baseDate;
    const originalTime = match[0];

    let note;
    if (sourceDate === null) {
      note =
        `${originalTime}: дата не знайдена, тому конвертація в Europe/Kyiv ненадійна. ` +
        "Не вигадуй час; якщо згадуєш його, залиш оригінальний UTC/GMT.";
    } else {
      const sourceDt = DateTime.fromObject(
        { year: sourceDate.year, month: sourceDate.month, day: sourceDate.day, hour, minute },
        { zone: timezoneFromUtcOffset(match.groups.offset) },
      );
      const converted = sourceDt.setZone(targetTz);
      const convertedLabel = formatConvertedTimeLabel(converted, target_timezone, sourceDate);
      note = `${originalTime} (${formatIsoDate(sourceDate)}) -> ${convertedLabel}`;
    }

    if (seen.has(note)) {
      continue;
    }

    seen.add(note);
    notes.push(note);
    if (notes.length >= 12) {
      break;
    }
  }

  if (!notes.length) {
    return "";
  }

  return notes.map((note) => `- ${note}`).join("\n");
}

function cleanDateText(value) {
  return collapseWhitespace(value);
}

function looksLikeDatetime(value) {
  return Boolean(/\d{1,2}:\d{2}/.test(value) || new RegExp(`${NOT_WORD_BEFORE}\\d{1,2}\\s*(?:AM|PM)${NOT_WORD_AFTER}`, "iu").test(value) || value.includes("T"));
}

/**
 * Resolve an IANA timezone name, falling back the same way the Python version
 * did: the configured zone, then Europe/Kyiv, then UTC. A typo in
 * ARTICLE_TIMEZONE must never stop the bot.
 */
function loadTimezone(targetTimezone) {
  if (IANAZone.isValidZone(targetTimezone)) {
    return IANAZone.create(targetTimezone);
  }
  logger.warning(`Unknown ARTICLE_TIMEZONE ${JSON.stringify(targetTimezone)}. Falling back to Europe/Kyiv.`);
  if (IANAZone.isValidZone("Europe/Kyiv")) {
    return IANAZone.create("Europe/Kyiv");
  }
  logger.warning("Europe/Kyiv timezone data is unavailable. Falling back to UTC.");
  return FixedOffsetZone.utcInstance;
}

function timezoneLabel(targetTimezone, value) {
  const offset = utcOffsetLabel(value);
  if (targetTimezone === "Europe/Kyiv") {
    return t("date.timezone_kyiv");
  }

  return t("date.timezone_generic", { timezone: targetTimezone, offset });
}

function machineDateToDate(value) {
  if (!value) {
    return null;
  }

  const text = String(value);
  const parsed = text.includes("T") ? DateTime.fromISO(text, { setZone: true }) : DateTime.fromISO(text);
  if (!parsed.isValid) {
    return null;
  }
  return { year: parsed.year, month: parsed.month, day: parsed.day };
}

function findContextDate(text, timePosition, fallbackDate) {
  const lineStart = text.lastIndexOf("\n", Math.max(0, timePosition - 1)) + 1;
  let lineEnd = text.indexOf("\n", timePosition);
  if (lineEnd === -1) {
    lineEnd = text.length;
  }

  const window = text.slice(lineStart, lineEnd);
  const relativeTimePosition = timePosition - lineStart;
  const candidates = [];
  DATE_CONTEXT_PATTERN.lastIndex = 0;
  for (const match of window.matchAll(DATE_CONTEXT_PATTERN)) {
    const parsed = parseContextDate(match[0], fallbackDate);
    if (parsed !== null) {
      candidates.push([match.index, parsed]);
    }
  }

  if (!candidates.length) {
    return null;
  }

  const preceding = candidates.filter(([start]) => start <= relativeTimePosition);
  if (preceding.length) {
    return preceding[preceding.length - 1][1];
  }

  return candidates[0][1];
}

function parseContextDate(value, fallbackDate) {
  const hasYear = new RegExp(`${NOT_WORD_BEFORE}\\d{4}${NOT_WORD_AFTER}`, "u").test(value);
  if (!hasYear && fallbackDate === null) {
    return null;
  }

  const defaultYear = (fallbackDate ?? DateTime.now()).year;
  const parsed = parseFuzzyDateTime(value, { year: defaultYear, month: 1, day: 1 });
  if (parsed === null) {
    return null;
  }

  return { year: parsed.year, month: parsed.month, day: parsed.day };
}

/**
 * Python `timezone(sign * timedelta(hours=..., minutes=...))` for the optional
 * `±HH[:MM]` suffix of a "10:00 UTC+3" style time. Anything unparseable falls
 * back to UTC, exactly as before.
 */
function timezoneFromUtcOffset(value) {
  if (!value || !value.trim()) {
    return FixedOffsetZone.utcInstance;
  }

  let normalized = value.replaceAll(" ", "");
  let sign = 1;
  if (normalized.startsWith("-")) {
    sign = -1;
    normalized = normalized.slice(1);
  } else if (normalized.startsWith("+")) {
    normalized = normalized.slice(1);
  }

  let rawHours;
  let rawMinutes;
  if (normalized.includes(":")) {
    const colon = normalized.indexOf(":");
    rawHours = normalized.slice(0, colon);
    rawMinutes = normalized.slice(colon + 1);
  } else if (normalized.length > 2) {
    rawHours = normalized.slice(0, -2);
    rawMinutes = normalized.slice(-2);
  } else {
    rawHours = normalized;
    rawMinutes = "0";
  }

  if (!/^\d+$/.test(rawHours) || !/^\d+$/.test(rawMinutes)) {
    return FixedOffsetZone.utcInstance;
  }
  const hours = Number(rawHours);
  const minutes = Number(rawMinutes);

  if (hours > 23 || minutes > 59) {
    return FixedOffsetZone.utcInstance;
  }

  return FixedOffsetZone.instance(sign * (hours * 60 + minutes));
}

function formatConvertedTimeLabel(value, targetTimezone, sourceDate) {
  const label =
    targetTimezone === "Europe/Kyiv"
      ? `${value.toFormat("HH:mm")} за Києвом`
      : `${value.toFormat("HH:mm")} за часовим поясом ${targetTimezone}`;

  if (value.year !== sourceDate.year || value.month !== sourceDate.month || value.day !== sourceDate.day) {
    return `${label} (${formatUkrainianDateOnly(value)})`;
  }

  return label;
}

function formatUkrainianDateOnly(value) {
  const month = t(`date.months.${value.month}`);
  return `${value.day} ${month} ${value.year}`;
}

function utcOffsetLabel(value) {
  const totalMinutes = value.offset;
  if (totalMinutes === null || totalMinutes === undefined || Number.isNaN(totalMinutes)) {
    return "UTC";
  }

  const sign = totalMinutes >= 0 ? "+" : "-";
  const absolute = Math.abs(totalMinutes);
  const hours = Math.floor(absolute / 60);
  const minutes = absolute % 60;
  if (minutes === 0) {
    return `UTC${sign}${hours}`;
  }

  return `UTC${sign}${hours}:${String(minutes).padStart(2, "0")}`;
}

// --------------------------------------------------------------------------- //
// Fuzzy date parsing (the `dateutil.parser.parse(..., fuzzy=True)` replacement)
// --------------------------------------------------------------------------- //

const MONTH_NAMES = {
  jan: 1, january: 1,
  feb: 2, february: 2,
  mar: 3, march: 3,
  apr: 4, april: 4,
  may: 5,
  jun: 6, june: 6,
  jul: 7, july: 7,
  aug: 8, august: 8,
  sep: 9, sept: 9, september: 9,
  oct: 10, october: 10,
  nov: 11, november: 11,
  dec: 12, december: 12,
};

/**
 * Extract a date/time from free text.
 *
 * `dateutil.parser.parse(..., fuzzy=True)` accepted arbitrary prose and pulled a
 * timestamp out of it. There is no equivalent in the Node ecosystem that behaves
 * the same way, so this covers exactly the shapes the collectors actually feed
 * it — a `<time datetime>` attribute, an `article:published_time` meta tag, and
 * the "June 12, 2026" / "12 June 2026" / "2026-06-12" / "6/12/2026" prose the
 * date-context scanner already enumerates in DATE_CONTEXT_PATTERN.
 *
 * Missing components fall back to `defaults`, matching dateutil's `default=`
 * argument. Returns null when the text carries no date and no time at all.
 *
 * @returns {{year:number, month:number, day:number, hour:number, minute:number,
 *            second:number, offsetMinutes:number|null} | null}
 */
export function parseFuzzyDateTime(text, defaults = null) {
  const value = String(text ?? "").trim();
  if (!value) return null;

  const now = DateTime.now();
  const base = defaults ?? { year: now.year, month: now.month, day: now.day };

  // Fast path: a full ISO-8601 timestamp, which is what every well-behaved feed
  // and `<time datetime="...">` attribute emits.
  const iso = DateTime.fromISO(value, { setZone: true });
  if (iso.isValid && /^\d{4}-\d{2}-\d{2}/.test(value)) {
    return {
      year: iso.year,
      month: iso.month,
      day: iso.day,
      hour: iso.hour,
      minute: iso.minute,
      second: iso.second,
      offsetMinutes: hasExplicitOffset(value) ? iso.offset : null,
    };
  }

  const rfc = DateTime.fromRFC2822(value, { setZone: true });
  if (rfc.isValid) {
    return {
      year: rfc.year,
      month: rfc.month,
      day: rfc.day,
      hour: rfc.hour,
      minute: rfc.minute,
      second: rfc.second,
      offsetMinutes: rfc.offset,
    };
  }

  const date = extractDateParts(value);
  const time = extractTimeParts(value);
  if (date === null && time === null) {
    return null;
  }

  const offsetMinutes = extractOffsetMinutes(value);
  return {
    year: date?.year ?? base.year,
    month: date?.month ?? base.month ?? 1,
    day: date?.day ?? base.day ?? 1,
    hour: time?.hour ?? 0,
    minute: time?.minute ?? 0,
    second: time?.second ?? 0,
    offsetMinutes,
  };
}

function hasExplicitOffset(value) {
  return /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value.trim());
}

function extractDateParts(value) {
  let match = /(\d{4})-(\d{1,2})-(\d{1,2})/.exec(value);
  if (match) {
    return validDate(Number(match[1]), Number(match[2]), Number(match[3]));
  }

  const monthWord = `(${Object.keys(MONTH_NAMES).sort((a, b) => b.length - a.length).join("|")})`;

  // Day-first ("12 June 2026") is tried BEFORE month-first: on that input the
  // month-first pattern would otherwise read the leading "20" of the year as the
  // day. The `(?!\d)` guards keep either pattern from slicing a day out of a
  // four-digit year in the first place.
  match = new RegExp(
    `${NOT_WORD_BEFORE}(\\d{1,2})(?!\\d)(?:st|nd|rd|th)?\\s+${monthWord}\\.?(?:,?\\s*(\\d{4}))?`,
    "iu",
  ).exec(value);
  if (match) {
    return validDate(match[3] ? Number(match[3]) : null, MONTH_NAMES[match[2].toLowerCase()], Number(match[1]));
  }

  match = new RegExp(
    `${NOT_WORD_BEFORE}${monthWord}\\.?\\s+(\\d{1,2})(?!\\d)(?:st|nd|rd|th)?(?:,?\\s*(\\d{4}))?`,
    "iu",
  ).exec(value);
  if (match) {
    return validDate(match[3] ? Number(match[3]) : null, MONTH_NAMES[match[1].toLowerCase()], Number(match[2]));
  }

  // dateutil defaults to month/day/year for ambiguous slash dates.
  match = /(\d{1,2})\/(\d{1,2})\/(\d{2,4})/.exec(value);
  if (match) {
    const rawYear = Number(match[3]);
    const year = match[3].length === 2 ? (rawYear < 69 ? 2000 + rawYear : 1900 + rawYear) : rawYear;
    return validDate(year, Number(match[1]), Number(match[2]));
  }

  return null;
}

function validDate(year, month, day) {
  if (month < 1 || month > 12 || day < 1 || day > 31) {
    return null;
  }
  return { year, month, day };
}

function extractTimeParts(value) {
  let match = /(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(am|pm)?/i.exec(value);
  if (match) {
    let hour = Number(match[1]);
    const meridiem = match[4]?.toLowerCase();
    if (meridiem === "pm" && hour < 12) hour += 12;
    if (meridiem === "am" && hour === 12) hour = 0;
    if (hour > 23 || Number(match[2]) > 59) return null;
    return { hour, minute: Number(match[2]), second: match[3] ? Number(match[3]) : 0 };
  }

  match = new RegExp(`${NOT_WORD_BEFORE}(\\d{1,2})\\s*(am|pm)${NOT_WORD_AFTER}`, "iu").exec(value);
  if (match) {
    let hour = Number(match[1]);
    const meridiem = match[2].toLowerCase();
    if (meridiem === "pm" && hour < 12) hour += 12;
    if (meridiem === "am" && hour === 12) hour = 0;
    if (hour > 23) return null;
    return { hour, minute: 0, second: 0 };
  }

  return null;
}

function extractOffsetMinutes(value) {
  const explicit = /(?:UTC|GMT)\s*([+-])\s*(\d{1,2})(?::?([0-5]\d))?/i.exec(value);
  if (explicit) {
    const sign = explicit[1] === "-" ? -1 : 1;
    return sign * (Number(explicit[2]) * 60 + Number(explicit[3] ?? 0));
  }

  if (new RegExp(`${NOT_WORD_BEFORE}(?:UTC|GMT|Z)${NOT_WORD_AFTER}`, "iu").test(value)) {
    return 0;
  }

  const numeric = /([+-])(\d{2}):?(\d{2})\s*$/.exec(value);
  if (numeric) {
    const sign = numeric[1] === "-" ? -1 : 1;
    return sign * (Number(numeric[2]) * 60 + Number(numeric[3]));
  }

  return null;
}

function toDateTime(parsed) {
  return DateTime.fromObject(
    {
      year: parsed.year,
      month: parsed.month,
      day: parsed.day,
      hour: parsed.hour,
      minute: parsed.minute,
      second: parsed.second,
    },
    { zone: FixedOffsetZone.instance(parsed.offsetMinutes ?? 0) },
  );
}

function isoDate(parsed) {
  return `${String(parsed.year).padStart(4, "0")}-${String(parsed.month).padStart(2, "0")}-${String(parsed.day).padStart(2, "0")}`;
}

function formatIsoDate(date) {
  return `${String(date.year).padStart(4, "0")}-${String(date.month).padStart(2, "0")}-${String(date.day).padStart(2, "0")}`;
}

/** Python's `datetime.isoformat(timespec="minutes")` — seconds always dropped. */
function isoMinutes(value) {
  return value.toFormat("yyyy-MM-dd'T'HH:mmZZ");
}
