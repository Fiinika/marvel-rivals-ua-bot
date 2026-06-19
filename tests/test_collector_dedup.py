"""Tests for cross-source semantic dedup: the BaseNewsCollector wiring that asks
Gemini whether a parsed candidate duplicates a recently-seen title, and the
get_recent_seen_titles query it relies on."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from types import SimpleNamespace

from database import Database
from services.collectors.base import (
    CollectionStats,
    CollectorDefinition,
    DraftCandidate,
    ListingEntry,
)
from services.collectors.runner import BaseNewsCollector, _is_newer_or_same_article_date
from services.gemini import DuplicateVerdict


DEFINITION = CollectorDefinition(
    collector_id="test_source",
    source_type="test_source",
    title_key="unused.title",
    button_key="unused.button",
)

CANDIDATE = DraftCandidate(
    source_id="s1",
    source_url="https://example.com/1",
    title="New shiny trailer",
    body_text="body",
    source_name="Test Source",
    username="tester",
    original_text="original",
    article_date="2026-06-16",
)

ENTRY = ListingEntry(dedup_key="s1", payload=None)


class _FakeDB:
    def __init__(self, *, seen: set[tuple[str, str]] | None = None, titles: list[str] | None = None) -> None:
        self.seen = set(seen or set())
        self.titles = list(titles or [])
        self.marked: list[dict] = []
        self.last_exclude: str | None = None

    async def is_source_seen(self, source_type: str, source_id: str) -> bool:
        return (source_type, source_id) in self.seen

    async def get_recent_seen_titles(self, *, limit: int, exclude_source_type: str | None = None) -> list[str]:
        self.last_exclude = exclude_source_type
        return self.titles[:limit]

    async def mark_source_seen(self, **kwargs) -> None:
        self.marked.append(kwargs)


class _FakeGenerator:
    def __init__(self, *, verdict: DuplicateVerdict | None = None, error: Exception | None = None) -> None:
        self.verdict = verdict if verdict is not None else DuplicateVerdict(is_duplicate=False)
        self.error = error
        self.calls: list[tuple[str, list[str]]] = []

    async def find_duplicate_title(self, new_title: str, existing_titles: list[str]) -> DuplicateVerdict:
        self.calls.append((new_title, list(existing_titles)))
        if self.error is not None:
            raise self.error
        return self.verdict


class _StubCollector(BaseNewsCollector):
    definition = DEFINITION

    def __init__(self, *, config, db) -> None:
        super().__init__(config=config, db=db, bot=None)
        self.parse_calls = 0

    async def fetch_listing(self) -> list[ListingEntry]:
        return [ENTRY]

    async def parse_entry(self, entry: ListingEntry) -> DraftCandidate:
        self.parse_calls += 1
        return CANDIDATE

    def missing_gemini_warning(self) -> str:
        return "no gemini"


def _config(*, enabled: bool = True, limit: int = 200) -> SimpleNamespace:
    return SimpleNamespace(enable_cross_source_dedup=enabled, cross_source_dedup_title_limit=limit)


def _fresh_stats() -> CollectionStats:
    return CollectionStats(collector_id="test_source", source_type="test_source", source_title="Test")


def _parse(collector: _StubCollector, generator: _FakeGenerator, stats: CollectionStats) -> DraftCandidate | None:
    return asyncio.run(
        collector._parse_candidate_if_needed(
            ENTRY, stats, latest_seen_article_date=None, generator=generator
        )
    )


def test_cross_source_duplicate_is_skipped_and_marked_seen() -> None:
    db = _FakeDB(titles=["New shiny trailer (official)"])
    gen = _FakeGenerator(verdict=DuplicateVerdict(is_duplicate=True, matched_title="New shiny trailer (official)"))
    collector = _StubCollector(config=_config(), db=db)
    stats = _fresh_stats()

    result = _parse(collector, gen, stats)

    assert result is None
    assert stats.duplicates == 1
    assert stats.new == 0
    assert gen.calls, "Gemini should have been consulted"
    # A source must never be compared against its own titles.
    assert db.last_exclude == "test_source"
    # Marked seen so the same item is not re-fetched and re-checked next run.
    assert len(db.marked) == 1
    assert db.marked[0]["source_id"] == "s1"
    assert db.marked[0]["title"] == "New shiny trailer"


def test_unique_candidate_passes_through() -> None:
    db = _FakeDB(titles=["Something unrelated"])
    gen = _FakeGenerator(verdict=DuplicateVerdict(is_duplicate=False))
    collector = _StubCollector(config=_config(), db=db)
    stats = _fresh_stats()

    result = _parse(collector, gen, stats)

    assert result is CANDIDATE
    assert stats.new == 1
    assert stats.duplicates == 0
    assert db.marked == []


def test_dedup_disabled_does_not_consult_gemini() -> None:
    db = _FakeDB(titles=["New shiny trailer"])
    gen = _FakeGenerator(verdict=DuplicateVerdict(is_duplicate=True))  # would say dup if asked
    collector = _StubCollector(config=_config(enabled=False), db=db)
    stats = _fresh_stats()

    result = _parse(collector, gen, stats)

    assert result is CANDIDATE
    assert stats.new == 1
    assert gen.calls == []


def test_zero_limit_does_not_consult_gemini() -> None:
    db = _FakeDB(titles=["New shiny trailer"])
    gen = _FakeGenerator(verdict=DuplicateVerdict(is_duplicate=True))
    collector = _StubCollector(config=_config(limit=0), db=db)
    stats = _fresh_stats()

    result = _parse(collector, gen, stats)

    assert result is CANDIDATE
    assert stats.new == 1
    assert gen.calls == []


def test_no_existing_titles_does_not_consult_gemini() -> None:
    db = _FakeDB(titles=[])
    gen = _FakeGenerator(verdict=DuplicateVerdict(is_duplicate=True))
    collector = _StubCollector(config=_config(), db=db)
    stats = _fresh_stats()

    result = _parse(collector, gen, stats)

    assert result is CANDIDATE
    assert stats.new == 1
    assert gen.calls == []


def test_dedup_check_failure_fails_open() -> None:
    db = _FakeDB(titles=["New shiny trailer"])
    gen = _FakeGenerator(error=RuntimeError("gemini exploded"))
    collector = _StubCollector(config=_config(), db=db)
    stats = _fresh_stats()

    result = _parse(collector, gen, stats)

    # Fail open: a check error must never drop real news.
    assert result is CANDIDATE
    assert stats.new == 1
    assert db.marked == []


def test_exact_seen_short_circuits_before_parsing_or_dedup() -> None:
    db = _FakeDB(seen={("test_source", "s1")}, titles=["New shiny trailer"])
    gen = _FakeGenerator(verdict=DuplicateVerdict(is_duplicate=True))
    collector = _StubCollector(config=_config(), db=db)
    stats = _fresh_stats()

    result = _parse(collector, gen, stats)

    assert result is None
    assert stats.duplicates == 1
    assert collector.parse_calls == 0  # never parsed
    assert gen.calls == []  # never asked Gemini


def test_authoritative_source_is_never_suppressed_by_cross_source_dedup() -> None:
    # A collector that opts out (the official, authoritative source) must never be
    # dropped as a cross-source duplicate of a shorter social post — and must not
    # even consult Gemini.
    db = _FakeDB(titles=["New shiny trailer (Bluesky)"])
    gen = _FakeGenerator(verdict=DuplicateVerdict(is_duplicate=True, matched_title="New shiny trailer (Bluesky)"))
    collector = _StubCollector(config=_config(), db=db)
    collector.participates_in_cross_source_dedup = False
    stats = _fresh_stats()

    result = _parse(collector, gen, stats)

    assert result is CANDIDATE
    assert stats.new == 1
    assert stats.duplicates == 0
    assert gen.calls == []
    assert db.marked == []


def test_official_collector_opts_out_but_base_default_participates() -> None:
    from services.collectors.official_marvel_rivals.collector import OfficialMarvelRivalsCollector

    assert OfficialMarvelRivalsCollector.participates_in_cross_source_dedup is False
    assert BaseNewsCollector.participates_in_cross_source_dedup is True


def test_date_gate_keeps_same_day_article_when_granularity_degrades() -> None:
    # latest_seen carries a full afternoon timestamp; a genuinely-new same-day
    # article whose detail-page fetch failed degrades to a date-only card date.
    # Comparing date-only midnight against the afternoon timestamp must NOT drop it.
    assert _is_newer_or_same_article_date("2026-06-16", "2026-06-16T18:00+03:00") is True
    # ...and the symmetric case (seen value is the date-only one).
    assert _is_newer_or_same_article_date("2026-06-16T09:00+03:00", "2026-06-16") is True


def test_date_gate_drops_strictly_older_date_only_article() -> None:
    assert _is_newer_or_same_article_date("2026-06-15", "2026-06-16T18:00+03:00") is False


def test_date_gate_full_timestamps_compared_precisely() -> None:
    assert _is_newer_or_same_article_date("2026-06-16T17:00+03:00", "2026-06-16T18:00+03:00") is False
    assert _is_newer_or_same_article_date("2026-06-16T19:00+03:00", "2026-06-16T18:00+03:00") is True


def test_date_gate_none_article_date_is_always_kept() -> None:
    assert _is_newer_or_same_article_date(None, "2026-06-16T18:00+03:00") is True


def test_get_recent_seen_titles_distinct_newest_first_capped(tmp_path) -> None:
    path = tmp_path / "bot.db"
    db = Database(str(path))
    asyncio.run(db.init())

    rows = [
        ("official", "u1", "url1", "Alpha", None, "2026-06-10T00:00:00+00:00"),
        ("official", "u2", "url2", "Beta", None, "2026-06-12T00:00:00+00:00"),
        ("youtube", "v1", "url3", "Beta", None, "2026-06-13T00:00:00+00:00"),  # same title, newer sighting
        ("youtube", "v2", "url4", "", None, "2026-06-14T00:00:00+00:00"),      # empty title -> excluded
        ("reddit", "r1", "url5", None, None, "2026-06-15T00:00:00+00:00"),     # null title -> excluded
        ("reddit", "r2", "url6", "Gamma", None, "2026-06-11T00:00:00+00:00"),
    ]
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executemany(
            "INSERT INTO seen_sources "
            "(source_type, source_id, source_url, title, article_date, first_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )

    # Distinct titles, ordered by most recent sighting: Beta(06-13), Gamma(06-11), Alpha(06-10).
    assert asyncio.run(db.get_recent_seen_titles(limit=10)) == ["Beta", "Gamma", "Alpha"]
    assert asyncio.run(db.get_recent_seen_titles(limit=2)) == ["Beta", "Gamma"]
    assert asyncio.run(db.get_recent_seen_titles(limit=0)) == []


def test_get_recent_seen_titles_excludes_own_source_type(tmp_path) -> None:
    path = tmp_path / "bot.db"
    db = Database(str(path))
    asyncio.run(db.init())

    rows = [
        ("official", "u1", "url1", "Official only", None, "2026-06-10T00:00:00+00:00"),
        ("youtube", "v1", "url2", "From YouTube", None, "2026-06-12T00:00:00+00:00"),
    ]
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executemany(
            "INSERT INTO seen_sources "
            "(source_type, source_id, source_url, title, article_date, first_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )

    # Excluding "official" leaves only the YouTube title — and with a single
    # source configured (only official rows) the comparison set would be empty.
    assert asyncio.run(db.get_recent_seen_titles(limit=10, exclude_source_type="official")) == ["From YouTube"]
    assert asyncio.run(db.get_recent_seen_titles(limit=10, exclude_source_type="youtube")) == ["Official only"]
    assert asyncio.run(
        db.get_recent_seen_titles(limit=10, exclude_source_type="official")
    ) != asyncio.run(db.get_recent_seen_titles(limit=10))
