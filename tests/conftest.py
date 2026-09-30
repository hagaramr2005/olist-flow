import pytest

from app.core.config import Settings
from app.integration.registry import CarrierRegistry
from app.intelligence.network import FulfillmentNetwork
from app.intelligence.prediction import PredictionEngine
from app.services.container import Container
from app.services.engine import FlowEngine
from app.services.events import EventBus
from app.services.store import Store


@pytest.fixture(scope="session", autouse=True)
def _hermetic_physics():
    """Engine-logic tests run on the fixed synthetic physics so they never depend on the data artifact.
    Calibrated behaviour is covered explicitly in tests/test_data.py."""
    from app.data import calibration
    with calibration.use(None):
        yield


@pytest.fixture(scope="session")
def predictor_base():
    p = PredictionEngine(seed=7)
    p.fit()
    return p


def make_container(predictor, **overrides) -> Container:
    settings = Settings(flakiness_scale=0.0, carrier_timeout_s=1.0, **overrides)
    store = Store(":memory:")
    registry = CarrierRegistry(settings)
    network = FulfillmentNetwork()
    bus = EventBus(store)
    # each test gets its own copy of the mutable reliability table so online learning can't leak between tests
    import copy
    pred = copy.copy(predictor)
    pred.table = copy.deepcopy(predictor.table)
    engine = FlowEngine(settings, store, registry, network, pred, bus)
    from app.api.security import seed_default_users
    seed_default_users(store)
    return Container(settings, store, registry, network, pred, bus, engine)


@pytest.fixture()
def c(predictor_base):
    return make_container(predictor_base)


@pytest.fixture()
def order():
    from app.core.models import OrderIn
    return OrderIn(order_id="T-1", customer_state="RJ", weight_kg=2.1, sla_days=4, allowed_origins=["SP-1"], seller_id="S001")
