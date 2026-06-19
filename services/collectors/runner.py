"""Shared collector orchestration engine.

This module holds :class:`BaseNewsCollector`, the abstract base every news
collector subclasses. It is kept separate from :mod:`services.collectors.base`
(which carries only the lightweight types the UI/registry import) because the
orchestration here depends on the moderation/Gemini layers — importing those
from ``base`` would create a ``base -> moderation -> keyboards -> base`` cycle.

To add a new source, subclass :class:`BaseNewsCollector` and implement
:meth:`~BaseNewsCollector.fetch_listing`, :meth:`~BaseNewsCollector.parse_entry`
and :meth:`~BaseNewsCollector.missing_gemini_warning`.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import date, datetime, time, timezone

from aiogram import Bot

from config import Config
from database import Database
from services.collectors.base import (
    CollectionMode,
    CollectionStats,
    CollectorDefinition,
    DraftCandidate,
    ListingEntry,
    empty_stats,
)
from services.collectors.throttle import SubmissionThrottle
from services.date_utils import build_utc_time_conversion_notes
from services.gemini import GeminiDraftGenerator, GeminiDraftInput
from services.i18n import t
from services.moderation import send_submission_to_moderation


logger = logging.getLogger(__name__)


class BaseNewsCollector(ABC):
    """Shared ``run_once`` orchestration for every news collector.

    Subclasses provide only the source-specific behaviour:

    * :meth:`fetch_listing` — return the listing entries to consider.
    * :meth:`parse_entry` — turn one entry into a :class:`DraftCandidate`.
    * :meth:`missing_gemini_warning` — the message shown when the Gemini key
      is absent.

    The generic dedup/date-gating/draft/moderation pipeline lives here so new
    sources do not re-implement it.
    """

    definition: CollectorDefinition

    # Whether this collector's items may be dropped as cross-source duplicates of
    # items already seen from OTHER sources. The authoritative official source
    # sets this False so its full-detail article is never suppressed in favour of
    # a shorter social post that surfaced the same story first; its titles still
    # remain visible for other sources to dedup against.
    participates_in_cross_source_dedup: bool = True

    def __init__(self, *, config: Config, db: Database, bot: Bot) -> None:
        self.config = config
        self.db = db
        self.bot = bot
        # No-op by default so a single manual run is never delayed. ``run_all_collectors``
        # swaps in one shared, configured throttle so a multi-item tick is spaced out.
        self.throttle = SubmissionThrottle(0)

    # --- source-specific hooks -------------------------------------------------

    @abstractmethod
    async def fetch_listing(self) -> list[ListingEntry]:
        """Fetch the listing and return one :class:`ListingEntry` per item."""

    @abstractmethod
    async def parse_entry(self, entry: ListingEntry) -> DraftCandidate:
        """Fetch and parse a single entry into a normalized draft candidate."""

    @abstractmethod
    def missing_gemini_warning(self) -> str:
        """User-facing warning appended to stats when GEMINI_API_KEY is missing."""

    # --- shared orchestration --------------------------------------------------

    async def run_once(self, mode: CollectionMode = CollectionMode.MANUAL_LATEST) -> CollectionStats:
        stats = empty_stats(self.definition)
        entries = await self.fetch_listing()
        stats.found = len(entries)

        if not entries:
            logger.warning("%s collector found no entries", self.definition.collector_id)
            return stats

        if not self.config.gemini_api_key:
            warning = self.missing_gemini_warning()
            logger.warning(
                "GEMINI_API_KEY is missing. Skipping AI draft generation for %s.",
                self.definition.collector_id,
            )
            stats.errors.append(warning)
            await self._count_seen_and_new_without_gemini(entries, stats, mode=mode)
            return stats

        generator = GeminiDraftGenerator(self.config.gemini_api_key, self.config.gemini_model)
        latest_seen_article_date = None
        if mode == CollectionMode.SCHEDULED_SINCE_LAST:
            latest_seen_article_date = await self.db.get_latest_seen_article_date(self.definition.source_type)

        if mode == CollectionMode.MANUAL_LATEST:
            candidate = await self._find_latest_unseen_candidate(entries, stats, generator)
            if candidate is not None:
                await self._create_moderation_submissions(candidate, generator, stats)
            return stats

        for entry in entries:
            candidate = await self._parse_candidate_if_needed(
                entry,
                stats,
                latest_seen_article_date=latest_seen_article_date,
                generator=generator,
            )
            if candidate is None:
                continue

            await self._create_moderation_submissions(candidate, generator, stats)

        return stats

    async def _count_seen_and_new_without_gemini(
        self,
        entries: list[ListingEntry],
        stats: CollectionStats,
        mode: CollectionMode,
    ) -> None:
        for entry in entries:
            if await self.db.is_source_seen(self.definition.source_type, entry.dedup_key):
                stats.duplicates += 1
            else:
                stats.new += 1
                if mode == CollectionMode.MANUAL_LATEST:
                    break

    async def _find_latest_unseen_candidate(
        self,
        entries: list[ListingEntry],
        stats: CollectionStats,
        generator: GeminiDraftGenerator,
    ) -> DraftCandidate | None:
        for entry in entries:
            candidate = await self._parse_candidate_if_needed(
                entry,
                stats,
                latest_seen_article_date=None,
                generator=generator,
            )
            if candidate is not None:
                return candidate

        return None

    async def _parse_candidate_if_needed(
        self,
        entry: ListingEntry,
        stats: CollectionStats,
        latest_seen_article_date: str | None,
        generator: GeminiDraftGenerator,
    ) -> DraftCandidate | None:
        source_type = self.definition.source_type
        if await self.db.is_source_seen(source_type, entry.dedup_key):
            stats.duplicates += 1
            return None

        candidate = await self.parse_entry(entry)

        if candidate.source_id != entry.dedup_key and await self.db.is_source_seen(source_type, candidate.source_id):
            stats.duplicates += 1
            return None

        if latest_seen_article_date and not _is_newer_or_same_article_date(
            candidate.article_date,
            latest_seen_article_date,
        ):
            return None

        if await self._is_cross_source_duplicate(candidate, generator):
            stats.duplicates += 1
            # Record it as seen so we don't re-fetch, re-parse and re-ask Gemini
            # about the same item on every subsequent run.
            await self.db.mark_source_seen(
                source_type=source_type,
                source_id=candidate.source_id,
                source_url=candidate.source_url,
                title=candidate.title,
                article_date=candidate.article_date,
            )
            return None

        stats.new += 1
        return candidate

    async def _is_cross_source_duplicate(
        self,
        candidate: DraftCandidate,
        generator: GeminiDraftGenerator,
    ) -> bool:
        """Ask Gemini whether this candidate's title already matches a recently
        seen one from any source. Fails open (returns False) so a check error
        never drops real news."""
        if not self.participates_in_cross_source_dedup:
            return False

        if not self.config.enable_cross_source_dedup:
            return False

        limit = self.config.cross_source_dedup_title_limit
        if limit <= 0:
            return False

        # Compare only against OTHER sources: a source never dedups against its
        # own titles, so with a single source configured this is a safe no-op and
        # cannot suppress genuinely-new articles from that same source.
        existing_titles = await self.db.get_recent_seen_titles(
            limit=limit,
            exclude_source_type=self.definition.source_type,
        )
        if not existing_titles:
            return False

        try:
            verdict = await generator.find_duplicate_title(candidate.title, existing_titles)
        except Exception:
            logger.exception(
                "Cross-source dedup check failed for %s; treating it as unique",
                candidate.source_url,
            )
            return False

        if verdict.is_duplicate:
            logger.info(
                "Skipping cross-source duplicate %r (matches %r) from %s",
                candidate.title,
                verdict.matched_title,
                candidate.source_url,
            )

        return verdict.is_duplicate

    async def _create_moderation_submissions(
        self,
        candidate: DraftCandidate,
        generator: GeminiDraftGenerator,
        stats: CollectionStats,
    ) -> bool:
        source_url = candidate.source_url
        try:
            max_part_length = 900 if candidate.has_media else 1600
            datetime_notes = build_utc_time_conversion_notes(
                candidate.body_text,
                article_date=candidate.article_date,
                target_timezone=self.config.article_timezone,
            )
            draft_package = await generator.generate_draft_package(
                GeminiDraftInput(
                    title=candidate.title,
                    article_url=source_url,
                    article_date_display=candidate.article_date_display,
                    datetime_notes=datetime_notes,
                    body_text=candidate.body_text,
                    source_type=self.definition.source_type,
                    source_name=candidate.source_name,
                ),
                max_part_length=max_part_length,
            )
            draft_parts = draft_package.draft_parts
            stats.drafts_created += len(draft_parts)
        except Exception as exc:
            stats.failed += 1
            message = t("collector_report.gemini_failed", url=source_url, error=exc)
            stats.errors.append(message)
            logger.exception("Gemini draft generation failed for %s", source_url)
            return False

        album_images = _album_images(candidate)
        try:
            # A single-part draft with 2+ photos becomes ONE grouped album post
            # (e.g. a Bluesky post with several infographics). Multi-part article
            # drafts keep the per-part image mapping; text / single-image stay as is.
            if len(draft_parts) == 1 and len(album_images) >= 2:
                submission_id = await self.db.create_album_submission(
                    username=candidate.username,
                    original_text=candidate.original_text,
                    caption=draft_parts[0],
                    image_urls=album_images,
                    source_type=self.definition.source_type,
                    source_id=candidate.source_id,
                    source_url=source_url,
                    article_date=candidate.article_date,
                    article_date_display=candidate.article_date_display,
                    tags=draft_package.tags,
                )
            else:
                submission_id = await self.db.create_ai_news_submission(
                    username=candidate.username,
                    original_text=candidate.original_text,
                    draft_text=draft_parts[0],
                    draft_parts=draft_parts,
                    message_type="photo" if candidate.has_media else "text",
                    media_url=candidate.media_url if candidate.has_media else None,
                    media_type=candidate.media_type if candidate.has_media else "none",
                    source_type=self.definition.source_type,
                    source_id=candidate.source_id,
                    source_url=source_url,
                    article_date=candidate.article_date,
                    article_date_display=candidate.article_date_display,
                    tags=draft_package.tags,
                    additional_media_urls=candidate.additional_media_urls if candidate.has_media else None,
                )
            # Space this send from the previous one so a tick with many new items
            # does not flood the moderation chat. No-op for the first/manual send.
            await self.throttle.wait()
            await send_submission_to_moderation(self.bot, self.config, self.db, submission_id)
            stats.sent_to_moderation += 1
        except Exception as exc:
            stats.failed += 1
            message = t("collector_report.moderation_send_failed", url=source_url, error=exc)
            stats.errors.append(message)
            logger.exception(
                "Failed to send %s article to moderation: %s",
                self.definition.collector_id,
                source_url,
            )
            return False

        await self.db.mark_source_seen(
            source_type=self.definition.source_type,
            source_id=candidate.source_id,
            source_url=source_url,
            title=candidate.title,
            article_date=candidate.article_date,
        )
        logger.info(
            "Created moderation draft for %s article %s",
            self.definition.collector_id,
            source_url,
        )
        return True


def _album_images(candidate: DraftCandidate) -> list[str]:
    """The candidate's photo URLs (primary + additional), for deciding whether to
    publish as one album. Empty unless the candidate carries 'photo' media."""
    if not (candidate.has_media and candidate.media_type == "photo" and candidate.media_url):
        return []
    return [candidate.media_url, *(candidate.additional_media_urls or [])]


def _is_newer_or_same_article_date(article_date: str | None, latest_seen_article_date: str) -> bool:
    if article_date is None:
        return True

    # The official source emits mixed granularity: a normally-parsed article
    # carries a full timestamp, but one whose detail page failed to load falls
    # back to a date-only card date. Comparing a date-only midnight against a
    # same-day afternoon timestamp would wrongly drop genuinely-new news, so when
    # EITHER side lacks a time component compare only the (local) calendar dates.
    if not _has_time_component(article_date) or not _has_time_component(latest_seen_article_date):
        new_date = _article_local_date(article_date)
        seen_date = _article_local_date(latest_seen_article_date)
        if new_date is None or seen_date is None:
            return True
        return new_date >= seen_date

    parsed_article_date = _parse_sortable_article_date(article_date)
    parsed_latest_date = _parse_sortable_article_date(latest_seen_article_date)
    if parsed_article_date is None or parsed_latest_date is None:
        return True

    return parsed_article_date >= parsed_latest_date


def _has_time_component(value: str) -> bool:
    return "T" in value


def _article_local_date(value: str) -> date | None:
    """The calendar date as the source intended it, taken from the raw string's
    leading ``YYYY-MM-DD`` so it is never shifted across midnight by a UTC
    conversion (which would defeat a same-day comparison)."""
    try:
        return date.fromisoformat(value.split("T", 1)[0])
    except ValueError:
        return None


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
