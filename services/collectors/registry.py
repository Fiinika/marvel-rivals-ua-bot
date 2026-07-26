from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from aiogram import Bot

from config import Config
from database import Database
from services.collectors.base import (
    CollectionMode,
    CollectionStats,
    CollectorDefinition,
    empty_stats,
    format_collection_report,
    format_collection_reports,
)
from services.collectors.bluesky.collector import (
    DEFINITION as BLUESKY_DEFINITION,
    BlueskyCollector,
)
from services.collectors.official_marvel_rivals.collector import (
    DEFINITION as OFFICIAL_MARVEL_RIVALS_DEFINITION,
    OfficialMarvelRivalsCollector,
)
from services.collectors.reddit.collector import (
    DEFINITION as REDDIT_DEFINITION,
    RedditLeaksCollector,
)
from services.collectors.rivalskins.collector import (
    DEFINITION as RIVALSKINS_DEFINITION,
    RivalSkinsCollector,
)
from services.collectors.throttle import SubmissionThrottle
from services.collectors.wiki_facts.collector import (
    DEFINITION as WIKI_FACTS_DEFINITION,
    WikiFactsCollector,
)
from services.collectors.youtube.collector import (
    DEFINITION as YOUTUBE_DEFINITION,
    YouTubeCollector,
)
from services.i18n import t


logger = logging.getLogger(__name__)

CollectorFactory = Callable[..., object]
CollectorEnabledCheck = Callable[[Config], bool]


def _always_enabled(_config: Config) -> bool:
    return True


@dataclass(frozen=True)
class _CollectorEntry:
    definition: CollectorDefinition
    factory: CollectorFactory
    is_enabled: CollectorEnabledCheck
    # False for a source that runs on its OWN schedule: it stays selectable as a
    # manual /fetch_news button but must never join the shared news tick, which
    # would post from it every NEWS_CHECK_INTERVAL_MINUTES instead of weekly.
    scheduled: bool = True


_COLLECTORS: dict[str, _CollectorEntry] = {
    OFFICIAL_MARVEL_RIVALS_DEFINITION.collector_id: _CollectorEntry(
        OFFICIAL_MARVEL_RIVALS_DEFINITION,
        OfficialMarvelRivalsCollector,
        _always_enabled,
    ),
    BLUESKY_DEFINITION.collector_id: _CollectorEntry(
        BLUESKY_DEFINITION,
        BlueskyCollector,
        lambda config: config.enable_bluesky_source,
    ),
    YOUTUBE_DEFINITION.collector_id: _CollectorEntry(
        YOUTUBE_DEFINITION,
        YouTubeCollector,
        lambda config: config.enable_youtube_source,
    ),
    REDDIT_DEFINITION.collector_id: _CollectorEntry(
        REDDIT_DEFINITION,
        RedditLeaksCollector,
        lambda config: config.enable_reddit_source,
    ),
    RIVALSKINS_DEFINITION.collector_id: _CollectorEntry(
        RIVALSKINS_DEFINITION,
        RivalSkinsCollector,
        lambda config: config.enable_rivalskins_source,
    ),
    WIKI_FACTS_DEFINITION.collector_id: _CollectorEntry(
        WIKI_FACTS_DEFINITION,
        WikiFactsCollector,
        lambda config: config.enable_wiki_facts,
        scheduled=False,  # weekly rubric: manual button only, never on the tick
    ),
}


def list_collector_definitions(config: Config | None = None) -> list[CollectorDefinition]:
    return [
        entry.definition
        for entry in _COLLECTORS.values()
        if config is None or entry.is_enabled(config)
    ]


def get_collector_definition(collector_id: str) -> CollectorDefinition | None:
    entry = _COLLECTORS.get(collector_id)
    return entry.definition if entry is not None else None


def create_collector(
    collector_id: str,
    *,
    config: Config,
    db: Database,
    bot: Bot,
):
    entry = _COLLECTORS.get(collector_id)
    if entry is None or not entry.is_enabled(config):
        return None

    return entry.factory(config=config, db=db, bot=bot)


def create_all_collectors(*, config: Config, db: Database, bot: Bot) -> list[object]:
    """The collectors that belong to the periodic news tick — self-scheduled
    sources are deliberately excluded."""
    return [
        entry.factory(config=config, db=db, bot=bot)
        for entry in _COLLECTORS.values()
        if entry.scheduled and entry.is_enabled(config)
    ]


async def run_all_collectors(
    *,
    config: Config,
    db: Database,
    bot: Bot,
    mode: CollectionMode = CollectionMode.SCHEDULED_SINCE_LAST,
) -> list[CollectionStats]:
    # Run collectors sequentially (not asyncio.gather): each collector's
    # cross-source dedup reads the seen-titles of the OTHERS, so the previous
    # collector must finish marking its story seen before the next one checks —
    # otherwise the same story arriving in two feeds in one tick slips past dedup.
    collectors = create_all_collectors(config=config, db=db, bot=bot)
    # One throttle shared across every collector so the inter-send gap is honoured
    # across sources within a tick, not just within a single collector's loop.
    throttle = SubmissionThrottle(config.moderation_send_interval_seconds)
    stats_list: list[CollectionStats] = []
    for collector in collectors:
        collector.throttle = throttle
        stats_list.append(await _run_collector_safely(collector, mode=mode))
    return stats_list


async def _run_collector_safely(collector, *, mode: CollectionMode) -> CollectionStats:
    definition = collector.definition
    try:
        return await collector.run_once(mode=mode)
    except Exception as exc:
        logger.exception("Collector %s failed", definition.collector_id)
        stats = empty_stats(definition)
        stats.failed = 1
        stats.errors.append(t("collector_report.collector_run_failed", error=exc))
        return stats


__all__ = [
    "CollectionStats",
    "CollectionMode",
    "CollectorDefinition",
    "create_all_collectors",
    "create_collector",
    "format_collection_report",
    "format_collection_reports",
    "get_collector_definition",
    "list_collector_definitions",
    "run_all_collectors",
]
