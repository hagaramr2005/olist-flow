"""Demo/seed data: order flow through the real decide -> book -> track pipeline.

`source="real"` replays *actual Olist orders* (bootstrapped from the real sample shipped with the calibration artifact):
real origin and destination state, parcel weight and volume, merchandise value, category, order month and the
customer delivery promise. `source="synthetic"` keeps the original hand-made generator (tight SLAs, useful for stress demos).
"""
from __future__ import annotations

import random
from typing import Optional

from app.core.geo import customer_weights, seller_weights
from app.core.models import OrderIn
from app.data import calibration
from app.intelligence.optimizer import NoFeasibleOption
from app.services.container import Container
from app.services.engine import ConflictError

PROFILE_MIX = ["balanced", "balanced", "economy", "reliability"]


def real_order(r: dict, order_id: str, rng: random.Random, handling_days: float) -> OrderIn:
    """Real Olist order -> OrderIn. The carrier's window is the customer promise minus the seller's real handling time."""
    sla = max(1.0, round(r["promised_days"] - handling_days, 1))
    return OrderIn(order_id=order_id, customer_state=r["customer_state"], weight_kg=round(max(0.05, min(100.0, r["weight_kg"])), 2),
                   volume_cm3=max(100.0, float(r["volume_cm3"])), value_brl=round(max(0.0, r["value_brl"]), 2),
                   category=r["category"], sla_days=sla, seller_hint=r["seller_state"], seller_id=f"S{rng.randint(1, 25):03d}",
                   priority="high" if rng.random() < 0.1 else "normal", ordered_month=r["month"])


def _synthetic_order(rng: random.Random, order_id: str) -> OrderIn:
    cw, sw = customer_weights(), seller_weights()
    dest = rng.choices(list(cw), weights=list(cw.values()))[0]
    seller_state = rng.choices(list(sw), weights=list(sw.values()))[0]
    return OrderIn(order_id=order_id, customer_state=dest, weight_kg=round(min(40, rng.lognormvariate(0.6, 0.8)), 2),
                   volume_cm3=rng.choice([2000, 5000, 9000, 20000]), value_brl=round(rng.uniform(20, 400), 2),
                   category=f"cat{rng.randint(0, 12)}", sla_days=rng.choice([None, 6, 8, 10, 14]),
                   seller_hint=seller_state, seller_id=f"S{rng.randint(1, 25):03d}", priority="high" if rng.random() < 0.1 else "normal",
                   ordered_month=rng.choice([5, 6, 11]))


def seed_demo(c: Container, n_orders: int = 80, advance_days: float = 4.0, seed: int = 5, prefix: str = "DEMO",
              source: Optional[str] = None) -> dict:
    cal = calibration.get()
    source = source or ("real" if cal else "synthetic")
    if source == "real" and not cal:
        raise ValueError("source=real needs the calibration artifact (run scripts/build_calibration.py)")
    rng = random.Random(seed)
    real = cal.sample_orders(rng, n_orders) if source == "real" else None
    handling = float(cal.network.get("handling_days_p50", 2.0)) if cal else 0.0
    booked = skipped = 0
    for i in range(n_orders):
        oid = f"{prefix}-{seed}-{i:05d}"
        o = real_order(real[i], oid, rng, handling) if real else _synthetic_order(rng, oid)
        try:
            d = c.engine.decide(o, profile=rng.choice(PROFILE_MIX), actor="seed")
            r = c.engine.book(o.order_id, f"seed-{seed}-{i}", d["decision_id"], actor="seed")
            booked += r["status"] == "booked"
        except (NoFeasibleOption, ConflictError):
            skipped += 1
    steps = int(advance_days / 0.5)
    last = {}
    for _ in range(steps):
        last = c.engine.tick(0.5, actor="seed")
    return {"source": source, "booked": booked, "skipped": skipped, "ticks": steps, "last_tick": last}
