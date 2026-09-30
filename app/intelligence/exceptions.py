"""Exception management: classify shipment health and prescribe automatic actions."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.models import Severity, ShipmentStatus

RANK = {Severity.NORMAL: 0, Severity.WARNING: 1, Severity.HIGH_RISK: 2, Severity.CRITICAL: 3}


@dataclass
class ShipmentSnapshot:
    status: ShipmentStatus
    day_now: float                      # simulated days since booking
    last_event_day: float
    promised_days: float
    predicted_days: float
    late_risk: float
    handling_days: float
    carrier_api_healthy: bool = True
    carrier_capacity_ok: bool = True
    tracking_seen: bool = True          # False if tracking calls keep failing


@dataclass
class Finding:
    code: str
    severity: Severity
    message: str


@dataclass
class ExceptionReport:
    severity: Severity
    findings: list[Finding] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    can_reoptimize: bool = False
    revised_eta_days: float | None = None


def evaluate(s: ShipmentSnapshot) -> ExceptionReport:
    f: list[Finding] = []
    delivered = s.status == ShipmentStatus.DELIVERED
    if delivered:
        return ExceptionReport(Severity.NORMAL)
    if s.status == ShipmentStatus.FAILED:
        f.append(Finding("delivery_failed", Severity.CRITICAL, "Delivery attempt failed"))
    if s.status == ShipmentStatus.CREATED and s.day_now > s.handling_days + 0.75:
        f.append(Finding("pickup_missed", Severity.HIGH_RISK, f"No pickup {s.day_now:.1f}d after booking"))
    silent = s.day_now - s.last_event_day
    if silent > 1.5 and s.status not in (ShipmentStatus.CREATED,):
        sev = Severity.HIGH_RISK if silent > 2.5 else Severity.WARNING
        f.append(Finding("tracking_stalled", sev, f"No tracking update for {silent:.1f}d"))
    if s.status == ShipmentStatus.AT_HUB and silent > 1.0:
        f.append(Finding("stuck_at_hub", Severity.HIGH_RISK, f"Parcel idle at hub for {silent:.1f}d"))
    if s.day_now > s.promised_days:
        over = s.day_now - s.promised_days
        f.append(Finding("eta_exceeded", Severity.CRITICAL if over > 1.5 else Severity.HIGH_RISK,
                         f"Promise of {s.promised_days:.0f}d exceeded by {over:.1f}d"))
    if not s.carrier_api_healthy:
        f.append(Finding("carrier_api_down", Severity.WARNING, "Carrier API circuit is open"))
    if not s.carrier_capacity_ok:
        f.append(Finding("capacity_exceeded", Severity.HIGH_RISK, "Carrier capacity exhausted"))
    if s.late_risk >= 0.5:
        f.append(Finding("high_delay_probability", Severity.HIGH_RISK, f"Delay probability {s.late_risk:.0%}"))
    elif s.late_risk >= 0.25:
        f.append(Finding("elevated_delay_probability", Severity.WARNING, f"Delay probability {s.late_risk:.0%}"))

    sev = max((x.severity for x in f), key=lambda v: RANK[v], default=Severity.NORMAL)
    actions: list[str] = []
    if sev in (Severity.HIGH_RISK, Severity.CRITICAL):
        actions += ["alert_operations", "notify_seller", "update_customer_eta", "recommend_intervention"]
    elif sev == Severity.WARNING:
        actions += ["monitor_closely", "update_customer_eta"]
    # Re-optimisation is only possible while the parcel is still with the seller (not picked up).
    can_reopt = s.status == ShipmentStatus.CREATED and sev != Severity.NORMAL and (
        not s.carrier_api_healthy or not s.carrier_capacity_ok or any(x.code in ("pickup_missed", "high_delay_probability") for x in f))
    if can_reopt:
        actions.append("reoptimize_fulfilment_plan")
    revised = None
    if sev != Severity.NORMAL:
        revised = round(max(s.predicted_days, s.day_now + 0.5) + (0.5 if sev == Severity.CRITICAL else 0.0), 1)
    return ExceptionReport(sev, f, actions, can_reopt, revised)
