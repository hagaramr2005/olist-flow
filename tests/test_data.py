"""Real-data integration: artifact integrity, calibrated physics, real-order replay, evidence API, and the ETL rules."""
import copy
import random

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.core import geo
from app.core.models import ServiceLevel
from app.data import calibration as cal
from app.data import evidence
from app.integration import world
from app.intelligence.prediction import PredictionEngine
from app.services import seed
from tests.conftest import make_container

ARTIFACT = cal.DEFAULT_PATH
pytestmark = pytest.mark.skipif(not ARTIFACT.exists(), reason="calibration artifact not built (scripts/build_calibration.py)")


@pytest.fixture(scope="module")
def real():
    c = cal.load(str(ARTIFACT))
    assert c is not None
    return c


@pytest.fixture(scope="module")
def predictor_real(real):
    with cal.use(real):
        p = PredictionEngine(seed=7)
        p.fit()
    return p


# ---------------------------------------------------------------- artifact
def test_artifact_is_intact_and_complete(real):
    assert real.integrity_ok
    h = real.d["headline"]
    assert h["orders_analysed"] > 90000
    assert abs(sum(real.customer_weights.values()) - 100) < 0.5 and abs(sum(real.seller_weights.values()) - 100) < 0.5
    assert set(real.customer_weights) <= set(geo.STATES) and set(real.seller_weights) <= set(geo.STATES)
    assert len(real.d["routes"]) > 300


def test_tampering_is_detected(real):
    d = copy.deepcopy(real.d)
    d["headline"]["late_rate"] = 0.0
    assert not cal.Calibration(d).integrity_ok


def test_time_calibration_never_hits_its_sanity_bounds(real):
    """If the clip ever binds, it silently hides a simulator that is out of touch with the real network."""
    assert real.d["diagnostics"]["time_factor_clipped_flow_share"] == 0.0


def test_published_measurements_are_in_the_measured_range(real):
    """Guards the numbers quoted in the README / data card against drift (exact values live in docs/DATA_CARD.md)."""
    h, r = real.d["headline"], real.d["review"]
    assert 0.63 < h["cross_state_share"] < 0.65
    assert 70 < h["freight_premium_order_pct"] < 80 and 70 < h["freight_premium_item_pct"] < 80
    assert 4.25 < r["on_time"] < 4.35 and 2.2 < r["late"] < 2.35
    assert 0.06 < h["late_rate"] < 0.08


# ---------------------------------------------------------------- fallback
def test_without_artifact_the_platform_falls_back_and_says_so():
    with cal.use(None):
        assert geo.customer_weights() == geo.SYNTHETIC_CUSTOMER_WEIGHTS
        assert evidence.build(cal.get())["active"] is False
        assert world.peak_penalty(11) == 0.09
        with pytest.raises(ValueError):
            seed.seed_demo(make_container(PredictionEngine(seed=1)), 1, source="real")


def test_corrupt_artifact_does_not_take_the_service_down(tmp_path):
    bad = tmp_path / "calibration.json"
    bad.write_text("{not json")
    assert cal.load(str(bad)) is None


# ---------------------------------------------------------------- calibrated physics
def test_real_routes_are_slower_and_riskier_than_the_old_simulator(real):
    p = next(x for x in world.PROFILES if x.carrier_id == "RAPIDOSUL")
    with cal.use(None):
        old = world.transit_days(p, "SP", "RJ", ServiceLevel.STANDARD)
    with cal.use(real):
        new = world.transit_days(p, "SP", "RJ", ServiceLevel.STANDARD)
        assert new > 1.5 * old                                  # Olist SP->RJ carrier transit median is ~9 days
        assert world.route_risk_index("SP", "RJ") > 1.5         # RJ is late ~2x as often as the network average
        assert world.route_risk_index("SP", "SP") < 1.0


def test_observed_disruption_months_are_penalised(real):
    with cal.use(real):
        assert world.peak_penalty(11) > 0.03 and world.peak_penalty(3) >= world.peak_penalty(11)
        assert world.peak_penalty(6) == 0.0


def test_real_distance_replaces_centroids_only_where_observed(real):
    with cal.use(real):
        assert geo.distance_km("SP", "SP") > 10                 # centroid distance would be exactly 0
        assert geo.distance_km("AC", "RR") == geo.haversine_km("AC", "RR")   # unobserved route -> fallback


def test_tariffs_match_observed_freight_level(real):
    p = next(x for x in world.PROFILES if x.carrier_id == "POSTALBR")
    with cal.use(real):
        scaled = world.freight_cost(p, "SP", "RJ", 1.0, 5000, ServiceLevel.STANDARD)
        raw = world.freight_cost(p, "SP", "RJ", 1.0, 5000, ServiceLevel.STANDARD, calibrated=False)
    assert scaled < raw


def test_calibrated_history_trains_a_useful_model(predictor_real):
    r = predictor_real.report
    assert r["history_source"].startswith("real_order_bootstrap") and r["late_auc"] > 0.6 and r["top_decile_lift"] > 1.5
    assert r["eta_mae_days"] < r["carrier_promise_mae_days"] * 1.05


def test_bootstrap_is_deterministic_and_real(real):
    a, b = real.sample_orders(random.Random(3), 50), real.sample_orders(random.Random(3), 50)
    assert a == b and all(x["customer_state"] in geo.STATES and x["weight_kg"] > 0 for x in a)


# ---------------------------------------------------------------- real-order replay
def test_real_order_replay_books_through_the_real_pipeline(real, predictor_real):
    with cal.use(real):
        c = make_container(predictor_real)
        out = seed.seed_demo(c, n_orders=40, advance_days=1, seed=11, source="real")
    assert out["source"] == "real" and out["booked"] >= 36
    assert c.store.one("SELECT COUNT(*) n FROM shipments")["n"] == out["booked"]


# ---------------------------------------------------------------- API
def test_evidence_endpoint_and_models_payload(real, predictor_real):
    with cal.use(real):
        c = make_container(predictor_real, rate_limit_per_min=100000)
        cl = TestClient(create_app(c))
        tok = cl.post("/auth/login", json={"username": "analyst", "password": "olistflow-dev"}).json()["access_token"]
        h = {"Authorization": "Bearer " + tok}
        e = cl.get("/v1/data/evidence", headers=h).json()
        assert e["active"] and e["integrity_ok"] and len(e["states"]) == 27 and e["top_routes"] and len(e["insights"]) >= 5
        m = cl.get("/v1/models", headers=h).json()
        assert m["data_calibration"]["active"] and m["real_data_benchmark"]["eta"]["mae_days_model"] < m["real_data_benchmark"]["eta"]["mae_days_olist_estimate"]
        assert cl.get("/health/ready").json()["calibration"] == real.token
        assert cl.get("/v1/data/evidence").status_code == 401


def test_seed_demo_rejects_real_source_without_artifact(predictor_real):
    with cal.use(None):
        c = make_container(predictor_real, rate_limit_per_min=100000)
        cl = TestClient(create_app(c))
        tok = cl.post("/auth/login", json={"username": "admin", "password": "olistflow-dev"}).json()["access_token"]
        r = cl.post("/v1/ops/seed-demo", json={"orders": 2, "source": "real"}, headers={"Authorization": "Bearer " + tok})
        assert r.status_code == 422


# ---------------------------------------------------------------- ETL rules (tiny hand-made frames)
pd = pytest.importorskip("pandas")


def _raw():
    ts = pd.Timestamp
    orders = pd.DataFrame({
        "order_id": ["a", "b", "c", "d", "e"], "customer_id": ["c1", "c2", "c3", "c4", "c5"],
        "order_status": ["delivered", "delivered", "delivered", "canceled", "delivered"],
        "order_purchase_timestamp": [ts("2018-01-01 10:00")] * 5,
        "order_approved_at": [ts("2018-01-01 11:00")] * 5,
        "order_delivered_carrier_date": [ts("2018-01-03 10:00")] * 5,
        "order_delivered_customer_date": [ts("2018-01-10 23:00"), ts("2018-01-12 09:00"), ts("2018-01-09 10:00"), pd.NaT, ts("2018-01-09 10:00")],
        "order_estimated_delivery_date": [ts("2018-01-10"), ts("2018-01-10"), ts("2018-01-10"), ts("2018-01-10"), ts("2018-01-10")]})
    items = pd.DataFrame({
        "order_id": ["a", "b", "c", "c", "d", "e"], "order_item_id": [1, 1, 1, 1, 1, 1],
        "product_id": ["p1", "p1", "p1", "p2", "p1", "p2"], "seller_id": ["s1", "s1", "s1", "s2", "s1", "s1"],
        "price": [100.0] * 6, "freight_value": [20.0] * 6})
    products = pd.DataFrame({"product_id": ["p1", "p2"], "product_category_name": ["beleza", "beleza"],
                             "product_weight_g": [500.0, None], "product_length_cm": [10.0, None],
                             "product_height_cm": [10.0, None], "product_width_cm": [10.0, None]})
    sellers = pd.DataFrame({"seller_id": ["s1", "s2"], "seller_state": ["SP", "RJ"], "seller_zip_code_prefix": [1000, 2000]})
    customers = pd.DataFrame({"customer_id": [f"c{i}" for i in range(1, 6)], "customer_state": ["RJ", "RJ", "SP", "SP", "SP"],
                              "customer_zip_code_prefix": [2000, 2000, 1000, 1000, 1000]})
    reviews = pd.DataFrame({"order_id": ["a", "a", "b"], "review_score": [1, 5, 2],
                            "review_answer_timestamp": [ts("2018-01-11"), ts("2018-01-12"), ts("2018-01-13")]})
    payments = pd.DataFrame({"order_id": ["a", "b", "e"], "payment_value": [120.0, 120.0, 500.0]})
    geo_df = pd.DataFrame({"geolocation_zip_code_prefix": [1000, 2000, 2000], "geolocation_lat": [-23.5, -22.9, 40.0],
                           "geolocation_lng": [-46.6, -43.2, 10.0]})           # last point is outside Brazil and must be ignored
    tr = pd.DataFrame({"product_category_name": ["beleza"], "product_category_name_english": ["health_beauty"]})
    return dict(orders=orders, items=items, products=products, sellers=sellers, customers=customers, reviews=reviews,
                payments=payments, geo=geo_df, translation=tr)


def test_etl_cleaning_rules():
    from app.data import olist_etl as etl
    d, q = etl.build_orders(_raw())
    by = d.set_index("order_id")
    assert set(by.index) == {"a", "b", "e"}                      # c multi-seller, d not delivered
    assert q["multi_seller_dropped"] == 1
    assert by.loc["a", "late"] == 0                              # delivered late on the estimated *day*: on time
    assert by.loc["b", "late"] == 1
    assert by.loc["e", "weight_kg"] == pytest.approx(0.5)        # missing product weight filled with category median
    assert by.loc["a", "review_score"] == 5                      # latest review wins
    assert by.loc["a", "category"] == "health_beauty"
    assert by.loc["a", "handling_days"] == pytest.approx(2.0) and by.loc["a", "transit_days"] == pytest.approx(7.0 + 13 / 24)
    assert 300 < by.loc["a", "km"] < 450 and by.loc["b", "km"] == by.loc["a", "km"]   # SP<->RJ zip centroids; stray point ignored
    assert q["payment_reconciles_pct"] == pytest.approx(66.67, abs=0.01)           # e does not reconcile (500 vs 120)


def test_etl_refuses_a_dataset_that_is_not_olist(tmp_path):
    from app.data import olist_etl as etl
    with pytest.raises(etl.DataError):
        etl.load_raw(tmp_path)
