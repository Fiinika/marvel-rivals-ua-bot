"""Tests for resolving a Bluesky video post to its direct getBlob MP4 URL."""

from __future__ import annotations

import asyncio
from urllib.parse import quote

import services.collectors.bluesky.video as video
from services.collectors.bluesky.video import _pds_endpoint, resolve_video_blob_url


DID = "did:plc:cbw6e4uggzgly7vfnde4rrac"
CID = "bafkreieyoi3vyluyzrqqzctywn2we7zwdpl6nkpcd5hz43tuprkgy6odke"


def test_unsupported_or_invalid_did_returns_none() -> None:
    # did:web is not resolvable via plc.directory; garbage is rejected outright.
    assert asyncio.run(resolve_video_blob_url("did:web:example.com", CID)) is None
    assert asyncio.run(resolve_video_blob_url("garbage", CID)) is None


def test_empty_cid_returns_none() -> None:
    assert asyncio.run(resolve_video_blob_url(DID, "")) is None


def _pds_doc(endpoint: str) -> dict:
    return {"service": [{"type": "AtprotoPersonalDataServer", "serviceEndpoint": endpoint}]}


def test_pds_endpoint_extraction() -> None:
    assert _pds_endpoint(_pds_doc("https://pds.host.bsky.network")) == "https://pds.host.bsky.network"
    # Non-https endpoints and wrong service types are rejected; empty docs yield None.
    assert _pds_endpoint(_pds_doc("http://pds.host.bsky.network")) is None
    assert _pds_endpoint({"service": [{"type": "Other", "serviceEndpoint": "https://x.bsky.network"}]}) is None
    assert _pds_endpoint({}) is None


def test_pds_endpoint_rejects_non_bsky_hosts() -> None:
    # SSRF guard: the PDS host comes from the (attacker-influenceable) DID document,
    # so only Bluesky's own PDS domains are accepted — a hostname that could resolve
    # to an internal IP is refused outright.
    assert _pds_endpoint(_pds_doc("https://attacker.example")) is None
    assert _pds_endpoint(_pds_doc("https://internal.host")) is None
    # A look-alike suffix must not slip through.
    assert _pds_endpoint(_pds_doc("https://evil-bsky.network.attacker.com")) is None


def test_pds_endpoint_strips_path_userinfo_and_port() -> None:
    # A path/query must not corrupt the getBlob request target...
    assert _pds_endpoint(_pds_doc("https://x.bsky.network/foo?a=b")) == "https://x.bsky.network"
    # ...and userinfo smuggling (real host after @) is rejected, not honoured.
    assert _pds_endpoint(_pds_doc("https://x.bsky.network@127.0.0.1/")) is None


def test_resolve_builds_getblob_url(monkeypatch) -> None:
    async def fake_doc(did: str):
        assert did == DID
        return {"service": [{"type": "AtprotoPersonalDataServer", "serviceEndpoint": "https://polypore.host.bsky.network"}]}

    monkeypatch.setattr(video, "_fetch_did_document", fake_doc)

    url = asyncio.run(resolve_video_blob_url(DID, CID))
    assert url == (
        f"https://polypore.host.bsky.network/xrpc/com.atproto.sync.getBlob?did={quote(DID)}&cid={quote(CID)}"
    )


def test_resolve_returns_none_when_did_doc_unavailable(monkeypatch) -> None:
    async def no_doc(_did: str):
        return None

    monkeypatch.setattr(video, "_fetch_did_document", no_doc)
    assert asyncio.run(resolve_video_blob_url(DID, CID)) is None
