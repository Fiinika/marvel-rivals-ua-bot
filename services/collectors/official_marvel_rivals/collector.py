from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timezone

from aiogram import Bot

from config import Config
from database import Database
from services.collectors.base import CollectionMode, CollectionStats, CollectorDefinition, empty_stats
from services.collectors.official_marvel_rivals.article_parser import ArticleParser, ParsedArticle
from services.collectors.official_marvel_rivals.news_fetcher import NewsArticleSummary, OfficialNewsFetcher
from services.gemini import GeminiDraftGenerator, GeminiDraftInput
from services.i18n import t
from services.moderation import send_submission_to_moderation


logger = logging.getLogger(__name__)

COLLECTOR_ID = "official_marvel_rivals"
SOURCE_TYPE = "official_marvel_rivals"

DEFINITION = CollectorDefinition(
    collector_id=COLLECTOR_ID,
    source_type=SOURCE_TYPE,
    title_key="collectors.official_marvel_rivals.title",
    button_key="buttons.collector_official_marvel_rivals",
)


@dataclass(frozen=True)
class ParsedArticleCandidate:
    summary: NewsArticleSummary
    article: ParsedArticle
    source_id: str
    source_url: str


class OfficialMarvelRivalsCollector:
    definition = DEFINITION

    def __init__(self, *, config: Config, db: Database, bot: Bot) -> None:
        self.config = config
        self.db = db
        self.bot = bot
        self.fetcher = OfficialNewsFetcher(config.official_news_url)
        self.parser = ArticleParser(config.article_timezone)

    async def run_once(self, mode: CollectionMode = CollectionMode.MANUAL_LATEST) -> CollectionStats:
        stats = empty_stats(self.definition)
        summaries = await self.fetcher.fetch_recent_articles()
        stats.found = len(summaries)

        if not summaries:
            logger.warning("Official Marvel Rivals news collector found no articles")
            return stats

        if not self.config.gemini_api_key:
            warning = t("collectors.official_marvel_rivals.errors.missing_gemini_api_key")
            logger.warning("GEMINI_API_KEY is missing. Skipping official Marvel Rivals AI draft generation.")
            stats.errors.append(warning)
            await self._count_seen_and_new_without_gemini(summaries, stats, mode=mode)
            return stats

        generator = GeminiDraftGenerator(self.config.gemini_api_key, self.config.gemini_model)
        latest_seen_article_date = None
        if mode == CollectionMode.SCHEDULED_SINCE_LAST:
            latest_seen_article_date = await self.db.get_latest_seen_article_date(SOURCE_TYPE)

        if mode == CollectionMode.MANUAL_LATEST:
            candidate = await self._find_latest_unseen_candidate(summaries, stats)
            if candidate is not None:
                await self._create_moderation_submissions(candidate, generator, stats)
            return stats

        for summary in summaries:
            candidate = await self._parse_candidate_if_needed(
                summary,
                stats,
                latest_seen_article_date=latest_seen_article_date,
            )
            if candidate is None:
                continue

            await self._create_moderation_submissions(candidate, generator, stats)

        return stats

    async def _count_seen_and_new_without_gemini(
        self,
        summaries: list[NewsArticleSummary],
        stats: CollectionStats,
        mode: CollectionMode,
    ) -> None:
        for summary in summaries:
            if await self.db.is_source_seen(SOURCE_TYPE, summary.canonical_url):
                stats.duplicates += 1
            else:
                stats.new += 1

    async def _find_latest_unseen_candidate(
        self,
        summaries: list[NewsArticleSummary],
        stats: CollectionStats,
    ) -> ParsedArticleCandidate | None:
        candidates: list[ParsedArticleCandidate] = []
        for summary in summaries:
            candidate = await self._parse_candidate_if_needed(
                summary,
                stats,
                latest_seen_article_date=None,
            )
            if candidate is not None:
                candidates.append(candidate)

        if not candidates:
            return None

        return max(
            enumerate(candidates),
            key=lambda item: _candidate_sort_key(item[0], item[1]),
        )[1]

    async def _parse_candidate_if_needed(
        self,
        summary: NewsArticleSummary,
        stats: CollectionStats,
        latest_seen_article_date: str | None,
    ) -> ParsedArticleCandidate | None:
        if await self.db.is_source_seen(SOURCE_TYPE, summary.canonical_url):
            stats.duplicates += 1
            return None

        article = await self.parser.fetch_and_parse(summary)
        source_id = article.canonical_url or summary.canonical_url
        source_url = article.canonical_url or article.article_url

        if source_id != summary.canonical_url and await self.db.is_source_seen(SOURCE_TYPE, source_id):
            stats.duplicates += 1
            return None

        if latest_seen_article_date and not _is_newer_or_same_article_date(
            article.date_info.article_date if article.date_info is not None else None,
            latest_seen_article_date,
        ):
            return None

        stats.new += 1
        return ParsedArticleCandidate(
            summary=summary,
            article=article,
            source_id=source_id,
            source_url=source_url,
        )

    async def _create_moderation_submissions(
        self,
        candidate: ParsedArticleCandidate,
        generator: GeminiDraftGenerator,
        stats: CollectionStats,
    ) -> bool:
        article = candidate.article
        source_id = candidate.source_id
        source_url = candidate.source_url
        try:
            max_part_length = 760 if article.media_url and article.media_type == "photo" else 3500
            draft_parts = await generator.generate_drafts(
                GeminiDraftInput(
                    title=article.title,
                    article_url=source_url,
                    article_date_display=(
                        article.date_info.article_date_display if article.date_info is not None else None
                    ),
                    body_text=article.body_text or article.raw_excerpt or "",
                    source_type=SOURCE_TYPE,
                    source_name=t("collectors.official_marvel_rivals.source_name"),
                ),
                max_part_length=max_part_length,
            )
            stats.drafts_created += len(draft_parts)
        except Exception as exc:
            stats.failed += 1
            message = t("collector_report.gemini_failed", url=source_url, error=exc)
            stats.errors.append(message)
            logger.exception("Gemini failed for %s", source_url)
            return False

        try:
            for index, draft_text in enumerate(draft_parts):
                has_first_part_media = index == 0 and article.media_url and article.media_type == "photo"
                submission_id = await self.db.create_ai_news_submission(
                    username=t("collectors.official_marvel_rivals.username"),
                    original_text=_build_original_text(article, source_url),
                    draft_text=draft_text,
                    message_type="photo" if has_first_part_media else "text",
                    media_url=article.media_url if has_first_part_media else None,
                    media_type=article.media_type if has_first_part_media else "none",
                    source_type=SOURCE_TYPE,
                    source_id=source_id,
                    source_url=source_url,
                    article_date=article.date_info.article_date if article.date_info is not None else None,
                    article_date_display=(
                        article.date_info.article_date_display if article.date_info is not None else None
                    ),
                )
                await send_submission_to_moderation(self.bot, self.config, self.db, submission_id)
                stats.sent_to_moderation += 1
        except Exception as exc:
            stats.failed += 1
            message = t("collector_report.moderation_send_failed", url=source_url, error=exc)
            stats.errors.append(message)
            logger.exception("Failed to send official news article to moderation: %s", source_url)
            return False

        await self.db.mark_source_seen(
            source_type=SOURCE_TYPE,
            source_id=source_id,
            source_url=source_url,
            title=article.title,
            article_date=article.date_info.article_date if article.date_info is not None else None,
        )
        logger.info("Created moderation draft for official Marvel Rivals article %s", source_url)
        return True


def _candidate_sort_key(index: int, candidate: ParsedArticleCandidate) -> tuple[int, datetime, int]:
    article_date = candidate.article.date_info.article_date if candidate.article.date_info is not None else None
    parsed_date = _parse_sortable_article_date(article_date)
    if parsed_date is None:
        return (0, datetime.min.replace(tzinfo=timezone.utc), -index)

    return (1, parsed_date, -index)


def _is_newer_or_same_article_date(article_date: str | None, latest_seen_article_date: str) -> bool:
    if article_date is None:
        return True

    parsed_article_date = _parse_sortable_article_date(article_date)
    parsed_latest_date = _parse_sortable_article_date(latest_seen_article_date)
    if parsed_article_date is None or parsed_latest_date is None:
        return True

    return parsed_article_date >= parsed_latest_date


def _parse_sortable_article_date(value: str) -> datetime | None:
    try:
        if "T" in value:
            parsed = datetime.fromisoformat(value)
        else:
            parsed = datetime.combine(datetime.fromisoformat(value).date(), time.min)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def _build_original_text(article: ParsedArticle, source_url: str) -> str:
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

    if article.body_text:
        parts.extend(["", t("collectors.common.original_text.parsed_article_text"), article.body_text])
    elif article.raw_excerpt:
        parts.extend(["", t("collectors.common.original_text.parsed_excerpt"), article.raw_excerpt])

    if article.media_url:
        parts.extend(
            [
                "",
                t("collectors.common.original_text.media_url", value=article.media_url),
                t("collectors.common.original_text.media_type", value=article.media_type),
            ]
        )

    return "\n".join(parts).strip()
