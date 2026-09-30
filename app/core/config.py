"""Central configuration. Every value can be overridden through an OLISTFLOW_* env var."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(f"OLISTFLOW_{name}", default)


def _review(kind: str, fallback: float) -> float:
    """Review-score impact of (late) delivery: measured on the real orders when a calibration artifact exists."""
    from app.data import calibration
    c = calibration.get()
    return round(float(c.review[kind]), 2) if c else fallback


@dataclass(frozen=True)
class Settings:
    app_name: str = "Olist Flow"
    env: str = field(default_factory=lambda: _env("ENV", "dev"))
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "olist_flow.db"))
    jwt_secret: str = field(default_factory=lambda: _env("JWT_SECRET", "dev-only-change-me-please-32-bytes!"))
    jwt_ttl_minutes: int = field(default_factory=lambda: int(_env("JWT_TTL_MIN", "60")))
    rate_limit_per_min: int = field(default_factory=lambda: int(_env("RATE_LIMIT", "300")))
    seed: int = field(default_factory=lambda: int(_env("SEED", "42")))
    # Resilience
    carrier_timeout_s: float = 1.5
    carrier_retries: int = 2
    breaker_failure_threshold: int = 5
    flakiness_scale: float = field(default_factory=lambda: float(_env("FLAKINESS", "0.4")))
    breaker_reset_s: float = 15.0
    # Business: review-score impact measured in the discovery notebook
    review_on_time: float = field(default_factory=lambda: _review("on_time", 4.29))
    review_late: float = field(default_factory=lambda: _review("late", 2.27))


def get_settings() -> Settings:
    s = Settings()
    if s.env == "production" and s.jwt_secret.startswith("dev-only"):
        raise RuntimeError("OLISTFLOW_JWT_SECRET must be set when OLISTFLOW_ENV=production")
    return s
