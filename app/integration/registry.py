"""Registry of resilient carrier gateways. The rest of the system only sees ResilientCarrier."""
from __future__ import annotations

from typing import Iterable

from app.core.config import Settings
from app.core.models import OrderIn, ServiceLevel
from app.integration.base import BookingResult, CarrierAdapter, CoverageResult, QuoteResult, TrackingEvent
from app.integration.resilience import BreakerState, CircuitBreaker, resilient_call
from app.integration.simulated import SimulatedCarrier
from app.integration.world import PROFILES


class ResilientCarrier:
    """Wraps any CarrierAdapter with timeout + retry + circuit breaker."""

    def __init__(self, adapter: CarrierAdapter, settings: Settings) -> None:
        self.adapter = adapter
        self.carrier_id = adapter.carrier_id
        self.name = adapter.name
        self.s = settings
        self.breaker = CircuitBreaker(adapter.carrier_id, settings.breaker_failure_threshold, settings.breaker_reset_s)

    def _call(self, op: str, fn):
        return resilient_call(self.breaker, fn, retries=self.s.carrier_retries,
                              timeout_s=self.s.carrier_timeout_s, op=op)

    def check_coverage(self, origin_state: str, order: OrderIn) -> CoverageResult:
        return self._call("coverage", lambda: self.adapter.check_coverage(origin_state, order))

    def get_quote(self, origin_state: str, order: OrderIn, service: ServiceLevel) -> QuoteResult:
        return self._call("quote", lambda: self.adapter.get_quote(origin_state, order, service))

    def book_shipment(self, origin_state, order, service, idempotency_key) -> BookingResult:
        return self._call("book", lambda: self.adapter.book_shipment(origin_state, order, service, idempotency_key))

    def cancel_shipment(self, code: str) -> bool:
        return self._call("cancel", lambda: self.adapter.cancel_shipment(code))

    def generate_label(self, code: str) -> str:
        return self._call("label", lambda: self.adapter.generate_label(code))

    def track_shipment(self, code: str, as_of_day: float) -> list[TrackingEvent]:
        return self._call("track", lambda: self.adapter.track_shipment(code, as_of_day))

    def capacity_snapshot(self) -> dict:
        d = self.adapter.capacity_snapshot()
        d["breaker"] = self.breaker.state.value
        return d

    @property
    def healthy(self) -> bool:
        return self.breaker.state != BreakerState.OPEN


class CarrierRegistry:
    def __init__(self, settings: Settings, adapters: Iterable[CarrierAdapter] | None = None) -> None:
        adapters = list(adapters) if adapters is not None else [SimulatedCarrier(p, settings.seed, settings.flakiness_scale) for p in PROFILES]
        self._carriers = {a.carrier_id: ResilientCarrier(a, settings) for a in adapters}

    def all(self) -> list[ResilientCarrier]:
        return list(self._carriers.values())

    def get(self, carrier_id: str) -> ResilientCarrier:
        return self._carriers[carrier_id]

    def raw(self, carrier_id: str):
        return self._carriers[carrier_id].adapter

    def __len__(self) -> int:
        return len(self._carriers)
