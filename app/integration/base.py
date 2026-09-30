"""Unified carrier contract. Olist code never talks to a carrier-specific API."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from pydantic import BaseModel

from app.core.models import OrderIn, ServiceLevel, ShipmentStatus


class CarrierError(Exception):
    """Base class for every carrier-side failure."""


class CarrierUnavailable(CarrierError):
    """Transient failure (5xx, timeout, connection). Safe to retry."""


class CoverageError(CarrierError):
    """Permanent: carrier cannot serve this request. Never retried."""


class CapacityExceeded(CarrierError):
    """Permanent for now: carrier is full for the requested window."""


class CoverageResult(BaseModel):
    covered: bool
    reason: str = ""


class QuoteResult(BaseModel):
    carrier_id: str
    service: ServiceLevel
    cost: float
    promised_days: float


class BookingResult(BaseModel):
    carrier_id: str
    tracking_code: str
    idempotency_key: str
    cost: float
    promised_days: float
    replayed: bool = False


class TrackingEvent(BaseModel):
    tracking_code: str
    status: ShipmentStatus
    day: float          # simulated days since booking
    hub: Optional[str] = None
    note: str = ""


class CarrierAdapter(ABC):
    carrier_id: str
    name: str

    @abstractmethod
    def check_coverage(self, origin_state: str, order: OrderIn) -> CoverageResult: ...

    @abstractmethod
    def get_quote(self, origin_state: str, order: OrderIn, service: ServiceLevel) -> QuoteResult: ...

    @abstractmethod
    def book_shipment(self, origin_state: str, order: OrderIn, service: ServiceLevel,
                      idempotency_key: str) -> BookingResult: ...

    @abstractmethod
    def cancel_shipment(self, tracking_code: str) -> bool: ...

    @abstractmethod
    def generate_label(self, tracking_code: str) -> str: ...

    @abstractmethod
    def track_shipment(self, tracking_code: str, as_of_day: float) -> list[TrackingEvent]: ...

    @abstractmethod
    def capacity_snapshot(self) -> dict: ...
