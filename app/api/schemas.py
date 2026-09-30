from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.core.models import OrderIn


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class DecisionIn(BaseModel):
    order: OrderIn
    profile: Optional[str] = Field(default=None, description="economy|express|reliability|peak_season|balanced; default = active policy")
    custom_weights: Optional[dict[str, float]] = None
    max_late_risk: Optional[float] = Field(default=None, gt=0, le=1)


class BookIn(BaseModel):
    order_id: str
    decision_id: Optional[int] = None


class PolicyIn(BaseModel):
    profile: str = "balanced"
    custom_weights: Optional[dict[str, float]] = None


class BatchIn(BaseModel):
    orders: list[OrderIn] = Field(min_length=1, max_length=500)
    profile: Optional[str] = None
    custom_weights: Optional[dict[str, float]] = None
    carrier_capacity: Optional[dict[str, int]] = Field(default=None, description="Override free capacity per carrier")
    apply: bool = Field(default=False, description="Book the assigned plans")


class ScenarioIn(BaseModel):
    name: str = "scenario"
    profile: str = "balanced"
    custom_weights: Optional[dict[str, float]] = None
    n_orders: int = Field(default=500, ge=50, le=3000)
    volume_multiplier: float = Field(default=1.0, ge=0.25, le=6)
    disabled_carriers: list[str] = []
    capacity_multiplier: dict[str, float] = {}
    extra_nodes: list[dict] = []
    month: int = Field(default=6, ge=1, le=12)
    sla_days: Optional[float] = Field(default=None, gt=0)
    strategy: str = Field(default="optimizer", pattern="^(optimizer|cheapest|single:[A-Z]+)$")


class WhatIfIn(BaseModel):
    scenario: ScenarioIn
    baseline: Optional[ScenarioIn] = None


class DisruptIn(BaseModel):
    carrier_id: str
    kind: str = Field(pattern="^(outage|capacity|latency|stall|clear)$")


class TickIn(BaseModel):
    days: float = Field(default=0.5, gt=0, le=30)


class SeedIn(BaseModel):
    orders: int = Field(default=80, ge=1, le=400)
    advance_days: float = Field(default=4.0, ge=0, le=30)
    seed: int = 5
    source: Optional[Literal["real", "synthetic"]] = None    # default: real Olist orders when the calibration artifact is present
