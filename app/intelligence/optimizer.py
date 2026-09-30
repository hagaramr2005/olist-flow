"""Optimization engine. Machine learning predicts outcomes; optimisation selects actions.

* Single order  : configurable weighted multi-objective scoring over a *feasible* set,
                  Pareto-front tagging, and an optional epsilon-constraint on late risk.
* Batch / peak  : Mixed-Integer Linear Program (scipy HiGHS) assigning many orders at once
                  under carrier and origin capacity constraints.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

from app.core.metrics import metrics
from app.core.models import Option

CRITERIA = ("cost", "eta", "late_risk", "reliability", "capacity")
RISK_SATURATION = 0.35     # a 35% late-risk is treated as "maximally bad" on the 0..1 risk scale

PROFILES: dict[str, dict[str, float]] = {
    "economy":     {"cost": .75, "eta": .05, "late_risk": .10, "reliability": .05, "capacity": .05},
    "express":     {"cost": .10, "eta": .55, "late_risk": .20, "reliability": .10, "capacity": .05},
    "reliability": {"cost": .10, "eta": .10, "late_risk": .40, "reliability": .35, "capacity": .05},
    "peak_season": {"cost": .15, "eta": .10, "late_risk": .20, "reliability": .20, "capacity": .35},
    "balanced":    {"cost": .30, "eta": .20, "late_risk": .25, "reliability": .15, "capacity": .10},
}


class NoFeasibleOption(Exception):
    pass


def normalise_weights(w: dict[str, float]) -> dict[str, float]:
    unknown = set(w) - set(CRITERIA)
    if unknown:
        raise ValueError(f"unknown criteria: {sorted(unknown)}")
    full = {k: max(0.0, float(w.get(k, 0.0))) for k in CRITERIA}
    total = sum(full.values())
    if total <= 0:
        raise ValueError("weights must sum to a positive number")
    return {k: v / total for k, v in full.items()}


def resolve_weights(profile: str = "balanced", custom: Optional[dict[str, float]] = None,
                    priority: str = "normal") -> dict[str, float]:
    if custom:
        w = normalise_weights(custom)
    else:
        if profile not in PROFILES:
            raise ValueError(f"unknown profile '{profile}'. options: {sorted(PROFILES)}")
        w = dict(PROFILES[profile])
    if priority == "high":                        # high-priority seller/order: time & risk matter more
        w["eta"] *= 1.5
        w["late_risk"] *= 1.5
        w = normalise_weights(w)
    return w


def _minmax(v: np.ndarray) -> np.ndarray:
    lo, hi = float(v.min()), float(v.max())
    return np.zeros_like(v) if hi - lo < 1e-9 else (v - lo) / (hi - lo)


def components(options: list[Option]) -> dict[str, np.ndarray]:
    """Normalised (0 = best, 1 = worst) criteria for every option."""
    return {
        "cost": _minmax(np.array([o.cost for o in options])),
        "eta": _minmax(np.array([o.predicted_days for o in options])),
        "late_risk": np.clip(np.array([o.late_risk for o in options]) / RISK_SATURATION, 0, 1),
        "reliability": 1.0 - np.clip(np.array([o.reliability for o in options]), 0, 1),
        "capacity": np.clip(1.0 - 0.5 * np.array([o.capacity_free_ratio for o in options])
                            - 0.5 * np.array([o.origin_capacity_free_ratio for o in options]), 0, 1) ** 2,
    }


def pareto_front(options: list[Option]) -> set[str]:
    """Non-dominated options on (cost, predicted days, late risk); lower is better on all three."""
    pts = np.array([[o.cost, o.predicted_days, o.late_risk] for o in options])
    keep = set()
    for i, o in enumerate(options):
        dominated = np.any(np.all(pts <= pts[i], axis=1) & np.any(pts < pts[i], axis=1))
        if not dominated:
            keep.add(o.option_id)
    return keep


@dataclass
class Decision:
    recommended: Option
    ranked: list[Option]
    weights: dict[str, float]
    profile: str
    pareto_ids: set[str] = field(default_factory=set)
    constraint_note: str = ""
    latency_ms: float = 0.0


def optimize(options: list[Option], profile: str = "balanced", custom_weights: Optional[dict[str, float]] = None,
             priority: str = "normal", max_late_risk: Optional[float] = None,
             exclude_carriers: Optional[set[str]] = None) -> Decision:
    t0 = time.perf_counter()
    weights = resolve_weights(profile, custom_weights, priority)
    pool = [o for o in options if o.feasible and (not exclude_carriers or o.carrier_id not in exclude_carriers)]
    if not pool:
        raise NoFeasibleOption("no feasible fulfilment option after coverage, SLA and availability filters")
    note = ""
    if max_late_risk is not None:
        within = [o for o in pool if o.late_risk <= max_late_risk]
        if within:
            pool = within
        else:
            note = f"no option meets max late-risk {max_late_risk:.0%}; relaxed to best available"
    comp = components(pool)
    scores = sum(weights[k] * comp[k] for k in CRITERIA)
    scored = []
    for i, o in enumerate(pool):
        scored.append(o.model_copy(update={"score": round(float(scores[i]), 5),
                                           "components": {k: round(float(comp[k][i]), 4) for k in CRITERIA}}))
    scored.sort(key=lambda o: (o.score, o.cost))
    d = Decision(scored[0], scored, weights, profile if not custom_weights else "custom",
                 pareto_front(pool), note, round((time.perf_counter() - t0) * 1000, 2))
    metrics.observe("optimizer", d.latency_ms)
    return d


# ---------------------------------------------------------------------------- batch MILP
@dataclass
class BatchResult:
    assignments: dict[str, Optional[Option]]
    objective: float
    unassigned: list[str]
    solver_status: str
    solve_ms: float
    carrier_usage: dict[str, int]
    origin_usage: dict[str, int]
    naive_capacity_violations: int
    naive_objective: float


def optimize_batch(orders: dict[str, list[Option]], weights: dict[str, float],
                   carrier_caps: dict[str, int], origin_caps: dict[str, int],
                   unassigned_penalty: float = 5.0, time_limit_s: float = 10.0) -> BatchResult:
    """Assign every order to one option under shared capacity. Each order's criteria are normalised
    within its own feasible set, so objective values are comparable across orders."""
    t0 = time.perf_counter()
    ids = list(orders)
    var: list[tuple[str, Option]] = []
    for oid in ids:
        feas = [o for o in orders[oid] if o.feasible]
        if not feas:
            continue
        comp = components(feas)
        sc = sum(weights[k] * comp[k] for k in CRITERIA)
        for i, o in enumerate(feas):
            var.append((oid, o.model_copy(update={"score": float(sc[i])})))
    n_x, n_u = len(var), len(ids)
    n = n_x + n_u
    c = np.concatenate([[o.score for _, o in var], np.full(n_u, unassigned_penalty)])
    carriers = sorted({o.carrier_id for _, o in var})
    origins = sorted({o.origin_id for _, o in var})
    rows = n_u + len(carriers) + len(origins)
    A = lil_matrix((rows, n))
    lo, hi = np.zeros(rows), np.zeros(rows)
    oidx = {oid: i for i, oid in enumerate(ids)}
    for j, (oid, o) in enumerate(var):
        A[oidx[oid], j] = 1
        A[n_u + carriers.index(o.carrier_id), j] = 1
        A[n_u + len(carriers) + origins.index(o.origin_id), j] = 1
    for i in range(n_u):
        A[i, n_x + i] = 1
        lo[i] = hi[i] = 1                                  # exactly one plan (or 'unassigned')
    for k, cid in enumerate(carriers):
        lo[n_u + k], hi[n_u + k] = 0, carrier_caps.get(cid, 0)
    for k, nid in enumerate(origins):
        lo[n_u + len(carriers) + k], hi[n_u + len(carriers) + k] = 0, origin_caps.get(nid, 0)
    res = milp(c, constraints=LinearConstraint(A.tocsr(), lo, hi), integrality=np.ones(n),
               bounds=Bounds(0, 1), options={"time_limit": time_limit_s})
    assign: dict[str, Optional[Option]] = {oid: None for oid in ids}
    if res.x is not None:
        for j, (oid, o) in enumerate(var):
            if res.x[j] > 0.5:
                assign[oid] = o
    unassigned = [oid for oid, o in assign.items() if o is None]
    # Baseline: every order independently picks its own best plan, ignoring shared capacity.
    naive_use_c: dict[str, int] = {}
    naive_use_o: dict[str, int] = {}
    naive_obj = 0.0
    for oid in ids:
        mine = [(o) for i, o in var if i == oid]
        if not mine:
            continue
        best = min(mine, key=lambda o: o.score)
        naive_obj += best.score
        naive_use_c[best.carrier_id] = naive_use_c.get(best.carrier_id, 0) + 1
        naive_use_o[best.origin_id] = naive_use_o.get(best.origin_id, 0) + 1
    viol = sum(max(0, u - carrier_caps.get(k, 0)) for k, u in naive_use_c.items()) \
        + sum(max(0, u - origin_caps.get(k, 0)) for k, u in naive_use_o.items())
    cu, ou = {}, {}
    for o in assign.values():
        if o:
            cu[o.carrier_id] = cu.get(o.carrier_id, 0) + 1
            ou[o.origin_id] = ou.get(o.origin_id, 0) + 1
    ms = round((time.perf_counter() - t0) * 1000, 1)
    metrics.observe("optimizer_batch", ms)
    return BatchResult(assign, float(res.fun) if res.fun is not None else float("nan"), unassigned,
                       str(res.message)[:80], ms, cu, ou, viol, naive_obj)
