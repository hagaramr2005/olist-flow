"""Digital Twin Lite + What-If simulator.

A fast, side-effect-free simulation of the network: synthetic orders flow through the same
coverage rules, prediction models and optimiser used in production, but against the carriers'
ground-truth physics instead of live APIs. Used for policy trade-off analysis (what-if) and
stress scenarios (carrier outage, 2x volume, new fulfilment partner).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from app.core.config import Settings
from app.core.geo import customer_weights, distance_km, region, seller_weights
from app.data import calibration
from app.core.models import Option, ServiceLevel
from app.integration import world
from app.intelligence import optimizer as opt
from app.intelligence.network import DEFAULT_NODES
from app.intelligence.prediction import PredictionEngine


@dataclass
class Scenario:
    name: str = "scenario"
    profile: str = "balanced"
    custom_weights: Optional[dict] = None
    n_orders: int = 400
    volume_multiplier: float = 1.0
    disabled_carriers: list[str] = field(default_factory=list)
    capacity_multiplier: dict[str, float] = field(default_factory=dict)   # carrier -> factor
    extra_nodes: list[dict] = field(default_factory=list)                # {node_id,state,capacity}
    month: int = 6
    sla_days: Optional[float] = None
    strategy: str = "optimizer"        # optimizer | cheapest | single:<CARRIER_ID>
    seed: int = 11


def _synthetic_orders(sc: Scenario, nodes: list[tuple]) -> list[dict]:
    """Scenario demand. Real Olist orders are bootstrapped (joint draw) when a calibration is active."""
    import hashlib
    rng = random.Random(sc.seed)
    n = int(sc.n_orders * sc.volume_multiplier)
    c = calibration.get()
    real = c.sample_orders(rng, n) if c else None
    sw, cw = (None, None) if c else (seller_weights(), customer_weights())
    orders = []
    for i in range(n):
        if real:
            r = real[i]
            d, seller, cat, w, vol = r["customer_state"], r["seller_state"], r["category"], min(60.0, r["weight_kg"]), r["volume_cm3"]
        else:
            d = rng.choices(list(cw), weights=list(cw.values()))[0]
            seller = rng.choices(list(sw), weights=list(sw.values()))[0]
            cat, w, vol = f"cat{rng.randint(0, 12)}", min(60.0, rng.lognormvariate(0.6, 0.8)), rng.choice([2000, 5000, 9000, 20000])
        # multi-node stock: seller's node + deterministic replicas (~45%), same rule as production (network.nodes_for)
        stocked = [nid for nid, _, st, _ in nodes
                   if st == seller or int(hashlib.md5(f"{cat}:{nid}".encode()).hexdigest(), 16) % 100 < 45]
        orders.append(dict(i=i, dest=d, weight=round(max(0.05, w), 2), volume=max(100.0, float(vol)), stocked=stocked or [nodes[0][0]]))
    return orders


def run(sc: Scenario, predictor: PredictionEngine, settings: Settings) -> dict:
    nodes = list(DEFAULT_NODES) + [(e["node_id"], e.get("name", e["node_id"]), e["state"], e["capacity"]) for e in sc.extra_nodes]
    node_state = {nid: st for nid, _, st, _ in nodes}
    node_cap = {nid: cap for nid, _, _, cap in nodes}
    # Capacity is per *scenario window*; volume multiplier stresses it.
    window_scale = max(1, sc.n_orders) / 1500.0   # fleet capacity ~2x baseline volume
    carrier_cap = {p.carrier_id: max(1, int(p.daily_capacity * window_scale * sc.capacity_multiplier.get(p.carrier_id, 1.0)))
                   for p in world.PROFILES if p.carrier_id not in sc.disabled_carriers}
    origin_cap = {nid: max(1, int(cap * window_scale)) for nid, cap in node_cap.items()}
    orders = _synthetic_orders(sc, nodes)

    # 1. build all candidate options (vectorised prediction)
    rows, meta = [], []
    for o in orders:
        for nid in o["stocked"]:
            for p in world.PROFILES:
                if p.carrier_id in sc.disabled_carriers:
                    continue
                os_, d = node_state[nid], o["dest"]
                if region(os_) not in p.origin_regions or region(d) not in p.regions or o["weight"] > p.max_weight_kg:
                    continue
                for svc in p.services:
                    prom = world.promised_days(p, os_, d, svc)
                    if sc.sla_days is not None and prom > sc.sla_days:
                        continue
                    rows.append((p.carrier_id, os_, d, svc.value, sc.month, o["weight"], prom, sc.sla_days))
                    meta.append((o["i"], nid, p, svc, prom))
    preds = predictor.predict_many(rows)
    per_order: dict[int, list[Option]] = {}
    for (i, nid, p, svc, prom), pr in zip(meta, preds):
        os_, d = node_state[nid], orders[i]["dest"]
        per_order.setdefault(i, []).append(Option(
            option_id=f"{nid}|{p.carrier_id}|{svc.value}", origin_id=nid, origin_state=os_, carrier_id=p.carrier_id,
            service=svc, cost=world.freight_cost(p, os_, d, orders[i]["weight"], orders[i]["volume"], svc),
            promised_days=prom, predicted_days=pr.predicted_days,
            late_risk=pr.sla_risk if pr.sla_risk is not None else pr.late_risk,
            reliability=pr.reliability, distance_km=distance_km(os_, d), capacity_free_ratio=1.0))

    # 2. assign sequentially with live capacity (orders arrive one after another)
    weights = opt.resolve_weights(sc.profile, sc.custom_weights)
    used_c: dict[str, int] = {}
    used_o: dict[str, int] = {}
    chosen, unserved = [], 0
    for o in orders:
        cands = per_order.get(o["i"], [])
        cands = [c for c in cands if used_c.get(c.carrier_id, 0) < carrier_cap.get(c.carrier_id, 0)
                 and used_o.get(c.origin_id, 0) < origin_cap.get(c.origin_id, 0)]
        if sc.strategy.startswith("single:"):
            cands = [c for c in cands if c.carrier_id == sc.strategy.split(":", 1)[1]]
        if not cands:
            unserved += 1
            continue
        if sc.strategy == "cheapest" or sc.strategy.startswith("single:"):
            pick = min(cands, key=lambda c: c.cost) if sc.strategy == "cheapest" else min(cands, key=lambda c: c.cost)
        else:
            # live utilisation feeds the capacity criterion, so load balancing emerges naturally
            live = [c.model_copy(update={
                "capacity_free_ratio": 1 - used_c.get(c.carrier_id, 0) / max(1, carrier_cap[c.carrier_id]),
                "origin_capacity_free_ratio": 1 - used_o.get(c.origin_id, 0) / max(1, origin_cap[c.origin_id])}) for c in cands]
            pick = opt.optimize(live, custom_weights=weights).recommended
        used_c[pick.carrier_id] = used_c.get(pick.carrier_id, 0) + 1
        used_o[pick.origin_id] = used_o.get(pick.origin_id, 0) + 1
        chosen.append(pick)

    n_ok = len(chosen)
    late = float(np.mean([c.late_risk for c in chosen])) if chosen else 0.0
    # Unserved orders are counted as late (they miss any SLA) for the headline rate.
    total = n_ok + unserved
    late_all = (late * n_ok + unserved) / max(total, 1)
    return {
        "scenario": sc.name, "strategy": sc.strategy, "profile": sc.profile if not sc.custom_weights else "custom",
        "orders": total, "served": n_ok, "unserved": unserved,
        "avg_cost": round(float(np.mean([c.cost for c in chosen])), 2) if chosen else 0.0,
        "total_cost": round(float(sum(c.cost for c in chosen)), 2),
        "avg_predicted_days": round(float(np.mean([c.predicted_days for c in chosen])), 2) if chosen else 0.0,
        "expected_late_rate": round(late_all, 4),
        "on_time_rate": round(1 - late_all, 4),
        "expected_review": round((1 - late_all) * settings.review_on_time + late_all * settings.review_late, 3),
        "carrier_mix": {k: v for k, v in sorted(used_c.items(), key=lambda kv: -kv[1])},
        "carrier_utilisation": {k: round(used_c.get(k, 0) / cap, 3) for k, cap in carrier_cap.items()},
        "origin_utilisation": {k: round(used_o.get(k, 0) / cap, 3) for k, cap in origin_cap.items() if used_o.get(k)},
    }


def compare(base: dict, alt: dict) -> dict:
    def pct(a, b):
        return round((b - a) / a * 100, 1) if a else 0.0
    return {
        "avg_cost_pct": pct(base["avg_cost"], alt["avg_cost"]),
        "total_cost_pct": pct(base["total_cost"], alt["total_cost"]),
        "late_rate_pts": round((alt["expected_late_rate"] - base["expected_late_rate"]) * 100, 2),
        "avg_days_delta": round(alt["avg_predicted_days"] - base["avg_predicted_days"], 2),
        "unserved_delta": alt["unserved"] - base["unserved"],
        "expected_review_delta": round(alt["expected_review"] - base["expected_review"], 3),
    }


def what_if(sc: Scenario, baseline: Scenario, predictor: PredictionEngine, settings: Settings) -> dict:
    b, a = run(baseline, predictor, settings), run(sc, predictor, settings)
    return {"baseline": b, "scenario": a, "impact": compare(b, a)}
