"""Tests for the weekly wiki-facts rubric: Trivia wikitext cleaning, the collector
mapping (a non-dedup'd "Чи знали ви?" candidate), and the dedicated Gemini prompt."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import services.collectors.wiki_facts.collector as wiki_collector
from services.collectors.base import ListingEntry
from services.collectors.runner import BaseNewsCollector
from services.collectors.wiki_facts.client import WikiFact, _clean_fact, _extract_facts
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


def test_clean_fact_unwraps_nested_templates() -> None:
    raw = "{{Quote|text with {{inner}} bits}} this is a real factual sentence about the hero."
    assert _clean_fact(raw) == "this is a real factual sentence about the hero."


def test_extract_facts_drops_residual_template_markup() -> None:
    # A template nested deeper than the unwrap cap leaves braces -> the bullet is
    # dropped rather than published garbled.
    deep = "* " + "{{" * 8 + "x" + "}}" * 8 + " trailing text that is long enough to pass the filter here"
    assert _extract_facts(deep) == []


def test_clean_fact_strips_media_links_and_keeps_display_text() -> None:
    raw = "See [[File:foo.png|thumb|200px|Caption]] and [[Hero Page|Magik]] do cool things together here."
    cleaned = _clean_fact(raw)
    assert "thumb" not in cleaned and "200px" not in cleaned and "File:" not in cleaned
    assert "Magik" in cleaned  # display text (last pipe segment) kept


def test_clean_fact_strips_bare_and_raw_urls() -> None:
    assert "http" not in _clean_fact("A fact with a bare link [https://evil.example.com] inside it here.")
    assert "http" not in _clean_fact("A fact with a raw https://evil.example.com url inside it right here.")


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


class _FakeDB:
    def __init__(self, seen: set[str] | None = None) -> None:
        self.seen = set(seen or set())

    async def is_source_seen(self, source_type: str, source_id: str) -> bool:
        return source_id in self.seen


def _collector(facts: dict[str, list[WikiFact]], *, db: _FakeDB | None = None) -> WikiFactsCollector:
    collector = WikiFactsCollector(
        config=SimpleNamespace(wiki_facts_api_url="https://w/api.php"), db=db or _FakeDB(), bot=None
    )
    collector.client = _StubClient(facts)
    return collector


def test_collector_opts_out_of_cross_source_dedup() -> None:
    assert WikiFactsCollector.participates_in_cross_source_dedup is False
    assert BaseNewsCollector.participates_in_cross_source_dedup is True


def test_fetch_listing_stops_at_first_hero_with_an_unseen_fact(monkeypatch) -> None:
    # Deterministic order; all facts unseen -> stop after the first hero (cheap).
    monkeypatch.setattr(wiki_collector.random, "shuffle", lambda seq: None)
    f1 = WikiFact("Magik", "Magik can teleport through Limbo stepping discs.", "https://w/Magik")
    f2 = WikiFact("Blade", "Blade is a Dhampir, half human and half vampire.", "https://w/Blade")
    entries = asyncio.run(_collector({"Magik": [f1], "Blade": [f2]}).fetch_listing())

    assert [e.dedup_key for e in entries] == [_fact_id(f1)]


def test_fetch_listing_keeps_scanning_when_first_heroes_are_all_seen(monkeypatch) -> None:
    # Anti-starvation: the first hero's only fact is already seen, so the scan must
    # continue to the next hero rather than give up.
    monkeypatch.setattr(wiki_collector.random, "shuffle", lambda seq: None)
    f1 = WikiFact("Magik", "Magik can teleport through Limbo stepping discs.", "https://w/Magik")
    f2 = WikiFact("Blade", "Blade is a Dhampir, half human and half vampire.", "https://w/Blade")
    db = _FakeDB(seen={_fact_id(f1)})
    entries = asyncio.run(_collector({"Magik": [f1], "Blade": [f2]}, db=db).fetch_listing())

    assert [e.dedup_key for e in entries] == [_fact_id(f1), _fact_id(f2)]


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
