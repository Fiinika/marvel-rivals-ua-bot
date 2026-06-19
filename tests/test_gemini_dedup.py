"""Tests for the Gemini duplicate-title verdict parser. Parsing must fail open
(never report a duplicate it isn't sure about), because a false positive would
silently drop real news while a miss only costs a rare double post."""

from __future__ import annotations

import asyncio

from services.gemini import (
    GeminiDraftGenerator,
    _gemini_retry_delay_seconds,
    _is_rate_limit_error,
    _is_retryable_gemini_error,
    _parse_duplicate_verdict,
)


class _FakeAPIError(Exception):
    def __init__(self, code: int, message: str = "") -> None:
        super().__init__(message)
        self.code = code


def test_rate_limit_detection() -> None:
    assert _is_rate_limit_error(_FakeAPIError(429, "RESOURCE_EXHAUSTED")) is True
    assert _is_rate_limit_error(_FakeAPIError(400, "bad request")) is False


def test_retryable_error_detection() -> None:
    assert _is_retryable_gemini_error(_FakeAPIError(429, "RequestsPerMinute throttle")) is True
    assert _is_retryable_gemini_error(_FakeAPIError(503, "UNAVAILABLE")) is True
    assert _is_retryable_gemini_error(_FakeAPIError(500)) is True
    assert _is_retryable_gemini_error(_FakeAPIError(400, "invalid")) is False
    # A per-DAY free-tier cap is NOT retried (a short retry won't clear it).
    assert _is_retryable_gemini_error(
        _FakeAPIError(429, "GenerateRequestsPerDayPerProjectPerModel-FreeTier, limit: 20")
    ) is False


def test_retry_delay_honours_api_then_caps_then_falls_back() -> None:
    assert _gemini_retry_delay_seconds(_FakeAPIError(429, "'retryDelay': '40s'"), 0) == 20.0  # capped
    assert _gemini_retry_delay_seconds(_FakeAPIError(429, "retryDelay: 5s"), 0) == 6.0  # api + 1
    assert _gemini_retry_delay_seconds(_FakeAPIError(429, "no delay"), 0) == 8.0  # fallback
    assert _gemini_retry_delay_seconds(_FakeAPIError(429, "no delay"), 1) == 16.0


def test_parse_true_with_exact_match() -> None:
    verdict = _parse_duplicate_verdict('{"duplicate": true, "match": "Old Title"}', ["Old Title"])
    assert verdict.is_duplicate is True
    assert verdict.matched_title == "Old Title"


def test_parse_true_maps_match_to_known_title_case_insensitively() -> None:
    verdict = _parse_duplicate_verdict('{"duplicate": true, "match": "old title"}', ["Old Title"])
    assert verdict.is_duplicate is True
    assert verdict.matched_title == "Old Title"  # canonical casing from the known list


def test_parse_true_keeps_unknown_match_text() -> None:
    verdict = _parse_duplicate_verdict('{"duplicate": true, "match": "Mystery"}', ["Old Title"])
    assert verdict.is_duplicate is True
    assert verdict.matched_title == "Mystery"


def test_parse_false() -> None:
    verdict = _parse_duplicate_verdict('{"duplicate": false, "match": ""}', ["Old Title"])
    assert verdict.is_duplicate is False
    assert verdict.matched_title is None


def test_parse_strips_code_fences() -> None:
    raw = "```json\n{\"duplicate\": true, \"match\": \"X\"}\n```"
    verdict = _parse_duplicate_verdict(raw, ["X"])
    assert verdict.is_duplicate is True


def test_parse_ignores_prose_around_json() -> None:
    raw = 'Here is my answer: {"duplicate": true, "match": "X"} — hope it helps!'
    verdict = _parse_duplicate_verdict(raw, ["X"])
    assert verdict.is_duplicate is True


def test_parse_fails_open_on_garbage() -> None:
    for raw in ("not json at all", "", "{broken", "[]", "null"):
        assert _parse_duplicate_verdict(raw, ["X"]).is_duplicate is False


def test_parse_stringified_false_is_not_a_duplicate() -> None:
    # A model that stringifies the boolean must NOT be misread as a duplicate —
    # bool("false") is truthy, so a naive check would silently drop real news.
    for flag in ('"false"', '"no"', '"0"', '"none"', "0", "false"):
        raw = f'{{"duplicate": {flag}, "match": "X"}}'
        assert _parse_duplicate_verdict(raw, ["X"]).is_duplicate is False, raw


def test_parse_accepts_truthy_variants() -> None:
    for flag in ("true", '"true"', '"TRUE"', '"yes"', '"1"', "1"):
        raw = f'{{"duplicate": {flag}, "match": "X"}}'
        assert _parse_duplicate_verdict(raw, ["X"]).is_duplicate is True, raw


def test_find_duplicate_title_short_circuits_without_calling_the_model() -> None:
    # No API call happens (and no key is needed) when there is nothing to compare.
    generator = GeminiDraftGenerator(api_key="unused", model="unused")
    assert asyncio.run(generator.find_duplicate_title("New", [])).is_duplicate is False
    assert asyncio.run(generator.find_duplicate_title("", ["Existing"])).is_duplicate is False
    assert asyncio.run(generator.find_duplicate_title("   ", ["Existing"])).is_duplicate is False
