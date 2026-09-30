import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from tests.conftest import make_container

ORDER = {"order": {"order_id": "A1", "customer_state": "RJ", "weight_kg": 2.1, "sla_days": 4, "allowed_origins": ["SP-1"]}}


@pytest.fixture()
def api(predictor_base):
    c = make_container(predictor_base, rate_limit_per_min=100000)
    return TestClient(create_app(c)), c


def login(cl, user):
    r = cl.post("/auth/login", json={"username": user, "password": "olistflow-dev"})
    assert r.status_code == 200
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def test_health_is_public_and_ready(api):
    cl, _ = api
    assert cl.get("/health/live").json() == {"status": "ok"}
    assert cl.get("/health/ready").json()["status"] == "ready"


def test_authentication(api):
    cl, _ = api
    assert cl.get("/v1/shipments").status_code == 401
    assert cl.get("/v1/shipments", headers={"Authorization": "Bearer garbage"}).status_code == 401
    assert cl.post("/auth/login", json={"username": "ops", "password": "wrong"}).status_code == 401
    assert cl.post("/auth/login", json={"username": "nobody", "password": "x"}).status_code == 401


def test_expired_token_is_rejected(api):
    import time, jwt
    cl, c = api
    t = jwt.encode({"sub": "ops", "role": "ops_manager", "exp": int(time.time()) - 5}, c.settings.jwt_secret, algorithm="HS256")
    assert cl.get("/auth/me", headers={"Authorization": "Bearer " + t}).status_code == 401
    forged = jwt.encode({"sub": "ops", "role": "admin", "exp": int(time.time()) + 500}, "wrong-secret-wrong-secret-wrong-secret", algorithm="HS256")
    assert cl.get("/auth/me", headers={"Authorization": "Bearer " + forged}).status_code == 401


def test_rbac_matrix(api):
    cl, _ = api
    seller, analyst, ops, admin = (login(cl, u) for u in ("seller", "analyst", "ops", "admin"))
    assert cl.get("/v1/control-tower", headers=seller).status_code == 403
    assert cl.get("/v1/control-tower", headers=analyst).status_code == 200
    assert cl.post("/v1/decisions", json=ORDER, headers=analyst).status_code == 403
    assert cl.post("/v1/ops/disrupt", json={"carrier_id": "RAPIDOSUL", "kind": "outage"}, headers=analyst).status_code == 403
    assert cl.post("/v1/ops/seed-demo", json={"orders": 1}, headers=ops).status_code == 403
    assert cl.get("/v1/audit", headers=ops).status_code == 403
    assert cl.get("/v1/audit", headers=admin).status_code == 200
    assert cl.get("/metrics", headers=seller).status_code == 403 and cl.get("/metrics", headers=analyst).status_code == 200


def test_order_flow_and_idempotency(api):
    cl, _ = api
    h = login(cl, "ops")
    r = cl.post("/v1/orders", json=ORDER, headers={**h, "Idempotency-Key": "abcdef-123456"})
    assert r.status_code == 201 and r.json()["shipment"]["status"] == "booked"
    r2 = cl.post("/v1/orders", json=ORDER, headers={**h, "Idempotency-Key": "abcdef-123456"})
    assert r2.status_code == 200 and r2.json()["replayed"]
    r3 = cl.post("/v1/orders", json=ORDER, headers={**h, "Idempotency-Key": "another-key-999"})
    assert r3.status_code == 409                                        # same order, new key: still no second shipment
    assert len(cl.get("/v1/shipments", headers=h).json()["items"]) == 1
    assert cl.post("/v1/orders", json=ORDER, headers=h).status_code == 400          # key is mandatory


def test_seller_is_scoped_to_own_data(api):
    cl, _ = api
    ops, seller = login(cl, "ops"), login(cl, "seller")
    mine = {"order": {**ORDER["order"], "order_id": "M1"}}
    theirs = {"order": {**ORDER["order"], "order_id": "T1", "seller_id": "S999"}}
    assert cl.post("/v1/orders", json=mine, headers={**seller, "Idempotency-Key": "seller-key-0001"}).status_code == 201
    assert cl.post("/v1/orders", json=theirs, headers={**seller, "Idempotency-Key": "seller-key-0002"}).status_code == 403
    other = {"order": {**ORDER["order"], "order_id": "O1", "seller_id": "S777", "customer_state": "MG"}}
    cl.post("/v1/orders", json=other, headers={**ops, "Idempotency-Key": "ops-key-000001"})
    items = cl.get("/v1/shipments", headers=seller).json()["items"]
    assert [i["order_id"] for i in items] == ["M1"]
    sid = cl.get("/v1/shipments", headers=ops).json()["items"][0]["shipment_id"]
    assert cl.get(f"/v1/shipments/{sid}", headers=seller).status_code in (200, 404)
    other_id = [i for i in cl.get("/v1/shipments", headers=ops).json()["items"] if i["order_id"] == "O1"][0]["shipment_id"]
    assert cl.get(f"/v1/shipments/{other_id}", headers=seller).status_code == 404


def test_validation_errors(api):
    cl, _ = api
    h = login(cl, "ops")
    bad = {"order": {"order_id": "V", "customer_state": "RJ", "weight_kg": -1}}
    assert cl.post("/v1/decisions", json=bad, headers=h).status_code == 422
    assert cl.post("/v1/decisions", json={"order": ORDER["order"], "profile": "nope"}, headers=h).status_code == 400
    huge = {"order": {"order_id": "H", "customer_state": "AM", "weight_kg": 90}}
    r = cl.post("/v1/decisions", json=huge, headers=h)
    assert r.status_code == 422 and r.json()["code"] == "no_feasible_option"


def test_policy_change_is_audited_and_applied(api):
    cl, c = api
    h = login(cl, "ops")
    assert cl.put("/v1/policy", json={"profile": "economy"}, headers=h).status_code == 200
    d = cl.post("/v1/decisions", json=ORDER, headers=h).json()
    assert d["profile"] == "economy"
    assert cl.put("/v1/policy", json={"profile": "custom", "custom_weights": {"cost": 0}}, headers=h).status_code == 400
    assert c.store.one("SELECT 1 FROM audit_log WHERE action='policy.changed'")
    assert cl.put("/v1/policy", json={"profile": "economy"}, headers=login(cl, "analyst")).status_code == 403


def test_disruption_endpoint_reoptimizes(api):
    cl, _ = api
    h = login(cl, "ops")
    r = cl.post("/v1/orders", json=ORDER, headers={**h, "Idempotency-Key": "disrupt-key-01"}).json()
    carrier = r["shipment"]["carrier_id"]
    out = cl.post("/v1/ops/disrupt", json={"carrier_id": carrier, "kind": "capacity"}, headers=h).json()
    assert len(out["reoptimized"]) == 1
    assert cl.post("/v1/ops/disrupt", json={"carrier_id": carrier, "kind": "bogus"}, headers=h).status_code == 422
    assert cl.post("/v1/ops/disrupt", json={"carrier_id": "NOPE", "kind": "outage"}, headers=h).status_code == 404


def test_batch_endpoint(api):
    cl, _ = api
    h = login(cl, "ops")
    orders = [{"order_id": f"B{i}", "customer_state": "RJ", "weight_kg": 2, "sla_days": 8, "allowed_origins": ["SP-1"]} for i in range(12)]
    r = cl.post("/v1/batch/optimize", json={"orders": orders, "carrier_capacity": {"RAPIDOSUL": 3, "CENTRALEXPRESS": 3, "POSTALBR": 3, "TRANSNORDESTE": 3, "MEGACARGO": 3, "ECOFREIGHT": 3}}, headers=h)
    assert r.status_code == 200 and r.json()["carrier_usage"] and all(v <= 3 for v in r.json()["carrier_usage"].values())


def test_rate_limit(predictor_base):
    c = make_container(predictor_base, rate_limit_per_min=5)
    cl = TestClient(create_app(c))
    codes = [cl.get("/v1/shipments").status_code for _ in range(9)]
    assert 429 in codes and codes[0] == 401
    r = [cl.get("/v1/shipments") for _ in range(2)][-1]
    assert r.status_code == 429 and "retry-after" in r.headers


def test_security_headers_and_ui(api):
    cl, _ = api
    r = cl.get("/")
    assert r.status_code == 200 and "Olist Flow" in r.text
    assert r.headers["x-content-type-options"] == "nosniff" and "default-src 'self'" in r.headers["content-security-policy"]
    assert "x-request-id" in r.headers
    assert cl.get("/static/app.js").status_code == 200
