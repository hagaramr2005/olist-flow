"""Brazilian geography: state centroids, regions, distance."""
from __future__ import annotations

import math

# state -> (lat, lon, region)
STATES: dict[str, tuple[float, float, str]] = {
    "SP": (-22.19, -48.79, "SE"), "RJ": (-22.25, -42.66, "SE"), "MG": (-18.10, -44.38, "SE"),
    "ES": (-19.19, -40.34, "SE"), "PR": (-24.89, -51.55, "S"), "SC": (-27.45, -50.95, "S"),
    "RS": (-30.17, -53.50, "S"), "BA": (-12.96, -41.70, "NE"), "PE": (-8.38, -37.86, "NE"),
    "CE": (-5.20, -39.53, "NE"), "MA": (-5.42, -45.44, "NE"), "PB": (-7.12, -36.72, "NE"),
    "RN": (-5.81, -36.59, "NE"), "AL": (-9.62, -36.82, "NE"), "SE": (-10.57, -37.45, "NE"),
    "PI": (-7.72, -42.73, "NE"), "GO": (-15.98, -49.86, "CO"), "DF": (-15.83, -47.86, "CO"),
    "MT": (-12.64, -55.42, "CO"), "MS": (-20.51, -54.54, "CO"), "PA": (-3.79, -52.48, "N"),
    "AM": (-3.47, -65.10, "N"), "TO": (-10.18, -48.33, "N"), "RO": (-10.83, -63.34, "N"),
    "AC": (-9.02, -70.81, "N"), "AP": (1.41, -51.77, "N"), "RR": (2.74, -61.31, "N"),
}

# Synthetic fallbacks, used only when no real-data calibration artifact is available.
# (shaped like the Olist findings: sellers are far more concentrated in SP than customers are).
SYNTHETIC_CUSTOMER_WEIGHTS = {"SP": 42, "RJ": 13, "MG": 12, "RS": 5.5, "PR": 5, "SC": 3.7, "BA": 3.4, "DF": 2.1,
                    "GO": 2.0, "ES": 2.0, "PE": 1.6, "CE": 1.3, "PA": 1.0, "MT": 0.9, "MA": 0.7, "MS": 0.7}
SYNTHETIC_SELLER_WEIGHTS = {"SP": 60, "PR": 11, "MG": 8, "SC": 7, "RJ": 6.5, "RS": 3.5, "DF": 1.0, "GO": 0.8, "ES": 0.7, "BA": 0.6}


def customer_weights() -> dict[str, float]:
    """Destination demand by state: real Olist shares when calibrated, synthetic otherwise."""
    from app.data import calibration
    c = calibration.get()
    return dict(c.customer_weights) if c else dict(SYNTHETIC_CUSTOMER_WEIGHTS)


def seller_weights() -> dict[str, float]:
    from app.data import calibration
    c = calibration.get()
    return dict(c.seller_weights) if c else dict(SYNTHETIC_SELLER_WEIGHTS)


def haversine_km(a: str, b: str) -> float:
    la1, lo1, _ = STATES[a]
    la2, lo2, _ = STATES[b]
    p1, p2 = math.radians(la1), math.radians(la2)
    dphi, dl = p2 - p1, math.radians(lo2 - lo1)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def region(state: str) -> str:
    return STATES[state][2]


def distance_km(a: str, b: str) -> float:
    """Route distance. Real zip-prefix-to-zip-prefix median (Olist geolocation) when observed, else state centroids."""
    from app.data import calibration
    c = calibration.get()
    real = c.distance_km(a, b) if c else None
    return real if real is not None else haversine_km(a, b)
