import json

import pytest

from app.core.models import OrderIn, Severity
from app.intelligence.optimizer import NoFeasibleOption
from app.services import analytics, twin
from app.services.engine import ConflictError, NotFound


def place(c, order, key="key-0001", profile="balanced"):
    d = c.engine.decide(order, profile)
    return d, c.engine.book(order.order_id, key, d["decision_id"])


def test_decide_book_and_explain(c, order):
    d, b = place(c, order)
    assert b["status"] == "booked" and b["tracking_code"].startswith("OF")
    ex = d["explanation"]
    assert ex["reasons"] and "carriers connected" in ex["funnel"] and ex["excluded"]
    assert d["recommended"]["option_id"] == d["alternatives"][0]["option_id"]
    assert c.store.one("SELECT COUNT(*) n FROM shipments")["n"] == 1


def test_booking_replay_and_double_booking_guard(c, order):
    d, b = place(c, order, "same-key-1")
    again = c.engine.book(order.order_id, "same-key-1")
    assert again["replayed"] and again["tracking_code"] == b["tracking_code"]
    with pytest.raises(ConflictError):
        c.engine.book(order.order_id, "different-key")
    assert c.store.one("SELECT COUNT(*) n FROM shipments")["n"] == 1


def test_fallback_chain_when_top_choice_is_full(c, order):
    d = c.engine.decide(order)
    top = d["recommended"]["carrier_id"]
    c.registry.raw(top).capacity_cut = True                     # goes down *between* decision and booking
    b = c.engine.book(order.order_id, "fallback-key-1", d["decision_id"])
    assert b["status"] == "booked" and b["carrier_id"] != top
    assert b["fallbacks_tried"] and top in b["fallbacks_tried"][0]["option"]


def test_booking_is_queued_when_every_plan_fails_then_recovers(c, order):
    d = c.engine.decide(order)
    for x in c.registry.all():
        c.registry.raw(x.carrier_id).capacity_cut = True
    b = c.engine.book(order.order_id, "queue-key-01", d["decision_id"])
    assert b["status"] == "queued"
    assert c.store.one("SELECT COUNT(*) n FROM shipments")["n"] == 0
    for x in c.registry.all():
        c.registry.raw(x.carrier_id).capacity_cut = False
    r = c.engine.process_queue()
    assert r["bookings_done"] == 1 and c.store.one("SELECT COUNT(*) n FROM shipments")["n"] == 1


def test_unknown_order_and_no_feasible(c):
    with pytest.raises(NotFound):
        c.engine.book("ghost", "ghost-key-01")
    with pytest.raises(NoFeasibleOption):
        c.engine.decide(OrderIn(order_id="huge", customer_state="AM", weight_kg=90))


def test_disruption_triggers_reoptimization_and_queues_cancel(c, order):
    d, b = place(c, order)
    r = c.engine.disrupt(b["carrier_id"], "outage")            # API down: booking can't be cancelled either
    assert len(r["reoptimized"]) == 1
    ch = r["reoptimized"][0]
    assert ch["from"]["carrier"] == b["carrier_id"] and ch["to"]["carrier"] != b["carrier_id"]
    s = c.store.one("SELECT * FROM shipments")
    assert s["carrier_id"] == ch["to"]["carrier"] and s["replan_count"] == 1
    assert c.store.one("SELECT COUNT(*) n FROM pending_cancels WHERE status='queued'")["n"] == 1
    c.engine.disrupt(b["carrier_id"], "clear")
    assert c.engine.process_queue()["cancellations_done"] == 1
    assert c.store.one("SELECT COUNT(*) n FROM decisions WHERE kind='reoptimization'")["n"] == 1


def test_no_replan_after_pickup(c, order):
    d, b = place(c, order)
    for _ in range(2):
        c.engine.tick(0.5)
    sid = c.store.one("SELECT shipment_id, status FROM shipments")
    if sid["status"] == "ShipmentCreated":
        pytest.skip("not picked up yet")
    r = c.engine.reoptimize(sid["shipment_id"])
    assert r["changed"] is False and "pickup" in r["reason"]


def test_tracking_delivery_feedback_and_learning(c, order):
    d, b = place(c, order)
    before = c.predictor.table.reliability(b["carrier_id"], "SP", "RJ")
    for _ in range(40):
        c.engine.tick(0.5)
        if not c.store.one("SELECT active FROM shipments")["active"]:
            break
    s = c.store.one("SELECT * FROM shipments")
    assert s["status"] in ("Delivered", "DeliveryFailed")
    if s["status"] == "Delivered":
        assert c.engine.feedback.report()["samples"] == 1
        assert c.predictor.table.reliability(b["carrier_id"], "SP", "RJ") != before          # online learning happened
    types = [e["type"] for e in c.store.all("SELECT type FROM events WHERE shipment_id=?", (s["shipment_id"],))]
    assert types[0] == "ShipmentCreated" and "PickedUp" in types


def test_stalled_tracking_raises_exception_and_actions(c, order):
    d, b = place(c, order.model_copy(update={"order_id": "S-1", "customer_state": "BA", "sla_days": 20}))
    c.engine.tick(0.5)
    c.engine.disrupt(b["carrier_id"], "stall")
    for _ in range(14):
        c.engine.tick(0.5)
    s = c.store.one("SELECT severity, active FROM shipments")
    if s["active"]:                                             # a fast parcel may have finished before the stall
        assert s["severity"] in ("warning", "high_risk", "critical")
        evs = c.store.all("SELECT payload FROM events WHERE type='ExceptionRaised'")
        assert evs and any("tracking_stalled" in json.loads(e["payload"])["findings"] for e in evs)


def test_audit_trail_is_complete_and_tamper_evident(c, order):
    place(c, order)
    rows = c.store.all("SELECT action FROM audit_log")
    assert {"decision.initial", "shipment.booked"} <= {r["action"] for r in rows}
    d = json.loads(c.store.one("SELECT details FROM audit_log WHERE action='decision.initial'")["details"])
    assert {"policy", "weights", "selected", "cost", "predicted_days", "late_risk"} <= d.keys()
    assert c.store.verify_audit_chain()["valid"]
    c.store.execute("UPDATE audit_log SET actor='mallory' WHERE id=1")
    assert not c.store.verify_audit_chain()["valid"]


def test_control_tower_kpis(c, order):
    place(c, order)
    k = analytics.control_tower(c)
    assert k["active_shipments"] == 1 and k["carriers_total"] == 7 and 0 <= k["network_health"] <= 100
    assert analytics.carriers(c) and analytics.routes(c)["cross_state_share_pct"] == 100.0


def test_digital_twin_tradeoffs(c):
    base = twin.run(twin.Scenario(n_orders=300), c.predictor, c.settings)
    cheap = twin.run(twin.Scenario(n_orders=300, strategy="cheapest"), c.predictor, c.settings)
    rel = twin.run(twin.Scenario(n_orders=300, profile="reliability"), c.predictor, c.settings)
    assert cheap["avg_cost"] < base["avg_cost"] and cheap["expected_late_rate"] > base["expected_late_rate"]
    assert rel["expected_late_rate"] <= base["expected_late_rate"] + 1e-9
    out = twin.run(twin.Scenario(n_orders=300, disabled_carriers=["RAPIDOSUL"]), c.predictor, c.settings)
    assert "RAPIDOSUL" not in out["carrier_mix"]
    surge = twin.run(twin.Scenario(n_orders=300, volume_multiplier=4), c.predictor, c.settings)
    assert surge["unserved"] > 0 and surge["expected_late_rate"] > base["expected_late_rate"]


def test_label_failure_does_not_undo_booking(c, order):
    d = c.engine.decide(order)
    real = c.engine._label
    c.engine.registry.raw(d["recommended"]["carrier_id"])  # sanity: carrier exists
    c.engine._label = lambda *a: None                      # label service unavailable
    b = c.engine.book(order.order_id, "label-key-001", d["decision_id"])
    c.engine._label = real
    assert b["status"] == "booked" and b["label"] is None
    assert c.store.one("SELECT COUNT(*) n FROM shipments")["n"] == 1


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_double_submit_creates_exactly_one_shipment(c, same_key):
    import threading
    o = OrderIn(order_id="RACE", customer_state="RJ", weight_kg=2, sla_days=6, allowed_origins=["SP-1"])
    d = c.engine.decide(o)
    out = []

    def go(i):
        try:
            out.append(c.engine.book("RACE", "race-key-1" if same_key else f"race-key-{i}", d["decision_id"]))
        except ConflictError:
            out.append("conflict")
    ts = [threading.Thread(target=go, args=(i,)) for i in range(12)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert c.store.one("SELECT COUNT(*) n FROM shipments")["n"] == 1
    assert sum(x.capacity_snapshot()["load"] for x in c.registry.all()) == 1      # carrier capacity charged once
    assert sum(n.load for n in c.network.all()) == 1                              # origin capacity charged once
    assert len({r["tracking_code"] for r in out if isinstance(r, dict)}) == 1


def test_cross_instance_race_is_compensated_at_the_carrier(c):
    """Bypass the in-process lock (simulating a second API instance): losers must cancel their carrier booking."""
    import threading
    o = OrderIn(order_id="RACE2", customer_state="RJ", weight_kg=2, sla_days=6, allowed_origins=["SP-1"])
    d = c.engine.decide(o)
    barrier = threading.Barrier(8)

    def go(i):
        barrier.wait()
        try:
            c.engine._book_locked("RACE2", f"x-key-{i}", d["decision_id"], "t")
        except ConflictError:
            pass
    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert c.store.one("SELECT COUNT(*) n FROM shipments")["n"] == 1
    assert sum(x.capacity_snapshot()["load"] for x in c.registry.all()) == 1      # no orphan bookings left at any carrier
    assert sum(n.load for n in c.network.all()) == 1
