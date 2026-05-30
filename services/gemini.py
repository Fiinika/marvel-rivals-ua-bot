from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from services.i18n import t
from services.post_footer import append_community_footer


logger = logging.getLogger(__name__)
PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "gemini_news_uk.md"
POST_SEPARATOR = "---POST---"


class GeminiDraftError(RuntimeError):
    """Raised when Gemini cannot generate a moderation draft."""


@dataclass(frozen=True)
class GeminiDraftInput:
    title: str
    article_url: str
    article_date_display: str | None
    body_text: str
    source_type: str
    source_name: str


class GeminiDraftGenerator:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    async def generate_drafts(self, draft_input: GeminiDraftInput, *, max_part_length: int) -> list[str]:
        prompt = _build_prompt(draft_input)
        try:
            draft = await asyncio.to_thread(self._generate_sync, prompt)
        except Exception as exc:
            raise GeminiDraftError("Gemini draft generation failed") from exc

        draft = _clean_response_text(draft)
        if not draft:
            raise GeminiDraftError("Gemini returned an empty draft")

        return [
            append_community_footer(_ensure_required_metadata(part, draft_input))
            for part in _split_draft_parts(draft, max_part_length=max_part_length)
        ]

    async def generate_draft(self, draft_input: GeminiDraftInput) -> str:
        drafts = await self.generate_drafts(draft_input, max_part_length=3500)
        return drafts[0]

    def _generate_sync(self, prompt: str) -> str:
        try:
            from google import genai
        except ImportError as exc:
            raise GeminiDraftError("google-genai is not installed") from exc

        client = genai.Client(api_key=self.api_key)
        response = client.models.generate_content(
            model=self.model,
            contents=prompt,
        )
        return getattr(response, "text", "") or ""


def _build_prompt(draft_input: GeminiDraftInput) -> str:
    return _load_prompt_template().format(
        source_type=draft_input.source_type,
        source_name=draft_input.source_name,
        title=draft_input.title,
        article_url=draft_input.article_url,
        date_line=draft_input.article_date_display or t("gemini.fallback_date"),
        body_text=draft_input.body_text or t("gemini.fallback_body"),
    )


def _clean_response_text(value: str) -> str:
    return value.strip().strip("`").strip()


def _ensure_required_metadata(draft: str, draft_input: GeminiDraftInput) -> str:
    result = _remove_admin_only_lines(draft, draft_input).strip()
    hashtags = t("gemini.hashtags")
    if hashtags not in result:
        result = f"{result}\n\n{hashtags}"

    return result


def _remove_admin_only_lines(draft: str, draft_input: GeminiDraftInput) -> str:
    date_marker = t("gemini.date_marker")
    source_marker = t("gemini.source_marker")
    lines = []
    for line in draft.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append(line)
            continue
        if date_marker in stripped:
            continue
        if draft_input.article_url in stripped:
            continue
        if source_marker in stripped and "http" in stripped:
            continue
        lines.append(line)

    return "\n".join(lines)


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
