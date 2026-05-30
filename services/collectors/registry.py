from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

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
from services.collectors.official_marvel_rivals.collector import (
    DEFINITION as OFFICIAL_MARVEL_RIVALS_DEFINITION,
    OfficialMarvelRivalsCollector,
)
from services.i18n import t


logger = logging.getLogger(__name__)

CollectorFactory = Callable[..., object]

_COLLECTORS: dict[str, tuple[CollectorDefinition, CollectorFactory]] = {
    OFFICIAL_MARVEL_RIVALS_DEFINITION.collector_id: (
        OFFICIAL_MARVEL_RIVALS_DEFINITION,
        OfficialMarvelRivalsCollector,
    ),
}


def list_collector_definitions() -> list[CollectorDefinition]:
    return [definition for definition, _factory in _COLLECTORS.values()]


def get_collector_definition(collector_id: str) -> CollectorDefinition | None:
    entry = _COLLECTORS.get(collector_id)
    return entry[0] if entry is not None else None


def create_collector(
    collector_id: str,
    *,
    config: Config,
    db: Database,
    bot: Bot,
):
    entry = _COLLECTORS.get(collector_id)
    if entry is None:
        return None

    _definition, factory = entry
    return factory(config=config, db=db, bot=bot)


def create_all_collectors(*, config: Config, db: Database, bot: Bot) -> list[object]:
    return [factory(config=config, db=db, bot=bot) for _definition, factory in _COLLECTORS.values()]


async def run_all_collectors(
    *,
    config: Config,
    db: Database,
    bot: Bot,
    mode: CollectionMode = CollectionMode.SCHEDULED_SINCE_LAST,
) -> list[CollectionStats]:
    collectors = create_all_collectors(config=config, db=db, bot=bot)
    return await asyncio.gather(*(_run_collector_safely(collector, mode=mode) for collector in collectors))


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
