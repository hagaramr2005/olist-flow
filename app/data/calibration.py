"""Real-data calibration layer (runtime side).

The Olist CSVs (~120 MB) are *not* needed at runtime. `scripts/build_calibration.py` distils them into a small,
checksummed artifact (`app/data/artifacts/calibration.json` + a bootstrap sample of real orders). Everything
here is numpy/json only, so the service starts without pandas or the raw files. If the artifact is absent the
platform falls back to its synthetic physics and says so (`/v1/data/evidence` -> `"active": false`).

Override with OLISTFLOW_CALIBRATION=<path> or OLISTFLOW_CALIBRATION=none.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from app.core.logging import get_logger

log = get_logger("calibration")

ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts"
DEFAULT_PATH = ARTIFACT_DIR / "calibration.json"
SAMPLE_NAME = "real_orders.json.gz"
SHRINK_K = 30            # pseudo-observations when blending a sparse route with its region pair


def checksum(d: dict) -> str:
    body = {k: v for k, v in d.items() if k != "meta"} | {"meta": {k: v for k, v in d["meta"].items() if k != "checksum"}}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Calibration:
    def __init__(self, d: dict, path: Optional[Path] = None) -> None:
        self.d = d
        self.path = path
        self.meta: dict = d["meta"]
        self.integrity_ok = checksum(d) == self.meta.get("checksum")
        self._sample: Optional[dict] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ identity
    @property
    def token(self) -> str:
        return str(self.meta.get("checksum", "none"))[:16]

    # ------------------------------------------------------------ marginals
    @property
    def customer_weights(self) -> dict[str, float]:
        return self.d["customer_weights"]

    @property
    def seller_weights(self) -> dict[str, float]:
        return self.d["seller_weights"]

    # ------------------------------------------------------------ routes
    def _pair(self, o: str, d: str, region_of) -> Optional[dict]:
        return self.d["region_pairs"].get(f"{region_of(o)}>{region_of(d)}")

    def route(self, o: str, d: str) -> Optional[dict]:
        return self.d["routes"].get(f"{o}>{d}")

    def route_value(self, o: str, d: str, key: str, region_of) -> Optional[float]:
        """Observed value for a route, shrunk toward its region pair when the route is sparse."""
        r, rp = self.route(o, d), self._pair(o, d, region_of)
        if r and r.get(key) is not None:
            if not rp or rp.get(key) is None:
                return float(r[key])
            w = r["n"] / (r["n"] + SHRINK_K)
            return float(w * r[key] + (1 - w) * rp[key])
        if rp and rp.get(key) is not None:
            return float(rp[key])
        return None

    def distance_km(self, o: str, d: str) -> Optional[float]:
        r = self.route(o, d)
        return float(r["km"]) if r and r.get("km") is not None and r["n"] >= 5 else None

    # ------------------------------------------------------------ dynamics
    @property
    def network(self) -> dict:
        return self.d["network"]

    def month_penalty(self, month: int) -> float:
        return float(self.d["month_penalty"].get(str(month), 0.0))

    def month_weights(self) -> dict[int, float]:
        return {int(k): float(v["n"]) for k, v in self.d["monthly"].items()}

    def freight_scale(self, dest_region: str) -> float:
        return float(self.d.get("freight_scale", {}).get(dest_region, 1.0))

    @property
    def review(self) -> dict:
        return self.d["review"]

    # ------------------------------------------------------------ bootstrap sample of real orders
    def sample(self) -> dict:
        with self._lock:
            if self._sample is None:
                p = (self.path.parent if self.path else ARTIFACT_DIR) / SAMPLE_NAME
                with gzip.open(p, "rt", encoding="utf-8") as f:
                    self._sample = json.load(f)
            return self._sample

    def sample_orders(self, rng, n: int) -> list[dict]:
        """Bootstrap n *real* orders (joint draw, so origin/destination/weight/SLA keep their real correlations)."""
        s = self.sample()
        states, cats, size = s["states"], s["categories"], len(s["month"])
        out = []
        for _ in range(n):
            i = rng.randrange(size)
            out.append({
                "seller_state": states[s["seller_state"][i]], "customer_state": states[s["customer_state"][i]],
                "month": s["month"][i], "weight_kg": s["weight_kg"][i], "volume_cm3": s["volume_cm3"][i],
                "value_brl": s["price"][i], "category": cats[s["category"][i]], "promised_days": s["promised_days"][i],
                "real_freight": s["freight"][i],
            })
        return out


# ------------------------------------------------------------------ registry
_state: dict[str, Any] = {"loaded": False, "cal": None}
_lock = threading.Lock()


def load(path: Optional[str] = None) -> Optional[Calibration]:
    raw = path or os.environ.get("OLISTFLOW_CALIBRATION") or str(DEFAULT_PATH)
    if raw.lower() == "none":
        return None
    p = Path(raw)
    if not p.exists():
        return None
    try:
        cal = Calibration(json.loads(p.read_text(encoding="utf-8")), p)
    except (OSError, ValueError, KeyError) as e:          # a corrupt artifact must never take the service down
        log.warning("calibration artifact unreadable; using synthetic physics", extra={"ctx": {"path": str(p), "error": str(e)}})
        return None
    if not cal.integrity_ok:
        log.warning("calibration checksum mismatch (artifact edited?)", extra={"ctx": {"path": str(p)}})
    return cal


def get() -> Optional[Calibration]:
    if not _state["loaded"]:
        with _lock:
            if not _state["loaded"]:
                _state["cal"], _state["loaded"] = load(), True
    return _state["cal"]


def token() -> str:
    c = get()
    return c.token if c else "none"


@contextmanager
def use(cal: Optional[Calibration]) -> Iterator[None]:
    """Temporarily swap the active calibration (ETL, tests). Caches elsewhere are keyed on `token()`."""
    with _lock:
        prev = (_state["cal"], _state["loaded"])
        _state["cal"], _state["loaded"] = cal, True
    try:
        yield
    finally:
        with _lock:
            _state["cal"], _state["loaded"] = prev
