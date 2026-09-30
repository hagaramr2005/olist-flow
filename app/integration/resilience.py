"""Retry with exponential backoff + jitter, hard timeout, circuit breaker."""
from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from enum import Enum
from typing import Callable, TypeVar

from app.core.logging import get_logger
from app.core.metrics import metrics
from app.integration.base import CarrierUnavailable

log = get_logger("resilience")
T = TypeVar("T")
_pool = ThreadPoolExecutor(max_workers=32, thread_name_prefix="carrier-io")


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpen(CarrierUnavailable):
    """Raised instantly (no network call) while the breaker is open."""


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 3, reset_s: float = 15.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.name, self.threshold, self.reset_s, self._clock = name, failure_threshold, reset_s, clock
        self._failures = 0
        self._opened_at = 0.0
        self._state = BreakerState.CLOSED
        self._lock = threading.Lock()

    @property
    def state(self) -> BreakerState:
        with self._lock:
            if self._state == BreakerState.OPEN and self._clock() - self._opened_at >= self.reset_s:
                self._state = BreakerState.HALF_OPEN
            return self._state

    def before_call(self) -> None:
        if self.state == BreakerState.OPEN:
            metrics.inc("breaker_short_circuit", carrier=self.name)
            raise CircuitOpen(f"circuit open for {self.name}")

    def on_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = BreakerState.CLOSED

    def on_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state == BreakerState.HALF_OPEN or self._failures >= self.threshold:
                if self._state != BreakerState.OPEN:
                    log.warning("circuit opened", extra={"ctx": {"carrier": self.name}})
                    metrics.inc("breaker_opened", carrier=self.name)
                self._state = BreakerState.OPEN
                self._opened_at = self._clock()


def call_with_timeout(fn: Callable[[], T], timeout_s: float) -> T:
    fut = _pool.submit(fn)
    try:
        return fut.result(timeout=timeout_s)
    except FutTimeout as e:
        fut.cancel()
        raise CarrierUnavailable(f"timeout after {timeout_s}s") from e


def resilient_call(breaker: CircuitBreaker, fn: Callable[[], T], *, retries: int, timeout_s: float,
                   op: str, base_delay_s: float = 0.05) -> T:
    """Only CarrierUnavailable is retried. Business errors (coverage, capacity) propagate at once."""
    breaker.before_call()
    attempt = 0
    while True:
        try:
            with metrics.timer("carrier_call", carrier=breaker.name, op=op):
                result = call_with_timeout(fn, timeout_s)
            breaker.on_success()
            metrics.inc("carrier_calls", carrier=breaker.name, op=op, outcome="ok")
            return result
        except CarrierUnavailable as e:
            breaker.on_failure()
            metrics.inc("carrier_calls", carrier=breaker.name, op=op, outcome="fail")
            attempt += 1
            if attempt > retries or breaker.state == BreakerState.OPEN:
                raise
            time.sleep(base_delay_s * (2 ** (attempt - 1)) * (0.5 + random.random()))
