"""Weekly fan-art digest.

Once a week (FANART_DIGEST_WEEKDAY at FANART_DIGEST_HOUR, ARTICLE_TIMEZONE) the
bot pulls the top "Fan Art" posts of the past week from FANART_SUBREDDIT and
queues ONE album submission — the top images plus a credited caption — into the
normal moderation queue. A per-ISO-week seen-key guards against double posting,
exactly like the collectors' source dedup; failures are logged and retried next
week, mirroring the news and backup schedulers.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone, tzinfo
from html import escape
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from aiogram import Bot

from config import Config
from database import Database
from services.collectors.reddit.feed_fetcher import RedditPost, RedditSearchFetcher
from services.i18n import t
from services.moderation import send_submission_to_moderation
from services.post_footer import format_community_footer_html


logger = logging.getLogger(__name__)

SOURCE_TYPE = "reddit_fanart"


def start_fanart_digest_scheduler(bot: Bot, config: Config, db: Database) -> asyncio.Task | None:
    """Start the weekly digest loop, or return None when the feature is off."""
    if not config.enable_fanart_digest:
        logger.info("Weekly fan-art digest is disabled (ENABLE_FANART_DIGEST).")
        return None

    logger.info(
        "Weekly fan-art digest enabled: weekday %s at %02d:00 %s, top %s from r/%s",
        config.fanart_digest_weekday,
        config.fanart_digest_hour,
        config.article_timezone,
        config.fanart_digest_count,
        config.fanart_subreddit,
    )
    return asyncio.create_task(_digest_loop(bot, config, db), name="fanart-digest-scheduler")


async def _digest_loop(bot: Bot, config: Config, db: Database) -> None:
    tz = _resolve_timezone(config.article_timezone)
    while True:
        now = datetime.now(tz)
        next_run = next_weekly_run_at(now, config.fanart_digest_weekday, config.fanart_digest_hour)
        await asyncio.sleep(max(1.0, (next_run - now).total_seconds()))
        try:
            await run_fanart_digest_once(bot, config, db)
        except Exception:
            logger.exception("Weekly fan-art digest failed; retrying next week.")


def next_weekly_run_at(now: datetime, weekday: int, hour: int) -> datetime:
    """The next occurrence of ``weekday`` (Mon=0) at ``hour``:00 strictly after now."""
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    days_ahead = (weekday - now.weekday()) % 7
    candidate += timedelta(days=days_ahead)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


async def run_fanart_digest_once(
    bot: Bot,
    config: Config,
    db: Database,
    *,
    now: datetime | None = None,
    force: bool = False,
) -> bool:
    """Build and queue this week's digest. Returns True when a submission was
    created, False when it was skipped (already done this week, or no art found).

    ``force`` bypasses the per-week guard so an admin can trigger a digest on
    demand (e.g. for testing) even if this week was already queued."""
    tz = _resolve_timezone(config.article_timezone)
    current = now or datetime.now(tz)
    week_key = _iso_week_key(current)

    if not force and await db.is_source_seen(SOURCE_TYPE, week_key):
        logger.info("Fan-art digest for %s already queued; skipping.", week_key)
        return False

    fetcher = RedditSearchFetcher(
        config.fanart_subreddit,
        [config.fanart_flair],
        sort="top",
        time_filter="week",
        limit=max(config.fanart_digest_count * 2, config.fanart_digest_count),
    )
    posts = await fetcher.fetch_recent_posts()
    # A media group is atomic — ONE URL Telegram can't fetch fails the whole album.
    # So include only direct i.redd.it images, which Telegram reliably fetches.
    # The fetcher already upgrades preview.redd.it thumbnails to their full-res
    # i.redd.it original, so only posts with no Reddit-hosted image at all
    # (gallery/video/external-link previews) are skipped here.
    arts = [post for post in posts if _is_direct_image(post.image_url)][: config.fanart_digest_count]
    if not arts:
        logger.warning("Fan-art digest for %s found no direct-image posts; not marking the week done.", week_key)
        return False

    subreddit_url = f"https://www.reddit.com/r/{config.fanart_subreddit}/"
    caption = _build_caption(arts, config.fanart_subreddit)
    submission_id = await db.create_album_submission(
        username=t("digests.fanart.username"),
        original_text=_build_original_text(arts, week_key),
        caption=caption,
        image_urls=[str(post.image_url) for post in arts],
        source_type=SOURCE_TYPE,
        source_id=week_key,
        source_url=subreddit_url,
        article_date=current.date().isoformat(),
        article_date_display=None,
        tags=[t("digests.fanart.tag")],
    )
    await send_submission_to_moderation(bot, config, db, submission_id)
    await db.mark_source_seen(
        source_type=SOURCE_TYPE,
        source_id=week_key,
        source_url=subreddit_url,
        title=f"{t('digests.fanart.title')} ({week_key})",
        article_date=current.date().isoformat(),
    )
    logger.info("Queued fan-art digest for %s with %s arts (submission %s)", week_key, len(arts), submission_id)
    return True


def _build_caption(arts: list[RedditPost], subreddit: str) -> str:
    """Build the album caption as HTML (sent raw, not via format_post_html): each
    art is credited by its author's Reddit nick, hyperlinked to the post — no post
    titles — followed by an engagement prompt, the tags and the community footer."""
    lines = [escape(t("digests.fanart.title")), "", escape(t("digests.fanart.intro")), ""]
    for index, post in enumerate(arts, start=1):
        author = _author_handle(post.author) or t("digests.fanart.unknown_author")
        if _is_safe_http_url(post.web_url):
            credit = f'<a href="{escape(post.web_url, quote=True)}">{escape(author)}</a>'
        else:
            credit = escape(author)
        lines.append(f"{index}. {credit}")

    lines.extend(["", escape(t("digests.fanart.engagement"))])
    lines.extend(["", escape(t("digests.fanart.hashtags"))])

    footer = format_community_footer_html()
    if footer:
        lines.extend(["", footer])

    return "\n".join(lines).strip()


def _is_safe_http_url(url: str) -> bool:
    parsed = urlsplit((url or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _build_original_text(arts: list[RedditPost], week_key: str) -> str:
    lines = [f"Фан-арт тижня ({week_key}) — {len(arts)} робіт:", ""]
    for index, post in enumerate(arts, start=1):
        author = _author_handle(post.author)
        lines.append(f"{index}. {post.title} {author} {post.web_url}".strip())
    return "\n".join(lines).strip()


_STILL_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")


def _is_direct_image(url: str | None) -> bool:
    """True only for a direct https://i.redd.it/... STILL image — the host Telegram
    can reliably fetch inside a media group (signed preview/external URLs cannot),
    and a still type the album upload accepts (animated .gif is excluded so an
    all-gif week doesn't create a submission whose every image is then skipped)."""
    if not url:
        return False
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc.lower() != "i.redd.it":
        return False
    return parsed.path.lower().endswith(_STILL_IMAGE_SUFFIXES)


def _author_handle(author: str) -> str:
    name = (author or "").strip().lstrip("/")
    if name.startswith("u/"):
        name = name[2:]
    name = name.strip()
    return f"u/{name}" if name else ""


def _iso_week_key(moment: datetime) -> str:
    iso = moment.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _resolve_timezone(name: str) -> tzinfo:
    try:
        return ZoneInfo(name)
    except Exception:
        logger.warning("Unknown ARTICLE_TIMEZONE %r; using UTC for the digest schedule.", name)
        return timezone.utc
