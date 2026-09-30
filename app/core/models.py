"""Domain models shared across layers."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ServiceLevel(str, Enum):
    ECONOMY = "economy"
    STANDARD = "standard"
    EXPRESS = "express"


class Severity(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    HIGH_RISK = "high_risk"
    CRITICAL = "critical"


class ShipmentStatus(str, Enum):
    CREATED = "ShipmentCreated"
    PICKED_UP = "PickedUp"
    AT_HUB = "ArrivedAtHub"
    OUT_FOR_DELIVERY = "OutForDelivery"
    DELIVERED = "Delivered"
    FAILED = "DeliveryFailed"
    CANCELLED = "Cancelled"


class OrderIn(BaseModel):
    order_id: str = Field(min_length=1, max_length=64)
    customer_state: str = Field(min_length=2, max_length=2)
    weight_kg: float = Field(gt=0, le=100)
    volume_cm3: float = Field(default=5000, gt=0)
    value_brl: float = Field(default=100, ge=0)
    category: str = "general"
    sla_days: Optional[float] = Field(default=None, gt=0, description="Customer promised delivery window")
    restricted: bool = False
    seller_id: Optional[str] = Field(default=None, max_length=64)
    seller_hint: Optional[str] = Field(default=None, description="State of the primary seller/warehouse")
    allowed_origins: Optional[list[str]] = Field(default=None, description="Restrict fulfilment to these node ids")
    priority: str = Field(default="normal", pattern="^(normal|high)$")
    ordered_month: Optional[int] = Field(default=None, ge=1, le=12)


class Origin(BaseModel):
    node_id: str
    name: str
    state: str
    capacity_per_day: int
    load: int = 0
    stock_ok: bool = True

    @property
    def free_capacity(self) -> int:
        return max(0, self.capacity_per_day - self.load)


class Quote(BaseModel):
    carrier_id: str
    service: ServiceLevel
    cost: float
    promised_days: float
    origin_id: str


class Option(BaseModel):
    """One candidate fulfilment plan = origin x carrier x service level."""
    option_id: str
    origin_id: str
    origin_state: str
    carrier_id: str
    service: ServiceLevel
    cost: float
    promised_days: float
    predicted_days: float
    late_risk: float
    reliability: float
    distance_km: float
    capacity_free_ratio: float
    origin_capacity_free_ratio: float = 1.0
    feasible: bool = True
    reasons: list[str] = Field(default_factory=list)
    score: float = 0.0
    components: dict[str, float] = Field(default_factory=dict)
