"""Resolve a Bluesky video post to a direct MP4 URL.

A video embed exposes only an HLS playlist (m3u8) — which would need ffmpeg to mux
into a Telegram-playable MP4. The ORIGINAL uploaded MP4, however, is a blob on the
author's PDS, fetchable via ``com.atproto.sync.getBlob``. We resolve the author's
PDS from the DID document (plc.directory) and build that getBlob URL, so the video
can be downloaded as a plain MP4 — no ffmpeg, no HLS.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote, urlsplit

import httpx


logger = logging.getLogger(__name__)

PLC_DIRECTORY_URL = "https://plc.directory"
GETBLOB_PATH = "/xrpc/com.atproto.sync.getBlob"
REQUEST_TIMEOUT_SECONDS = 20.0
_PDS_SERVICE_TYPE = "AtprotoPersonalDataServer"

# Only did:plc is resolvable via plc.directory; did:web resolution differs and is
# not supported (the post then falls back to a text post).
_DID_PLC_RE = re.compile(r"^did:plc:[a-z2-7]{20,}$")

# The PDS host comes from the DID document, which is attacker-influenceable. It is
# therefore constrained to Bluesky's own PDS domains (the followed actor is
# bsky-hosted on *.host.bsky.network) so a tampered serviceEndpoint can never point
# the downloader at an internal host — an SSRF guard the IP-literal block alone
# can't provide (a hostname can resolve to an internal IP). An actor on a custom
# PDS simply falls back to a text post.
_ALLOWED_PDS_HOSTS = frozenset({"bsky.social"})
_ALLOWED_PDS_HOST_SUFFIXES = (".bsky.network",)


async def resolve_video_blob_url(did: str, cid: str) -> str | None:
    """The getBlob MP4 URL for ``(did, cid)``, or None when the DID is not a
    resolvable did:plc, the PDS cannot be found, or the endpoint is unsafe."""
    if not _DID_PLC_RE.match(did or ""):
        logger.info("Bluesky video: unsupported/invalid DID %r", did)
        return None
    if not cid:
        return None

    document = await _fetch_did_document(did)
    if document is None:
        return None

    pds = _pds_endpoint(document)
    if pds is None:
        logger.info("Bluesky video: no PDS endpoint in DID document for %s", did)
        return None

    return f"{pds.rstrip('/')}{GETBLOB_PATH}?did={quote(did)}&cid={quote(cid)}"


async def _fetch_did_document(did: str) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
            follow_redirects=True,
        ) as client:
            response = await client.get(f"{PLC_DIRECTORY_URL}/{quote(did)}")
            response.raise_for_status()
            document = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Bluesky video: failed to resolve DID %s: %s", did, exc)
        return None

    return document if isinstance(document, dict) else None


def _pds_endpoint(document: dict[str, Any]) -> str | None:
    for service in document.get("service") or []:
        if not isinstance(service, dict):
            continue
        if service.get("type") != _PDS_SERVICE_TYPE:
            continue
        base = _normalized_pds_base(str(service.get("serviceEndpoint") or "").strip())
        if base:
            return base
    return None


def _normalized_pds_base(endpoint: str) -> str | None:
    """A safe ``https://host`` base for a Bluesky PDS endpoint, or None.

    Accepts only https endpoints on an allowlisted Bluesky PDS host, and rebuilds
    the base from the bare host alone — dropping any userinfo, port, path, query or
    fragment so a crafted serviceEndpoint can neither retarget the getBlob request
    (e.g. ``https://x.bsky.network@127.0.0.1/``) nor corrupt its path.
    """
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    host = parsed.hostname.lower()
    if host not in _ALLOWED_PDS_HOSTS and not any(host.endswith(s) for s in _ALLOWED_PDS_HOST_SUFFIXES):
        return None
    return f"https://{host}"
