"""Fetch hero "Trivia" facts from the Marvel Rivals Fandom wiki via api.php.

The wiki content is CC BY-SA, so every published fact must credit the wiki with a
link to the source page (handled by the collector's attribution line). This client
only reads: it lists the heroes in Category:Heroes and extracts the cleaned bullet
points of each hero's "Trivia" section.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx


logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://marvelrivals.fandom.com/api.php"
DEFAULT_PAGE_BASE = "https://marvelrivals.fandom.com/wiki/"
HEROES_CATEGORY = "Category:Heroes"
USER_AGENT = "MarvelRivalsUACollector/1.0 (Telegram news bot; +https://t.me/MarvelRivalsUABot)"
REQUEST_TIMEOUT_SECONDS = 25.0

# A trivia bullet must be a real sentence, not a stray fragment or a giant essay.
_MIN_FACT_LENGTH = 25
_MAX_FACT_LENGTH = 600

_REF_RE = re.compile(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>", re.IGNORECASE | re.DOTALL)
_TEMPLATE_RE = re.compile(r"\{\{[^{}]*\}\}")
_WIKILINK_RE = re.compile(r"\[\[(?:[^\]|]+\|)?([^\]]+)\]\]")
_EXTLINK_RE = re.compile(r"\[(?:https?://)\S+\s+([^\]]+)\]")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_BULLET_RE = re.compile(r"^\*+\s*(.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class WikiFact:
    hero: str
    fact: str
    page_url: str


class WikiFactsClient:
    def __init__(self, api_url: str = DEFAULT_API_URL, *, page_base: str = DEFAULT_PAGE_BASE) -> None:
        self.api_url = api_url
        self.page_base = page_base

    async def fetch_hero_titles(self) -> list[str]:
        data = await self._get(
            action="query",
            list="categorymembers",
            cmtitle=HEROES_CATEGORY,
            cmlimit="500",
            cmtype="page",
        )
        if data is None:
            return []
        members = (data.get("query") or {}).get("categorymembers") or []
        # Drop the category's own landing page ("Heroes") and any blank title.
        return [
            title
            for member in members
            if isinstance(member, dict) and (title := str(member.get("title") or "").strip()) and title != "Heroes"
        ]

    async def fetch_trivia_facts(self, hero: str) -> list[WikiFact]:
        sections = await self._get(action="parse", page=hero, prop="sections")
        if sections is None:
            return []
        section_list = (sections.get("parse") or {}).get("sections") or []
        index = next(
            (
                str(section.get("index"))
                for section in section_list
                if isinstance(section, dict) and str(section.get("line") or "").strip().casefold() == "trivia"
            ),
            None,
        )
        if not index:
            return []

        parsed = await self._get(action="parse", page=hero, section=index, prop="wikitext")
        if parsed is None:
            return []
        wikitext = ((parsed.get("parse") or {}).get("wikitext") or {}).get("*") or ""

        page_url = f"{self.page_base}{quote(hero.replace(' ', '_'))}"
        return [WikiFact(hero=hero, fact=fact, page_url=page_url) for fact in _extract_facts(wikitext)]

    async def _get(self, **params: str) -> dict[str, Any] | None:
        params.setdefault("format", "json")
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            ) as client:
                response = await client.get(self.api_url, params=params)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Wiki API request failed (%s): %s", params.get("action"), exc)
            return None
        return data if isinstance(data, dict) else None


def _extract_facts(wikitext: str) -> list[str]:
    facts: list[str] = []
    for raw in _BULLET_RE.findall(wikitext or ""):
        fact = _clean_fact(raw)
        if _MIN_FACT_LENGTH <= len(fact) <= _MAX_FACT_LENGTH:
            facts.append(fact)
    return facts


def _clean_fact(raw: str) -> str:
    text = _REF_RE.sub("", raw)
    text = _TEMPLATE_RE.sub("", text)
    text = _EXTLINK_RE.sub(r"\1", text)
    text = _WIKILINK_RE.sub(r"\1", text)
    text = _HTML_TAG_RE.sub("", text)
    text = text.replace("'''", "").replace("''", "")
    return " ".join(text.split()).strip()
