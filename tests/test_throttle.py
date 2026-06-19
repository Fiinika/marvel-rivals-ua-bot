"""Tests for the moderation-queue send throttle and its wiring into the collector
orchestration (the gap that stops a tick dumping many items at once)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import services.collectors.runner as runner_mod
from services.collectors.base import (
    CollectionStats,
    CollectorDefinition,
    DraftCandidate,
)
from services.collectors.runner import BaseNewsCollector
from services.collectors.throttle import SubmissionThrottle


class _FakeClock:
    """Returns the supplied times in order, holding the last value afterwards."""

    def __init__(self, *times: float) -> None:
        self._times = list(times)
        self._i = 0

    def __call__(self) -> float:
        value = self._times[min(self._i, len(self._times) - 1)]
        self._i += 1
        return value


def _throttle(interval: float, *times: float):
    slept: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)

    throttle = SubmissionThrottle(interval, clock=_FakeClock(*times), sleep=_sleep)
    return throttle, slept


def test_zero_interval_never_sleeps() -> None:
    throttle, slept = _throttle(0, 0.0, 0.0, 0.0, 0.0)

    async def run() -> None:
        await throttle.wait()
        await throttle.wait()

    asyncio.run(run())
    assert slept == []


def test_first_send_is_not_delayed() -> None:
    throttle, slept = _throttle(5, 0.0, 0.0)
    asyncio.run(throttle.wait())
    assert slept == []


def test_rapid_second_send_waits_full_interval() -> None:
    # Both sends happen at t=0 -> the second waits the whole window. Clock reads:
    # first wait stamps (0.0); second wait measures (0.0) then re-stamps (0.0).
    throttle, slept = _throttle(5, 0.0, 0.0, 0.0)

    async def run() -> None:
        await throttle.wait()
        await throttle.wait()

    asyncio.run(run())
    assert slept == [5]


def test_second_send_waits_only_the_remaining_time() -> None:
    # First send stamps t=0; second is measured at t=2 -> only 3s of a 5s window left.
    throttle, slept = _throttle(5, 0.0, 2.0, 2.0)

    async def run() -> None:
        await throttle.wait()
        await throttle.wait()

    asyncio.run(run())
    assert slept == [3]


def test_send_after_window_elapsed_does_not_sleep() -> None:
    # Second send measured at t=10, well past the 5s window -> no wait.
    throttle, slept = _throttle(5, 0.0, 10.0, 10.0)

    async def run() -> None:
        await throttle.wait()
        await throttle.wait()

    asyncio.run(run())
    assert slept == []


# --- wiring: the runner waits on the throttle BEFORE each moderation send -------

DEFINITION = CollectorDefinition(
    collector_id="t",
    source_type="t",
    title_key="unused.title",
    button_key="unused.button",
)

CANDIDATE = DraftCandidate(
    source_id="s1",
    source_url="https://example.com/1",
    title="A perfectly fine news title",
    body_text="Some body text without any times in it.",
    source_name="Test Source",
    username="tester",
    original_text="original",
    article_date="2026-06-16",
    article_date_display="16 червня 2026",
)


class _OrderThrottle:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def wait(self) -> None:
        self.events.append("wait")


class _Gen:
    async def generate_draft_package(self, _draft_input, *, max_part_length):  # noqa: ANN001
        return SimpleNamespace(draft_parts=["draft text"], tags=[])


class _DB:
    async def create_ai_news_submission(self, **_kwargs) -> int:
        return 42

    async def create_album_submission(self, **_kwargs) -> int:
        return 42

    async def mark_source_seen(self, **_kwargs) -> None:
        return None


class _Collector(BaseNewsCollector):
    definition = DEFINITION

    async def fetch_listing(self):
        return []

    async def parse_entry(self, entry):
        return CANDIDATE

    def missing_gemini_warning(self) -> str:
        return "no gemini"


def test_create_moderation_submission_waits_before_sending(monkeypatch) -> None:
    events: list[str] = []

    async def _fake_send(*_args, **_kwargs) -> None:
        events.append("send")

    monkeypatch.setattr(runner_mod, "send_submission_to_moderation", _fake_send)

    collector = _Collector(config=SimpleNamespace(article_timezone="Europe/Kyiv"), db=_DB(), bot=None)
    collector.throttle = _OrderThrottle(events)
    stats = CollectionStats(collector_id="t", source_type="t", source_title="Test")

    sent = asyncio.run(collector._create_moderation_submissions(CANDIDATE, _Gen(), stats))

    assert sent is True
    assert events == ["wait", "send"]  # the gap is enforced BEFORE the send
    assert stats.sent_to_moderation == 1


# --- end-to-end: a multi-item tick spaces every send AND still sends them all ---


class _VirtualClock:
    """A controllable monotonic clock; sleeps and item prep advance it explicitly."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _PrepGen:
    """A draft generator whose 'Gemini call' takes ``prep`` seconds of clock time."""

    def __init__(self, clock: _VirtualClock, prep: float) -> None:
        self._clock = clock
        self._prep = prep

    async def generate_draft_package(self, _draft_input, *, max_part_length):  # noqa: ANN001
        self._clock.advance(self._prep)
        return SimpleNamespace(draft_parts=["draft text"], tags=[])


def _drive_tick(monkeypatch, *, interval: float, prep: float, n_items: int):
    """Run ``n_items`` candidates through TWO collectors sharing one throttle and
    return (send_times, slept) — mirroring the real scheduled-tick send path."""
    clock = _VirtualClock()
    slept: list[float] = []
    send_times: list[float] = []

    async def _sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(seconds)

    async def _fake_send(*_args, **_kwargs) -> None:
        send_times.append(clock())

    monkeypatch.setattr(runner_mod, "send_submission_to_moderation", _fake_send)

    throttle = SubmissionThrottle(interval, clock=clock, sleep=_sleep)
    config = SimpleNamespace(article_timezone="Europe/Kyiv")
    collector_a = _Collector(config=config, db=_DB(), bot=None)
    collector_b = _Collector(config=config, db=_DB(), bot=None)
    collector_a.throttle = throttle
    collector_b.throttle = throttle  # shared, exactly like run_all_collectors
    gen = _PrepGen(clock, prep)
    stats = CollectionStats(collector_id="t", source_type="t", source_title="Test")

    async def run() -> None:
        # First two items on collector A, the rest on collector B — proving the gap
        # carries ACROSS sources within a tick, not just within one collector.
        for i in range(n_items):
            collector = collector_a if i < 2 else collector_b
            await collector._create_moderation_submissions(CANDIDATE, gen, stats)

    asyncio.run(run())
    return send_times, slept, stats


def test_fast_items_are_spaced_by_the_interval(monkeypatch) -> None:
    # The user's actual complaint: cheap items that would otherwise dump at once.
    send_times, slept, stats = _drive_tick(monkeypatch, interval=5, prep=0.5, n_items=4)

    assert stats.sent_to_moderation == 4  # ALL items still get through
    assert len(send_times) == 4
    gaps = [round(b - a, 6) for a, b in zip(send_times, send_times[1:])]
    assert gaps == [5, 5, 5]  # every consecutive send is exactly one interval apart
    assert slept == [4.5, 4.5, 4.5]  # first send paid nothing; the rest waited the remainder


def test_slow_items_are_not_extra_delayed_but_stay_spaced(monkeypatch) -> None:
    # When each Gemini call already exceeds the interval, sends are naturally >5s
    # apart, so the throttle correctly adds nothing — and there is no burst anyway.
    send_times, slept, stats = _drive_tick(monkeypatch, interval=5, prep=6.0, n_items=3)

    assert stats.sent_to_moderation == 3
    gaps = [round(b - a, 6) for a, b in zip(send_times, send_times[1:])]
    assert gaps == [6, 6]  # already wider than the interval
    assert slept == []  # no artificial delay added
    assert all(g >= 5 for g in gaps)  # never a burst
