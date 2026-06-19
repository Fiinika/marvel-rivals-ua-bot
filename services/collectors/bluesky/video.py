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
        endpoint = str(service.get("serviceEndpoint") or "").strip()
        # Require a plain https host (no IP literal / non-https) before we hand the
        # URL to the downloader, as defence in depth against a tampered DID document.
        if _is_safe_https_host(endpoint):
            return endpoint
    return None


def _is_safe_https_host(url: str) -> bool:
    parsed = urlsplit(url.strip())
    return parsed.scheme == "https" and bool(parsed.hostname)
