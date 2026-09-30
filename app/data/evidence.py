"""Builds the /v1/data/evidence payload: what the real data says, where it is used, and what it cannot tell us."""
from __future__ import annotations

from typing import Optional

from app.data.calibration import Calibration

HOW_USED = [
    "Demand and supply geography: real origin/destination state shares",
    "Real-order bootstrap: joint origin, destination, month, weight, volume, value, category and promise feed training, the Digital Twin and demo replay",
    "Route physics: real per-route carrier-transit medians, route late-risk and zip-to-zip distances shape every simulated carrier",
    "Seasonality: observed monthly lateness above the network mean (Nov-2017 and the Feb-Mar 2018 disruption)",
    "Tariffs: simulated prices scaled per destination region to match observed Olist freight",
    "Business impact: review-score penalty for late delivery",
    "Benchmark: carrier-agnostic ETA and late-risk models trained on real orders, scored on a chronological hold-out",
]
LIMITS = [
    "Olist has no carrier identifier: carrier-level behaviour is simulated behind the adapter contract; real data constrains the routes and market it operates in",
    "The customer promise in the data is heavily padded, so the low late rate says little about carrier quality",
    "Feb-Mar 2018 is a network-wide disruption, not a stable seasonal pattern: use it as a stress scenario",
    "The data ends in Oct 2018 and is not live",
]


def _insights(a: dict) -> list[dict]:
    h, n, r = a["headline"], a["network"], a["review"]
    months = sorted((t for t in a["timeline"] if t["n"] >= 1000), key=lambda t: -t["late"])[:3]
    routes = [(k, v) for k, v in a["routes"].items() if v["n"] >= 300 and v["late"] is not None]
    worst, best = max(routes, key=lambda kv: kv[1]["late"]), min(routes, key=lambda kv: kv[1]["late"])
    return [
        {"title": "Most parcels cross a state line", "value": f"{h['cross_state_share']:.0%}",
         "detail": f"and they pay {h['freight_premium_order_pct']:.0f}% more freight per order than same-state parcels"},
        {"title": "The customer promise is padded", "value": f"{n['sla_padding_ratio']}×",
         "detail": f"promise {n['promised_p50']} d vs median delivery {n['total_p50']} d; {n['delivered_10d_early_share']:.0%} of orders arrive 10+ days early"},
        {"title": "Lateness is bursty, not seasonal", "value": ", ".join(f"{t['ym']} {t['late']:.0%}" for t in months),
         "detail": f"against a network mean of {h['late_rate']:.1%}"},
        {"title": "Late delivery costs reviews", "value": f"{r['on_time']:.2f} → {r['late']:.2f}",
         "detail": f"average review score, on-time vs late ({r['n_late']:,} late orders)"},
        {"title": "Route risk varies widely", "value": f"{best[1]['late']:.0%} – {worst[1]['late']:.0%}",
         "detail": f"late rate from {best[0].replace('>', '→')} to {worst[0].replace('>', '→')} (routes with 300+ orders)"},
        {"title": "Real carrier transit is slow", "value": f"{n['transit_p50']} d",
         "detail": f"median hand-off-to-door time (p90 {n['transit_p90']} d); seller handling adds {n['handling_days_p50']} d"},
    ]


def build(c: Optional[Calibration]) -> dict:
    if c is None:
        return {"active": False, "reason": "no calibration artifact; the platform is running on synthetic physics",
                "how_to_enable": "python scripts/build_calibration.py --data-dir <olist csv folder>"}
    a = c.d
    tile = [{"state": st, "orders": v["orders"], "avg_cost": v["freight_mean"], "avg_risk_pct": round(100 * (v["late"] or 0), 1),
             "late_rate_pct": round(100 * (v["late"] or 0), 1), "total_p50": v["total_p50"], "transit_p50": v["transit_p50"]}
            for st, v in a["states"].items()]
    big = sorted(((k, v) for k, v in a["routes"].items() if v["n"] >= 200), key=lambda kv: -kv[1]["n"])[:12]
    top_routes = [{"route": k.replace(">", " → "), "orders": v["n"], "km": v["km"], "transit_p50": v["transit_p50"], "transit_p90": v["transit_p90"],
                   "late_pct": round(100 * v["late"], 1), "freight_p50": v["freight_p50"]} for k, v in big]
    m = a["meta"]
    return {
        "active": True, "integrity_ok": c.integrity_ok,
        "provenance": {"built_at": m["built_at"], "etl_version": m["etl_version"], "checksum": m["checksum"],
                       "late_definition": m["late_definition"], "source_files": m["source_files"], "quality": m["quality"]},
        "headline": a["headline"], "network": a["network"], "review": a["review"], "insights": _insights(a),
        "timeline": a["timeline"], "month_penalty": a["month_penalty"], "states": tile, "top_routes": top_routes,
        "categories": a["categories"], "freight_scale": a["freight_scale"], "diagnostics": a.get("diagnostics", {}),
        "benchmark": a.get("benchmark"), "how_used": HOW_USED, "limits": LIMITS,
    }
