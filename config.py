from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(ValueError):
    """Raised when required environment configuration is missing or invalid."""


DEFAULT_YOUTUBE_CHANNEL_ID = "UCWzmOSSiSPbVnVu3ZAyDx2w"  # official @MarvelRivals
# Title keywords (lowercased) that mark a YouTube upload as noise to skip. Scoped
# narrowly to genuine ESPORTS-VOD coverage — NOT "tournament"/"vs"/Shorts, because
# the channel ships costume/event/game-mode reveal trailers as Shorts titled e.g.
# "Shenloong Tournament Event Trailer" or "18 vs. 18 ... New Game Mode Trailer",
# which are exactly the content we want. Matched as a case-insensitive substring.
DEFAULT_YOUTUBE_EXCLUDE_KEYWORDS = frozenset(
    {
        "grand final",
        "group stage",
        "playoff",
        "qualifier",
        "invitational",
        "highlights",
        "esports",
    }
)

DEFAULT_REDDIT_SUBREDDIT = "MarvelRivalsLeaks"
# Flairs routed via search.rss (entries carry no flair text, so the query IS the
# filter). These are the image-heavy datamine flairs; combined with OR in one request.
DEFAULT_REDDIT_FLAIRS = ("Official News", "Reliable", "Confirmed")
# Recurring discussion/sticky threads to skip — matched as a case-insensitive
# substring of the title.
DEFAULT_REDDIT_EXCLUDE_KEYWORDS = frozenset({"megathread"})

DEFAULT_FANART_SUBREDDIT = "MarvelRivals"
DEFAULT_FANART_FLAIR = "Fan Art"
# Friday (Mon=0 .. Sun=6) at 18:00 local time (ARTICLE_TIMEZONE).
DEFAULT_FANART_DIGEST_WEEKDAY = 4
DEFAULT_FANART_DIGEST_HOUR = 18
# Telegram media groups allow at most 10 items.
DEFAULT_FANART_DIGEST_COUNT = 10


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_chat_id: int
    publish_chat_id: int
    admin_user_ids: frozenset[int]
    database_path: str = "bot.db"
    submission_cooldown_seconds: int = 120
    # Anti-spam guard for user-submitted plain text: a text submission with fewer
    # than this many words OR characters is bounced back with a hint instead of
    # being queued. Links and media submissions are exempt (their URL/file is the
    # content). Setting either to 0 disables that half of the check.
    min_submission_text_words: int = 3
    min_submission_text_chars: int = 10
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    official_news_url: str = "https://www.marvelrivals.com/news/"
    news_check_interval_minutes: int | None = 30
    article_timezone: str = "Europe/Kyiv"
    # Discord moderation module (optional, fully independent from the Telegram flow).
    enable_discord_moderation: bool = False
    discord_bot_token: str | None = None
    discord_mod_log_channel_id: int | None = None
    discord_allowed_invites: frozenset[str] = frozenset()
    discord_guild_id: int | None = None
    # Optional welcome system + channels referenced in the welcome message.
    # (Rules are shown inline from discord_rules.txt, not via a channel link.)
    discord_welcome_channel_id: int | None = None
    discord_chat_channel_id: int | None = None
    discord_lft_channel_id: int | None = None
    # Telegram group-chat moderation module (optional, runs in the same process).
    # Like the Discord block, every value is parsed leniently so a typo can never
    # raise ConfigError and stop the bot from starting.
    enable_telegram_moderation: bool = False
    telegram_moderation_chat_ids: frozenset[int] = frozenset()
    # Where moderation actions are logged. Falls back to admin_chat_id at use-time.
    telegram_mod_log_chat_id: int | None = None
    # Permitted t.me link codes/usernames (lowercased). Empty = block all t.me links.
    telegram_link_allowlist: frozenset[str] = frozenset()
    # Seconds before the welcome message auto-deletes. 0 disables auto-delete.
    telegram_welcome_delete_seconds: int = 60
    # Nightly backup: the SQLite file is snapshotted once a day into backups/
    # next to the database; the newest N snapshots are kept. Parsed leniently —
    # a typo never stops the bot.
    enable_database_backup: bool = True
    database_backup_hour: int = 4
    database_backup_keep: int = 14
    # Cross-source semantic dedup. Before queuing a parsed article, ask Gemini
    # whether its title already matches a recently-seen title (from any source),
    # so the same story surfaced via several sources is not re-posted twice.
    # Needs GEMINI_API_KEY; fails open (keeps the item) on any error. Parsed
    # leniently. The limit caps how many recent titles are compared (0 disables).
    enable_cross_source_dedup: bool = True
    cross_source_dedup_title_limit: int = 200
    # Seconds the scheduler waits BETWEEN consecutive moderation-queue sends so a
    # tick that finds many new items trickles them in instead of dumping a burst
    # into the admin chat. This is a MINIMUM gap: the first/isolated send pays
    # nothing, only a rapid follow-up waits out the remainder. 0 disables it.
    moderation_send_interval_seconds: int = 5
    # Bluesky as a news source (opt-in). When enabled, the bot polls the public
    # author feed of BLUESKY_ACTOR and queues new posts for moderation, exactly
    # like the official-site collector. Parsed leniently.
    enable_bluesky_source: bool = False
    bluesky_actor: str = "marvelrivalsglobal.bsky.social"
    # YouTube as a news source (opt-in). When enabled, the bot polls the public RSS
    # feed of YOUTUBE_CHANNEL_ID and queues new videos for moderation. The channel
    # re-uploads the same trailer under several video IDs, so items are deduped by a
    # NORMALISED TITLE rather than the video id. YOUTUBE_EXCLUDE_KEYWORDS drops esports
    # VODs / Shorts whose title contains any listed keyword. Parsed leniently.
    enable_youtube_source: bool = False
    youtube_channel_id: str = DEFAULT_YOUTUBE_CHANNEL_ID
    youtube_exclude_keywords: frozenset[str] = DEFAULT_YOUTUBE_EXCLUDE_KEYWORDS
    # Download the YouTube video (yt-dlp) and re-upload it as a NATIVE Telegram
    # video so it plays inline, instead of a tap-to-open link preview. Only videos
    # within YOUTUBE_VIDEO_MAX_MB (Telegram's bot upload limit) are sent natively;
    # larger ones fall back to the link preview. Set false to always use the preview.
    enable_youtube_video_download: bool = True
    youtube_video_max_mb: int = 48
    # Reddit leaks as a news source (opt-in). When enabled, the bot polls ONE
    # combined flair-filtered search.rss feed of REDDIT_SUBREDDIT and queues new
    # posts for moderation as RUMOURS (the short-form prompt frames them as чутки).
    # Reddit rate-limits hard, so this deliberately makes a single request per run.
    # Parsed leniently.
    enable_reddit_source: bool = False
    reddit_subreddit: str = DEFAULT_REDDIT_SUBREDDIT
    reddit_flairs: tuple[str, ...] = DEFAULT_REDDIT_FLAIRS
    reddit_exclude_keywords: frozenset[str] = DEFAULT_REDDIT_EXCLUDE_KEYWORDS
    # Weekly fan-art digest (opt-in): once a week the bot pulls the top "Fan Art"
    # posts of the past week from FANART_SUBREDDIT and queues ONE album post (the
    # top images + a credited caption) for moderation. Runs on its own weekday/hour
    # schedule, independent of the per-tick collectors. Parsed leniently.
    enable_fanart_digest: bool = False
    fanart_subreddit: str = DEFAULT_FANART_SUBREDDIT
    fanart_flair: str = DEFAULT_FANART_FLAIR
    fanart_digest_weekday: int = DEFAULT_FANART_DIGEST_WEEKDAY
    fanart_digest_hour: int = DEFAULT_FANART_DIGEST_HOUR
    fanart_digest_count: int = DEFAULT_FANART_DIGEST_COUNT


def load_config() -> Config:
    load_dotenv()

    bot_token = _required("BOT_TOKEN")
    admin_chat_id = _required_int("ADMIN_CHAT_ID")
    publish_chat_id = _required_int("PUBLISH_CHAT_ID")
    admin_user_ids = _parse_admin_user_ids(_required("ADMIN_USER_IDS"))
    database_path = os.getenv("DATABASE_PATH", "bot.db").strip() or "bot.db"
    submission_cooldown_seconds = _optional_non_negative_int("SUBMISSION_COOLDOWN_SECONDS", 120)
    min_submission_text_words = _optional_non_negative_int("MIN_SUBMISSION_TEXT_WORDS", 3)
    min_submission_text_chars = _optional_non_negative_int("MIN_SUBMISSION_TEXT_CHARS", 10)
    gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip() or None
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
    official_news_url = os.getenv("OFFICIAL_NEWS_URL", "https://www.marvelrivals.com/news/").strip()
    official_news_url = official_news_url or "https://www.marvelrivals.com/news/"
    news_check_interval_minutes = _optional_positive_int_or_none("NEWS_CHECK_INTERVAL_MINUTES", 30)
    article_timezone = os.getenv("ARTICLE_TIMEZONE", "Europe/Kyiv").strip() or "Europe/Kyiv"

    # Discord values are parsed leniently: a misconfigured Discord setting must never
    # raise ConfigError, because that would also stop the Telegram bot from starting.
    enable_discord_moderation = _optional_bool("ENABLE_DISCORD_MODERATION", False)
    discord_bot_token = os.getenv("DISCORD_BOT_TOKEN", "").strip() or None
    discord_mod_log_channel_id = _optional_int_or_none("DISCORD_MOD_LOG_CHANNEL_ID")
    discord_allowed_invites = _parse_invite_allowlist("DISCORD_ALLOWED_INVITES")
    discord_guild_id = _optional_int_or_none("DISCORD_GUILD_ID")
    discord_welcome_channel_id = _optional_int_or_none("DISCORD_WELCOME_CHANNEL_ID")
    discord_chat_channel_id = _optional_int_or_none("DISCORD_CHAT_CHANNEL_ID")
    discord_lft_channel_id = _optional_int_or_none("DISCORD_LFT_CHANNEL_ID")

    # Telegram moderation values are parsed leniently for the same reason as Discord:
    # a misconfigured moderation setting must never stop the whole bot from starting.
    enable_telegram_moderation = _optional_bool("ENABLE_TELEGRAM_MODERATION", False)
    telegram_moderation_chat_ids = _parse_int_id_set("TELEGRAM_MODERATION_CHAT_IDS")
    telegram_mod_log_chat_id = _optional_int_or_none("TELEGRAM_MOD_LOG_CHAT_ID")
    # The link allowlist reuses the invite parser: it strips full URLs down to the
    # trailing code and lowercases it, so "https://t.me/UAMarvelRivalsChat" -> "uamarvelrivalschat".
    telegram_link_allowlist = _parse_invite_allowlist("TELEGRAM_LINK_ALLOWLIST")
    telegram_welcome_delete_seconds = _optional_lenient_non_negative_int("TELEGRAM_WELCOME_DELETE_SECONDS", 60)

    enable_database_backup = _optional_bool("ENABLE_DATABASE_BACKUP", True)
    database_backup_hour = _optional_hour("DATABASE_BACKUP_HOUR", 4)
    # `or 14`: zero would mean "keep nothing", which can only be a mistake.
    database_backup_keep = _optional_lenient_non_negative_int("DATABASE_BACKUP_KEEP", 14) or 14

    enable_cross_source_dedup = _optional_bool("ENABLE_CROSS_SOURCE_DEDUP", True)
    cross_source_dedup_title_limit = _optional_lenient_non_negative_int(
        "CROSS_SOURCE_DEDUP_TITLE_LIMIT", 200
    )
    moderation_send_interval_seconds = _optional_lenient_non_negative_int(
        "MODERATION_SEND_INTERVAL_SECONDS", 5
    )

    enable_bluesky_source = _optional_bool("ENABLE_BLUESKY_SOURCE", False)
    bluesky_actor = os.getenv("BLUESKY_ACTOR", "marvelrivalsglobal.bsky.social").strip()
    bluesky_actor = bluesky_actor or "marvelrivalsglobal.bsky.social"

    enable_youtube_source = _optional_bool("ENABLE_YOUTUBE_SOURCE", False)
    youtube_channel_id = os.getenv("YOUTUBE_CHANNEL_ID", DEFAULT_YOUTUBE_CHANNEL_ID).strip()
    youtube_channel_id = youtube_channel_id or DEFAULT_YOUTUBE_CHANNEL_ID
    youtube_exclude_keywords = _parse_keyword_set("YOUTUBE_EXCLUDE_KEYWORDS", DEFAULT_YOUTUBE_EXCLUDE_KEYWORDS)
    enable_youtube_video_download = _optional_bool("ENABLE_YOUTUBE_VIDEO_DOWNLOAD", True)
    youtube_video_max_mb = _optional_lenient_non_negative_int("YOUTUBE_VIDEO_MAX_MB", 48) or 48

    enable_reddit_source = _optional_bool("ENABLE_REDDIT_SOURCE", False)
    reddit_subreddit = os.getenv("REDDIT_SUBREDDIT", DEFAULT_REDDIT_SUBREDDIT).strip() or DEFAULT_REDDIT_SUBREDDIT
    reddit_flairs = _parse_flairs("REDDIT_FLAIRS", DEFAULT_REDDIT_FLAIRS)
    reddit_exclude_keywords = _parse_keyword_set("REDDIT_EXCLUDE_KEYWORDS", DEFAULT_REDDIT_EXCLUDE_KEYWORDS)

    enable_fanart_digest = _optional_bool("ENABLE_FANART_DIGEST", False)
    fanart_subreddit = os.getenv("FANART_SUBREDDIT", DEFAULT_FANART_SUBREDDIT).strip() or DEFAULT_FANART_SUBREDDIT
    fanart_flair = os.getenv("FANART_FLAIR", DEFAULT_FANART_FLAIR).strip() or DEFAULT_FANART_FLAIR
    fanart_digest_weekday = _optional_weekday("FANART_DIGEST_WEEKDAY", DEFAULT_FANART_DIGEST_WEEKDAY)
    fanart_digest_hour = _optional_hour("FANART_DIGEST_HOUR", DEFAULT_FANART_DIGEST_HOUR)
    # Clamp the count to Telegram's media-group max of 10; 0/invalid keeps the default.
    fanart_digest_count = min(
        _optional_lenient_non_negative_int("FANART_DIGEST_COUNT", DEFAULT_FANART_DIGEST_COUNT) or DEFAULT_FANART_DIGEST_COUNT,
        10,
    )

    return Config(
        bot_token=bot_token,
        admin_chat_id=admin_chat_id,
        publish_chat_id=publish_chat_id,
        admin_user_ids=admin_user_ids,
        database_path=database_path,
        submission_cooldown_seconds=submission_cooldown_seconds,
        min_submission_text_words=min_submission_text_words,
        min_submission_text_chars=min_submission_text_chars,
        gemini_api_key=gemini_api_key,
        gemini_model=gemini_model,
        official_news_url=official_news_url,
        news_check_interval_minutes=news_check_interval_minutes,
        article_timezone=article_timezone,
        enable_discord_moderation=enable_discord_moderation,
        discord_bot_token=discord_bot_token,
        discord_mod_log_channel_id=discord_mod_log_channel_id,
        discord_allowed_invites=discord_allowed_invites,
        discord_guild_id=discord_guild_id,
        discord_welcome_channel_id=discord_welcome_channel_id,
        discord_chat_channel_id=discord_chat_channel_id,
        discord_lft_channel_id=discord_lft_channel_id,
        enable_telegram_moderation=enable_telegram_moderation,
        telegram_moderation_chat_ids=telegram_moderation_chat_ids,
        telegram_mod_log_chat_id=telegram_mod_log_chat_id,
        telegram_link_allowlist=telegram_link_allowlist,
        telegram_welcome_delete_seconds=telegram_welcome_delete_seconds,
        enable_database_backup=enable_database_backup,
        database_backup_hour=database_backup_hour,
        database_backup_keep=database_backup_keep,
        enable_cross_source_dedup=enable_cross_source_dedup,
        cross_source_dedup_title_limit=cross_source_dedup_title_limit,
        moderation_send_interval_seconds=moderation_send_interval_seconds,
        enable_bluesky_source=enable_bluesky_source,
        bluesky_actor=bluesky_actor,
        enable_youtube_source=enable_youtube_source,
        youtube_channel_id=youtube_channel_id,
        youtube_exclude_keywords=youtube_exclude_keywords,
        enable_youtube_video_download=enable_youtube_video_download,
        youtube_video_max_mb=youtube_video_max_mb,
        enable_reddit_source=enable_reddit_source,
        reddit_subreddit=reddit_subreddit,
        reddit_flairs=reddit_flairs,
        reddit_exclude_keywords=reddit_exclude_keywords,
        enable_fanart_digest=enable_fanart_digest,
        fanart_subreddit=fanart_subreddit,
        fanart_flair=fanart_flair,
        fanart_digest_weekday=fanart_digest_weekday,
        fanart_digest_hour=fanart_digest_hour,
        fanart_digest_count=fanart_digest_count,
    )


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ConfigError(f"Missing required environment variable: {name}")
    return value.strip()


def _required_int(name: str) -> int:
    value = _required(name)
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _optional_non_negative_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default

    try:
        parsed = int(value.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc

    if parsed < 0:
        raise ConfigError(f"{name} must be 0 or greater")

    return parsed


def _optional_positive_int_or_none(name: str, default: int | None) -> int | None:
    value = os.getenv(name)
    if value is None:
        return default

    if not value.strip():
        return None

    try:
        parsed = int(value.strip())
    except ValueError:
        return None

    if parsed <= 0:
        return None

    return parsed


_TRUE_VALUES = {"true", "1", "yes", "on"}
_FALSE_VALUES = {"false", "0", "no", "off"}


def _optional_bool(name: str, default: bool) -> bool:
    """Parse an optional boolean, falling back to the default on typos.

    Unrecognized values keep the default (like the other lenient parsers) so a
    misspelt value can never silently flip a default-on feature like the
    nightly backup off — explicit "false"/"0"/"no"/"off" is required for that.
    """
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    token = value.strip().lower()
    if token in _TRUE_VALUES:
        return True
    if token in _FALSE_VALUES:
        return False
    return default


def _optional_int_or_none(name: str) -> int | None:
    """Parse an optional integer, returning None when missing, empty, or invalid.

    Used for Discord settings so a typo cannot crash the Telegram bot at startup.
    """
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


def _optional_lenient_non_negative_int(name: str, default: int) -> int:
    """Parse an optional non-negative int, returning the default on missing/invalid.

    Unlike _optional_non_negative_int, this never raises ConfigError — used for
    Telegram moderation settings so a typo cannot stop the whole bot from starting.
    """
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


def _optional_hour(name: str, default: int) -> int:
    """Parse an hour of day (0-23), returning the default on missing/invalid.

    Lenient for the same reason as the other backup/moderation settings: a typo
    must never stop the whole bot from starting.
    """
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        return default
    return parsed if 0 <= parsed <= 23 else default


def _optional_weekday(name: str, default: int) -> int:
    """Parse a weekday (0=Mon .. 6=Sun), returning the default on missing/invalid.

    Lenient like the other scheduler settings so a typo never stops the bot.
    """
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value.strip())
    except ValueError:
        return default
    return parsed if 0 <= parsed <= 6 else default


def _parse_flairs(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Parse a comma-separated Reddit flair list (case preserved) into a tuple.

    Missing or empty keeps the ``default``; an explicit value REPLACES it. Used to
    build the combined ``flair:"A" OR flair:"B"`` search query.
    """
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    flairs = tuple(part.strip() for part in value.split(",") if part.strip())
    return flairs or default


def _parse_keyword_set(name: str, default: frozenset[str]) -> frozenset[str]:
    """Parse a comma-separated, case-insensitive keyword list for title filtering.

    Missing or empty keeps the ``default``; an explicit value REPLACES it. A lone
    ``-`` or ``none`` yields an empty set — i.e. "disable filtering" without a code
    change. Keywords are casefolded so matching is case-insensitive.
    """
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    if value.strip().lower() in {"-", "none"}:
        return frozenset()
    keywords = {part.strip().casefold() for part in value.split(",") if part.strip()}
    return frozenset(keywords) or default


def _parse_int_id_set(name: str) -> frozenset[int]:
    """Parse a comma-separated list of chat IDs leniently into a frozenset[int].

    Blank or non-integer entries are skipped rather than raising, so one bad value
    in TELEGRAM_MODERATION_CHAT_IDS cannot stop the bot from starting.
    """
    raw = os.getenv(name, "")
    ids: set[int] = set()
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            ids.add(int(token))
        except ValueError:
            continue
    return frozenset(ids)


def _parse_invite_allowlist(name: str) -> frozenset[str]:
    """Normalise DISCORD_ALLOWED_INVITES into a set of lowercase invite codes.

    Accepts bare codes (``abc123``) or full URLs (``https://discord.gg/abc123``)
    and keeps only the trailing code. An empty value means "block every invite".
    """
    raw = os.getenv(name, "")
    codes: set[str] = set()
    for part in raw.split(","):
        token = part.strip().rstrip("/")
        if not token:
            continue
        if "/" in token:
            token = token.rsplit("/", 1)[-1]
        if token:
            codes.add(token.lower())
    return frozenset(codes)


def _parse_admin_user_ids(value: str) -> frozenset[int]:
    raw_ids = [part.strip() for part in value.split(",") if part.strip()]
    if not raw_ids:
        raise ConfigError("ADMIN_USER_IDS must contain at least one Telegram user ID")

    admin_ids: set[int] = set()
    for raw_id in raw_ids:
        try:
            admin_ids.add(int(raw_id))
        except ValueError as exc:
            raise ConfigError("ADMIN_USER_IDS must be comma-separated integers") from exc

    return frozenset(admin_ids)
