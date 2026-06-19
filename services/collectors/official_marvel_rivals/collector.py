from __future__ import annotations

import logging

from aiogram import Bot

from config import Config
from database import Database
from services.collectors.base import CollectorDefinition, DraftCandidate, ListingEntry
from services.collectors.runner import BaseNewsCollector
from services.collectors.official_marvel_rivals.article_parser import ArticleParser, ParsedArticle
from services.collectors.official_marvel_rivals.news_fetcher import NewsArticleSummary, OfficialNewsFetcher
from services.date_utils import build_utc_time_conversion_notes
from services.i18n import t


logger = logging.getLogger(__name__)

COLLECTOR_ID = "official_marvel_rivals"
SOURCE_TYPE = "official_marvel_rivals"

DEFINITION = CollectorDefinition(
    collector_id=COLLECTOR_ID,
    source_type=SOURCE_TYPE,
    title_key="collectors.official_marvel_rivals.title",
    button_key="buttons.collector_official_marvel_rivals",
)


class OfficialMarvelRivalsCollector(BaseNewsCollector):
    definition = DEFINITION
    # The official site is the authoritative, full-detail source; never let it be
    # suppressed as a cross-source duplicate of a shorter social-media post.
    participates_in_cross_source_dedup = False

    def __init__(self, *, config: Config, db: Database, bot: Bot) -> None:
        super().__init__(config=config, db=db, bot=bot)
        self.fetcher = OfficialNewsFetcher(config.official_news_url)
        self.parser = ArticleParser(config.article_timezone)

    def missing_gemini_warning(self) -> str:
        return t("collectors.official_marvel_rivals.errors.missing_gemini_api_key")

    async def fetch_listing(self) -> list[ListingEntry]:
        summaries = await self.fetcher.fetch_recent_articles()
        return [ListingEntry(dedup_key=summary.canonical_url, payload=summary) for summary in summaries]

    async def parse_entry(self, entry: ListingEntry) -> DraftCandidate:
        summary: NewsArticleSummary = entry.payload  # type: ignore[assignment]
        article = await self.parser.fetch_and_parse(summary)
        source_id = article.canonical_url or summary.canonical_url
        source_url = article.canonical_url or article.article_url
        has_media = bool(article.media_url and article.media_type == "photo")
        article_date = article.date_info.article_date if article.date_info is not None else None
        article_date_display = (
            article.date_info.article_date_display if article.date_info is not None else None
        )

        return DraftCandidate(
            source_id=source_id,
            source_url=source_url,
            title=article.title,
            body_text=article.body_text or article.raw_excerpt or "",
            source_name=t("collectors.official_marvel_rivals.source_name"),
            username=t("collectors.official_marvel_rivals.username"),
            original_text=_build_original_text(article, source_url, self.config.article_timezone),
            article_date=article_date,
            article_date_display=article_date_display,
            has_media=has_media,
            media_url=article.media_url,
            media_type=article.media_type,
            additional_media_urls=list(article.media_urls[1:]) if has_media else None,
        )


def _build_original_text(article: ParsedArticle, source_url: str, article_timezone: str) -> str:
    parts = [
        t("collectors.common.original_text.article_title", value=article.title),
        t("collectors.common.original_text.article_url", value=source_url),
    ]

    if article.raw_date:
        parts.append(t("collectors.common.original_text.original_article_date", value=article.raw_date))
    if article.date_info is not None:
        parts.append(
            t(
                "collectors.common.original_text.converted_article_date",
                value=article.date_info.article_date_display,
            )
        )

    datetime_notes = build_utc_time_conversion_notes(
        article.body_text or article.raw_excerpt or "",
        article_date=article.date_info.article_date if article.date_info is not None else None,
        target_timezone=article_timezone,
    )
    if datetime_notes:
        parts.extend(["", "Конвертація UTC-часів для чернетки:", datetime_notes])

    if article.body_text:
        parts.extend(["", t("collectors.common.original_text.parsed_article_text"), article.body_text])
    elif article.raw_excerpt:
        parts.extend(["", t("collectors.common.original_text.parsed_excerpt"), article.raw_excerpt])

    if article.media_urls:
        media_lines: list[str] = []
        for media_url in article.media_urls:
            media_lines.append(t("collectors.common.original_text.media_url", value=media_url))
        parts.extend(
            [
                "",
                *media_lines,
                t("collectors.common.original_text.media_type", value=article.media_type),
            ]
        )

    return "\n".join(parts).strip()
