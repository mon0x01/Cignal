from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.integrations.retry import fetch_with_retry, is_retryable_connector_error


def test_fetch_retries_transient_failure_with_bounded_exponential_backoff():
    calls = {"fetch": 0, "sleep": []}

    def fetch():
        calls["fetch"] += 1
        if calls["fetch"] < 3:
            raise TimeoutError("temporary upstream timeout")
        return ["signal"]

    result, attempts = fetch_with_retry(
        fetch, max_attempts=4, base_delay_seconds=0.5, max_delay_seconds=4,
        sleep=calls["sleep"].append, jitter=lambda low, high: high,
    )

    assert result == ["signal"]
    assert attempts == 3
    assert calls["fetch"] == 3
    assert calls["sleep"] == [0.5, 1.0]


def test_fetch_stops_after_bounded_attempts_and_exposes_attempt_count():
    calls = {"fetch": 0, "sleep": []}

    def fetch():
        calls["fetch"] += 1
        raise TimeoutError("provider timeout")

    with pytest.raises(TimeoutError) as captured:
        fetch_with_retry(
            fetch, max_attempts=3, base_delay_seconds=0.1, max_delay_seconds=1,
            sleep=calls["sleep"].append, jitter=lambda low, high: high,
        )

    assert calls["fetch"] == 3
    assert calls["sleep"] == [0.1, 0.2]
    assert captured.value.cignal_fetch_attempts == 3


def test_fetch_does_not_retry_permanent_client_or_payload_errors():
    calls = {"fetch": 0, "sleep": []}

    def fetch():
        calls["fetch"] += 1
        raise ValueError("invalid provider payload")

    with pytest.raises(ValueError):
        fetch_with_retry(fetch, sleep=calls["sleep"].append)

    assert calls == {"fetch": 1, "sleep": []}
    assert is_retryable_connector_error(ValueError("bad payload")) is False


def test_retry_after_is_honored_but_capped_by_policy():
    calls = {"fetch": 0, "sleep": []}

    class RateLimitedError(Exception):
        response = SimpleNamespace(status_code=429, headers={"Retry-After": "100"})

    def fetch():
        calls["fetch"] += 1
        if calls["fetch"] == 1:
            raise RateLimitedError()
        return []

    result, attempts = fetch_with_retry(
        fetch, max_attempts=2, base_delay_seconds=0.5, max_delay_seconds=3,
        sleep=calls["sleep"].append, jitter=lambda low, high: 0,
    )

    assert result == []
    assert attempts == 2
    assert calls["sleep"] == [3]


def test_non_list_connector_result_is_rejected_without_retry():
    calls = {"fetch": 0}

    def fetch():
        calls["fetch"] += 1
        return {"not": "a list"}

    with pytest.raises(TypeError, match="connector_fetch_must_return_list"):
        fetch_with_retry(fetch)
    assert calls["fetch"] == 1
