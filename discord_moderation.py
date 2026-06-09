"""Optional Discord moderation bot for the Marvel Rivals UA community server.

This module is completely independent from the Telegram news/submission/approval
flow. It is started from ``main.py`` as a cancellable asyncio task that runs
alongside the existing aiogram long-polling loop in the same process. It never
calls ``asyncio.run()`` itself, and any failure here is contained so the Telegram
bot keeps running.

It does NOT parse official Marvel Rivals Discord news. It only provides
moderation/utility features on our own community server:

  * anti-spam (rate limit + short timeout)
  * Discord invite filtering
  * suspicious / scam link filtering
  * a small, conservative bad-word filter
  * moderation logging to a configured channel
  * persistent warning history with configurable auto-actions
  * a member /report flow and an optional welcome system for new members
  * slash commands: /help, /clear, /timeout, /warn, /report, /warnings,
    /clearwarnings, /lfthelp (LFT-forum guide for everyone)

------------------------------------------------------------------------------
DISCORD DEVELOPER PORTAL REMINDERS (one-time setup):

1. Privileged "MESSAGE CONTENT INTENT" must be enabled for the bot under
   Developer Portal -> Your App -> Bot -> Privileged Gateway Intents.
   Without it the bot logs in but cannot read message content, so the
   invite/scam/bad-word/anti-spam filters cannot work.

2. Slash commands require the bot to be invited with BOTH the ``bot`` and
   ``applications.commands`` OAuth2 scopes. If the slash commands do not show
   up, re-invite the bot using an OAuth2 URL that includes
   ``scope=bot%20applications.commands``.

3. The bot does NOT need Administrator. Grant only:
   View Channels, Send Messages, Manage Messages, Read Message History,
   Moderate Members, Embed Links, Use Application Commands.

4. The optional welcome system (DISCORD_WELCOME_CHANNEL_ID) needs the privileged
   "SERVER MEMBERS INTENT" enabled in the Developer Portal. It is requested ONLY
   when a welcome channel is configured, so leaving welcome off keeps the bot's
   intents (and behaviour) exactly as they were before.
------------------------------------------------------------------------------
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import time
from collections import defaultdict, deque
from pathlib import Path

import aiosqlite

from config import Config

logger = logging.getLogger(__name__)

# discord.py is an optional dependency. Import it defensively so that a missing
# install (or import error) disables the module instead of crashing the whole
# process when main.py imports this file.
try:
    import discord
    from discord import app_commands
    from discord.ext import commands

    DISCORD_IMPORT_ERROR: str | None = None
except Exception as exc:  # pragma: no cover - depends on environment
    discord = None  # type: ignore[assignment]
    app_commands = None  # type: ignore[assignment]
    commands = None  # type: ignore[assignment]
    DISCORD_IMPORT_ERROR = exc.__class__.__name__


# --------------------------------------------------------------------------- #
# Tunable moderation settings (kept in code on purpose, as requested).
# --------------------------------------------------------------------------- #

# Anti-spam: more than SPAM_MAX_MESSAGES messages from the same user in the same
# channel within SPAM_WINDOW_SECONDS triggers an action.
SPAM_WINDOW_SECONDS = 7.0
SPAM_MAX_MESSAGES = 5
SPAM_TIMEOUT_SECONDS = 60

# Discord's hard maximum timeout is 28 days.
MAX_TIMEOUT_MINUTES = 28 * 24 * 60  # 40320

# Auto-actions after repeated /warn warnings (configurable; kept in code on
# purpose, like the anti-spam thresholds above). Keys are the *active* warning
# count that triggers the action; values are the timeout length in seconds.
# The bot NEVER auto-bans or auto-kicks, and never auto-punishes staff (see
# _is_staff). Each /warn adds exactly one warning, so an exact-count match fires
# every threshold once.
WARN_TIMEOUT_THRESHOLDS: dict[int, int] = {
    3: 10 * 60,  # 3 warnings -> 10-minute timeout
    5: 60 * 60,  # 5 warnings -> 1-hour timeout
}
# At this many warnings, post an urgent mod-log note asking moderators to review
# manually. No automatic ban/kick is ever performed.
WARN_ESCALATION_THRESHOLD = 7

# Bad-word filter. The list itself lives in an external, easy-to-edit text file
# (discord_badwords.txt next to this module) so it can be curated without
# touching Python. It is loaded once at startup; matching uses Unicode-aware word
# boundaries, so terms match only WHOLE words, never substrings of normal words.
_BAD_WORDS_FILE = Path(__file__).resolve().parent / "discord_badwords.txt"

# Minimal fallback used only if discord_badwords.txt is missing/unreadable/empty.
_DEFAULT_BAD_WORDS: tuple[str, ...] = (
    "nigger",
    "faggot",
    "retard",
)


def _load_bad_words() -> tuple[str, ...]:
    try:
        raw = _BAD_WORDS_FILE.read_text(encoding="utf-8")
    except OSError:
        logger.warning(
            "Bad-word list %s not found or unreadable; using built-in defaults.",
            _BAD_WORDS_FILE.name,
        )
        return _DEFAULT_BAD_WORDS
    words: list[str] = []
    for line in raw.splitlines():
        token = line.strip()
        if not token or token.startswith("#"):
            continue
        words.append(token.lower())
    # De-duplicate while preserving order; fall back to defaults if file is empty.
    return tuple(dict.fromkeys(words)) or _DEFAULT_BAD_WORDS


BAD_WORDS: tuple[str, ...] = _load_bad_words()

# Welcome rules. Like the bad-word list, the text lives in an external,
# easy-to-edit file (discord_rules.txt) so the community rules shown in the
# welcome message can be curated without touching Python. One rule per line;
# blank lines and lines starting with "#" are ignored. If the file is missing or
# empty, the welcome simply omits the rules section.
_WELCOME_RULES_FILE = Path(__file__).resolve().parent / "discord_rules.txt"


def _load_welcome_rules() -> tuple[str, ...]:
    try:
        raw = _WELCOME_RULES_FILE.read_text(encoding="utf-8")
    except OSError:
        return ()
    rules: list[str] = []
    for line in raw.splitlines():
        token = line.strip()
        if not token or token.startswith("#"):
            continue
        rules.append(token)
    return tuple(rules)


WELCOME_RULES: tuple[str, ...] = _load_welcome_rules()

# Detect Discord invite links: discord.gg/<code>, discord.com/invite/<code>,
# discordapp.com/invite/<code> (with or without scheme / www).
_INVITE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/([A-Za-z0-9\-]+)",
    re.IGNORECASE,
)

# Always-suspicious scam phrases / phishing look-alike domains. Kept conservative.
_SCAM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"free\s+(?:discord\s+)?nitro", re.IGNORECASE),
    re.compile(r"nitro\s+(?:for\s+)?free", re.IGNORECASE),
    re.compile(r"(?:steam|cs:?go)\s+gift", re.IGNORECASE),
    re.compile(r"free\s+(?:robux|v-?bucks|skins?)\b", re.IGNORECASE),
    re.compile(r"(?:double|2x)\s+your\s+(?:crypto|btc|eth|bitcoin)", re.IGNORECASE),
    re.compile(r"\bcrypto\s+airdrop\b", re.IGNORECASE),
    # Common phishing look-alike domains / typosquats.
    re.compile(
        r"\b(?:dlscord|discrod|dlsc0rd|discord-nitro|discordgift|discordapp\.gift|"
        r"steamcommunlty|steamcommun[il]ty|steamcomunity)\b",
        re.IGNORECASE,
    ),
    # IP-logger / grabber services are effectively always malicious.
    re.compile(r"https?://(?:grabify\.link|iplogger\.\w+|blasze\.\w+)/\S+", re.IGNORECASE),
)

# Generic URL shorteners: only treated as suspicious when combined with a scam
# keyword, to avoid blocking legitimate shortened links.
_SHORTENER_RE = re.compile(
    r"https?://(?:bit\.ly|tinyurl\.com|cutt\.ly|is\.gd|shorturl\.at|t\.co|rb\.gy)/\S+",
    re.IGNORECASE,
)
_SCAM_KEYWORD_RE = re.compile(
    r"\b(?:nitro|free|gift|airdrop|giveaway|claim|reward|prize|crypto|wallet)\b",
    re.IGNORECASE,
)


def _build_bad_word_re() -> re.Pattern[str] | None:
    if not BAD_WORDS:
        return None
    joined = "|".join(re.escape(word) for word in BAD_WORDS)
    # (?<!\w) / (?!\w) act as Unicode-aware word boundaries for str patterns.
    return re.compile(rf"(?<!\w)(?:{joined})(?!\w)", re.IGNORECASE)


_BAD_WORD_RE = _build_bad_word_re()


def _suspicious_reason(content: str) -> str | None:
    """Return a short reason string if the content looks like a scam, else None."""
    for pattern in _SCAM_PATTERNS:
        if pattern.search(content):
            return "Suspicious / scam link or phrase"
    if _SHORTENER_RE.search(content) and _SCAM_KEYWORD_RE.search(content):
        return "Suspicious shortened link"
    return None


def _has_bad_word(content: str) -> bool:
    return _BAD_WORD_RE is not None and _BAD_WORD_RE.search(content) is not None


def _is_staff(member: "discord.Member") -> bool:
    """True for members the bot must never auto-punish (mods / admins / owner)."""
    if discord is None or not isinstance(member, discord.Member):
        return False
    perms = member.guild_permissions
    return perms.administrator or perms.manage_messages or perms.moderate_members


def _format_warn_timestamp(raw: str) -> str:
    """Render a stored ISO-8601 timestamp as a Discord <t:...> markup string.

    Discord localises <t:epoch:f> to each viewer's timezone. Falls back to the
    raw value if it cannot be parsed, so a bad row never breaks /warnings.
    """
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return raw or "—"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return f"<t:{int(parsed.timestamp())}:f>"


class _WarningStore:
    """Persistent Discord warning history stored in the project's SQLite file.

    Kept inside this module so the Discord feature stays self-contained and the
    Telegram database layer (database.py) is untouched. It opens its own
    short-lived aiosqlite connections (with a generous busy timeout) against the
    SAME database file, in a dedicated ``discord_warnings`` table the Telegram
    side never reads. Warning volume is low, so brief write contention is handled
    by SQLite's busy timeout without changing the shared file's journal mode.
    """

    _CONNECT_TIMEOUT = 30.0

    def __init__(self, db_path: str) -> None:
        self._path = db_path

    async def init(self) -> None:
        async with aiosqlite.connect(self._path, timeout=self._CONNECT_TIMEOUT) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS discord_warnings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    moderator_id INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_discord_warnings_guild_user "
                "ON discord_warnings (guild_id, user_id)"
            )
            await db.commit()

    async def add(self, *, guild_id: int, user_id: int, moderator_id: int, reason: str) -> int:
        """Insert a warning and return the active warning count for that member."""
        created_at = dt.datetime.now(dt.timezone.utc).isoformat()
        async with aiosqlite.connect(self._path, timeout=self._CONNECT_TIMEOUT) as db:
            await db.execute(
                "INSERT INTO discord_warnings "
                "(guild_id, user_id, moderator_id, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                (guild_id, user_id, moderator_id, reason, created_at),
            )
            await db.commit()
            async with db.execute(
                "SELECT COUNT(*) FROM discord_warnings WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ) as cur:
                row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def list(self, *, guild_id: int, user_id: int) -> "list[dict]":
        async with aiosqlite.connect(self._path, timeout=self._CONNECT_TIMEOUT) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT moderator_id, reason, created_at FROM discord_warnings "
                "WHERE guild_id = ? AND user_id = ? ORDER BY id ASC",
                (guild_id, user_id),
            ) as cur:
                rows = await cur.fetchall()
        return [
            {"moderator_id": r["moderator_id"], "reason": r["reason"], "created_at": r["created_at"]}
            for r in rows
        ]

    async def clear(self, *, guild_id: int, user_id: int) -> int:
        async with aiosqlite.connect(self._path, timeout=self._CONNECT_TIMEOUT) as db:
            cur = await db.execute(
                "DELETE FROM discord_warnings WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            await db.commit()
            return cur.rowcount if cur.rowcount is not None else 0


# --------------------------------------------------------------------------- #
# Public entry point used by main.py
# --------------------------------------------------------------------------- #

def start_discord_moderation(config: Config) -> "asyncio.Task | None":
    """Start the Discord moderation bot as a background task, or return None.

    Returns None (and logs a safe reason) when the module is disabled, when
    discord.py is unavailable, or when the bot token is missing. Never raises,
    so the Telegram bot is never affected by Discord configuration problems.
    """
    if not config.enable_discord_moderation:
        logger.info("Discord moderation is disabled (ENABLE_DISCORD_MODERATION is not true).")
        return None

    if discord is None:
        logger.error(
            "Discord moderation is enabled but discord.py is not installed (%s). "
            "Run: pip install -r requirements.txt. Telegram bot continues without it.",
            DISCORD_IMPORT_ERROR,
        )
        return None

    if not config.discord_bot_token:
        logger.error(
            "Discord moderation is enabled but DISCORD_BOT_TOKEN is empty. "
            "Skipping Discord startup; Telegram bot continues normally."
        )
        return None

    bot = ModerationBot(config)
    logger.info("Starting Discord moderation bot task.")
    return asyncio.create_task(_run_bot(bot, config.discord_bot_token), name="discord-moderation")


async def _run_bot(bot: "ModerationBot", token: str) -> None:
    """Run the Discord client and swallow failures so Telegram is unaffected.

    Secrets are never logged: only error class names / safe guidance are emitted.
    """
    try:
        await bot.start(token)
    except asyncio.CancelledError:
        raise
    except discord.LoginFailure:
        logger.error("Discord login failed: the DISCORD_BOT_TOKEN is invalid. Telegram bot keeps running.")
    except discord.PrivilegedIntentsRequired:
        logger.error(
            "Discord login failed: a required privileged intent is not enabled for this bot. "
            "Enable MESSAGE CONTENT INTENT (and, if you set DISCORD_WELCOME_CHANNEL_ID, also "
            "SERVER MEMBERS INTENT) in the Developer Portal (Bot -> Privileged Gateway Intents). "
            "Telegram bot keeps running."
        )
    except Exception:
        # Log the traceback but not the token; discord.py does not echo it.
        logger.exception("Discord moderation crashed. Telegram bot keeps running.")
    finally:
        if not bot.is_closed():
            try:
                await bot.close()
            except Exception:
                logger.exception("Error while closing the Discord client.")


# --------------------------------------------------------------------------- #
# The bot
# --------------------------------------------------------------------------- #

if discord is not None:

    class ModerationBot(commands.Bot):
        """Discord moderation client. Holds config + per-channel spam state."""

        def __init__(self, config: Config) -> None:
            intents = discord.Intents.default()
            # Privileged: required to read message content for the filters.
            # Must also be enabled in the Developer Portal (see module docstring).
            intents.message_content = True
            # Server Members Intent is privileged and only needed for the welcome
            # system. Request it ONLY when a welcome channel is configured, so the
            # existing bot keeps working unchanged when welcome is disabled.
            if config.discord_welcome_channel_id:
                intents.members = True
            super().__init__(
                command_prefix=commands.when_mentioned,  # we only use slash commands
                intents=intents,
                help_command=None,
            )
            self.mod_config = config
            # (channel_id, user_id) -> deque[monotonic timestamp]
            self._spam_tracker: dict[tuple[int, int], deque[float]] = defaultdict(deque)
            self._mod_log_warned = False
            # Persistent warning history (same SQLite file, dedicated table).
            self.warning_store = _WarningStore(config.database_path)

        async def setup_hook(self) -> None:
            # Ensure the warnings table exists before any command can touch it.
            # A failure here only disables warning history; the rest keeps working.
            try:
                await self.warning_store.init()
            except Exception:
                logger.exception("Failed to initialise the Discord warnings table; warning history disabled.")
            _register_commands(self)
            self.tree.on_error = self._on_app_command_error
            # Sync slash commands. A guild-scoped sync is instant; a global sync
            # can take up to ~1 hour to propagate. Set DISCORD_GUILD_ID for fast
            # iteration on your own server.
            try:
                if self.mod_config.discord_guild_id:
                    guild = discord.Object(id=self.mod_config.discord_guild_id)
                    self.tree.copy_global_to(guild=guild)
                    await self.tree.sync(guild=guild)
                    logger.info("Synced Discord slash commands to guild %s.", self.mod_config.discord_guild_id)
                else:
                    await self.tree.sync()
                    logger.info("Synced Discord slash commands globally (may take up to ~1h to appear).")
            except Exception:
                logger.exception("Failed to sync Discord slash commands.")

        async def on_ready(self) -> None:
            user = self.user
            logger.info(
                "Discord moderation connected as %s (id %s); serving %s guild(s).",
                user,
                getattr(user, "id", "?"),
                len(self.guilds),
            )

        # ----- welcome system ----- #

        async def on_member_join(self, member: "discord.Member") -> None:
            try:
                await self._send_welcome(member)
            except Exception:
                logger.exception("Error while sending the Discord welcome message.")

        async def _send_welcome(self, member: "discord.Member") -> None:
            channel_id = self.mod_config.discord_welcome_channel_id
            if not channel_id or member.bot:
                return  # welcome disabled, or a bot joined

            channel = self.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    channel = None
            if channel is None:
                logger.warning("Welcome channel %s is missing or inaccessible.", channel_id)
                return

            perms = channel.permissions_for(channel.guild.me)
            if not (perms.send_messages and perms.embed_links):
                logger.warning("Missing 'Send Messages' / 'Embed Links' in welcome channel %s.", channel_id)
                return

            embed = discord.Embed(
                title="Вітаємо в Marvel Rivals UA 🇺🇦",
                description=(
                    "Ознайомся з правилами, обери роль і приєднуйся до спілкування. Гарної гри!"
                ),
                colour=discord.Colour.blurple(),
            )
            if WELCOME_RULES:
                rules_text = "\n".join(f"{index}. {rule}" for index, rule in enumerate(WELCOME_RULES, start=1))
                embed.add_field(name="📜 Правила", value=rules_text[:1024], inline=False)
            links = self._welcome_channel_links()
            if links:
                embed.add_field(name="Корисні канали", value=links, inline=False)

            try:
                # Ping only the joining member; never @everyone or roles.
                await channel.send(
                    content=member.mention,
                    embed=embed,
                    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
                )
            except discord.HTTPException:
                logger.exception("Failed to send the Discord welcome message.")

        def _welcome_channel_links(self) -> str:
            """Channel mentions for the welcome embed, only for IDs set in .env.

            Uses <#id> markup (renders as a channel link, never pings). Channels
            are never hardcoded — unset values are simply omitted. Rules are shown
            inline (see WELCOME_RULES / discord_rules.txt), not as a channel link.
            """
            cfg = self.mod_config
            entries = (
                ("💬 Чат", cfg.discord_chat_channel_id),
                ("🤝 Пошук команди", cfg.discord_lft_channel_id),
            )
            return "\n".join(f"{label}: <#{cid}>" for label, cid in entries if cid)

        # ----- message moderation ----- #

        async def on_message(self, message: "discord.Message") -> None:
            try:
                await self._moderate(message)
            except Exception:
                logger.exception("Error while moderating a Discord message.")

        async def _moderate(self, message: "discord.Message") -> None:
            # Ignore bots, webhooks, DMs, and our own messages.
            if message.author.bot or message.webhook_id is not None or message.guild is None:
                return

            author = message.author
            # Trusted members (those who can manage messages) bypass content filters.
            is_trusted = isinstance(author, discord.Member) and author.guild_permissions.manage_messages
            if is_trusted:
                return

            content = message.content or ""

            if self._has_blocked_invite(content):
                await self._punish(
                    message,
                    action="Invite link removed",
                    reason="Unauthorized Discord invite link",
                    timeout_seconds=0,
                )
                return

            scam_reason = _suspicious_reason(content)
            if scam_reason:
                await self._punish(
                    message,
                    action="Suspicious link removed",
                    reason=scam_reason,
                    timeout_seconds=0,
                )
                return

            if _has_bad_word(content):
                await self._punish(
                    message,
                    action="Message removed (blocked word)",
                    reason="Blocked word filter",
                    timeout_seconds=0,
                )
                return

            if self._register_and_check_spam(message):
                await self._punish(
                    message,
                    action="Anti-spam",
                    reason=f"More than {SPAM_MAX_MESSAGES} messages within {SPAM_WINDOW_SECONDS:.0f}s",
                    timeout_seconds=SPAM_TIMEOUT_SECONDS,
                )
                return

        def _has_blocked_invite(self, content: str) -> bool:
            allowed = self.mod_config.discord_allowed_invites
            for match in _INVITE_RE.finditer(content):
                code = match.group(1).lower()
                if code not in allowed:
                    return True  # at least one non-allowlisted invite -> block
            # No invites, or every invite found was on the allowlist.
            return False

        def _register_and_check_spam(self, message: "discord.Message") -> bool:
            key = (message.channel.id, message.author.id)
            now = time.monotonic()
            bucket = self._spam_tracker[key]
            bucket.append(now)
            while bucket and now - bucket[0] > SPAM_WINDOW_SECONDS:
                bucket.popleft()
            if len(bucket) > SPAM_MAX_MESSAGES:
                bucket.clear()  # reset so we act once, not on every later message
                return True
            return False

        # ----- enforcement helpers ----- #

        async def _punish(
            self,
            message: "discord.Message",
            *,
            action: str,
            reason: str,
            timeout_seconds: int,
        ) -> None:
            channel = message.channel
            guild = message.guild
            deleted = await self._safe_delete(message)

            if timeout_seconds > 0 and isinstance(message.author, discord.Member):
                await self._safe_timeout(message.author, timeout_seconds, reason)

            await self.send_mod_log(
                action=action if deleted else f"{action} (delete failed — missing permission?)",
                member=message.author,
                channel=channel,
                reason=reason,
                preview=message.content,
            )

        async def _safe_delete(self, message: "discord.Message") -> bool:
            channel = message.channel
            me = message.guild.me
            if not channel.permissions_for(me).manage_messages:
                logger.warning("Missing 'Manage Messages' in #%s; cannot delete flagged message.", channel)
                return False
            try:
                await message.delete()
                return True
            except discord.NotFound:
                return True  # already gone
            except discord.Forbidden:
                logger.warning("Forbidden to delete message in #%s.", channel)
            except discord.HTTPException:
                logger.exception("Failed to delete message in #%s.", channel)
            return False

        async def _safe_timeout(self, member: "discord.Member", seconds: int, reason: str) -> bool:
            guild = member.guild
            me = guild.me
            if not me.guild_permissions.moderate_members:
                logger.warning("Missing 'Moderate Members'; cannot timeout user %s.", member.id)
                return False
            if member == guild.owner or member.top_role >= me.top_role:
                logger.warning("Cannot timeout user %s due to role hierarchy / ownership.", member.id)
                return False
            try:
                await member.timeout(dt.timedelta(seconds=seconds), reason=reason)
                return True
            except discord.Forbidden:
                logger.warning("Forbidden to timeout user %s.", member.id)
            except discord.HTTPException:
                logger.exception("Failed to timeout user %s.", member.id)
            return False

        # ----- mod log ----- #

        async def _get_log_channel(self) -> "discord.abc.Messageable | None":
            """Resolve the configured mod-log channel, or warn once and return None.

            Shared by every logging path (moderation actions, reports, auto-actions)
            so channel resolution and permission checks live in one place.
            """
            channel_id = self.mod_config.discord_mod_log_channel_id
            if not channel_id:
                self._warn_mod_log_once(
                    "DISCORD_MOD_LOG_CHANNEL_ID is not set; Discord moderation actions are not logged to a channel."
                )
                return None

            log_channel = self.get_channel(channel_id)
            if log_channel is None:
                try:
                    log_channel = await self.fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    log_channel = None
            if log_channel is None:
                self._warn_mod_log_once(f"Mod-log channel {channel_id} is missing or inaccessible.")
                return None

            perms = log_channel.permissions_for(log_channel.guild.me)
            if not (perms.send_messages and perms.embed_links):
                self._warn_mod_log_once(
                    f"Missing 'Send Messages' / 'Embed Links' in mod-log channel {channel_id}."
                )
                return None
            return log_channel

        async def _send_log_embed(self, embed: "discord.Embed") -> None:
            log_channel = await self._get_log_channel()
            if log_channel is None:
                return
            try:
                # allowed_mentions=none so the log itself never pings anyone.
                await log_channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                logger.exception("Failed to send Discord mod-log message.")

        async def send_mod_log(
            self,
            *,
            action: str,
            member: "discord.abc.User | discord.Member",
            channel: "discord.abc.GuildChannel | discord.Thread | None",
            reason: str,
            preview: str | None = None,
        ) -> None:
            embed = discord.Embed(
                title=f"🛡️ {action}",
                colour=discord.Colour.orange(),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="User", value=f"{member.mention} (`{member.id}`)", inline=False)
            embed.add_field(
                name="Channel",
                value=getattr(channel, "mention", str(channel)) if channel is not None else "—",
                inline=True,
            )
            embed.add_field(name="Reason", value=(reason or "—")[:1024], inline=True)
            if preview:
                safe = discord.utils.escape_markdown(preview)[:200]
                if safe:
                    embed.add_field(name="Message preview", value=safe, inline=False)
            await self._send_log_embed(embed)

        async def send_report_log(
            self,
            *,
            reporter: "discord.abc.User | discord.Member",
            reported: "discord.abc.User | discord.Member",
            channel: "discord.abc.GuildChannel | discord.Thread | None",
            reason: str,
        ) -> None:
            embed = discord.Embed(
                title="🚩 Report",
                colour=discord.Colour.red(),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="Reporter", value=f"{reporter.mention} (`{reporter.id}`)", inline=False)
            embed.add_field(name="Reported member", value=f"{reported.mention} (`{reported.id}`)", inline=False)
            embed.add_field(
                name="Channel",
                value=getattr(channel, "mention", str(channel)) if channel is not None else "—",
                inline=True,
            )
            embed.add_field(name="Reason", value=(reason or "—")[:1024], inline=False)
            await self._send_log_embed(embed)

        async def _apply_warning_autoactions(
            self,
            *,
            member: "discord.Member",
            channel: "discord.abc.GuildChannel | discord.Thread | None",
            count: int,
        ) -> None:
            """Auto-act on the active warning count. Never bans/kicks; never hits staff."""
            if _is_staff(member):
                return  # never auto-punish moderators / admins / the owner

            timeout_seconds = WARN_TIMEOUT_THRESHOLDS.get(count)
            if timeout_seconds:
                minutes = timeout_seconds // 60
                ok = await self._safe_timeout(member, timeout_seconds, f"Авто-дія: {count} попереджень")
                if ok:
                    await self.send_mod_log(
                        action=f"Auto-timeout ({count} warnings)",
                        member=member,
                        channel=channel,
                        reason=f"{minutes}-minute timeout after reaching {count} warnings",
                    )
                else:
                    # Could not time out (missing permission / role hierarchy / owner).
                    await self.send_mod_log(
                        action="Auto-timeout skipped — please review manually",
                        member=member,
                        channel=channel,
                        reason=(
                            f"Reached {count} warnings (wanted a {minutes}-minute timeout) but I could not apply it "
                            "— check my 'Moderate Members' permission and role position."
                        ),
                    )

            if count == WARN_ESCALATION_THRESHOLD:
                await self.send_mod_log(
                    action="⚠️ Manual review needed",
                    member=member,
                    channel=channel,
                    reason=(
                        f"{member} reached {count} warnings. Please review manually — "
                        "no automatic ban/kick is ever performed."
                    ),
                )

        def _warn_mod_log_once(self, message: str) -> None:
            if not self._mod_log_warned:
                logger.warning(message)
                self._mod_log_warned = True

        # ----- slash command error handler ----- #

        async def _on_app_command_error(
            self,
            interaction: "discord.Interaction",
            error: "app_commands.AppCommandError",
        ) -> None:
            if isinstance(error, app_commands.MissingPermissions):
                msg = "У тебе немає прав для використання цієї команди."
            elif isinstance(error, app_commands.BotMissingPermissions):
                msg = "Мені бракує дозволів, щоб це зробити."
            elif isinstance(error, app_commands.CommandOnCooldown):
                msg = "Команда на перезарядці, спробуй за мить."
            else:
                logger.warning("Slash command error: %s", error.__class__.__name__)
                msg = "Щось пішло не так під час виконання команди."
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(msg, ephemeral=True)
                else:
                    await interaction.response.send_message(msg, ephemeral=True)
            except discord.HTTPException:
                pass

else:  # pragma: no cover - discord.py not installed

    class ModerationBot:  # type: ignore[no-redef]
        """Placeholder so type references resolve when discord.py is missing."""

        def __init__(self, *_args, **_kwargs) -> None:
            raise RuntimeError("discord.py is not installed")


# --------------------------------------------------------------------------- #
# Slash commands
# --------------------------------------------------------------------------- #
# NOTE: slash commands require the bot to be invited with the
# `applications.commands` OAuth2 scope (in addition to `bot`). See module
# docstring. `default_permissions(...)` hides each command from members who lack
# the relevant permission, and we re-check at runtime for safety.

def _register_commands(bot: "ModerationBot") -> None:
    @bot.tree.command(name="help", description="Показати список доступних команд модерації.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    async def help_command(interaction: "discord.Interaction") -> None:
        # "Server administration only": anyone who can run a moderation command.
        # Administrator / owner implicitly hold both of these permissions.
        perms = interaction.user.guild_permissions
        if not (perms.manage_messages or perms.moderate_members):
            await interaction.response.send_message(
                "Ця команда доступна лише модерації сервера.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🛡️ Команди модерації — Marvel Rivals UA",
            description="Доступно лише модерації сервера. Цей список бачиш лише ти.",
            colour=discord.Colour.blurple(),
        )
        embed.add_field(
            name="/help",
            value="Показати цей список команд модерації.",
            inline=False,
        )
        embed.add_field(
            name="/clear <кількість>",
            value=(
                "Видалити від 1 до 100 останніх повідомлень у поточному каналі.\n"
                "Потрібен дозвіл: **Manage Messages** (Керування повідомленнями)."
            ),
            inline=False,
        )
        embed.add_field(
            name="/timeout <учасник> <хвилини> [причина]",
            value=(
                "Тимчасово обмежити учаснику можливість писати (тайм-аут) на 1–40320 хв "
                "(до 28 днів).\n"
                "Потрібен дозвіл: **Moderate Members** (Тайм-аут учасників)."
            ),
            inline=False,
        )
        embed.add_field(
            name="/warn <учасник> <причина>",
            value=(
                "Винести попередження учаснику (зберігається в історію). Дію записано в канал "
                "мод-логів і надіслано учаснику в особисті повідомлення, якщо це можливо.\n"
                "Потрібен дозвіл: **Moderate Members** (Тайм-аут учасників)."
            ),
            inline=False,
        )
        embed.add_field(
            name="/warnings <учасник>",
            value=(
                "Показати історію попереджень учасника.\n"
                "Потрібен дозвіл: **Moderate Members** (Тайм-аут учасників)."
            ),
            inline=False,
        )
        embed.add_field(
            name="/clearwarnings <учасник>",
            value=(
                "Очистити всі попередження учасника.\n"
                "Потрібен дозвіл: **Moderate Members** (Тайм-аут учасників)."
            ),
            inline=False,
        )
        embed.add_field(
            name="/report <учасник> <причина> — для всіх",
            value=(
                "Будь-який учасник може поскаржитися на іншого; скарга надходить у канал "
                "мод-логів. Відповідь бачить лише той, хто скаржиться."
            ),
            inline=False,
        )
        embed.add_field(
            name="/lfthelp — для всіх",
            value=(
                "Показати інструкцію та шаблон поста для форуму пошуку тіммейтів. "
                "Відповідь бачить лише той, хто викликав команду."
            ),
            inline=False,
        )

        warn_rules = ", ".join(
            f"{count} → тайм-аут {secs // 60} хв"
            for count, secs in sorted(WARN_TIMEOUT_THRESHOLDS.items())
        )
        embed.add_field(
            name="ℹ️ Автоматична модерація (працює без команд)",
            value=(
                f"• Антиспам: понад {SPAM_MAX_MESSAGES} повідомлень за ~{SPAM_WINDOW_SECONDS:.0f} с "
                f"→ видалення + тайм-аут {SPAM_TIMEOUT_SECONDS} с.\n"
                "• Фільтр запрошень Discord (окрім дозволених у білому списку).\n"
                "• Фільтр підозрілих / шахрайських посилань.\n"
                "• Фільтр заборонених слів.\n"
                f"• Авто-дії за попередженнями: {warn_rules}, "
                f"{WARN_ESCALATION_THRESHOLD} → сигнал модерації для ручного розгляду. "
                "Бот ніколи не банить і не кікає автоматично; модераторів авто-дії не торкаються.\n"
                "Учасники з дозволом **Manage Messages** не підпадають під фільтри контенту."
            ),
            inline=False,
        )
        if bot.mod_config.discord_welcome_channel_id:
            embed.add_field(
                name="👋 Привітання нових учасників",
                value="Увімкнено: нові учасники отримують вітальне повідомлення у вітальному каналі.",
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="clear", description="Видалити кілька останніх повідомлень у цьому каналі.")
    @app_commands.describe(amount="Скільки повідомлень видалити (1–100).")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    async def clear(interaction: "discord.Interaction", amount: "app_commands.Range[int, 1, 100]") -> None:
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message(
                "Тобі потрібен дозвіл **Manage Messages** (Керування повідомленнями).", ephemeral=True
            )
            return
        me = interaction.guild.me
        perms = interaction.channel.permissions_for(me)
        if not (perms.manage_messages and perms.read_message_history):
            await interaction.response.send_message(
                "Мені потрібні дозволи **Manage Messages** і **Read Message History** у цьому каналі.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            deleted = await interaction.channel.purge(limit=amount)
        except discord.Forbidden:
            await interaction.followup.send("Мені не дозволено видаляти повідомлення тут.", ephemeral=True)
            return
        except discord.HTTPException:
            logger.exception("/clear failed in #%s", interaction.channel)
            await interaction.followup.send("Не вдалося видалити повідомлення (помилка Discord).", ephemeral=True)
            return

        await interaction.followup.send(f"Видалено повідомлень: {len(deleted)}.", ephemeral=True)
        await bot.send_mod_log(
            action="/clear",
            member=interaction.user,
            channel=interaction.channel,
            reason=f"Cleared {len(deleted)} message(s)",
        )

    @bot.tree.command(name="timeout", description="Видати учаснику тайм-аут на задану кількість хвилин.")
    @app_commands.describe(
        user="Учасник, якому видати тайм-аут.",
        minutes="Тривалість у хвилинах (1–40320 = 28 днів).",
        reason="Причина тайм-ауту.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def timeout(
        interaction: "discord.Interaction",
        user: "discord.Member",
        minutes: "app_commands.Range[int, 1, 40320]",  # 40320 = MAX_TIMEOUT_MINUTES (28 days)
        reason: str = "Причину не вказано",
    ) -> None:
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message(
                "Тобі потрібен дозвіл **Moderate Members** (Тайм-аут учасників).", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        ok = await bot._safe_timeout(user, minutes * 60, reason)
        if not ok:
            await interaction.followup.send(
                "Не вдалося видати тайм-аут цьому учаснику. Перевір мій дозвіл **Moderate Members** і що моя "
                "роль вища за роль учасника (власника сервера заглушити не можна).",
                ephemeral=True,
            )
            return

        await interaction.followup.send(f"Видано тайм-аут {user.mention} на {minutes} хв.", ephemeral=True)
        await bot.send_mod_log(
            action="/timeout",
            member=user,
            channel=interaction.channel,
            reason=f"{minutes} min — {reason}",
        )

    @bot.tree.command(
        name="warn",
        description="Попередити учасника (з логом у мод-канал і повідомленням в особисті, якщо можливо).",
    )
    @app_commands.describe(user="Учасник, якого попередити.", reason="Причина попередження.")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def warn(interaction: "discord.Interaction", user: "discord.Member", reason: str) -> None:
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message(
                "Тобі потрібен дозвіл **Moderate Members** (Тайм-аут учасників).", ephemeral=True
            )
            return

        # Persist first so /warnings reflects it and auto-actions can count it.
        active_count: int | None = None
        try:
            active_count = await bot.warning_store.add(
                guild_id=interaction.guild.id,
                user_id=user.id,
                moderator_id=interaction.user.id,
                reason=reason,
            )
        except Exception:
            logger.exception("Failed to persist a Discord warning; continuing without history.")

        dm_sent = False
        try:
            await user.send(f"⚠️ Тобі винесли попередження на сервері **{interaction.guild.name}**: {reason}")
            dm_sent = True
        except discord.HTTPException:
            dm_sent = False  # DMs closed or blocked; not an error.

        total_note = f" Усього активних попереджень: {active_count}." if active_count is not None else ""
        await interaction.response.send_message(
            f"Винесено попередження {user.mention}."
            + (" (повідомлення в особисті надіслано)" if dm_sent else " (не вдалося написати в особисті)")
            + total_note,
            ephemeral=True,
        )
        await bot.send_mod_log(
            action="/warn",
            member=user,
            channel=interaction.channel,
            reason=reason,
        )
        if active_count is not None:
            await bot._apply_warning_autoactions(
                member=user, channel=interaction.channel, count=active_count
            )

    @bot.tree.command(name="report", description="Поскаржитися модерації на учасника.")
    @app_commands.describe(member="Учасник, на якого скаржишся.", reason="Що сталося / причина скарги.")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(3, 60.0)  # light anti-abuse: 3 reports / minute per user
    async def report(interaction: "discord.Interaction", member: "discord.Member", reason: str) -> None:
        if member.id == interaction.user.id:
            await interaction.response.send_message("Не можна поскаржитися на самого себе.", ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message("Не можна поскаржитися на бота.", ephemeral=True)
            return

        await bot.send_report_log(
            reporter=interaction.user,
            reported=member,
            channel=interaction.channel,
            reason=reason,
        )
        await interaction.response.send_message("Дякуємо, репорт надіслано модерації.", ephemeral=True)

    @bot.tree.command(
        name="lfthelp",
        description="Як знайти тіммейтів у Marvel Rivals: інструкція та шаблон поста.",
    )
    @app_commands.guild_only()
    async def lfthelp(interaction: "discord.Interaction") -> None:
        # Available to everyone; the reply is ephemeral so it never spams a channel.
        lft_channel_id = bot.mod_config.discord_lft_channel_id
        forum_ref = f"<#{lft_channel_id}>" if lft_channel_id else "форумі пошуку тіммейтів"
        embed = discord.Embed(
            title="🤝 Пошук тіммейтів — як це працює",
            description=(
                f"Шукай команду у {forum_ref} — один пост = один пошук.\n"
                "1. Натисни **New Post / Новий пост**.\n"
                "2. У назві коротко вкажи головне — режим, ранг, роль. "
                "Напр.: `Ranked Diamond+, шукаю Strategist`.\n"
                "3. Обери теги: платформу, режим і роль.\n"
                "4. Скопіюй шаблон нижче у свій пост і заповни 👇"
            ),
            colour=discord.Colour.green(),
        )
        embed.add_field(
            name="Шаблон поста",
            value=(
                "```\n"
                "**Платформа:** PC / PlayStation / Xbox\n"
                "**Режим:** Ranked / Casual / Quick Match\n"
                "**Ранг:**\n"
                "**Роль:** Vanguard (танк) / Duelist (дпс) / Strategist (сапорт)\n"
                "**Час гри:** напр., 19:00–23:00 за Києвом\n"
                "**Коментар:** вік, войс, кілька слів про себе\n"
                "```"
            ),
            inline=False,
        )
        embed.add_field(
            name="Поради",
            value=(
                "• Один активний пост на людину. Знайшов команду — напиши про це в пості й закрий його 🔒\n"
                "• Хочеш відгукнутися на чийсь пост? Просто напиши в ньому.\n"
                "• Будь привітним — з токсиками грати ніхто не хоче 😉\n"
                "• Бачиш порушення — скористайся командою `/report`."
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @bot.tree.command(name="warnings", description="Показати історію попереджень учасника.")
    @app_commands.describe(member="Учасник, чиї попередження показати.")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def warnings(interaction: "discord.Interaction", member: "discord.Member") -> None:
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message(
                "Тобі потрібен дозвіл **Moderate Members** (Тайм-аут учасників).", ephemeral=True
            )
            return

        try:
            records = await bot.warning_store.list(guild_id=interaction.guild.id, user_id=member.id)
        except Exception:
            logger.exception("Failed to read Discord warnings.")
            await interaction.response.send_message("Не вдалося прочитати історію попереджень.", ephemeral=True)
            return

        if not records:
            await interaction.response.send_message(
                f"У {member.mention} немає активних попереджень.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        embed = discord.Embed(
            title=f"Попередження — {member.display_name}",
            description=f"{member.mention} • усього активних: {len(records)}",
            colour=discord.Colour.orange(),
        )
        # Embeds allow 25 fields; show the 20 most recent and note if truncated.
        recent = records[-20:]
        for position, record in enumerate(recent, start=len(records) - len(recent) + 1):
            when = _format_warn_timestamp(record["created_at"])
            moderator = f"<@{record['moderator_id']}>"
            reason_text = (record["reason"] or "—")[:300]
            embed.add_field(
                name=f"#{position} • {when}",
                value=f"Модератор: {moderator}\nПричина: {reason_text}",
                inline=False,
            )
        if len(records) > len(recent):
            embed.set_footer(text=f"Показано останні {len(recent)} із {len(records)}.")

        await interaction.response.send_message(
            embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @bot.tree.command(name="clearwarnings", description="Очистити всі попередження учасника.")
    @app_commands.describe(member="Учасник, чиї попередження очистити.")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def clearwarnings(interaction: "discord.Interaction", member: "discord.Member") -> None:
        if not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message(
                "Тобі потрібен дозвіл **Moderate Members** (Тайм-аут учасників).", ephemeral=True
            )
            return

        try:
            removed = await bot.warning_store.clear(guild_id=interaction.guild.id, user_id=member.id)
        except Exception:
            logger.exception("Failed to clear Discord warnings.")
            await interaction.response.send_message("Не вдалося очистити попередження.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"Очищено попереджень для {member.mention}: {removed}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await bot.send_mod_log(
            action="/clearwarnings",
            member=member,
            channel=interaction.channel,
            reason=f"Cleared {removed} warning(s) by {interaction.user} ({interaction.user.id})",
        )
