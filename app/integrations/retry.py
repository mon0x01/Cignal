"""Bounded retry policy for transient connector fetch failures."""
from __future__ import annotations

import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


def is_retryable_connector_error(exc: Exception) -> bool:
    """Retry only likely-transient transport, timeout and server-side failures."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code in {408, 425, 429}:
        return True
    if isinstance(status_code, int) and 500 <= status_code <= 599:
        return True
    name = type(exc).__name__.lower()
    return isinstance(exc, (TimeoutError, ConnectionError)) or any(
        marker in name
        for marker in ("timeout", "connecterror", "connectionerror", "readerror", "remoteprotocol")
    )


def _retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(value))
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def fetch_with_retry(
    fetch: Callable[[], list],
    *,
    max_attempts: int = 3,
    base_delay_seconds: float = 0.5,
    max_delay_seconds: float = 8.0,
    retryable: Callable[[Exception], bool] = is_retryable_connector_error,
    sleep: Callable[[float], None] | None = None,
    jitter: Callable[[float, float], float] | None = None,
    on_retry: Callable[[int, float, Exception], None] | None = None,
) -> tuple[list, int]:
    """Fetch with capped exponential backoff and jitter; return data and attempts.

    Non-transient errors fail immediately. Retry-After is honored but capped so a
    misconfigured provider cannot stall the ingestion worker indefinitely.
    """
    attempts_limit = max(1, min(5, int(max_attempts)))
    maximum_delay = max(0.0, min(30.0, float(max_delay_seconds)))
    base_delay = min(maximum_delay, max(0.0, float(base_delay_seconds)))
    sleep_fn = sleep or time.sleep
    jitter_fn = jitter or random.uniform

    for attempt in range(1, attempts_limit + 1):
        try:
            result = fetch()
            if not isinstance(result, list):
                raise TypeError("connector_fetch_must_return_list")
            return result, attempt
        except Exception as exc:
            exc.cignal_fetch_attempts = attempt
            if attempt >= attempts_limit or not retryable(exc):
                raise
            ceiling = min(maximum_delay, base_delay * (2 ** (attempt - 1)))
            delay = max(0.0, min(ceiling, float(jitter_fn(0.0, ceiling))))
            retry_after = _retry_after_seconds(exc)
            if retry_after is not None:
                delay = min(maximum_delay, max(delay, retry_after))
            if on_retry is not None:
                on_retry(attempt, delay, exc)
            sleep_fn(delay)

    raise RuntimeError("connector_retry_loop_exited_unexpectedly")
