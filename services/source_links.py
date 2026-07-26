"""Keep the genuinely useful links a source post carries in its own body.

Drafts are generated with every URL stripped: a model-written URL cannot be
trusted, and a raw source URL does not belong in a public post. That also threw
away links the post existed to share — a Spotify playlist, an event page, a
Twitch stream.

So links are taken from the ORIGINAL post text rather than from the draft, and
only survive when they clear three gates:

1. the source is trusted enough to carry links at all (see LINK_SOURCE_TYPES);
2. the destination host is on a small allowlist of official/known platforms;
3. link shorteners are resolved one hop first, so a hidden destination is judged
   on where it actually goes.

Anything else is dropped exactly as before. The bot never fetches the linked
page — a shortener is resolved by reading one redirect header.
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlsplit

import httpx


logger = logging.getLogger(__name__)

# Only Bluesky for now. The official site's own articles are already reachable
# through the attribution link; YouTube descriptions are mostly boilerplate
# social-media links; and Reddit/RivalSkins are user-submitted leak content,
# which is exactly where an unvetted link is most likely to be hostile.
LINK_SOURCE_TYPES = frozenset({"bluesky"})

# Exact hosts, and hosts whose subdomains are equally acceptable.
_ALLOWED_HOSTS = frozenset(
    {
        "marvelrivals.com",
        "youtube.com",
        "youtu.be",
        "twitch.tv",
        "spotify.com",
        "discord.gg",
        "discord.com",
        "store.steampowered.com",
    }
)
_ALLOWED_SUFFIXES = (
    ".marvelrivals.com",
    ".youtube.com",
    ".twitch.tv",
    ".spotify.com",
)

# Shorteners hide their destination, so they are resolved before being judged.
# The request goes to one of these fixed, well-known hosts and reads only the
# redirect header — the destination itself is never fetched.
_SHORTENER_HOSTS = frozenset({"bit.ly", "t.co", "tinyurl.com", "ow.ly", "buff.ly"})

_HOST_LABELS = {
    "marvelrivals.com": "Офіційний сайт",
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "twitch.tv": "Twitch",
    "spotify.com": "Spotify",
    "discord.gg": "Discord",
    "discord.com": "Discord",
    "store.steampowered.com": "Steam",
}

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?'\"»)]}"

_RESOLVE_TIMEOUT_SECONDS = 10.0
_MAX_LINKS = 2
_USER_AGENT = "MarvelRivalsUACollector/1.0 (Telegram news bot; +https://t.me/MarvelRivalsUABot)"


async def collect_publishable_links(
    body_text: str,
    *,
    source_type: str,
    article_url: str = "",
    limit: int = _MAX_LINKS,
) -> list[tuple[str, str]]:
    """Return up to ``limit`` (label, url) pairs worth keeping in the public post.

    Empty for a source that does not carry links, and for any URL whose final
    destination is not allowlisted.
    """
    if source_type not in LINK_SOURCE_TYPES or not body_text:
        return []

    candidates = _unique_urls(body_text, skip=article_url)
    if not candidates:
        return []

    resolved = await asyncio.gather(*(_resolve(url) for url in candidates))

    links: list[tuple[str, str]] = []
    seen: set[str] = set()
    for url in resolved:
        if url is None:
            continue
        label = _label_for(url)
        if label is None or url in seen:
            continue
        seen.add(url)
        links.append((label, url))
        if len(links) >= max(1, limit):
            break
    return links


def _unique_urls(text: str, *, skip: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(_TRAILING_PUNCTUATION)
        if not url or url in seen or (skip and url == skip):
            continue
        seen.add(url)
        urls.append(url)
    return urls


async def _resolve(url: str) -> str | None:
    """The allowlisted destination of ``url``, or None when it is not acceptable."""
    accepted = _accept(url)
    if accepted is not None:
        return accepted

    host = _host(url)
    if host is None or host not in _SHORTENER_HOSTS:
        return None

    target = await _redirect_target(url)
    return _accept(target) if target else None


def _accept(url: str) -> str | None:
    """``url`` normalised to https when its host is allowlisted, else None.

    Shorteners routinely resolve to an http URL (bit.ly hands back
    ``http://youtube.com/...``), and rejecting those would throw away good links
    on a technicality. Since the link is only ever printed — never fetched — and
    every allowlisted host serves https, upgrading the scheme is safe.
    """
    host = _host(url)
    if host is None or not _is_allowed_host(host):
        return None
    parsed = urlsplit(url.strip())
    return parsed._replace(scheme="https").geturl()


async def _redirect_target(url: str) -> str | None:
    """Follow exactly one redirect hop by reading the Location header."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(_RESOLVE_TIMEOUT_SECONDS),
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            response = await client.head(url)
    except httpx.HTTPError as exc:
        logger.info("Could not resolve shortened link %s: %s", url, exc)
        return None

    if response.status_code not in {301, 302, 303, 307, 308}:
        return None
    location = str(response.headers.get("location") or "").strip()
    return location or None


def _host(url: str) -> str | None:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return parsed.hostname.lower()


def _is_allowed_host(host: str) -> bool:
    return host in _ALLOWED_HOSTS or any(host.endswith(suffix) for suffix in _ALLOWED_SUFFIXES)


def _label_for(url: str) -> str | None:
    host = _host(url)
    if host is None:
        return None
    if host in _HOST_LABELS:
        return _HOST_LABELS[host]
    for known, label in _HOST_LABELS.items():
        if host.endswith(f".{known}"):
            return label
    return None
