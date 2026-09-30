"""Reproduce every number in the README "Measured results" section.

    python scripts/measure.py                 # calibrated physics (default when the artifact exists)
    python scripts/measure.py --physics synthetic
    python scripts/measure.py --json out.json
"""
import argparse
import json
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.api.main import PRESETS                                  # noqa: E402
from app.core.config import Settings                              # noqa: E402
from app.core.models import OrderIn                               # noqa: E402
from app.data import calibration as cal                           # noqa: E402
from app.intelligence.prediction import PredictionEngine          # noqa: E402
from app.services import seed as seed_mod, twin                   # noqa: E402
from tests.conftest import make_container                         # noqa: E402

SCENARIOS = ["cheapest_only", "cost_priority", "reliability_priority", "carrier_outage", "double_volume", "peak_season", "disruption_2018"]


def run(physics: str) -> dict:
    active = cal.load() if physics == "calibrated" else None
    if physics == "calibrated" and active is None:
        sys.exit("no calibration artifact: run scripts/build_calibration.py or use --physics synthetic")
    out: dict = {"physics": physics, "calibration": active.token if active else None}
    with cal.use(active):
        p = PredictionEngine(seed=7)
        p.fit()
        out["prediction"] = {k: p.report[k] for k in ("n_test", "eta_mae_days", "carrier_promise_mae_days", "late_auc", "base_late_rate",
                                                      "top_decile_late_rate", "top_decile_lift", "history_source")}
        # latency: full decide() pipeline on 200 real-shaped orders
        c = make_container(p)
        rng = random.Random(1)
        real = active.sample_orders(rng, 200) if active else None
        handling = float(active.network.get("handling_days_p50", 2.0)) if active else 0.0
        lat = []
        for i in range(200):
            o = seed_mod.real_order(real[i], f"M-{i}", rng, handling) if real else seed_mod._synthetic_order(rng, f"M-{i}")
            t = time.perf_counter()
            try:
                c.engine.decide(o, persist=False)
            except Exception:
                continue
            lat.append((time.perf_counter() - t) * 1000)
        lat.sort()
        out["latency_ms"] = {"n": len(lat), "p50": round(statistics.median(lat), 1), "p95": round(lat[int(0.95 * len(lat)) - 1], 1)}
        # digital twin: 1,000 orders per scenario vs Balanced
        s = Settings()
        t0 = time.perf_counter()
        base = twin.run(twin.Scenario(name="baseline", n_orders=1000), p, s)
        out["twin_seconds_per_1000"] = round(time.perf_counter() - t0, 1)
        out["baseline"] = {k: base[k] for k in ("avg_cost", "expected_late_rate", "avg_predicted_days", "expected_review")}
        rows = {}
        for name in SCENARIOS:
            label, kw = PRESETS[name]
            a = twin.run(twin.Scenario(name=label, n_orders=1000, **kw), p, s)
            rows[name] = {"label": label, **twin.compare(base, a), "unserved": a["unserved"]}
        out["scenarios"] = rows
    return out


def table(o: dict) -> str:
    b, pr, L = o["baseline"], o["prediction"], o["latency_ms"]
    lines = [f"physics: {o['physics']} (calibration {o['calibration']})", "",
             f"Prediction (hold-out {pr['n_test']}): ETA MAE {pr['eta_mae_days']} d vs carrier promise {pr['carrier_promise_mae_days']} d; "
             f"late AUC {pr['late_auc']}; riskiest decile {pr['top_decile_late_rate']:.1%} ({pr['top_decile_lift']}x) vs {pr['base_late_rate']:.1%} overall",
             f"Latency ({L['n']} orders): p50 {L['p50']} ms, p95 {L['p95']} ms; 1,000-order twin run {o['twin_seconds_per_1000']} s", "",
             f"Baseline (Balanced): R${b['avg_cost']} avg freight, {b['expected_late_rate']:.1%} expected late, {b['avg_predicted_days']} d, review {b['expected_review']}", "",
             "| Scenario | Avg freight | Late rate | Avg delivery | Unserved |", "|---|---|---|---|---|"]
    for r in o["scenarios"].values():
        lines.append(f"| {r['label']} | {r['avg_cost_pct']:+.1f}% | {r['late_rate_pts']:+.1f} pts | {r['avg_days_delta']:+.2f} d | {r['unserved']} |")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--physics", choices=["calibrated", "synthetic"], default="calibrated" if cal.DEFAULT_PATH.exists() else "synthetic")
    ap.add_argument("--json")
    a = ap.parse_args()
    res = run(a.physics)
    print(table(res))
    if a.json:
        Path(a.json).write_text(json.dumps(res, indent=1))
