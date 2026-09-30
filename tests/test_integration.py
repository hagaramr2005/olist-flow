import pytest

from app.core.config import Settings
from app.core.models import OrderIn, ServiceLevel, ShipmentStatus
from app.integration.base import CapacityExceeded, CarrierUnavailable, CoverageError
from app.integration.resilience import BreakerState, CircuitBreaker, CircuitOpen, resilient_call
from app.integration.simulated import SimulatedCarrier
from app.integration.world import PROFILE_BY_ID

O = OrderIn(order_id="x", customer_state="RJ", weight_kg=2.0)


def carrier(cid="RAPIDOSUL"):
    return SimulatedCarrier(PROFILE_BY_ID[cid], seed=1, flakiness_scale=0.0)


def test_booking_is_idempotent():
    c = carrier()
    a = c.book_shipment("SP", O, ServiceLevel.STANDARD, "k1")
    b = c.book_shipment("SP", O, ServiceLevel.STANDARD, "k1")
    assert a.tracking_code == b.tracking_code and b.replayed and not a.replayed
    assert c.capacity_snapshot()["load"] == 1            # no second shipment created


def test_capacity_and_coverage_errors():
    c = carrier()
    c.capacity_cut = True
    with pytest.raises(CapacityExceeded):
        c.book_shipment("SP", O, ServiceLevel.STANDARD, "k2")
    with pytest.raises(CoverageError):
        carrier("ECOFREIGHT").get_quote("SP", OrderIn(order_id="y", customer_state="AM", weight_kg=1), ServiceLevel.ECONOMY)
    assert not carrier("AMAZONLOG").check_coverage("SP", O).covered          # AmazonLog does not serve Rio


def test_restricted_and_weight_rules():
    assert not carrier("RAPIDOSUL").check_coverage("SP", O.model_copy(update={"restricted": True})).covered
    assert not carrier("CENTRALEXPRESS").check_coverage("SP", O.model_copy(update={"weight_kg": 50})).covered


def test_tracking_timeline_is_ordered_and_terminal():
    c = carrier()
    b = c.book_shipment("SP", O, ServiceLevel.STANDARD, "k3")
    ev = c.track_shipment(b.tracking_code, 60)
    days = [e.day for e in ev]
    assert days == sorted(days) and ev[0].status == ShipmentStatus.CREATED
    assert ev[-1].status in (ShipmentStatus.DELIVERED, ShipmentStatus.FAILED)
    assert c.cancel_shipment(b.tracking_code) and c.capacity_snapshot()["load"] == 0


def test_retry_then_success():
    br = CircuitBreaker("t", 5, 10)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise CarrierUnavailable("503")
        return "ok"
    assert resilient_call(br, flaky, retries=3, timeout_s=1, op="t", base_delay_s=0.001) == "ok" and calls["n"] == 3


def test_business_errors_are_not_retried():
    br = CircuitBreaker("t", 5, 10)
    calls = {"n": 0}

    def bad():
        calls["n"] += 1
        raise CoverageError("no")
    with pytest.raises(CoverageError):
        resilient_call(br, bad, retries=3, timeout_s=1, op="t", base_delay_s=0.001)
    assert calls["n"] == 1


def test_timeout_counts_as_failure():
    import time
    br = CircuitBreaker("t", 5, 10)
    with pytest.raises(CarrierUnavailable):
        resilient_call(br, lambda: time.sleep(0.5), retries=0, timeout_s=0.05, op="t")


def test_circuit_breaker_opens_short_circuits_and_recovers():
    now = [0.0]
    br = CircuitBreaker("t", failure_threshold=2, reset_s=10, clock=lambda: now[0])
    boom = lambda: (_ for _ in ()).throw(CarrierUnavailable("down"))
    for _ in range(2):
        with pytest.raises(CarrierUnavailable):
            resilient_call(br, boom, retries=0, timeout_s=1, op="t")
    assert br.state == BreakerState.OPEN
    with pytest.raises(CircuitOpen):
        resilient_call(br, lambda: "never called", retries=0, timeout_s=1, op="t")
    now[0] = 11.0
    assert br.state == BreakerState.HALF_OPEN
    assert resilient_call(br, lambda: "ok", retries=0, timeout_s=1, op="t") == "ok" and br.state == BreakerState.CLOSED
