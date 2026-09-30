"""Historical shipment generator for the prediction layer (Digital-Twin data source).

Olist has no carrier identifier, so a carrier-level delivery history cannot be *observed*. What is real here
(when the calibration artifact is present) is everything the marketplace does reveal: the joint distribution of
origin state, destination state, order month, parcel weight and customer SLA (bootstrapped from real orders), the
route transit times, route risk, monthly lateness and freight level that shape the carrier physics in `world.py`.
Carrier behaviour on top of that is simulated. `history_source()` reports which mode produced the data.
"""
from __future__ import annotations

import random

import numpy as np

from app.core.geo import customer_weights, distance_km, region, seller_weights
from app.core.models import ServiceLevel
from app.data import calibration
from app.integration import world

CARRIER_IDS = [p.carrier_id for p in world.PROFILES]
SERVICES = [s.value for s in ServiceLevel]
REGIONS = ["SE", "S", "CO", "NE", "N"]


def _pick(rng: random.Random, weights: dict[str, float]) -> str:
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def history_source() -> str:
    return "real_order_bootstrap+calibrated_physics" if calibration.get() else "synthetic"


def _draws(rng: random.Random):
    """Yield (origin, destination, month, weight_kg): real joint bootstrap when calibrated, synthetic marginals otherwise."""
    c = calibration.get()
    if c:
        while True:
            for r in c.sample_orders(rng, 512):
                yield r["seller_state"], r["customer_state"], r["month"], r["weight_kg"]
    sw, cw = seller_weights(), customer_weights()
    while True:
        yield _pick(rng, sw), _pick(rng, cw), rng.randint(1, 12), rng.lognormvariate(0.6, 0.8)


def generate_history(n: int = 24000, seed: int = 7) -> dict:
    rng = random.Random(seed)
    rows = []
    draws = _draws(rng)
    while len(rows) < n:
        o, d, month, raw_w = next(draws)
        p = rng.choice(world.PROFILES)
        if region(o) not in p.origin_regions or region(d) not in p.regions:
            continue
        svc = rng.choice(p.services)
        weight = round(max(0.05, min(p.max_weight_kg, raw_w)), 2)
        actual, delayed = world.sample_actual_days(p, o, d, svc, rng, world.peak_penalty(month))
        promised = world.promised_days(p, o, d, svc)
        rows.append((p.carrier_id, o, d, svc.value, month, weight, promised, actual, int(actual > promised)))
    return featurize_rows(rows)


def featurize_rows(rows) -> dict:
    X, y_days, y_late, keys = [], [], [], []
    for cid, o, d, svc, month, w, promised, actual, late in rows:
        X.append(feature_vector(cid, o, d, svc, month, w, promised))
        y_days.append(actual)
        y_late.append(late)
        keys.append((cid, o, d))
    return {"X": np.array(X, dtype=float), "y_days": np.array(y_days), "y_late": np.array(y_late), "keys": keys}


CAT_IDX = [0, 1, 2, 3]  # carrier, origin region, dest region, service


def feature_vector(carrier_id: str, o: str, d: str, service: str, month: int, weight: float, promised: float) -> list[float]:
    return [
        CARRIER_IDS.index(carrier_id), REGIONS.index(region(o)), REGIONS.index(region(d)),
        SERVICES.index(service), distance_km(o, d), float(o == d), weight, month,
        float(month in (11, 12)), promised,
    ]
