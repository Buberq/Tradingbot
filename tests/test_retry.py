from __future__ import annotations

import random

import pytest

from krakenbot.config import RetryConfig
from krakenbot.retry import (
    PermanentError,
    RetryExhaustedError,
    TransientError,
    compute_delay,
    retry,
)


def make(fail_times: int, exc: type[Exception] = TransientError):
    calls = {"n": 0}

    def fn() -> str:
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise exc("boom")
        return "ok"

    return fn, calls


def test_succeeds_after_transient_failures():
    sleeps: list[float] = []
    cfg = RetryConfig(max_attempts=5, base_delay_s=1, max_delay_s=60, jitter=False)
    fn, calls = make(2)
    assert retry(cfg, sleep=sleeps.append)(fn)() == "ok"
    assert calls["n"] == 3
    assert sleeps == [1, 2]


def test_exhausted_raises_with_cause():
    cfg = RetryConfig(max_attempts=3, base_delay_s=1, jitter=False)
    fn, calls = make(10)
    with pytest.raises(RetryExhaustedError) as ei:
        retry(cfg, sleep=lambda _: None)(fn)()
    assert calls["n"] == 3
    assert isinstance(ei.value.__cause__, TransientError)


def test_permanent_error_is_not_retried():
    cfg = RetryConfig(max_attempts=5)
    fn, calls = make(10, PermanentError)
    with pytest.raises(PermanentError):
        retry(cfg, sleep=lambda _: None)(fn)()
    assert calls["n"] == 1


def test_unlisted_errors_are_not_retried():
    cfg = RetryConfig(max_attempts=5)
    fn, calls = make(10, ValueError)
    with pytest.raises(ValueError):
        retry(cfg, sleep=lambda _: None)(fn)()
    assert calls["n"] == 1


def test_timeouts_and_connection_errors_are_retried():
    cfg = RetryConfig(max_attempts=3, jitter=False)
    for exc in (TimeoutError, ConnectionError):
        fn, calls = make(1, exc)
        assert retry(cfg, sleep=lambda _: None)(fn)() == "ok"
        assert calls["n"] == 2


def test_backoff_is_exponential_and_capped():
    cfg = RetryConfig(base_delay_s=1, max_delay_s=10, jitter=False)
    assert [compute_delay(a, cfg) for a in range(1, 7)] == [1, 2, 4, 8, 10, 10]


def test_jitter_stays_within_cap():
    cfg = RetryConfig(base_delay_s=2, max_delay_s=30, jitter=True)
    rng = random.Random(0)
    for attempt in range(1, 8):
        cap = min(30, 2 * 2 ** (attempt - 1))
        assert 0 <= compute_delay(attempt, cfg, rng) <= cap
