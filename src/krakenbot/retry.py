"""Ponawianie z wykładniczym backoffem i losowym rozrzutem (full jitter).

Ponawiamy tylko błędy przejściowe (TransientError, timeouty, błędy połączenia).
Błąd trwały (np. odrzucone zlecenie, zły podpis) przerywa od razu. Dzięki temu
bot nie zasypuje Krakena powtórkami i nie wpada w limity zapytań.
"""

from __future__ import annotations

import functools
import random
import time
from collections.abc import Callable
from typing import ParamSpec, TypeVar

from krakenbot.config import RetryConfig
from krakenbot.logging_setup import get_logger

P = ParamSpec("P")
T = TypeVar("T")

log = get_logger("retry")


class TransientError(Exception):
    """Błąd przejściowy: warto spróbować ponownie (timeout, 5xx, rate limit)."""


class PermanentError(Exception):
    """Błąd trwały: ponawianie nic nie da i może zaszkodzić."""


class RetryExhaustedError(Exception):
    """Wszystkie próby się nie powiodły. Oryginalny błąd w __cause__."""


DEFAULT_RETRY_ON: tuple[type[BaseException], ...] = (
    TransientError,
    TimeoutError,
    ConnectionError,
)


def compute_delay(attempt: int, cfg: RetryConfig, rng: random.Random | None = None) -> float:
    """Opóźnienie przed kolejną próbą. attempt liczony od 1 (po pierwszej porażce)."""
    cap = min(cfg.max_delay_s, cfg.base_delay_s * (2 ** (attempt - 1)))
    if not cfg.jitter:
        return cap
    return (rng or random).uniform(0, cap)  # noqa: S311 - jitter, nie kryptografia


def retry(
    cfg: RetryConfig,
    retry_on: tuple[type[BaseException], ...] = DEFAULT_RETRY_ON,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    def decorator(fn: Callable[P, T]) -> Callable[P, T]:
        @functools.wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            for attempt in range(1, cfg.max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except PermanentError:
                    raise
                except retry_on as exc:
                    if attempt == cfg.max_attempts:
                        log.error(
                            "%s: wyczerpano %d prób, ostatni błąd: %s",
                            fn.__qualname__, attempt, exc,
                        )
                        raise RetryExhaustedError(
                            f"{fn.__qualname__}: {attempt} nieudanych prób"
                        ) from exc
                    delay = compute_delay(attempt, cfg)
                    log.warning(
                        "%s: próba %d/%d nieudana (%s), ponawiam za %.1fs",
                        fn.__qualname__, attempt, cfg.max_attempts, exc, delay,
                    )
                    sleep(delay)
            raise AssertionError("unreachable")  # pragma: no cover

        return wrapper

    return decorator
