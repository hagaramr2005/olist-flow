"""Ground-truth logistics 'physics' used by the simulated carriers and by the Digital Twin.

Everything here is calibrated to the discovery notebook: cross-state freight is ~76% more
expensive than same-state, and long/remote routes are where delays concentrate.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache

from app.core.geo import STATES, distance_km, region
from app.core.models import ServiceLevel
from app.data import calibration as cal

SERVICE_SPEED = {ServiceLevel.ECONOMY: 0.78, ServiceLevel.STANDARD: 1.0, ServiceLevel.EXPRESS: 1.55}
SERVICE_PRICE = {ServiceLevel.ECONOMY: 0.82, ServiceLevel.STANDARD: 1.0, ServiceLevel.EXPRESS: 1.75}
# Regions that are structurally harder to reach.
REGION_DIFFICULTY = {"SE": 1.0, "S": 1.05, "CO": 1.2, "NE": 1.35, "N": 1.9}


@dataclass(frozen=True)
class CarrierProfile:
    carrier_id: str
    name: str
    base_cost: float                 # R$ fixed
    cost_per_kg: float
    cost_per_100km: float
    speed_kmpd: float                # effective km per day in transit
    handling_days: float
    regions: tuple[str, ...]         # served destination regions
    origin_regions: tuple[str, ...]
    max_weight_kg: float
    services: tuple[ServiceLevel, ...]
    daily_capacity: int
    accepts_restricted: bool = False
    # route-specific reliability: (origin_region, dest_region) -> on-time probability
    reliability: dict = field(default_factory=dict)
    default_reliability: float = 0.85
    api_flakiness: float = 0.0       # prob. of transient API failure per call


PROFILES: list[CarrierProfile] = [
    CarrierProfile("POSTALBR", "PostalBR", 8.0, 1.6, 0.9, 380, 1.6, ("SE", "S", "CO", "NE", "N"),
                   ("SE", "S", "CO", "NE"), 30, (ServiceLevel.ECONOMY, ServiceLevel.STANDARD), 900,
                   True, {("SE", "SE"): .90, ("SE", "S"): .88, ("SE", "NE"): .74, ("SE", "N"): .62}, .80, 0.03),
    CarrierProfile("RAPIDOSUL", "RapidoSul", 10.5, 1.9, 1.0, 620, 0.9, ("SE", "S", "CO"),
                   ("SE", "S"), 25, (ServiceLevel.STANDARD, ServiceLevel.EXPRESS), 500,
                   False, {("SE", "SE"): .96, ("SE", "S"): .95, ("S", "SE"): .94, ("SE", "CO"): .90}, .85, 0.02),
    CarrierProfile("TRANSNORDESTE", "TransNordeste", 12.0, 2.1, 1.05, 480, 1.3, ("NE", "SE", "N"),
                   ("SE", "NE"), 40, (ServiceLevel.ECONOMY, ServiceLevel.STANDARD), 350,
                   False, {("SE", "NE"): .93, ("NE", "NE"): .95, ("SE", "N"): .80, ("SE", "SE"): .84}, .78, 0.05),
    CarrierProfile("AMAZONLOG", "AmazonLog", 18.0, 2.8, 1.25, 330, 2.4, ("N", "CO", "NE"),
                   ("SE", "CO", "N"), 60, (ServiceLevel.STANDARD,), 200,
                   True, {("SE", "N"): .86, ("CO", "N"): .90, ("SE", "CO"): .82}, .70, 0.06),
    CarrierProfile("CENTRALEXPRESS", "CentralExpress", 19.0, 3.1, 1.4, 900, 0.6, ("SE", "S", "CO", "NE", "N"),
                   ("SE", "S", "CO"), 20, (ServiceLevel.STANDARD, ServiceLevel.EXPRESS), 260,
                   False, {}, .95, 0.02),
    CarrierProfile("ECOFREIGHT", "EcoFreight", 5.5, 1.2, 0.7, 300, 2.0, ("SE", "S"),
                   ("SE", "S"), 25, (ServiceLevel.ECONOMY,), 700,
                   False, {("SE", "SE"): .83, ("SE", "S"): .76}, .70, 0.08),
    CarrierProfile("MEGACARGO", "MegaCargo", 24.0, 1.4, 1.1, 450, 1.8, ("SE", "S", "CO", "NE"),
                   ("SE", "S"), 100, (ServiceLevel.STANDARD, ServiceLevel.ECONOMY), 150,
                   True, {("SE", "SE"): .92, ("SE", "S"): .90}, .82, 0.04),
]
PROFILE_BY_ID = {p.carrier_id: p for p in PROFILES}


def route_reliability(p: CarrierProfile, o_state: str, d_state: str) -> float:
    """On-time probability of a carrier on a *specific route* (not one score per carrier)."""
    r = p.reliability.get((region(o_state), region(d_state)), p.default_reliability)
    # Long hauls erode reliability regardless of carrier.
    km = distance_km(o_state, d_state)
    r = r - 0.00003 * max(0.0, km - 800)
    # Real-data route risk: a route that is late twice as often as the network average (Olist orders) loses twice the
    # on-time probability for every carrier that serves it. Carrier ranking is preserved; route difficulty is real.
    r = 1.0 - (1.0 - r) * route_risk_index(o_state, d_state)
    return max(0.35, min(0.995, r))


def _raw_transit(p: CarrierProfile, o_state: str, d_state: str, service: ServiceLevel) -> float:
    km = max(60.0, distance_km(o_state, d_state))
    diff = REGION_DIFFICULTY[region(d_state)]
    base = p.handling_days + km / (p.speed_kmpd * SERVICE_SPEED[service])
    return base * (0.55 + 0.45 * diff)


# Sanity bounds only. They must never bind on real routes: the build reports the share of order flow that would be clipped.
TIME_FACTOR_BOUNDS = (0.25, 6.0)


@lru_cache(maxsize=4096)
def _route_time_factor(o_state: str, d_state: str, token: str) -> float:
    c = cal.get()
    real = c.route_value(o_state, d_state, "transit_p50", region) if c else None
    if real is None:
        return 1.0
    serving = [q for q in PROFILES if region(o_state) in q.origin_regions and region(d_state) in q.regions] or PROFILES
    ref = sum(_raw_transit(q, o_state, d_state, ServiceLevel.STANDARD if ServiceLevel.STANDARD in q.services else q.services[0])
              for q in serving) / len(serving)
    return max(TIME_FACTOR_BOUNDS[0], min(TIME_FACTOR_BOUNDS[1], real / ref))


def route_time_factor(o_state: str, d_state: str) -> float:
    """Real carrier-transit median (Olist: delivered-to-customer minus handed-to-carrier) over the simulated network mean."""
    return _route_time_factor(o_state, d_state, cal.token())


@lru_cache(maxsize=4096)
def _route_risk_index(o_state: str, d_state: str, token: str) -> float:
    c = cal.get()
    if not c:
        return 1.0
    late = c.route_value(o_state, d_state, "late", region)
    base = c.network.get("late_rate")
    if late is None or not base:
        return 1.0
    return max(0.4, min(3.0, late / base))


def route_risk_index(o_state: str, d_state: str) -> float:
    return _route_risk_index(o_state, d_state, cal.token())


def transit_days(p: CarrierProfile, o_state: str, d_state: str, service: ServiceLevel) -> float:
    return round(_raw_transit(p, o_state, d_state, service) * route_time_factor(o_state, d_state), 2)


def freight_cost(p: CarrierProfile, o_state: str, d_state: str, weight: float, volume: float,
                 service: ServiceLevel, calibrated: bool = True) -> float:
    km = distance_km(o_state, d_state)
    vol_weight = volume / 6000.0                 # cubic weight
    chargeable = max(weight, vol_weight)
    cross_state_penalty = 1.0 if o_state == d_state else 1.76 ** 0.5  # ~ +76% overall once distance is added
    raw = p.base_cost + p.cost_per_kg * chargeable + p.cost_per_100km * (km / 100.0)
    scale = cal.get().freight_scale(region(d_state)) if calibrated and cal.get() else 1.0   # tariffs match observed Olist freight
    return round(raw * SERVICE_PRICE[service] * cross_state_penalty * 0.75 * scale, 2)


def promised_days(p: CarrierProfile, o_state: str, d_state: str, service: ServiceLevel) -> float:
    """Carriers are optimistic: they promise faster than they deliver on hard routes."""
    t = transit_days(p, o_state, d_state, service)
    optimism = 0.88 if REGION_DIFFICULTY[region(d_state)] > 1.2 else 0.95
    return max(1.0, math.ceil(t * optimism))


def sample_actual_days(p: CarrierProfile, o_state: str, d_state: str, service: ServiceLevel, rng,
                       rel_penalty: float = 0.0) -> tuple[float, bool]:
    """Draw a realised delivery time. Returns (days, was_delayed)."""
    promised = promised_days(p, o_state, d_state, service)
    rel = route_reliability(p, o_state, d_state) - rel_penalty
    if rng.random() > rel:
        return round(promised * rng.uniform(1.2, 2.1) + rng.uniform(0.5, 2.0), 2), True
    t = transit_days(p, o_state, d_state, service)
    return round(max(0.8, min(promised, t * rng.uniform(0.85, 1.05))), 2), False


def peak_penalty(month: int) -> float:
    """Extra late probability by calendar month. Real (observed Olist lateness above the network mean) when calibrated:
    Nov-2017 Black Friday *and* the Feb-Mar 2018 disruption stand out. Synthetic Nov/Dec bump otherwise."""
    c = cal.get()
    if c:
        return c.month_penalty(month)
    return {11: 0.09, 12: 0.06, 1: 0.02}.get(month, 0.0)
