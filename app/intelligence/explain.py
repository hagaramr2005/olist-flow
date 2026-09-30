"""Explainable optimisation: every recommendation ships with the reasons it won."""
from __future__ import annotations

from app.core.models import Option
from app.intelligence.coverage import CoverageOutcome
from app.intelligence.optimizer import CRITERIA, Decision

LABELS = {"cost": "shipping cost", "eta": "delivery time", "late_risk": "late-delivery risk",
          "reliability": "route reliability", "capacity": "spare capacity"}


def _name(o: Option) -> str:
    return f"{o.carrier_id} {o.service.value} from {o.origin_id}"


def explain(decision: Decision, coverage: CoverageOutcome, sla_days: float | None) -> dict:
    best = decision.recommended
    reasons: list[str] = []
    comparisons: list[dict] = []

    # 1. Contribution of each criterion to the winning score (weight x normalised value; lower = better).
    contrib = {k: decision.weights[k] * best.components[k] for k in CRITERIA}
    strengths = sorted(CRITERIA, key=lambda k: (best.components[k], -decision.weights[k]))
    top = [k for k in strengths if decision.weights[k] >= 0.15][:2]
    if top:
        reasons.append("Strong on the objectives this policy prioritises: " + ", ".join(LABELS[k] for k in top))

    # 2. Head-to-head deltas against the strongest alternatives (different carrier, at least one per axis).
    others = [o for o in decision.ranked[1:] if o.carrier_id != best.carrier_id or o.service != best.service]
    seen = set()
    delta_reasons = 0
    for alt in others[:6]:
        d = {
            "vs": _name(alt),
            "cost_delta_pct": round((best.cost - alt.cost) / alt.cost * 100, 1),
            "eta_delta_days": round(best.predicted_days - alt.predicted_days, 2),
            "late_risk_delta_pts": round((best.late_risk - alt.late_risk) * 100, 1),
        }
        comparisons.append(d)
        key = alt.carrier_id
        if key in seen:
            continue
        seen.add(key)
        if delta_reasons >= 4 or alt.carrier_id == best.carrier_id:
            continue
        if d["cost_delta_pct"] <= -5:
            reasons.append(f"{abs(d['cost_delta_pct']):.0f}% cheaper than {alt.carrier_id} ({alt.service.value})")
            delta_reasons += 1
        if d["late_risk_delta_pts"] <= -3 and delta_reasons < 4:
            reasons.append(f"{abs(d['late_risk_delta_pts']):.0f} pts lower late risk than {alt.carrier_id} ({alt.service.value})")
            delta_reasons += 1
        if d["eta_delta_days"] <= -0.5 and delta_reasons < 4:
            reasons.append(f"{abs(d['eta_delta_days']):.1f} days faster than {alt.carrier_id} ({alt.service.value})")
            delta_reasons += 1
    if sla_days is not None:
        reasons.append(f"Meets customer SLA: promised {best.promised_days:.0f}d <= {sla_days:.0f}d, "
                       f"P(miss SLA) = {best.late_risk:.0%}")
    reasons.append(f"Capacity available ({best.capacity_free_ratio:.0%} carrier, {best.origin_capacity_free_ratio:.0%} origin free)")
    if best.option_id in decision.pareto_ids:
        reasons.append("Pareto-optimal: no other feasible plan is better on cost, time and risk simultaneously")
    if decision.constraint_note:
        reasons.append(decision.constraint_note)

    # 3. Why others were excluded (funnel narrative)
    excluded = [{"origin": r.origin_id, "carrier": r.carrier_id, "service": r.service, "stage": r.stage,
                 "reason": r.reason} for r in coverage.rejections]
    f = coverage.funnel
    funnel_text = (f"{f['carriers_connected']} carriers connected -> {f['carriers_serving_route']} serve this route -> "
                   f"{f['carriers_meeting_sla']} meet SLA -> {f['feasible_plans']} feasible plans evaluated")
    return {
        "headline": f"Recommended: {best.carrier_id} ({best.service.value}) from {best.origin_id} - "
                    f"{decision.profile} policy",
        "funnel": funnel_text,
        "reasons": reasons,
        "score_contribution": {k: round(v, 4) for k, v in contrib.items()},
        "comparisons": comparisons,
        "excluded": excluded,
    }
