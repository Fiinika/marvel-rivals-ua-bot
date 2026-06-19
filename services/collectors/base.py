from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from services.i18n import t


class CollectionMode(str, Enum):
    MANUAL_LATEST = "manual_latest"
    SCHEDULED_SINCE_LAST = "scheduled_since_last"


@dataclass(frozen=True)
class CollectorDefinition:
    collector_id: str
    source_type: str
    title_key: str
    button_key: str

    @property
    def title(self) -> str:
        return t(self.title_key)

    @property
    def button_text(self) -> str:
        return t(self.button_key)


@dataclass
class CollectionStats:
    collector_id: str
    source_type: str
    source_title: str
    found: int = 0
    duplicates: int = 0
    new: int = 0
    drafts_created: int = 0
    sent_to_moderation: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ListingEntry:
    """A lightweight listing entry produced before the full article is fetched.

    ``dedup_key`` is the source-scoped identifier used for the first
    ``is_source_seen`` check; ``payload`` carries the source-specific summary
    object that ``parse_entry`` needs to fetch and parse the full item.
    """

    dedup_key: str
    payload: object = None


@dataclass(frozen=True)
class DraftCandidate:
    """Normalized, source-agnostic input for one moderation submission."""

    source_id: str
    source_url: str
    title: str
    body_text: str
    source_name: str
    username: str
    original_text: str
    article_date: str | None = None
    article_date_display: str | None = None
    has_media: bool = False
    media_url: str | None = None
    media_type: str = "none"
    additional_media_urls: list[str] | None = None


class NewsCollector(Protocol):
    definition: CollectorDefinition

    async def run_once(self, mode: CollectionMode = CollectionMode.MANUAL_LATEST) -> CollectionStats:
        """Run one collection pass."""


def empty_stats(definition: CollectorDefinition) -> CollectionStats:
    return CollectionStats(
        collector_id=definition.collector_id,
        source_type=definition.source_type,
        source_title=definition.title,
    )


def format_collection_report(stats: CollectionStats) -> str:
    lines = [
        t("collector_report.completed", source=stats.source_title),
        "",
        t("collector_report.found", count=stats.found),
        t("collector_report.duplicates", count=stats.duplicates),
        t("collector_report.new", count=stats.new),
        t("collector_report.drafts_created", count=stats.drafts_created),
        t("collector_report.sent_to_moderation", count=stats.sent_to_moderation),
        t("collector_report.failed", count=stats.failed),
    ]

    if stats.errors:
        lines.append("")
        lines.append(t("collector_report.errors_header"))
        lines.extend(f"- {error}" for error in stats.errors[:5])

    return "\n".join(lines)


def format_collection_reports(stats_list: list[CollectionStats]) -> str:
    if len(stats_list) == 1:
        return format_collection_report(stats_list[0])

    reports = [t("collector_report.all_completed")]
    reports.extend(format_collection_report(stats) for stats in stats_list)
    return "\n\n".join(reports)
