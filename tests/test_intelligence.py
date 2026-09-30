import pytest

from app.core.models import OrderIn
from app.intelligence import optimizer as opt
from app.intelligence.exceptions import ShipmentSnapshot, evaluate
from app.core.models import Severity, ShipmentStatus as S
from tests.conftest import make_container


def options(c, order):
    return c.engine.coverage.evaluate(order)


def test_prediction_beats_carrier_promise(predictor_base):
    r = predictor_base.report
    assert r["eta_mae_days"] < r["carrier_promise_mae_days"]
    assert r["late_auc"] > 0.6 and r["top_decile_lift"] > 1.5


def test_reliability_is_route_specific(predictor_base):
    t = predictor_base.table
    assert t.reliability("RAPIDOSUL", "SP", "RJ") > t.reliability("POSTALBR", "SP", "AM") + 0.1
    assert t.reliability("TRANSNORDESTE", "SP", "BA") > t.reliability("TRANSNORDESTE", "SP", "SP") - 0.2
    assert 0 < t.reliability("POSTALBR", "SP", "RJ") < 1


def test_sla_risk_rises_as_sla_tightens(predictor_base):
    loose = predictor_base.predict("POSTALBR", "SP", "RJ", "standard", 6, 2, 4, sla_days=10).sla_risk
    tight = predictor_base.predict("POSTALBR", "SP", "RJ", "standard", 6, 2, 4, sla_days=2).sla_risk
    assert tight > loose


def test_coverage_funnel_and_rejections(c, order):
    out = options(c, order)
    f = out.funnel
    assert f["carriers_connected"] == 7 >= f["carriers_serving_route"] >= f["carriers_meeting_sla"] >= 1
    assert any(r.carrier_id == "AMAZONLOG" and r.stage == "coverage" for r in out.rejections)
    assert all(o.promised_days <= order.sla_days for o in out.options if o.feasible)


def test_open_breaker_removes_carrier(c, order):
    c.registry.raw("RAPIDOSUL").outage = True
    out = options(c, order)
    assert "RAPIDOSUL" not in {o.carrier_id for o in out.options}
    assert any(r.carrier_id == "RAPIDOSUL" and r.stage == "availability" for r in out.rejections)


def test_profiles_change_the_winner(c, order):
    opts = options(c, order).options
    eco = opt.optimize(opts, "economy").recommended
    exp = opt.optimize(opts, "express").recommended
    assert eco.cost <= exp.cost and exp.predicted_days <= eco.predicted_days


def test_custom_weights_and_validation(c, order):
    opts = options(c, order).options
    assert opt.optimize(opts, custom_weights={"cost": 1}).recommended.cost == min(o.cost for o in opts if o.feasible)
    with pytest.raises(ValueError):
        opt.resolve_weights(custom={"nonsense": 1})
    with pytest.raises(ValueError):
        opt.resolve_weights(custom={"cost": 0})
    assert abs(sum(opt.resolve_weights("balanced", priority="high").values()) - 1) < 1e-9


def test_pareto_and_risk_constraint(c, order):
    opts = [o for o in options(c, order).options if o.feasible]
    d = opt.optimize(opts, "economy", max_late_risk=0.05)
    assert d.recommended.late_risk <= 0.05
    front = opt.pareto_front(opts)
    assert front and len(front) < len(opts)
    risky = [o for o in opts if o.late_risk > 0.02]           # every remaining option violates a 1% cap
    d2 = opt.optimize(risky, "economy", max_late_risk=0.01)
    assert "relaxed" in d2.constraint_note and d2.recommended in d2.ranked


def test_no_feasible_option_raises(c):
    o = OrderIn(order_id="big", customer_state="AM", weight_kg=90)
    with pytest.raises(opt.NoFeasibleOption):
        opt.optimize(options(c, o).options)


def test_batch_milp_respects_capacity_and_beats_naive(c):
    per = {}
    for i in range(40):
        o = OrderIn(order_id=f"B{i}", customer_state="RJ", weight_kg=2, sla_days=8, allowed_origins=["SP-1"])
        per[o.order_id] = options(c, o).options
    caps = {x.carrier_id: 8 for x in c.registry.all()}
    r = opt.optimize_batch(per, opt.resolve_weights("balanced"), caps, {"SP-1": 100})
    assert not r.unassigned
    assert all(u <= 8 for u in r.carrier_usage.values())
    assert r.naive_capacity_violations > 0                                 # ignoring capacity would overload carriers


def test_batch_leaves_orders_unassigned_when_capacity_is_short(c):
    per = {f"U{i}": options(c, OrderIn(order_id=f"U{i}", customer_state="RJ", weight_kg=2, allowed_origins=["SP-1"])).options for i in range(6)}
    r = opt.optimize_batch(per, opt.resolve_weights("balanced"), {x.carrier_id: 1 for x in c.registry.all()}, {"SP-1": 2})
    assert len(r.unassigned) == 4


def test_exception_classification():
    ok = evaluate(ShipmentSnapshot(S.OUT_FOR_DELIVERY, 1.0, 0.8, 3, 2, 0.05, 1.0))
    assert ok.severity == Severity.NORMAL and not ok.actions
    missed = evaluate(ShipmentSnapshot(S.CREATED, 3.0, 0, 5, 3, 0.1, 1.0))
    assert missed.severity == Severity.HIGH_RISK and missed.can_reoptimize and "alert_operations" in missed.actions
    late = evaluate(ShipmentSnapshot(S.AT_HUB, 6.0, 2.0, 3, 3, 0.1, 1.0))
    assert late.severity == Severity.CRITICAL and not late.can_reoptimize          # cannot re-plan after pickup
    down = evaluate(ShipmentSnapshot(S.CREATED, 0.2, 0, 3, 2, 0.05, 1.0, carrier_api_healthy=False))
    assert down.can_reoptimize
    assert evaluate(ShipmentSnapshot(S.DELIVERED, 3, 3, 3, 3, 0.9, 1)).severity == Severity.NORMAL
