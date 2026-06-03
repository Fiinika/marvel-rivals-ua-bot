from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiohttp import ClientOSError


logger = logging.getLogger(__name__)

T = TypeVar("T")

TELEGRAM_SEND_RETRIES = 3
TELEGRAM_REQUEST_TIMEOUT = 30
TELEGRAM_INTER_MESSAGE_DELAY_SECONDS = 0.35
TELEGRAM_MAX_BACKOFF_SECONDS = 30

RETRYABLE_SEND_ERRORS = (
    TelegramNetworkError,
    TelegramRetryAfter,
    TimeoutError,
    ClientOSError,
)


async def send_with_retries(
    send_method: Callable[..., Awaitable[T]],
    *,
    label: str,
    attempts: int = TELEGRAM_SEND_RETRIES,
    request_timeout: int = TELEGRAM_REQUEST_TIMEOUT,
    **kwargs: Any,
) -> T:
    kwargs.setdefault("request_timeout", request_timeout)
    delay = 1.0

    for attempt in range(1, attempts + 1):
        try:
            return await send_method(**kwargs)
        except RETRYABLE_SEND_ERRORS as exc:
            if attempt >= attempts:
                logger.exception(
                    "Telegram send failed after %s attempts: %s",
                    attempts,
                    label,
                )
                raise

            sleep_for = _retry_delay(exc, delay)
            logger.warning(
                "Telegram send failed for %s on attempt %s/%s: %s. Retrying in %.1fs.",
                label,
                attempt,
                attempts,
                exc,
                sleep_for,
            )
            await asyncio.sleep(sleep_for)
            delay = min(delay * 2, 4.0)

    raise RuntimeError("unreachable")


async def delay_between_telegram_sends() -> None:
    await asyncio.sleep(TELEGRAM_INTER_MESSAGE_DELAY_SECONDS)


def _retry_delay(exc: BaseException, fallback_delay: float) -> float:
    if isinstance(exc, TelegramRetryAfter):
        return min(max(float(exc.retry_after), fallback_delay), TELEGRAM_MAX_BACKOFF_SECONDS)

    return fallback_delay
