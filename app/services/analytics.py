"""Control-tower analytics computed from operational tables."""
from __future__ import annotations

import json
from collections import defaultdict

import numpy as np

from app.services.container import Container

RISK_LEVELS = ((0.25, "high"), (0.12, "medium"), (0.0, "low"))


def _risk_level(v: float) -> str:
    return next(name for thr, name in RISK_LEVELS if v >= thr)


def _late_lookup(c: Container) -> dict[int, dict]:
    return {r["shipment_id"]: r for r in c.store.all("SELECT * FROM feedback")}


def carriers(c: Container) -> list[dict]:
    ships = c.store.all("SELECT * FROM shipments")
    fb = c.store.all("SELECT * FROM feedback")
    failed = c.store.all("SELECT s.carrier_id, COUNT(*) n FROM events e JOIN shipments s USING(shipment_id) "
                         "WHERE e.type='DeliveryFailed' GROUP BY s.carrier_id")
    failed = {r["carrier_id"]: r["n"] for r in failed}
    out = []
    for car in c.registry.all():
        cs = [s for s in ships if s["carrier_id"] == car.carrier_id]
        cf = [f for f in fb if f["carrier_id"] == car.carrier_id]
        snap = car.capacity_snapshot()
        out.append({
            "carrier_id": car.carrier_id, "name": car.name,
            "active_shipments": sum(1 for s in cs if s["active"]),
            "total_shipments": len(cs),
            "avg_cost": round(float(np.mean([s["cost"] for s in cs])), 2) if cs else None,
            "delivered": len(cf),
            "on_time_pct": round(100 * (1 - float(np.mean([f["was_late"] for f in cf]))), 1) if cf else None,
            "avg_actual_days": round(float(np.mean([f["actual_days"] for f in cf])), 2) if cf else None,
            "promise_error_days": round(float(np.mean([abs(f["actual_days"] - f["promised_days"]) for f in cf])), 2) if cf else None,
            "failure_rate_pct": round(100 * failed.get(car.carrier_id, 0) / len(cs), 1) if cs else 0.0,
            "avg_predicted_risk_pct": round(100 * float(np.mean([s["late_risk"] for s in cs])), 1) if cs else None,
            "capacity_free_pct": round(100 * snap["free_ratio"], 1), "breaker": snap["breaker"], "outage": snap["outage"],
        })
    return out


def routes(c: Container, top: int = 5) -> dict:
    fb = _late_lookup(c)
    groups: dict[tuple, list] = defaultdict(list)
    for s in c.store.all("SELECT * FROM shipments"):
        groups[(s["seller_state"], s["dest_state"])].append(s)
    rows = []
    for (o, d), ss in groups.items():
        fbs = [fb[s["shipment_id"]] for s in ss if s["shipment_id"] in fb]
        rows.append({
            "route": f"{o}->{d}", "orders": len(ss), "avg_cost": round(float(np.mean([s["cost"] for s in ss])), 2),
            "avg_risk_pct": round(100 * float(np.mean([s["late_risk"] for s in ss])), 1),
            "late_rate_pct": round(100 * float(np.mean([f["was_late"] for f in fbs])), 1) if fbs else None,
            "avg_days": round(float(np.mean([f["actual_days"] for f in fbs] if fbs else [s["predicted_days"] for s in ss])), 2),
            "cross_state": o != d})
    def key_delay(r):
        return r["late_rate_pct"] if r["late_rate_pct"] is not None else r["avg_risk_pct"]
    return {
        "most_delayed": sorted(rows, key=key_delay, reverse=True)[:top],
        "most_expensive": sorted(rows, key=lambda r: -r["avg_cost"])[:top],
        "highest_volume": sorted(rows, key=lambda r: -r["orders"])[:top],
        "worst": sorted(rows, key=lambda r: -(key_delay(r) * 0.6 + r["avg_cost"] * 0.4))[:top],
        "cross_state_share_pct": round(100 * sum(r["orders"] for r in rows if r["cross_state"]) / max(1, sum(r["orders"] for r in rows)), 1),
        "all": rows,
    }


def geo(c: Container) -> list[dict]:
    fb = _late_lookup(c)
    by: dict[str, list] = defaultdict(list)
    for s in c.store.all("SELECT * FROM shipments"):
        by[s["dest_state"]].append(s)
    out = []
    for st, ss in by.items():
        fbs = [fb[s["shipment_id"]] for s in ss if s["shipment_id"] in fb]
        out.append({"state": st, "orders": len(ss), "avg_cost": round(float(np.mean([s["cost"] for s in ss])), 2),
                    "avg_risk_pct": round(100 * float(np.mean([s["late_risk"] for s in ss])), 1),
                    "late_rate_pct": round(100 * float(np.mean([f["was_late"] for f in fbs])), 1) if fbs else None})
    return sorted(out, key=lambda r: -r["orders"])


def sellers(c: Container, seller_id: str | None = None) -> list[dict]:
    fb = _late_lookup(c)
    by: dict[str, list] = defaultdict(list)
    for s in c.store.all("SELECT * FROM shipments WHERE seller_id IS NOT NULL"):
        if seller_id is None or s["seller_id"] == seller_id:
            by[s["seller_id"]].append(s)
    out = []
    for sid, ss in by.items():
        fbs = [fb[s["shipment_id"]] for s in ss if s["shipment_id"] in fb]
        risk = float(np.mean([s["late_risk"] for s in ss]))
        out.append({"seller_id": sid, "orders": len(ss),
                    "late_rate_pct": round(100 * float(np.mean([f["was_late"] for f in fbs])), 1) if fbs else None,
                    "avg_fulfilment_days": round(float(np.mean([f["actual_days"] for f in fbs] if fbs else [s["predicted_days"] for s in ss])), 2),
                    "avg_risk_pct": round(100 * risk, 1), "risk_level": _risk_level(risk)})
    return sorted(out, key=lambda r: -r["orders"])


def estimated_savings(c: Container) -> dict:
    """Savings versus the *median-cost feasible plan* per booked order (a conservative baseline)."""
    rows = c.store.all("SELECT d.ranked, s.cost FROM shipments s JOIN decisions d ON d.decision_id=s.decision_id")
    saved, base = 0.0, 0.0
    for r in rows:
        costs = [o["cost"] for o in json.loads(r["ranked"])]
        if len(costs) >= 2:
            med = float(np.median(costs))
            saved += med - r["cost"]
            base += med
    return {"amount": round(saved, 2), "pct_of_baseline": round(100 * saved / base, 2) if base else 0.0,
            "baseline": "median cost of feasible plans per order"}


def control_tower(c: Container) -> dict:
    ships = c.store.all("SELECT * FROM shipments")
    active = [s for s in ships if s["active"]]
    at_risk = [s for s in active if s["severity"] in ("high_risk", "critical")]
    fb = c.store.all("SELECT * FROM feedback")
    cars = carriers(c)
    healthy = sum(1 for x in cars if x["breaker"] == "closed" and not x["outage"] and x["capacity_free_pct"] > 0)
    sla_breach_ids = set()
    for e in c.store.all("SELECT shipment_id, payload FROM events WHERE type='ExceptionRaised'"):
        if "eta_exceeded" in json.loads(e["payload"]).get("findings", []):
            sla_breach_ids.add(e["shipment_id"])
    health = 100 * (0.5 * healthy / max(1, len(cars)) + 0.5 * (1 - len(at_risk) / max(1, len(active)))) if cars else 0.0
    exp_on_time = 100 * (1 - float(np.mean([s["late_risk"] for s in active]))) if active else None
    realised_late = float(np.mean([f["was_late"] for f in fb])) if fb else None
    st = c.settings
    return {
        "sim_day": c.engine.sim_day,
        "network_health": round(health, 1),
        "active_shipments": len(active), "at_risk_shipments": len(at_risk),
        "warning_shipments": sum(1 for s in active if s["severity"] == "warning"),
        "expected_on_time_pct": None if exp_on_time is None else round(exp_on_time, 1),
        "avg_freight_cost": round(float(np.mean([s["cost"] for s in ships])), 2) if ships else None,
        "avg_predicted_eta_days": round(float(np.mean([s["predicted_days"] for s in active])), 2) if active else None,
        "carrier_sla_breaches": len(sla_breach_ids),
        "estimated_savings": estimated_savings(c),
        "total_shipments": len(ships), "delivered": len(fb),
        "realised_on_time_pct": None if realised_late is None else round(100 * (1 - realised_late), 1),
        "median_delivery_days": round(float(np.median([f["actual_days"] for f in fb])), 2) if fb else None,
        "expected_review_score": None if realised_late is None else round((1 - realised_late) * st.review_on_time + realised_late * st.review_late, 2),
        "reoptimizations": int(sum(s["replan_count"] for s in ships)),
        "queued_bookings": c.store.one("SELECT COUNT(*) n FROM pending_bookings WHERE status='queued'")["n"],
        "carriers_healthy": healthy, "carriers_total": len(cars),
    }
