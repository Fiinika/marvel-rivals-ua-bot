"""Spacing for moderation-queue sends within one collection tick.

A scheduler tick can find several new items across all sources at once and, left
unthrottled, fire them into the admin chat back-to-back in a second or two. That
both reads as spam and risks Telegram's per-chat flood limit. :class:`SubmissionThrottle`
enforces a MINIMUM gap between consecutive sends so a burst trickles in instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


def _default_clock() -> float:
    # Monotonic time tied to the running loop; never goes backwards.
    return asyncio.get_running_loop().time()


class SubmissionThrottle:
    """Enforces a minimum interval between successive :meth:`wait` calls.

    The first call (and any call made after the interval has already elapsed)
    returns immediately, so a single item is never delayed — only a rapid
    follow-up sleeps out the time still remaining in the window. A zero or
    negative interval disables throttling entirely. One instance is shared across
    every collector in a tick, so the gap is honoured across sources too.

    ``clock``/``sleep`` are injectable purely so the timing can be unit-tested
    without real waits; production uses the loop clock and ``asyncio.sleep``.
    """

    def __init__(
        self,
        min_interval_seconds: float,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._clock = clock or _default_clock
        self._sleep = sleep or asyncio.sleep
        self._last_send_at: float | None = None

    async def wait(self) -> None:
        if self.min_interval_seconds <= 0:
            return

        if self._last_send_at is not None:
            remaining = self.min_interval_seconds - (self._clock() - self._last_send_at)
            if remaining > 0:
                await self._sleep(remaining)

        # Stamp the moment the caller is cleared to send (after any wait), so the
        # next gap is measured from this send, not from when we started waiting.
        self._last_send_at = self._clock()
