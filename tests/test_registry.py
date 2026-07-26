"""Tests for opt-in collector gating: Bluesky is registered but only surfaces /
runs when ENABLE_BLUESKY_SOURCE is on; the official source is always available."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import services.collectors.registry as registry
from services.collectors.base import CollectionStats
from services.collectors.registry import (
    create_all_collectors,
    create_collector,
    list_collector_definitions,
    run_all_collectors,
)


def _config(
    bluesky: bool,
    youtube: bool = False,
    reddit: bool = False,
    rivalskins: bool = False,
    wiki_facts: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        enable_bluesky_source=bluesky,
        bluesky_actor="marvelrivalsglobal.bsky.social",
        enable_youtube_source=youtube,
        youtube_channel_id="UCWzmOSSiSPbVnVu3ZAyDx2w",
        youtube_exclude_keywords=frozenset({"esports"}),
        enable_reddit_source=reddit,
        reddit_subreddit="MarvelRivalsLeaks",
        reddit_flairs=("Official News", "Reliable", "Confirmed"),
        reddit_exclude_keywords=frozenset({"megathread"}),
        enable_rivalskins_source=rivalskins,
        rivalskins_feed_url="https://rivalskins.com/category/leaks/feed/",
        enable_wiki_facts=wiki_facts,
        wiki_facts_api_url="https://marvelrivals.fandom.com/api.php",
        official_news_url="https://www.marvelrivals.com/news/",
        article_timezone="Europe/Kyiv",
    )


def test_bluesky_hidden_when_disabled() -> None:
    ids = {d.collector_id for d in list_collector_definitions(_config(False))}
    assert "bluesky" not in ids
    assert "official_marvel_rivals" in ids


def test_bluesky_shown_when_enabled() -> None:
    ids = {d.collector_id for d in list_collector_definitions(_config(True))}
    assert "bluesky" in ids


def test_youtube_hidden_when_disabled_and_shown_when_enabled() -> None:
    assert "youtube" not in {d.collector_id for d in list_collector_definitions(_config(False, youtube=False))}
    assert "youtube" in {d.collector_id for d in list_collector_definitions(_config(False, youtube=True))}


def test_youtube_create_respects_gate() -> None:
    assert create_collector("youtube", config=_config(False, youtube=False), db=None, bot=None) is None
    collector = create_collector("youtube", config=_config(False, youtube=True), db=None, bot=None)
    assert collector is not None
    assert collector.definition.collector_id == "youtube"


def test_reddit_hidden_when_disabled_and_shown_when_enabled() -> None:
    assert "reddit" not in {d.collector_id for d in list_collector_definitions(_config(False, reddit=False))}
    assert "reddit" in {d.collector_id for d in list_collector_definitions(_config(False, reddit=True))}


def test_reddit_create_respects_gate() -> None:
    assert create_collector("reddit", config=_config(False, reddit=False), db=None, bot=None) is None
    collector = create_collector("reddit", config=_config(False, reddit=True), db=None, bot=None)
    assert collector is not None
    assert collector.definition.collector_id == "reddit"


def test_rivalskins_hidden_when_disabled_and_shown_when_enabled() -> None:
    assert "rivalskins" not in {d.collector_id for d in list_collector_definitions(_config(False, rivalskins=False))}
    assert "rivalskins" in {d.collector_id for d in list_collector_definitions(_config(False, rivalskins=True))}


def test_rivalskins_create_respects_gate() -> None:
    assert create_collector("rivalskins", config=_config(False, rivalskins=False), db=None, bot=None) is None
    collector = create_collector("rivalskins", config=_config(False, rivalskins=True), db=None, bot=None)
    assert collector is not None
    assert collector.definition.collector_id == "rivalskins"


def test_wiki_facts_hidden_when_disabled_and_shown_when_enabled() -> None:
    assert "wiki_facts" not in {d.collector_id for d in list_collector_definitions(_config(False, wiki_facts=False))}
    assert "wiki_facts" in {d.collector_id for d in list_collector_definitions(_config(False, wiki_facts=True))}


def test_wiki_facts_create_respects_gate() -> None:
    assert create_collector("wiki_facts", config=_config(False, wiki_facts=False), db=None, bot=None) is None
    collector = create_collector("wiki_facts", config=_config(False, wiki_facts=True), db=None, bot=None)
    assert collector is not None
    assert collector.definition.collector_id == "wiki_facts"


def test_wiki_facts_never_joins_the_news_tick() -> None:
    # The rubric is weekly and has its own scheduler. It is a manual /fetch_news
    # button only — joining the tick would post a fact every interval instead.
    ids = [c.definition.collector_id for c in create_all_collectors(config=_config(True, wiki_facts=True), db=None, bot=None)]
    assert "wiki_facts" not in ids
    assert "official_marvel_rivals" in ids  # ordinary sources still run on the tick


def test_list_without_config_returns_all() -> None:
    ids = {d.collector_id for d in list_collector_definitions()}
    assert {"official_marvel_rivals", "bluesky", "youtube", "reddit", "rivalskins", "wiki_facts"} <= ids


def test_create_collector_respects_gate() -> None:
    assert create_collector("bluesky", config=_config(False), db=None, bot=None) is None
    collector = create_collector("bluesky", config=_config(True), db=None, bot=None)
    assert collector is not None
    assert collector.definition.collector_id == "bluesky"


def test_create_all_collectors_respects_gate() -> None:
    disabled = [c.definition.collector_id for c in create_all_collectors(config=_config(False), db=None, bot=None)]
    assert "bluesky" not in disabled
    assert "official_marvel_rivals" in disabled

    enabled = [c.definition.collector_id for c in create_all_collectors(config=_config(True), db=None, bot=None)]
    assert "bluesky" in enabled


class _FakeCollector:
    def __init__(self, name: str, order: list[str]) -> None:
        self.definition = SimpleNamespace(collector_id=name)
        self.name = name
        self._order = order

    async def run_once(self, *, mode):
        self._order.append(f"{self.name}:start")
        await asyncio.sleep(0)
        self._order.append(f"{self.name}:end")
        return CollectionStats(collector_id=self.name, source_type=self.name, source_title=self.name)


def _tick_config(interval: int = 0) -> SimpleNamespace:
    return SimpleNamespace(moderation_send_interval_seconds=interval)


def test_run_all_collectors_runs_sequentially(monkeypatch) -> None:
    # Sequential execution is what lets each collector's cross-source dedup see
    # the previous collector's just-marked titles. If they ran concurrently, the
    # asyncio.sleep(0) yield would interleave the start/end markers.
    order: list[str] = []
    monkeypatch.setattr(
        registry,
        "create_all_collectors",
        lambda **_kwargs: [_FakeCollector("a", order), _FakeCollector("b", order)],
    )

    stats = asyncio.run(run_all_collectors(config=_tick_config(), db=None, bot=None))

    assert order == ["a:start", "a:end", "b:start", "b:end"]
    assert [s.collector_id for s in stats] == ["a", "b"]


def test_run_all_collectors_shares_one_throttle(monkeypatch) -> None:
    # Every collector in a tick must get the SAME throttle, built from the config
    # interval, so the inter-send gap is honoured across sources, not reset per
    # source.
    collectors = [_FakeCollector("a", []), _FakeCollector("b", [])]
    monkeypatch.setattr(registry, "create_all_collectors", lambda **_kwargs: collectors)

    asyncio.run(run_all_collectors(config=_tick_config(interval=7), db=None, bot=None))

    assert collectors[0].throttle is collectors[1].throttle
    assert collectors[0].throttle.min_interval_seconds == 7
