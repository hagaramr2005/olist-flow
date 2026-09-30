"""Simulated carrier adapter implementing the unified contract.

Stands in for real carrier APIs: same interface, injectable failures, real capacity accounting,
idempotent booking and a deterministic tracking timeline per shipment.
"""
from __future__ import annotations

import hashlib
import random
import threading
import time
from typing import Optional

from app.core.geo import region
from app.core.models import OrderIn, ServiceLevel, ShipmentStatus
from app.integration import world
from app.integration.base import (BookingResult, CapacityExceeded, CarrierAdapter, CarrierUnavailable,
                                  CoverageError, CoverageResult, QuoteResult, TrackingEvent)


class SimulatedCarrier(CarrierAdapter):
    def __init__(self, profile: world.CarrierProfile, seed: int = 42, flakiness_scale: float = 1.0) -> None:
        self.p = profile
        self.carrier_id = profile.carrier_id
        self.name = profile.name
        self._seed = seed
        self._flaky = profile.api_flakiness * flakiness_scale
        self._rng = random.Random(f"{seed}:{profile.carrier_id}")
        self._lock = threading.RLock()
        self._bookings: dict[str, BookingResult] = {}      # idempotency_key -> booking
        self._meta: dict[str, dict] = {}                   # tracking_code -> shipment facts
        self._load = 0
        # Fault-injection switches (driven by the disruption API / tests)
        self.outage = False
        self.capacity_cut = False
        self.latency_s = 0.0
        self.stalled: set[str] = set()

    # ------------------------------------------------------------------ helpers
    def _maybe_fail(self) -> None:
        if self.latency_s:
            time.sleep(self.latency_s)
        if self.outage:
            raise CarrierUnavailable(f"{self.name}: API unavailable")
        if self._rng.random() < self._flaky:
            raise CarrierUnavailable(f"{self.name}: transient 503")

    @property
    def capacity(self) -> int:
        return 0 if self.capacity_cut else self.p.daily_capacity

    # ----------------------------------------------------------------- contract
    def _coverage_rule(self, origin_state: str, order: OrderIn) -> CoverageResult:
        """Pure business rule (no network simulation)."""
        p = self.p
        if region(origin_state) not in p.origin_regions:
            return CoverageResult(covered=False, reason=f"no pickup in {origin_state}")
        if region(order.customer_state) not in p.regions:
            return CoverageResult(covered=False, reason=f"no service area {order.customer_state}")
        if order.weight_kg > p.max_weight_kg:
            return CoverageResult(covered=False, reason=f"weight>{p.max_weight_kg}kg")
        if order.restricted and not p.accepts_restricted:
            return CoverageResult(covered=False, reason="restricted goods not accepted")
        return CoverageResult(covered=True)

    def check_coverage(self, origin_state: str, order: OrderIn) -> CoverageResult:
        self._maybe_fail()
        return self._coverage_rule(origin_state, order)

    def get_quote(self, origin_state: str, order: OrderIn, service: ServiceLevel) -> QuoteResult:
        self._maybe_fail()
        cov = self._coverage_rule(origin_state, order)
        if not cov.covered:
            raise CoverageError(cov.reason)
        if service not in self.p.services:
            raise CoverageError(f"service {service.value} not offered")
        return QuoteResult(
            carrier_id=self.carrier_id, service=service,
            cost=world.freight_cost(self.p, origin_state, order.customer_state, order.weight_kg, order.volume_cm3, service),
            promised_days=world.promised_days(self.p, origin_state, order.customer_state, service))

    def book_shipment(self, origin_state: str, order: OrderIn, service: ServiceLevel,
                      idempotency_key: str) -> BookingResult:
        with self._lock:
            if idempotency_key in self._bookings:                      # replay -> same result, no new shipment
                return self._bookings[idempotency_key].model_copy(update={"replayed": True})
        self._maybe_fail()
        with self._lock:
            if idempotency_key in self._bookings:
                return self._bookings[idempotency_key].model_copy(update={"replayed": True})
            if self._load >= self.capacity:
                raise CapacityExceeded(f"{self.name}: no capacity")
            q = self.get_quote(origin_state, order, service)
            code = "OF" + hashlib.sha1(f"{self.carrier_id}:{idempotency_key}".encode()).hexdigest()[:10].upper()
            rng = random.Random(f"{self._seed}:{code}")
            actual, delayed = world.sample_actual_days(self.p, origin_state, order.customer_state, service, rng)
            failed = rng.random() < 0.012
            self._meta[code] = dict(actual=actual, delayed=delayed, failed=failed, promised=q.promised_days,
                                    origin=origin_state, dest=order.customer_state, cancelled=False,
                                    handling=self.p.handling_days)
            self._load += 1
            b = BookingResult(carrier_id=self.carrier_id, tracking_code=code, idempotency_key=idempotency_key,
                              cost=q.cost, promised_days=q.promised_days)
            self._bookings[idempotency_key] = b
            return b

    def cancel_shipment(self, tracking_code: str) -> bool:
        self._maybe_fail()
        with self._lock:
            m = self._meta.get(tracking_code)
            if not m or m["cancelled"]:
                return False
            m["cancelled"] = True
            self._load = max(0, self._load - 1)
            return True

    def generate_label(self, tracking_code: str) -> str:
        self._maybe_fail()
        if tracking_code not in self._meta:
            raise CoverageError("unknown shipment")
        m = self._meta[tracking_code]
        return f"LABEL|{self.name}|{tracking_code}|{m['origin']}->{m['dest']}"

    def track_shipment(self, tracking_code: str, as_of_day: float) -> list[TrackingEvent]:
        self._maybe_fail()
        m = self._meta.get(tracking_code)
        if not m:
            raise CoverageError("unknown shipment")
        if m["cancelled"]:
            return [TrackingEvent(tracking_code=tracking_code, status=ShipmentStatus.CANCELLED, day=0)]
        a = m["actual"]
        n_hubs = max(1, int(a / 1.4))                      # long hauls emit a scan roughly every ~1.4 days
        hub_days = [0.25 * a + (0.55 * a) * (i + 1) / (n_hubs + 1) for i in range(n_hubs)]
        tl = [(0.0, ShipmentStatus.CREATED, None),
              (min(m["handling"] * 0.5, a * 0.2), ShipmentStatus.PICKED_UP, m["origin"])]
        tl += [(d, ShipmentStatus.AT_HUB, f"{region(m['dest'])}-HUB{i + 1}") for i, d in enumerate(hub_days)]
        tl += [(max(a - 0.3, a * 0.85), ShipmentStatus.OUT_FOR_DELIVERY, m["dest"]),
               (a, ShipmentStatus.FAILED if m["failed"] else ShipmentStatus.DELIVERED, m["dest"])]
        stall_at = hub_days[0] if tracking_code in self.stalled else None
        out = []
        for day, st, hub in tl:
            if day <= as_of_day and (stall_at is None or day <= stall_at):
                out.append(TrackingEvent(tracking_code=tracking_code, status=st, day=round(day, 2), hub=hub))
        return out

    def capacity_snapshot(self) -> dict:
        return {"carrier_id": self.carrier_id, "capacity": self.capacity, "load": self._load,
                "free_ratio": 0.0 if self.capacity == 0 else round(max(0, self.capacity - self._load) / self.capacity, 3),
                "outage": self.outage}

    # ---- twin/ops helpers
    def shipment_truth(self, tracking_code: str) -> Optional[dict]:
        return self._meta.get(tracking_code)

    def reset_load(self) -> None:
        with self._lock:
            self._load = 0
