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


def test_pds_endpoint_extraction() -> None:
    doc = {"service": [{"type": "AtprotoPersonalDataServer", "serviceEndpoint": "https://pds.host.bsky.network"}]}
    assert _pds_endpoint(doc) == "https://pds.host.bsky.network"
    # Non-https endpoints and wrong service types are rejected; empty docs yield None.
    assert _pds_endpoint({"service": [{"type": "AtprotoPersonalDataServer", "serviceEndpoint": "http://pds"}]}) is None
    assert _pds_endpoint({"service": [{"type": "Other", "serviceEndpoint": "https://x"}]}) is None
    assert _pds_endpoint({}) is None


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
