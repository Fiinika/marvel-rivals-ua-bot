"""Weekly "Чи знали ви?" hero-trivia rubric, sourced from the Marvel Rivals Fandom
wiki (CC BY-SA).

Unlike the per-tick news collectors this is NOT registered in the registry; it runs
on its own weekly schedule (like the fan-art digest) and reuses the shared
BaseNewsCollector pipeline so each English trivia fact is translated to Ukrainian by
Gemini and queued for moderation, credited to the wiki with a link to the source
page. It opts out of cross-source dedup (a fact is not a news story).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from datetime import datetime

from aiogram import Bot

from config import Config
from database import Database
from services.collectors.base import (
    CollectionMode,
    CollectorDefinition,
    DraftCandidate,
    ListingEntry,
)
from services.collectors.runner import BaseNewsCollector
from services.collectors.wiki_facts.client import WikiFact, WikiFactsClient
from services.digests.fanart import _resolve_timezone, next_weekly_run_at
from services.i18n import t


logger = logging.getLogger(__name__)

COLLECTOR_ID = "wiki_facts"
SOURCE_TYPE = "wiki_facts"
# How many random heroes to pull trivia from per run — enough to almost always find
# an unseen fact, while bounding the number of wiki API calls.
HEROES_PER_RUN = 5

DEFINITION = CollectorDefinition(
    collector_id=COLLECTOR_ID,
    source_type=SOURCE_TYPE,
    title_key="collectors.wiki_facts.title",
    button_key="buttons.collector_wiki_facts",
)


class WikiFactsCollector(BaseNewsCollector):
    definition = DEFINITION
    # A trivia fact must never be dropped as a "duplicate" of a news story, nor
    # suppress one.
    participates_in_cross_source_dedup = False

    def __init__(self, *, config: Config, db: Database, bot: Bot) -> None:
        super().__init__(config=config, db=db, bot=bot)
        self.client = WikiFactsClient(config.wiki_facts_api_url)

    def missing_gemini_warning(self) -> str:
        return t("collectors.wiki_facts.errors.missing_gemini_api_key")

    async def fetch_listing(self) -> list[ListingEntry]:
        heroes = await self.client.fetch_hero_titles()
        if not heroes:
            return []

        random.shuffle(heroes)
        entries: list[ListingEntry] = []
        for hero in heroes[:HEROES_PER_RUN]:
            for fact in await self.client.fetch_trivia_facts(hero):
                entries.append(ListingEntry(dedup_key=_fact_id(fact), payload=fact))
        return entries

    async def parse_entry(self, entry: ListingEntry) -> DraftCandidate:
        fact: WikiFact = entry.payload  # type: ignore[assignment]
        return DraftCandidate(
            source_id=entry.dedup_key,
            source_url=fact.page_url,
            title=t("collectors.wiki_facts.fact_title", hero=fact.hero),
            body_text=fact.fact,
            source_name=t("collectors.wiki_facts.source_name"),
            username=t("collectors.wiki_facts.username"),
            original_text=_build_original_text(fact),
            article_date=None,
            article_date_display=None,
            has_media=False,
            media_url=None,
            media_type="none",
            additional_media_urls=None,
        )


def _fact_id(fact: WikiFact) -> str:
    digest = hashlib.sha1(" ".join(fact.fact.split()).casefold().encode("utf-8")).hexdigest()[:16]
    return f"{fact.hero}:{digest}"


def _build_original_text(fact: WikiFact) -> str:
    return "\n".join(
        [
            t("collectors.common.original_text.article_title", value=fact.hero),
            t("collectors.common.original_text.article_url", value=fact.page_url),
            "",
            t("collectors.common.original_text.parsed_article_text"),
            fact.fact,
        ]
    ).strip()


# --- weekly scheduler ----------------------------------------------------------


def start_wiki_facts_scheduler(bot: Bot, config: Config, db: Database) -> asyncio.Task | None:
    """Start the weekly wiki-facts loop, or return None when it is off / unusable."""
    if not config.enable_wiki_facts:
        logger.info("Weekly wiki-facts rubric is disabled (ENABLE_WIKI_FACTS).")
        return None
    if not config.gemini_api_key:
        logger.warning("GEMINI_API_KEY is missing; wiki-facts needs translation and is disabled.")
        return None

    logger.info(
        "Weekly wiki-facts enabled: weekday %s at %02d:00 %s",
        config.wiki_facts_weekday,
        config.wiki_facts_hour,
        config.article_timezone,
    )
    return asyncio.create_task(_wiki_facts_loop(bot, config, db), name="wiki-facts-scheduler")


async def _wiki_facts_loop(bot: Bot, config: Config, db: Database) -> None:
    tz = _resolve_timezone(config.article_timezone)
    while True:
        now = datetime.now(tz)
        next_run = next_weekly_run_at(now, config.wiki_facts_weekday, config.wiki_facts_hour)
        await asyncio.sleep(max(1.0, (next_run - now).total_seconds()))
        try:
            await run_wiki_facts_once(bot, config, db)
        except Exception:
            logger.exception("Weekly wiki-facts run failed; retrying next week.")


async def run_wiki_facts_once(bot: Bot, config: Config, db: Database) -> bool:
    """Queue one translated "Чи знали ви?" fact for moderation. Returns True when a
    submission was created."""
    collector = WikiFactsCollector(config=config, db=db, bot=bot)
    stats = await collector.run_once(mode=CollectionMode.MANUAL_LATEST)
    logger.info(
        "Wiki-facts run: found=%s new=%s sent=%s failed=%s",
        stats.found,
        stats.new,
        stats.sent_to_moderation,
        stats.failed,
    )
    return stats.sent_to_moderation > 0
