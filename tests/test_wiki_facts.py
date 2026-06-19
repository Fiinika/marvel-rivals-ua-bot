"""Tests for the weekly wiki-facts rubric: Trivia wikitext cleaning, the collector
mapping (a non-dedup'd "Чи знали ви?" candidate), and the dedicated Gemini prompt."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from services.collectors.base import ListingEntry
from services.collectors.runner import BaseNewsCollector
from services.collectors.wiki_facts.client import WikiFact, _extract_facts
from services.collectors.wiki_facts.collector import WikiFactsCollector, _fact_id
from services.gemini import GeminiDraftInput, _build_prompt, _select_prompt_template, _load_wiki_fact_prompt_template


def test_extract_facts_cleans_wikitext() -> None:
    wikitext = (
        "* [[Adam Warlock]]'s first appearance was in '''Fantastic Four''' (1961) #66.<ref name=x>cite</ref>\n"
        "* His staff is called the [[Karmic Staff|Karmic Staff]].\n"
        "* {{Template|junk}}Short\n"  # too short after template strip -> dropped
        "* See [https://example.com/x the source] for more about Limbo time travel mechanics here.\n"
    )
    facts = _extract_facts(wikitext)

    assert facts[0] == "Adam Warlock's first appearance was in Fantastic Four (1961) #66."
    assert facts[1] == "His staff is called the Karmic Staff."
    # external-link markup reduced to its label; the too-short bullet was dropped.
    assert any("the source for more about Limbo" in f for f in facts)
    assert all("[[" not in f and "<ref" not in f and "{{" not in f for f in facts)
    assert "Short" not in facts


def test_fact_id_is_stable_and_whitespace_insensitive() -> None:
    a = WikiFact("Magik", "Illyana   first appeared in 1975.", "https://w/Magik")
    b = WikiFact("Magik", "illyana first appeared in 1975.", "https://w/Magik")
    assert _fact_id(a) == _fact_id(b)
    assert _fact_id(a).startswith("Magik:")


class _StubClient:
    def __init__(self, facts: dict[str, list[WikiFact]]) -> None:
        self._facts = facts

    async def fetch_hero_titles(self) -> list[str]:
        return list(self._facts)

    async def fetch_trivia_facts(self, hero: str) -> list[WikiFact]:
        return self._facts.get(hero, [])


def _collector(facts: dict[str, list[WikiFact]]) -> WikiFactsCollector:
    collector = WikiFactsCollector(
        config=SimpleNamespace(wiki_facts_api_url="https://w/api.php"), db=None, bot=None
    )
    collector.client = _StubClient(facts)
    return collector


def test_collector_opts_out_of_cross_source_dedup() -> None:
    assert WikiFactsCollector.participates_in_cross_source_dedup is False
    assert BaseNewsCollector.participates_in_cross_source_dedup is True


def test_fetch_listing_flattens_facts_to_entries() -> None:
    f1 = WikiFact("Magik", "Magik can teleport through Limbo stepping discs.", "https://w/Magik")
    f2 = WikiFact("Blade", "Blade is a Dhampir, half human and half vampire.", "https://w/Blade")
    entries = asyncio.run(_collector({"Magik": [f1], "Blade": [f2]}).fetch_listing())

    keys = {e.dedup_key for e in entries}
    assert keys == {_fact_id(f1), _fact_id(f2)}


def test_parse_entry_builds_did_you_know_candidate() -> None:
    fact = WikiFact("Magik", "Magik can teleport through Limbo stepping discs.", "https://w/Magik")
    collector = _collector({})
    entry = ListingEntry(dedup_key=_fact_id(fact), payload=fact)

    candidate = asyncio.run(collector.parse_entry(entry))

    assert candidate.source_id == _fact_id(fact)  # == dedup_key, so seen-check is consistent
    assert candidate.source_url == "https://w/Magik"
    assert candidate.body_text == fact.fact
    assert candidate.source_name == "Marvel Rivals Wiki (CC BY-SA)"
    assert "Magik" in candidate.title
    assert candidate.has_media is False


def test_wiki_fact_prompt_is_selected_and_renders() -> None:
    draft_input = GeminiDraftInput(
        title="Цікавий факт про Magik",
        article_url="https://w/Magik",
        article_date_display=None,
        datetime_notes="",
        body_text="Magik can teleport through Limbo stepping discs.",
        source_type="wiki_facts",
        source_name="Marvel Rivals Wiki (CC BY-SA)",
    )
    assert "Чи знали ви" in _load_wiki_fact_prompt_template()

    prompt = _build_prompt(draft_input)
    # The wiki prompt is chosen and renders (all placeholders present) with the fact,
    # the CC BY-SA attribution and the injection guard.
    assert _select_prompt_template(draft_input) == _load_wiki_fact_prompt_template()
    assert "Чи знали ви" in prompt
    assert "Magik can teleport" in prompt
    assert "CC BY-SA" in prompt
    assert "ВАЖЛИВО ПРО БЕЗПЕКУ" in prompt
