"""Pure moderation logic for the Telegram group-chat moderation feature.

This module holds NO aiogram / Telegram I/O. It mirrors the pure parts of
``discord_moderation.py`` (anti-spam tracker, invite/scam/bad-word filters,
external editable word/rule lists) so the rules stay consistent across both
platforms while ``handlers/moderation.py`` keeps all the Telegram API calls.

Everything here is synchronous and side-effect free except for reading the two
optional, editable text files at import time. Those loaders never raise: a
missing/unreadable file falls back to safe defaults, exactly like the Discord
module, so a deployment without the files still starts and moderates.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from collections.abc import Callable
from pathlib import Path


# --------------------------------------------------------------------------- #
# Tunable moderation settings (kept in code on purpose, like discord_moderation).
# --------------------------------------------------------------------------- #

# Anti-flood: more than SPAM_MAX_MESSAGES messages from the same user in the same
# chat within SPAM_WINDOW_SECONDS triggers a delete + temporary mute.
SPAM_WINDOW_SECONDS = 7.0
SPAM_MAX_MESSAGES = 5
SPAM_MUTE_SECONDS = 60

# Auto-actions after repeated /warn warnings. Keys are the *active* warning count
# that triggers the action; values are the mute length in seconds. The bot NEVER
# auto-bans or auto-kicks, and never auto-punishes chat admins (see handlers).
WARN_MUTE_THRESHOLDS: dict[int, int] = {
    3: 10 * 60,  # 3 warnings -> 10-minute mute
    5: 60 * 60,  # 5 warnings -> 1-hour mute
}
# At this many warnings, post an urgent mod-log note asking for manual review.
WARN_ESCALATION_THRESHOLD = 7

# Telegram clamps restriction durations: an until_date less than 30 seconds or
# more than 366 days from now is treated as "forever". Keep temporary mutes
# safely inside that window so a short anti-flood mute never becomes permanent.
MIN_MUTE_SECONDS = 35
MAX_MUTE_SECONDS = 366 * 24 * 60 * 60 - 60


# --------------------------------------------------------------------------- #
# External, editable word / rule lists (repo root, two levels up from services/).
# --------------------------------------------------------------------------- #

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BAD_WORDS_FILE = _REPO_ROOT / "telegram_badwords.txt"
_RULES_FILE = _REPO_ROOT / "telegram_rules.txt"

# Minimal fallback used only if telegram_badwords.txt is missing/unreadable/empty.
_DEFAULT_BAD_WORDS: tuple[str, ...] = (
    "nigger",
    "faggot",
    "retard",
)


def _load_bad_words() -> tuple[str, ...]:
    try:
        raw = _BAD_WORDS_FILE.read_text(encoding="utf-8")
    except OSError:
        return _DEFAULT_BAD_WORDS
    words: list[str] = []
    for line in raw.splitlines():
        token = line.strip()
        if not token or token.startswith("#"):
            continue
        words.append(token.lower())
    # De-duplicate while preserving order; fall back to defaults if file is empty.
    return tuple(dict.fromkeys(words)) or _DEFAULT_BAD_WORDS


def load_welcome_rules() -> tuple[str, ...]:
    """Return the editable welcome rules, or () when the file is missing/empty.

    Read on demand (not cached) so editing telegram_rules.txt is picked up on the
    next join without restarting; the file is tiny and joins are infrequent.
    """
    try:
        raw = _RULES_FILE.read_text(encoding="utf-8")
    except OSError:
        return ()
    rules: list[str] = []
    for line in raw.splitlines():
        token = line.strip()
        if not token or token.startswith("#"):
            continue
        rules.append(token)
    return tuple(rules)


BAD_WORDS: tuple[str, ...] = _load_bad_words()


# --------------------------------------------------------------------------- #
# Regexes (adapted from discord_moderation.py, kept conservative).
# --------------------------------------------------------------------------- #

# Telegram invite / link to a group, channel, or bot: t.me/<code>, t.me/+<hash>,
# t.me/joinchat/<hash>, t.me/s/<channel>, telegram.me/..., telegram.dog/...
# The leading lookbehind keeps the host from matching inside OTHER domains that
# merely end in "t" (about.me/john, support.me/...).
_INVITE_RE = re.compile(
    r"(?<![\w.\-])(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)"
    r"/(?:joinchat/|s/|\+)?([A-Za-z0-9_\-]+)",
    re.IGNORECASE,
)

# Well-known t.me paths that are NOT group/channel invites; ignore to avoid
# over-blocking (e.g. sticker packs, share links, proxies, instant view).
# "c" covers t.me/c/<internal_id>/<msg_id> message deep links: they cannot be
# used to join anything and members paste them when quoting chat messages.
_NON_INVITE_TME_PATHS = frozenset(
    {
        "share",
        "addstickers",
        "addemoji",
        "addtheme",
        "proxy",
        "socks",
        "iv",
        "bg",
        "setlanguage",
        "confirmphone",
        "c",
        "s",
    }
)

# Always-suspicious scam phrases / phishing look-alikes. Kept conservative.
_SCAM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"free\s+(?:discord\s+)?nitro", re.IGNORECASE),
    re.compile(r"nitro\s+(?:for\s+)?free", re.IGNORECASE),
    re.compile(r"free\s+telegram\s+premium", re.IGNORECASE),
    re.compile(r"telegram\s+premium\s+(?:for\s+)?free", re.IGNORECASE),
    re.compile(r"(?:double|2x)\s+your\s+(?:crypto|btc|eth|usdt|bitcoin)", re.IGNORECASE),
    re.compile(r"\bcrypto\s+(?:airdrop|giveaway)\b", re.IGNORECASE),
    re.compile(r"\b(?:pump\s+(?:and\s+)?dump|pump\s+group|insider\s+signals?)\b", re.IGNORECASE),
    re.compile(r"\bearn\s+\$?\d+\s+(?:per|a)\s+day\b", re.IGNORECASE),
    # Common phishing look-alike domains / typosquats.
    re.compile(
        r"\b(?:dlscord|discrod|dlsc0rd|discord-nitro|discordgift|discordapp\.gift|"
        r"steamcommunlty|steamcommun[il]ty|steamcomunity|telegrann|telergam)\b",
        re.IGNORECASE,
    ),
    # IP-logger / grabber services are effectively always malicious.
    re.compile(r"https?://(?:grabify\.link|iplogger\.\w+|blasze\.\w+)/\S+", re.IGNORECASE),
)

# Phrases that are common in NORMAL gaming chat ("куплю steam gift card", "free
# skins в івенті") — Marvel Rivals is a Steam game with free-skin events, so
# these count as scam only when the message also carries a link.
_LINKED_SCAM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:steam|cs:?go)\s+gift", re.IGNORECASE),
    re.compile(r"free\s+(?:robux|v-?bucks|skins?)\b", re.IGNORECASE),
)
_URL_HINT_RE = re.compile(r"https?://|www\.|t\.me/", re.IGNORECASE)

# Generic URL shorteners: only treated as suspicious when combined with a scam
# keyword, to avoid blocking legitimate shortened links.
_SHORTENER_RE = re.compile(
    r"https?://(?:bit\.ly|tinyurl\.com|cutt\.ly|is\.gd|shorturl\.at|t\.co|rb\.gy)/\S+",
    re.IGNORECASE,
)
_SCAM_KEYWORD_RE = re.compile(
    r"\b(?:nitro|free|gift|airdrop|giveaway|claim|reward|prize|crypto|wallet|premium)\b",
    re.IGNORECASE,
)


def _build_bad_word_re() -> re.Pattern[str] | None:
    if not BAD_WORDS:
        return None
    joined = "|".join(re.escape(word) for word in BAD_WORDS)
    # (?<!\w) / (?!\w) act as Unicode-aware word boundaries for str patterns, so
    # terms match only WHOLE words, never substrings of normal Ukrainian words.
    return re.compile(rf"(?<!\w)(?:{joined})(?!\w)", re.IGNORECASE)


_BAD_WORD_RE = _build_bad_word_re()


# --------------------------------------------------------------------------- #
# Pure predicates used by the handlers.
# --------------------------------------------------------------------------- #

def suspicious_reason(content: str) -> str | None:
    """Return a short reason string if the content looks like a scam, else None."""
    for pattern in _SCAM_PATTERNS:
        if pattern.search(content):
            return "scam"
    if _URL_HINT_RE.search(content):
        for pattern in _LINKED_SCAM_PATTERNS:
            if pattern.search(content):
                return "scam"
    if _SHORTENER_RE.search(content) and _SCAM_KEYWORD_RE.search(content):
        return "shortener"
    return None


def has_bad_word(content: str) -> bool:
    return _BAD_WORD_RE is not None and _BAD_WORD_RE.search(content) is not None


def has_blocked_invite(content: str, allowlist: frozenset[str]) -> bool:
    """True if the text contains a t.me invite/link whose code is not allowlisted.

    Well-known non-invite t.me paths (sticker packs, share links, proxies, …) are
    ignored. A message with no t.me links, or only allowlisted ones, returns False.
    """
    for match in _INVITE_RE.finditer(content):
        code = match.group(1).lower()
        if code in _NON_INVITE_TME_PATHS:
            continue
        # The allowlist parser keeps a leading "+" from full t.me/+hash URLs while
        # the regex strips it, so accept the code in both spellings.
        if code not in allowlist and f"+{code}" not in allowlist:
            return True  # at least one non-allowlisted invite -> block
    return False


# --------------------------------------------------------------------------- #
# Anti-flood tracker.
# --------------------------------------------------------------------------- #

class SpamTracker:
    """Per-(chat, user) sliding-window message-rate tracker.

    Mirrors discord_moderation's deque approach. Stale buckets are swept
    opportunistically every _SWEEP_EVERY registrations, so the key space does
    not grow unbounded in a busy public group.
    """

    _SWEEP_EVERY = 512

    def __init__(
        self,
        *,
        window_seconds: float = SPAM_WINDOW_SECONDS,
        max_messages: int = SPAM_MAX_MESSAGES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = window_seconds
        self._max = max_messages
        self._clock = clock
        self._buckets: dict[tuple[int, int], deque[float]] = defaultdict(deque)
        self._calls = 0

    def register_and_check(self, chat_id: int, user_id: int) -> bool:
        """Record one message; return True exactly once when the user is flooding."""
        self._calls += 1
        if self._calls % self._SWEEP_EVERY == 0:
            self.sweep()

        key = (chat_id, user_id)
        now = self._clock()
        bucket = self._buckets[key]
        bucket.append(now)
        while bucket and now - bucket[0] > self._window:
            bucket.popleft()
        if len(bucket) > self._max:
            # Reset so we act once, not on every later message in the burst.
            del self._buckets[key]
            return True
        return False

    def sweep(self) -> None:
        """Drop buckets whose newest entry is older than the window. Optional upkeep."""
        now = self._clock()
        stale = [
            key
            for key, bucket in self._buckets.items()
            if not bucket or now - bucket[-1] > self._window
        ]
        for key in stale:
            self._buckets.pop(key, None)


# --------------------------------------------------------------------------- #
# Duration parsing for /mute, /ban.
# --------------------------------------------------------------------------- #

_DURATION_RE = re.compile(r"^(\d+)\s*([smhdw]?)$", re.IGNORECASE)
_DURATION_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
    "": 60,  # a bare number means minutes (common moderation-bot convention)
}
_PERMANENT_TOKENS = frozenset({"0", "perm", "permanent", "forever", "назавжди", "назавжди."})


def parse_duration(text: str | None) -> int | None:
    """Parse a human duration into seconds.

    Returns:
      * a positive int — duration in seconds (e.g. "30" -> 1800, "2h" -> 7200);
      * ``0`` — explicit permanent ("0", "perm", "forever", "назавжди");
      * ``None`` — empty or unparseable input (caller decides the default).
    """
    if text is None:
        return None
    token = text.strip().lower()
    if not token:
        return None
    if token in _PERMANENT_TOKENS:
        return 0
    match = _DURATION_RE.match(token)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    seconds = amount * _DURATION_UNIT_SECONDS[unit]
    return seconds if seconds > 0 else 0


def clamp_mute_seconds(seconds: int) -> int:
    """Clamp a positive temporary-mute duration into Telegram's valid window."""
    if seconds < MIN_MUTE_SECONDS:
        return MIN_MUTE_SECONDS
    if seconds > MAX_MUTE_SECONDS:
        return MAX_MUTE_SECONDS
    return seconds
