from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from services.i18n import t


# Gemini's free tier throttles by requests-per-minute; a burst (e.g. several
# dedup + draft calls in one scheduler tick) trips a 429 that is transient. Retry
# it a few times with a bounded backoff so one throttle doesn't drop a draft.
_GEMINI_MAX_ATTEMPTS = 3
_GEMINI_MAX_RETRY_DELAY_SECONDS = 20.0


logger = logging.getLogger(__name__)
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "gemini_news_uk.md"
SHORT_FORM_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "gemini_shortform_uk.md"
STYLE_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "official_news_style.md"
DEDUP_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "gemini_dedup_uk.md"
WIKI_FACT_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "gemini_wiki_fact_uk.md"
POST_SEPARATOR = "---POST---"
TAGS_SEPARATOR = "---TAGS---"
OFFICIAL_SOURCE_TYPES = {"official_marvel_rivals"}
# Sources whose items are unofficial leaks/datamines: the short-form draft must
# frame them as rumours (чутки), never as confirmed/official statements.
RUMOR_SOURCE_TYPES = {"reddit", "rivalskins"}
# Sources that get the dedicated "Чи знали ви?" trivia prompt (translate + frame a
# wiki fact) instead of the social short-form prompt.
WIKI_FACT_SOURCE_TYPES = {"wiki_facts"}
OFFICIAL_BASE_HASHTAGS = ["#MarvelRivalsUA", "#Офіційно"]
OFFICIAL_SOURCE_ATTRIBUTION = "Повні деталі — на офіційному сайті."
OFFICIAL_TOPIC_TAG_RULES = [
    (("patch notes", "version update", "game update", "patch"), "#Патч"),
    (("fix", "bug", "issue", "known issue", "optimization", "optimisation"), "#Фікси"),
    (("balance", "hero changes", "ability changes"), "#Баланс"),
    (("event", "challenge", "reward", "token", "login bonus", "chrono"), "#Івент"),
    (("shop", "store", "units", "price", "limited-time", "limited time"), "#Магазин"),
    (("skin", "costume", "bundle", "cosmetic", "cosmetics"), "#Скіни"),
    (("trailer", "teaser", "video"), "#Трейлер"),
    (("hero", "character", "abilities", "ability"), "#Герої"),
    (("map",), "#Карта"),
    (("mode", "gameplay"), "#Геймплей"),
    (("season", "battle pass"), "#Сезон"),
    (("maintenance", "server", "downtime"), "#ТехнічніРоботи"),
    (("ranked", "competitive", "rank"), "#Рейтинг"),
    (("vote", "voting", "poll", "winner"), "#Голосування"),
    (("esports", "tournament"), "#Кіберспорт"),
    (("announcement", "notice"), "#Анонс"),
]
OFFICIAL_POST_TYPE_RULES = [
    (
        "patch",
        "Patch notes / game update",
        ("patch notes", "version update", "game update", "patch"),
    ),
    (
        "vote",
        "Vote / poll / community choice",
        ("vote", "voting", "poll", "winner", "community choice", "player choice"),
    ),
    (
        "trailer",
        "Trailer / teaser / map reveal",
        ("trailer", "teaser", "video", "map reveal", "character reveal", "reveal"),
    ),
    (
        "event",
        "Event / rewards / login bonus",
        ("event", "challenge", "reward", "token", "login bonus", "chrono"),
    ),
    (
        "shop",
        "Shop / skins / bundles",
        ("shop", "store", "skin", "costume", "bundle", "cosmetic", "cosmetics", "units", "price"),
    ),
]
OFFICIAL_NORMAL_MAX_LENGTH = 1200
OFFICIAL_PATCH_MAX_LENGTH = 1600
OFFICIAL_SOURCE_LINK_PATTERN = re.compile(
    r"<a\s+[^>]*href=[\"'][^\"']+[\"'][^>]*>\s*офіційному сайті\s*</a>",
    re.IGNORECASE,
)


class GeminiDraftError(RuntimeError):
    """Raised when Gemini cannot generate a moderation draft."""


@dataclass(frozen=True)
class GeminiDraftInput:
    title: str
    article_url: str
    article_date_display: str | None
    datetime_notes: str | None
    body_text: str
    source_type: str
    source_name: str


@dataclass(frozen=True)
class GeminiDraftPackage:
    draft_parts: list[str]
    tags: list[str]


@dataclass(frozen=True)
class DuplicateVerdict:
    is_duplicate: bool
    matched_title: str | None = None


class GeminiDraftGenerator:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    async def generate_drafts(self, draft_input: GeminiDraftInput, *, max_part_length: int) -> list[str]:
        package = await self.generate_draft_package(draft_input, max_part_length=max_part_length)
        return package.draft_parts

    async def generate_draft_package(
        self,
        draft_input: GeminiDraftInput,
        *,
        max_part_length: int,
    ) -> GeminiDraftPackage:
        prompt = _build_prompt(draft_input)
        try:
            draft = await asyncio.to_thread(self._generate_sync, prompt)
        except GeminiDraftError:
            raise
        except Exception as exc:
            if _is_rate_limit_error(exc):
                raise GeminiDraftError(
                    "Gemini вичерпав ліміт запитів (rate limit). Спробуйте ще раз за хвилину."
                ) from exc
            raise GeminiDraftError("Gemini draft generation failed") from exc

        draft = _clean_response_text(draft)
        if not draft:
            raise GeminiDraftError("Gemini returned an empty draft")

        draft_text, tags = _extract_tags(draft)
        if not draft_text:
            raise GeminiDraftError("Gemini returned tags without draft text")

        if _is_official_source(draft_input):
            tags = _official_database_tags(draft_input)
        else:
            tags = tags or _fallback_tags(draft_input)

        if _is_official_source(draft_input):
            draft_parts = [_prepare_official_draft(draft_text, draft_input, max_part_length=max_part_length)]
        else:
            draft_parts = [
                _ensure_required_metadata(part, draft_input)
                for part in _split_draft_parts(draft_text, max_part_length=max_part_length)
            ]
        return GeminiDraftPackage(draft_parts=draft_parts, tags=tags)

    async def generate_draft(self, draft_input: GeminiDraftInput) -> str:
        drafts = await self.generate_drafts(draft_input, max_part_length=3500)
        return drafts[0]

    async def find_duplicate_title(self, new_title: str, existing_titles: list[str]) -> DuplicateVerdict:
        """Ask Gemini whether ``new_title`` is the same specific story as any of
        ``existing_titles``. Returns a not-duplicate verdict when there is nothing
        meaningful to compare; raises GeminiDraftError if the model call fails."""
        candidate = (new_title or "").strip()
        known = [title.strip() for title in existing_titles if title and title.strip()]
        if not candidate or not known:
            return DuplicateVerdict(is_duplicate=False)

        prompt = _build_dedup_prompt(candidate, known)
        raw = await asyncio.to_thread(self._generate_sync, prompt)
        return _parse_duplicate_verdict(raw, known)

    def _generate_sync(self, prompt: str) -> str:
        try:
            from google import genai
            from google.genai import errors as genai_errors
        except ImportError as exc:
            raise GeminiDraftError("google-genai is not installed") from exc

        client = genai.Client(api_key=self.api_key)
        for attempt in range(_GEMINI_MAX_ATTEMPTS):
            try:
                response = client.models.generate_content(model=self.model, contents=prompt)
                return getattr(response, "text", "") or ""
            except genai_errors.APIError as exc:
                if attempt >= _GEMINI_MAX_ATTEMPTS - 1 or not _is_retryable_gemini_error(exc):
                    raise
                delay = _gemini_retry_delay_seconds(exc, attempt)
                logger.warning(
                    "Gemini call throttled/unavailable (%s); retrying in %.0fs (attempt %s/%s)",
                    getattr(exc, "code", "?"),
                    delay,
                    attempt + 1,
                    _GEMINI_MAX_ATTEMPTS,
                )
                time.sleep(delay)
        # Unreachable: the final attempt either returns or re-raises above.
        raise GeminiDraftError("Gemini call exhausted all retries")


def _is_rate_limit_error(exc: Exception) -> bool:
    code = getattr(exc, "code", None)
    return code == 429 or "RESOURCE_EXHAUSTED" in str(exc)


def _is_retryable_gemini_error(exc: Exception) -> bool:
    """Retry per-minute rate limits (429) and transient server errors (500/503),
    but NOT a per-DAY free-tier cap — that won't clear on a short retry, so failing
    fast gives a clear message instead of stalling for nothing."""
    text = str(exc)
    if "PerDay" in text or "RequestsPerDay" in text:
        return False
    code = getattr(exc, "code", None)
    if code in {429, 500, 503}:
        return True
    return any(marker in text for marker in ("RESOURCE_EXHAUSTED", "UNAVAILABLE", "INTERNAL"))


def _gemini_retry_delay_seconds(exc: Exception, attempt: int) -> float:
    """Honour the API's suggested retryDelay when present (capped), else use a
    bounded exponential backoff."""
    match = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)", str(exc))
    if match:
        return min(float(match.group(1)) + 1.0, _GEMINI_MAX_RETRY_DELAY_SECONDS)
    return min(8.0 * (attempt + 1), _GEMINI_MAX_RETRY_DELAY_SECONDS)


def _build_prompt(draft_input: GeminiDraftInput) -> str:
    source_line = _source_attribution_line(draft_input)
    return _select_prompt_template(draft_input).format(
        style_prompt=_load_style_prompt(),
        source_type=draft_input.source_type,
        source_name=draft_input.source_name,
        title=draft_input.title,
        article_url=draft_input.article_url,
        article_type_label=_official_post_type_label(draft_input),
        date_line=draft_input.article_date_display or t("gemini.fallback_date"),
        source_line=source_line,
        hashtag_line=_public_hashtag_line(draft_input),
        datetime_notes=draft_input.datetime_notes or "UTC/GMT-часів для конвертації не знайдено.",
        body_text=draft_input.body_text or t("gemini.fallback_body"),
        rumor_notice=_rumor_notice(draft_input),
    )


def _is_rumor_source(draft_input: GeminiDraftInput) -> bool:
    return draft_input.source_type in RUMOR_SOURCE_TYPES


def _rumor_notice(draft_input: GeminiDraftInput) -> str:
    """A strong, source-scoped instruction for leak/datamine sources so the draft
    is framed as a rumour. Empty for every other source (the placeholder then
    renders as nothing)."""
    if not _is_rumor_source(draft_input):
        return ""

    return (
        "УВАГА: це НЕОФІЦІЙНИЙ злив/витік (датамайн або чутка), НЕ підтверджений розробниками. "
        "Обов'язково познач це в тексті як чутку/неофіційну інформацію (напр. «за чутками», "
        "«за даними датамайну», «неофіційно») і коротко нагадай, що деталі ще можуть змінитися. "
        "Не подавай це як офіційну заяву чи підтверджений факт."
    )


def _select_prompt_template(draft_input: GeminiDraftInput) -> str:
    """Wiki trivia uses the "Чи знали ви?" prompt; official articles use the
    long-form article prompt; every other (social / short-form) source uses the
    concise short-form prompt. All accept the same format placeholders, so the
    kwargs in _build_prompt stay shared."""
    if draft_input.source_type in WIKI_FACT_SOURCE_TYPES:
        return _load_wiki_fact_prompt_template()
    if _is_official_source(draft_input):
        return _load_prompt_template()

    return _load_short_form_prompt_template()


def _clean_response_text(value: str) -> str:
    return value.strip().strip("`").strip()


def _build_dedup_prompt(new_title: str, existing_titles: list[str]) -> str:
    numbered = "\n".join(f"{index}. {title}" for index, title in enumerate(existing_titles, start=1))
    return _load_dedup_prompt_template().format(new_title=new_title, existing_titles=numbered)


@lru_cache(maxsize=1)
def _load_dedup_prompt_template() -> str:
    return DEDUP_PROMPT_PATH.read_text(encoding="utf-8")


def _coerce_duplicate_flag(value: object) -> bool:
    """Interpret the model's ``duplicate`` field, biased toward not-a-duplicate.

    A genuine JSON ``true``/``1`` counts as a duplicate; everything else —
    including the common stringified ``"false"``/``"no"``/``"0"`` deviations —
    is treated as NOT a duplicate, so a formatting quirk never drops real news.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return False


def _parse_duplicate_verdict(raw: str, known_titles: list[str]) -> DuplicateVerdict:
    """Parse the model's JSON verdict, failing open (not a duplicate) on anything
    unexpected — a missed dedup only costs a rare double post, but a false positive
    would silently drop real news."""
    text = _clean_response_text(raw)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return DuplicateVerdict(is_duplicate=False)

    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return DuplicateVerdict(is_duplicate=False)

    if not isinstance(data, dict) or not _coerce_duplicate_flag(data.get("duplicate")):
        return DuplicateVerdict(is_duplicate=False)

    matched = data.get("match")
    matched_title: str | None = None
    if isinstance(matched, str) and matched.strip():
        lowered = matched.strip().lower()
        matched_title = next((title for title in known_titles if title.lower() == lowered), matched.strip())

    return DuplicateVerdict(is_duplicate=True, matched_title=matched_title)


def _ensure_required_metadata(draft: str, draft_input: GeminiDraftInput) -> str:
    # Short-form (non-official) posts deliberately carry NO publication-date line:
    # the post itself is the announcement, and a bare "2026-06-12" mid-post reads
    # as noise. The article date stays available to admins via the original_text.
    result = _clean_model_draft(draft, draft_input).strip()
    result = _apply_datetime_notes(result, draft_input.datetime_notes)

    source_line = _source_attribution_line(draft_input)
    if source_line and not _has_source_attribution(result):
        result = f"{result}\n\n{source_line}".strip()

    hashtags = _public_hashtag_line(draft_input)
    if hashtags not in result:
        result = f"{result}\n\n{hashtags}"

    return _normalize_blank_lines(result)


def _clean_model_draft(draft: str, draft_input: GeminiDraftInput) -> str:
    lines = []
    for line in draft.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append(line)
            continue
        if stripped == t("gemini.fallback_date"):
            continue
        if draft_input.article_url in stripped:
            cleaned_line = _strip_raw_urls(line).strip()
            if not cleaned_line or _looks_like_source_only_line(cleaned_line):
                continue
            lines.append(_strip_unsupported_markdown(cleaned_line))
            continue
        if re.search(r"https?://|www\.", stripped, flags=re.IGNORECASE):
            cleaned_line = _strip_raw_urls(line).strip()
            if not cleaned_line or _looks_like_source_only_line(cleaned_line):
                continue
            lines.append(_strip_unsupported_markdown(cleaned_line))
            continue
        if _is_hashtag_only_line(stripped):
            continue
        lines.append(_strip_unsupported_markdown(line))

    return _strip_raw_urls("\n".join(lines))


def _extract_tags(draft: str) -> tuple[str, list[str]]:
    if TAGS_SEPARATOR not in draft:
        return draft, []

    draft_text, _separator, raw_tags = draft.partition(TAGS_SEPARATOR)
    return draft_text.strip(), _normalize_tags(raw_tags)


def _normalize_tags(raw_tags: str) -> list[str]:
    normalized_tags: list[str] = []
    seen: set[str] = set()
    for value in re.split(r"[,;\n]", raw_tags):
        normalized = value.strip().lower().lstrip("#")
        normalized = re.sub(r"[_\-]+", " ", normalized)
        normalized = re.sub(r"\s+", " ", normalized)
        normalized = normalized.strip(".,;:!?'\"()[]{}")
        if not normalized or normalized in seen or len(normalized) > 40:
            continue

        seen.add(normalized)
        normalized_tags.append(normalized)

        if len(normalized_tags) >= 12:
            break

    return normalized_tags


def _fallback_tags(draft_input: GeminiDraftInput) -> list[str]:
    fallback_tag = t("gemini.fallback_tag")
    if _is_wiki_fact_source(draft_input):
        # Keep the stored tags in step with the rubric's public hashtags.
        return [fallback_tag, t("collectors.wiki_facts.tag")]

    tags = [fallback_tag]
    if _is_rumor_source(draft_input):
        # Keep the stored tags in step with the permanent public "#Чутки" marker.
        tags.append(t("gemini.rumor_tag"))
    lowered = f"{draft_input.title}\n{draft_input.body_text}".lower()
    keyword_tags = {
        "patch": "патч",
        "balance": "баланс",
        "skin": "скін",
        "costume": "костюм",
        "event": "івент",
        "season": "сезон",
        "map": "карта",
        "mode": "режим",
        "hero": "герой",
        "bug": "виправлення",
        "fix": "виправлення",
    }
    for keyword, tag in keyword_tags.items():
        if keyword in lowered and tag not in tags:
            tags.append(tag)

    return tags


def _is_official_source(draft_input: GeminiDraftInput) -> bool:
    return draft_input.source_type in OFFICIAL_SOURCE_TYPES


def _is_wiki_fact_source(draft_input: GeminiDraftInput) -> bool:
    return draft_input.source_type in WIKI_FACT_SOURCE_TYPES


def _prepare_official_draft(
    draft_text: str,
    draft_input: GeminiDraftInput,
    *,
    max_part_length: int,
) -> str:
    collapsed_draft = _collapse_post_parts(draft_text)
    result = _sanitize_official_public_draft(collapsed_draft, draft_input)
    max_length = min(_official_max_length(draft_input), max_part_length)
    return _enforce_official_length(result, draft_input, max_length=max_length)


def _sanitize_official_public_draft(draft: str, draft_input: GeminiDraftInput) -> str:
    result = _clean_model_draft(draft, draft_input)
    result = _remove_official_public_metadata(result, draft_input)
    result = _apply_datetime_notes(result, draft_input.datetime_notes)
    result = _strip_unsupported_markdown(result)
    result = _strip_raw_urls(result)
    result = _remove_official_public_metadata(result, draft_input)
    result = _ensure_official_source_attribution(result, draft_input)
    result = _ensure_hashtags_at_bottom(result, draft_input)
    return _normalize_blank_lines(result)


def _public_hashtag_line(draft_input: GeminiDraftInput) -> str:
    if _is_official_source(draft_input):
        return " ".join(_official_hashtags(draft_input))

    # The trivia rubric gets its own fixed tag. The news topic rules are keyed on
    # patch/event/shop wording that comic-history trivia never contains, so it fell
    # through to the "#Анонс" default — labelling a 1941 comics fact an announcement.
    if _is_wiki_fact_source(draft_input):
        return t("collectors.wiki_facts.hashtags")

    # A leak is never an announcement, so rumour sources carry a permanent
    # "#Чутки" marker instead of the "#Анонс" default. It is added on EVERY such
    # post, not only when no topic matched: a marker that appears at random is
    # one readers cannot rely on, and as a constant it doubles as a channel-wide
    # filter separating rumours from official news.
    if _is_rumor_source(draft_input):
        return " ".join(
            [t("gemini.hashtags"), t("gemini.rumor_hashtag"), *_topic_hashtags(draft_input, fallback=False)]
        )

    # Non-official (social/short-form) posts get the base tag PLUS the same topic
    # tags, just without the #Офіційно marker, so they are tagged too — not bare.
    return " ".join([t("gemini.hashtags"), *_topic_hashtags(draft_input)])


def _official_database_tags(draft_input: GeminiDraftInput) -> list[str]:
    return [tag.lstrip("#") for tag in _official_hashtags(draft_input)]


def _topic_hashtags(draft_input: GeminiDraftInput, *, fallback: bool = True) -> list[str]:
    """Up to three topic tags matched from the title/body.

    ``fallback`` adds "#Анонс" when nothing matched. Callers that already supply
    their own marker (rumour sources) turn it off, so a leak is never labelled an
    announcement just because no keyword hit.
    """
    text = f"{draft_input.title}\n{draft_input.body_text}".lower()
    topic_tags: list[str] = []
    for keywords, hashtag in OFFICIAL_TOPIC_TAG_RULES:
        if hashtag in topic_tags:
            continue
        if any(_matches_keyword(text, keyword) for keyword in keywords):
            topic_tags.append(hashtag)
        if len(topic_tags) >= 3:
            break

    if not topic_tags and fallback:
        topic_tags.append("#Анонс")

    return topic_tags[:3]


def _official_hashtags(draft_input: GeminiDraftInput) -> list[str]:
    return [*OFFICIAL_BASE_HASHTAGS, *_topic_hashtags(draft_input)]


def _official_post_type(draft_input: GeminiDraftInput) -> str:
    text = f"{draft_input.title}\n{draft_input.body_text}".lower()
    for post_type, _label, keywords in OFFICIAL_POST_TYPE_RULES:
        if any(_matches_keyword(text, keyword) for keyword in keywords):
            return post_type

    return "announcement"


def _official_post_type_label(draft_input: GeminiDraftInput) -> str:
    post_type = _official_post_type(draft_input)
    for rule_type, label, _keywords in OFFICIAL_POST_TYPE_RULES:
        if rule_type == post_type:
            return label

    return "Short announcement"


def _official_max_length(draft_input: GeminiDraftInput) -> int:
    if _official_post_type(draft_input) == "patch":
        return OFFICIAL_PATCH_MAX_LENGTH

    return OFFICIAL_NORMAL_MAX_LENGTH


def _source_attribution_line(draft_input: GeminiDraftInput) -> str:
    if _is_official_source(draft_input):
        return OFFICIAL_SOURCE_ATTRIBUTION

    return t("gemini.source_line", source_name=draft_input.source_name)


def _has_source_attribution(text: str) -> bool:
    lowered = text.lower()
    return "офіційному сайті" in lowered or "джерело:" in lowered


def _remove_official_public_metadata(text: str, draft_input: GeminiDraftInput) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue

        if _is_official_metadata_line(stripped, draft_input):
            continue

        lines.append(stripped)

    return _normalize_blank_lines("\n".join(lines))


def _is_official_metadata_line(line: str, draft_input: GeminiDraftInput) -> bool:
    lowered = line.lower().strip()
    normalized = lowered.strip(":-—– ")
    if not normalized:
        return False

    if draft_input.article_date_display and normalized == draft_input.article_date_display.lower():
        return True

    if draft_input.article_url and draft_input.article_url.lower() in lowered:
        return True

    metadata_prefixes = (
        "дата публікації",
        "дата статті",
        "оригінальна дата",
        "дата статті після",
        "джерело",
        "url",
        "source",
        "source url",
        "article date",
        "publication date",
        "посилання",
        "тип джерела",
        "назва джерела",
    )
    if any(normalized.startswith(prefix) for prefix in metadata_prefixes):
        return True

    source_name = draft_input.source_name.lower().strip()
    if source_name and normalized in {source_name, "офіційний сайт marvel rivals"}:
        return True

    return False


def _ensure_official_source_attribution(text: str, draft_input: GeminiDraftInput) -> str:
    source_line = _source_attribution_line(draft_input)
    body = _remove_source_attribution_lines(text)
    if not body:
        return source_line

    return f"{body}\n\n{source_line}"


def _remove_source_attribution_lines(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
            continue

        lowered = stripped.lower()
        if "офіційному сайті" in lowered and ("повні деталі" in lowered or "<a" in lowered):
            continue

        if _looks_like_source_only_line(stripped):
            continue

        lines.append(stripped)

    return _normalize_blank_lines("\n".join(lines))


def _ensure_hashtags_at_bottom(text: str, draft_input: GeminiDraftInput) -> str:
    hashtags = _public_hashtag_line(draft_input)
    body_lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            body_lines.append("")
            continue

        if _is_hashtag_only_line(stripped) or stripped.lower().startswith("теги:"):
            continue

        body_lines.append(stripped)

    body = _normalize_blank_lines("\n".join(body_lines))
    if not body:
        return hashtags

    return f"{body}\n\n{hashtags}"


def _collapse_post_parts(draft: str) -> str:
    parts = [part.strip() for part in draft.split(POST_SEPARATOR) if part.strip()]
    if not parts:
        return draft.strip()

    return "\n\n".join(parts[:2]).strip()


def _enforce_official_length(text: str, draft_input: GeminiDraftInput, *, max_length: int) -> str:
    text = _normalize_blank_lines(text)
    if len(text) <= max_length:
        return text

    hashtags = _public_hashtag_line(draft_input)
    source_line = _source_attribution_line(draft_input)
    body = _remove_exact_line(text, hashtags)
    body = _remove_exact_line(body, source_line).strip()

    suffix_parts = [source_line, hashtags]
    suffix = "\n\n".join(part for part in suffix_parts if part)
    available = max(260, max_length - len(suffix) - 4)
    shortened = _truncate_plain_text(body, available).strip()
    if source_line and source_line not in shortened:
        shortened = f"{shortened}\n\n{source_line}".strip()

    result = f"{shortened}\n\n{hashtags}".strip()
    if len(result) <= max_length:
        return _normalize_blank_lines(result)

    available = max(180, max_length - len(hashtags) - 4)
    shortened = _truncate_plain_text(shortened, available)
    return _normalize_blank_lines(f"{shortened}\n\n{hashtags}")


def _matches_keyword(text: str, keyword: str) -> bool:
    if " " in keyword:
        return keyword in text

    return re.search(rf"\b{re.escape(keyword)}\b", text) is not None


def _strip_unsupported_markdown(text: str) -> str:
    text = _replace_official_source_anchor(text)
    text = re.sub(r"</?(?:b|strong|i|em|u|s|span|h[1-6])[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>\n]+>", "", text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"__(.*?)__", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"\1", text)
    text = text.replace("`", "")
    text = re.sub(r"(?m)^\s*[*-]\s+", "", text)
    text = re.sub(r"(?m)^\s*\*\s*$", "", text)
    text = re.sub(r"\*{2,}", "", text)
    return text


def _strip_raw_urls(text: str) -> str:
    text = _replace_official_source_anchor(text)
    text = re.sub(r"\s*https?://\S+", "", text)
    text = re.sub(r"\s*www\.\S+", "", text)
    text = re.sub(
        r"\s*(?:деталі|повні деталі|details|source|link|url|посилання)\s*:\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return text


def _replace_official_source_anchor(text: str) -> str:
    return OFFICIAL_SOURCE_LINK_PATTERN.sub("офіційному сайті", text)


def _looks_like_source_only_line(text: str) -> bool:
    normalized = text.strip().strip(":-—– ").lower()
    return normalized in {"джерело", "source", "url", "link", "посилання", "деталі", "повні деталі"}


def _remove_exact_line(text: str, line_to_remove: str) -> str:
    if not line_to_remove:
        return text

    lines = [line for line in text.splitlines() if line.strip() != line_to_remove]
    return "\n".join(lines)


def _truncate_plain_text(text: str, max_length: int) -> str:
    if len(text) <= max_length:
        return text

    ending = "…\n\nПовні деталі — на офіційному сайті."
    available = max(80, max_length - len(ending))
    truncated = text[:available].rstrip()
    split_at = max(truncated.rfind("\n\n"), truncated.rfind(". "), truncated.rfind("! "), truncated.rfind("? "))
    if split_at > available // 2:
        truncated = truncated[: split_at + 1].rstrip()
    else:
        split_at = truncated.rfind(" ")
        if split_at > available // 2:
            truncated = truncated[:split_at].rstrip()

    return f"{truncated}{ending}"


def _is_hashtag_only_line(value: str) -> bool:
    return bool(value.startswith("#") and re.fullmatch(r"(?:#[^\s#]+)(?:\s+#[^\s#]+)*", value))


def _normalize_blank_lines(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _apply_datetime_notes(text: str, datetime_notes: str | None) -> str:
    if not text or not datetime_notes:
        return text

    result = text
    for line in datetime_notes.splitlines():
        match = re.match(
            r"^\s*-\s*"
            r"(?P<original>\d{1,2}:\d{2}\s*(?:UTC|GMT)(?:\s*[+-]\s*\d{1,2}(?::?[0-5]\d)?)?)"
            r"\s*\([^)]+\)\s*->\s*(?P<converted>.+?)\s*$",
            line,
            flags=re.IGNORECASE,
        )
        if match is None:
            continue

        original = match.group("original")
        converted = match.group("converted").strip()
        result = re.sub(re.escape(original), converted, result, flags=re.IGNORECASE)

    return result


def _split_draft_parts(draft: str, *, max_part_length: int) -> list[str]:
    explicit_parts = [part.strip() for part in draft.split(POST_SEPARATOR) if part.strip()]
    if not explicit_parts:
        explicit_parts = [draft.strip()]

    normalized_parts: list[str] = []
    for part in explicit_parts:
        normalized_parts.extend(_split_oversized_part(part, max_part_length=max_part_length))

    return normalized_parts or [draft.strip()]


def _split_oversized_part(part: str, *, max_part_length: int) -> list[str]:
    if len(part) <= max_part_length:
        return [part]

    paragraphs = [paragraph.strip() for paragraph in part.split("\n\n") if paragraph.strip()]
    if not paragraphs:
        paragraphs = [part.strip()]

    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= max_part_length:
            current = candidate
            continue

        if current:
            chunks.append(current)
        current = paragraph

        while len(current) > max_part_length:
            split_at = current.rfind("\n", 0, max_part_length)
            if split_at < max_part_length // 2:
                split_at = current.rfind(" ", 0, max_part_length)
            if split_at < max_part_length // 2:
                split_at = max_part_length

            chunks.append(current[:split_at].strip())
            current = current[split_at:].strip()

    if current:
        chunks.append(current)

    if len(chunks) <= 1:
        return chunks

    return [_prefix_part(chunk, index + 1) for index, chunk in enumerate(chunks)]


def _prefix_part(part: str, number: int) -> str:
    first_line, separator, rest = part.partition("\n")
    part_title = t("gemini.part_title", number=number)
    if first_line.startswith("📰"):
        return f"{first_line} · {part_title}{separator}{rest}".strip()

    return f"📰 {part_title}\n\n{part}".strip()


@lru_cache(maxsize=1)
def _load_prompt_template() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8").strip()


@lru_cache(maxsize=1)
def _load_short_form_prompt_template() -> str:
    return SHORT_FORM_PROMPT_PATH.read_text(encoding="utf-8").strip()


@lru_cache(maxsize=1)
def _load_wiki_fact_prompt_template() -> str:
    return WIKI_FACT_PROMPT_PATH.read_text(encoding="utf-8").strip()


@lru_cache(maxsize=1)
def _load_style_prompt() -> str:
    try:
        return STYLE_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        logger.warning("Official news style prompt %s is unavailable", STYLE_PROMPT_PATH)
        return "Створи короткий Telegram-пост українською: 400-900 символів, без URL, з тегами в кінці."
