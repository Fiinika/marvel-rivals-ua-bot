"""Tests for the news scheduler's cadence: it checks immediately on startup and
keeps a fixed period regardless of how long a run takes."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import main


class _Stop(Exception):
    """Breaks out of the scheduler's infinite loop inside a test."""


def _run_scheduler(monkeypatch, *, interval_minutes: int, run_durations: list[float]):
    """Drive the scheduler until it has slept len(run_durations) times.

    Returns (number of collector runs, the sleep durations it asked for).
    """
    sleeps: list[float] = []
    runs: list[int] = []
    clock = {"now": 1000.0}

    monkeypatch.setattr(main.time, "monotonic", lambda: clock["now"])

    async def fake_run_all(**_kwargs):
        # Each run consumes its scripted duration from the fake clock.
        clock["now"] += run_durations[len(runs)]
        runs.append(1)
        return []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= len(run_durations):
            raise _Stop

    monkeypatch.setattr(main, "run_all_collectors", fake_run_all)
    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    config = SimpleNamespace(news_check_interval_minutes=interval_minutes)
    try:
        asyncio.run(main._news_scheduler(bot=None, config=config, db=None))
    except _Stop:
        pass
    return len(runs), sleeps


def test_first_check_happens_before_any_sleep(monkeypatch) -> None:
    # A restart used to leave the bot blind for a whole interval — which is exactly
    # when it is most likely to have missed something.
    runs, sleeps = _run_scheduler(monkeypatch, interval_minutes=5, run_durations=[0.0])

    assert runs == 1
    assert len(sleeps) == 1


def test_the_period_is_measured_from_the_start_of_a_run(monkeypatch) -> None:
    # A 40-second run must be followed by a 260-second wait, not a 300-second one,
    # so the schedule cannot drift away from the top of the hour.
    _runs, sleeps = _run_scheduler(monkeypatch, interval_minutes=5, run_durations=[40.0, 0.0])

    assert sleeps[0] == 300.0 - 40.0
    assert sleeps[1] == 300.0


def test_a_run_longer_than_the_interval_still_yields(monkeypatch) -> None:
    # Never a zero/negative sleep: the loop must give the event loop a chance to
    # run other tasks even when a tick overruns its own period.
    _runs, sleeps = _run_scheduler(monkeypatch, interval_minutes=1, run_durations=[900.0])

    assert sleeps == [1.0]


def test_a_failing_run_does_not_stop_the_schedule(monkeypatch) -> None:
    sleeps: list[float] = []
    calls: list[int] = []
    monkeypatch.setattr(main.time, "monotonic", lambda: 0.0)

    async def boom(**_kwargs):
        calls.append(1)
        raise RuntimeError("source exploded")

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise _Stop

    monkeypatch.setattr(main, "run_all_collectors", boom)
    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    config = SimpleNamespace(news_check_interval_minutes=5)
    try:
        asyncio.run(main._news_scheduler(bot=None, config=config, db=None))
    except _Stop:
        pass

    assert len(calls) == 2  # kept polling after the failure
    assert sleeps == [300.0, 300.0]
