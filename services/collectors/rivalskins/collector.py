from __future__ import annotations

import logging

from aiogram import Bot

from config import Config
from database import Database
from services.collectors.base import CollectorDefinition, DraftCandidate, ListingEntry
from services.collectors.rivalskins.feed_fetcher import RivalSkinsFeedFetcher, RivalSkinsPost
from services.collectors.runner import BaseNewsCollector
from services.i18n import t


logger = logging.getLogger(__name__)

COLLECTOR_ID = "rivalskins"
SOURCE_TYPE = "rivalskins"

TITLE_MAX_LENGTH = 140
FALLBACK_TITLE = "Витік скіна Marvel Rivals (RivalSkins)"

DEFINITION = CollectorDefinition(
    collector_id=COLLECTOR_ID,
    source_type=SOURCE_TYPE,
    title_key="collectors.rivalskins.title",
    button_key="buttons.collector_rivalskins",
)


class RivalSkinsCollector(BaseNewsCollector):
    definition = DEFINITION

    def __init__(self, *, config: Config, db: Database, bot: Bot) -> None:
        super().__init__(config=config, db=db, bot=bot)
        self.fetcher = RivalSkinsFeedFetcher(config.rivalskins_feed_url)

    def missing_gemini_warning(self) -> str:
        return t("collectors.rivalskins.errors.missing_gemini_api_key")

    async def fetch_listing(self) -> list[ListingEntry]:
        posts = await self.fetcher.fetch_recent_posts()
        return [
            ListingEntry(dedup_key=post.post_id, payload=post)
            for post in posts
            if post.title or post.body_text
        ]

    async def parse_entry(self, entry: ListingEntry) -> DraftCandidate:
        post: RivalSkinsPost = entry.payload  # type: ignore[assignment]
        has_media = bool(post.image_url)
        title = _derive_title(post.title)

        return DraftCandidate(
            source_id=post.post_id,
            source_url=post.web_url,
            title=title,
            body_text=post.body_text,
            source_name=t("collectors.rivalskins.source_name"),
            username=t("collectors.rivalskins.username"),
            original_text=_build_original_text(post, title),
            article_date=post.created_at,
            article_date_display=_display_date(post.created_at),
            has_media=has_media,
            media_url=post.image_url if has_media else None,
            media_type="photo" if has_media else "none",
            additional_media_urls=None,
        )


def _derive_title(title: str) -> str:
    stripped = title.strip()
    if not stripped:
        return FALLBACK_TITLE

    if len(stripped) <= TITLE_MAX_LENGTH:
        return stripped

    truncated = stripped[:TITLE_MAX_LENGTH].rsplit(" ", 1)[0].strip()
    return f"{truncated or stripped[:TITLE_MAX_LENGTH].strip()}…"


def _display_date(created_at: str | None) -> str | None:
    if not created_at:
        return None

    return created_at[:10]


def _build_original_text(post: RivalSkinsPost, title: str) -> str:
    parts = [
        t("collectors.common.original_text.article_title", value=title),
        t("collectors.common.original_text.article_url", value=post.web_url),
    ]

    if post.created_at:
        parts.append(t("collectors.common.original_text.original_article_date", value=post.created_at))

    if post.body_text:
        parts.extend(["", t("collectors.common.original_text.parsed_article_text"), post.body_text])

    if post.image_url:
        parts.extend(
            [
                "",
                t("collectors.common.original_text.media_url", value=post.image_url),
                t("collectors.common.original_text.media_type", value="photo"),
            ]
        )

    return "\n".join(parts).strip()
