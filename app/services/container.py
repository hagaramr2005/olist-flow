"""Composition root: builds every service once and wires them together."""
from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings, get_settings
from app.core.logging import get_logger, setup_logging
from app.integration.registry import CarrierRegistry
from app.intelligence.network import FulfillmentNetwork
from app.intelligence.prediction import PredictionEngine
from app.services.engine import FlowEngine
from app.services.events import EventBus
from app.services.store import Store

log = get_logger("container")


@dataclass
class Container:
    settings: Settings
    store: Store
    registry: CarrierRegistry
    network: FulfillmentNetwork
    predictor: PredictionEngine
    bus: EventBus
    engine: FlowEngine


def build_container(settings: Settings | None = None, db_path: str | None = None, seed_users: bool = True) -> Container:
    settings = settings or get_settings()
    setup_logging()
    store = Store(db_path if db_path is not None else settings.db_path)
    predictor = PredictionEngine(seed=settings.seed)
    predictor.fit()
    registry = CarrierRegistry(settings)
    network = FulfillmentNetwork()
    bus = EventBus(store)
    engine = FlowEngine(settings, store, registry, network, predictor, bus)
    if seed_users:
        from app.api.security import seed_default_users
        seed_default_users(store)
    return Container(settings, store, registry, network, predictor, bus, engine)
