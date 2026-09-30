"""HTTP layer: FastAPI app factory, middleware, routes."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.api import schemas as sc
from app.api.security import (PERMISSIONS, Principal, RateLimiter, current_principal, issue_token, require,
                              verify_password)
from app.core.logging import correlation_id, get_logger
from app.core.metrics import metrics
from app.core.models import OrderIn
from app.data import calibration, evidence
from app.integration.base import CarrierError
from app.intelligence import optimizer as opt
from app.intelligence.optimizer import PROFILES
from app.services import analytics, seed, twin
from app.services.container import Container, build_container
from app.services.engine import ConflictError, NotFound

log = get_logger("api")
UI_DIR = Path(__file__).resolve().parent.parent / "ui"

PRESETS = {
    "cost_priority":        ("Cost priority (Economy mode)", dict(profile="economy")),
    "reliability_priority": ("Reliability priority", dict(profile="reliability")),
    "express_priority":     ("Express priority", dict(profile="express")),
    "carrier_outage":       ("Top carrier RapidoSul unavailable", dict(disabled_carriers=["RAPIDOSUL"])),
    "double_volume":        ("2x order volume", dict(volume_multiplier=2.0)),
    "peak_season":          ("Black-Friday peak (Nov, 2.5x volume, Peak Season mode)",
                             dict(month=11, volume_multiplier=2.5, profile="peak_season")),
    "rio_partner":          ("New fulfilment partner in Rio de Janeiro",
                             dict(extra_nodes=[{"node_id": "RJ-2", "name": "Rio Partner 2", "state": "RJ", "capacity": 450}])),
    "disruption_2018":      ("Feb-Mar 2018 disruption replay (real lateness spike, Reliability mode)",
                             dict(month=3, profile="reliability")),
    "cheapest_only":        ("Naive policy: always the cheapest carrier", dict(strategy="cheapest")),
}


def create_app(container: Optional[Container] = None) -> FastAPI:
    app = FastAPI(title="Olist Flow", version="1.0.0",
                  description="Intelligent Logistics Optimization & Control Tower")
    c = container or build_container()
    app.state.c = c
    app.state.limiter = RateLimiter(c.settings.rate_limit_per_min)
    app.state.login_limiter = RateLimiter(10)
    app.state.twin_cache = OrderedDict()

    # ------------------------------------------------------------- middleware
    @app.middleware("http")
    async def base_middleware(request: Request, call_next):
        cid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        correlation_id.set(cid)
        ip = request.client.host if request.client else "?"
        auth = request.headers.get("authorization", "")
        key = f"{ip}:{hashlib.sha1(auth.encode()).hexdigest()[:10]}" if auth else ip
        limiter = app.state.login_limiter if request.url.path == "/auth/login" else app.state.limiter
        if request.url.path.startswith(("/v1", "/auth")):
            ok, retry = limiter.allow(key)
            if not ok:
                metrics.inc("rate_limited")
                return JSONResponse({"detail": "rate limit exceeded"}, 429, headers={"Retry-After": str(int(retry) + 1)})
        t0 = time.perf_counter()
        response = await call_next(request)
        ms = (time.perf_counter() - t0) * 1000
        route = request.scope.get("route")
        path = route.path if route else "unmatched"
        metrics.observe("api_latency", ms, path=path, method=request.method)
        metrics.inc("api_requests", path=path, status=str(response.status_code))
        response.headers["X-Request-ID"] = cid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:"
        return response

    # ------------------------------------------------------ error translation
    @app.exception_handler(opt.NoFeasibleOption)
    async def _nf(_, e):
        return JSONResponse({"detail": str(e), "code": "no_feasible_option"}, 422)

    @app.exception_handler(ConflictError)
    async def _cf(_, e):
        return JSONResponse({"detail": str(e), "code": "conflict"}, 409)

    @app.exception_handler(NotFound)
    async def _nfd(_, e):
        return JSONResponse({"detail": str(e), "code": "not_found"}, 404)

    @app.exception_handler(CarrierError)
    async def _ce(_, e):
        return JSONResponse({"detail": str(e), "code": "carrier_error"}, 502)

    @app.exception_handler(ValueError)
    async def _ve(_, e):
        return JSONResponse({"detail": str(e), "code": "invalid_request"}, 400)

    # ------------------------------------------------------------------ helpers
    def policy() -> dict:
        raw = c.store.get_kv("policy", "")
        return json.loads(raw) if raw else {"profile": "balanced", "custom_weights": None}

    def resolve(profile, weights):
        if profile is None and not weights:
            p = policy()
            return p["profile"], p["custom_weights"]
        return profile or "balanced", weights

    def scoped_order(order: OrderIn, p: Principal) -> OrderIn:
        if p.role == "seller":
            if order.seller_id and order.seller_id != p.seller_id:
                raise HTTPException(403, "sellers may only create orders for their own seller_id")
            return order.model_copy(update={"seller_id": p.seller_id})
        return order

    def can_see(shipment: dict, p: Principal) -> bool:
        return p.role != "seller" or shipment.get("seller_id") == p.seller_id

    # --------------------------------------------------------------- health
    @app.get("/health/live", tags=["health"])
    def live():
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    def ready():
        checks = {"database": False, "models": c.predictor.eta_model is not None, "carriers": len(c.registry),
                  "calibration": calibration.token()}
        try:
            c.store.one("SELECT 1")
            checks["database"] = True
        except Exception:
            pass
        healthy = sum(1 for x in c.registry.all() if x.healthy)
        ok = checks["database"] and checks["models"] and healthy > 0
        return JSONResponse({"status": "ready" if ok else "degraded", **checks, "carriers_healthy": healthy}, 200 if ok else 503)

    @app.get("/metrics", response_class=PlainTextResponse, tags=["health"])
    def prom(_: Principal = Depends(require("metrics:read"))):
        return metrics.render_prometheus()

    # ----------------------------------------------------------------- auth
    @app.post("/auth/login", tags=["auth"])
    def login(body: sc.LoginIn):
        u = c.store.one("SELECT * FROM users WHERE username=?", (body.username,))
        # verify against a dummy hash when the user is unknown so timing does not reveal valid usernames
        stored = u["pw_hash"] if u else "scrypt$00$00"
        ok = verify_password(body.password, stored) and u is not None
        c.store.audit(body.username, "auth.login" if ok else "auth.login_failed", "user", body.username)
        if not ok:
            raise HTTPException(401, "invalid credentials")
        token, ttl = issue_token(c.settings, u)
        return {"access_token": token, "token_type": "bearer", "expires_in": ttl, "role": u["role"], "seller_id": u["seller_id"]}

    @app.get("/auth/me", tags=["auth"])
    def me(p: Principal = Depends(current_principal)):
        return {"username": p.username, "role": p.role, "seller_id": p.seller_id,
                "permissions": sorted(k for k, v in PERMISSIONS.items() if p.role in v)}

    # ------------------------------------------------------- policy / profiles
    @app.get("/v1/policy", tags=["policy"])
    def get_policy(_: Principal = Depends(require("policy:read"))):
        p = policy()
        active = opt.resolve_weights(p["profile"], p["custom_weights"])
        return {"active": p, "effective_weights": active, "profiles": PROFILES}

    @app.put("/v1/policy", tags=["policy"])
    def put_policy(body: sc.PolicyIn, p: Principal = Depends(require("policy:write"))):
        w = opt.resolve_weights(body.profile, body.custom_weights)       # validates
        c.store.set_kv("policy", json.dumps({"profile": body.profile if not body.custom_weights else "custom",
                                             "custom_weights": body.custom_weights}))
        c.store.audit(p.username, "policy.changed", "policy", "default", {"profile": body.profile, "weights": w})
        return {"active": policy(), "effective_weights": w}

    # ----------------------------------------------------- decisions & booking
    @app.post("/v1/decisions", tags=["decisions"])
    def decide(body: sc.DecisionIn, p: Principal = Depends(require("order:create"))):
        prof, w = resolve(body.profile, body.custom_weights)
        return c.engine.decide(scoped_order(body.order, p), prof, w, body.max_late_risk, actor=p.username)

    @app.get("/v1/decisions/{order_id}", tags=["decisions"])
    def decision_history(order_id: str, p: Principal = Depends(require("shipment:read"))):
        rows = c.store.all("SELECT decision_id,created_at,kind,profile,weights,recommended,explanation,funnel,latency_ms "
                           "FROM decisions WHERE order_id=? ORDER BY decision_id", (order_id,))
        if not rows:
            raise NotFound(f"no decisions for {order_id}")
        o = c.store.one("SELECT payload FROM orders WHERE order_id=?", (order_id,))
        if p.role == "seller" and json.loads(o["payload"]).get("seller_id") != p.seller_id:
            raise HTTPException(403, "not your order")
        return [{**r, "weights": json.loads(r["weights"]), "recommended": json.loads(r["recommended"]),
                 "explanation": json.loads(r["explanation"]), "funnel": json.loads(r["funnel"])} for r in rows]

    def _idem(key: Optional[str]) -> str:
        if not key or len(key) < 8 or len(key) > 128:
            raise HTTPException(400, "Idempotency-Key header (8-128 chars) is required for booking")
        return key

    @app.post("/v1/shipments", tags=["shipments"], status_code=201)
    def book(body: sc.BookIn, idempotency_key: Optional[str] = Header(default=None), p: Principal = Depends(require("order:create"))):
        key = _idem(idempotency_key)
        o = c.store.one("SELECT payload FROM orders WHERE order_id=?", (body.order_id,))
        if p.role == "seller" and o and json.loads(o["payload"]).get("seller_id") != p.seller_id:
            raise HTTPException(403, "not your order")
        r = c.engine.book(body.order_id, key, body.decision_id, actor=p.username)
        return JSONResponse(r, 202 if r["status"] == "queued" else (200 if r.get("replayed") else 201))

    @app.post("/v1/orders", tags=["decisions"], status_code=201)
    def place_order(body: sc.DecisionIn, idempotency_key: Optional[str] = Header(default=None),
                    p: Principal = Depends(require("order:create"))):
        """Decide + book in one call (the production path)."""
        key = _idem(idempotency_key)
        prior = c.store.idem_get(key, "order")
        if prior:
            return JSONResponse({**prior, "replayed": True}, 200)
        order = scoped_order(body.order, p)
        prof, w = resolve(body.profile, body.custom_weights)
        d = c.engine.decide(order, prof, w, body.max_late_risk, actor=p.username)
        b = c.engine.book(order.order_id, key, d["decision_id"], actor=p.username)
        resp = {"decision": d, "shipment": b}
        if b["status"] == "booked":
            c.store.idem_put(key, "order", resp)
        return JSONResponse(resp, 202 if b["status"] == "queued" else 201)

    @app.get("/v1/shipments", tags=["shipments"])
    def list_shipments(status: Optional[str] = None, severity: Optional[str] = None, active: Optional[bool] = None,
                       limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                       p: Principal = Depends(require("shipment:read"))):
        where, args = ["1=1"], []
        if status:
            where.append("status=?"); args.append(status)
        if severity:
            where.append("severity=?"); args.append(severity)
        if active is not None:
            where.append("active=?"); args.append(int(active))
        if p.role == "seller":
            where.append("seller_id=?"); args.append(p.seller_id)
        rows = c.store.all(f"SELECT shipment_id,order_id,seller_id,seller_state,dest_state,carrier_id,origin_id,service,tracking_code,"
                           f"cost,promised_days,predicted_days,late_risk,status,severity,revised_eta,replan_count,active,booked_day "
                           f"FROM shipments WHERE {' AND '.join(where)} ORDER BY shipment_id DESC LIMIT ? OFFSET ?", (*args, limit, offset))
        return {"items": rows, "limit": limit, "offset": offset, "sim_day": c.engine.sim_day}

    @app.get("/v1/shipments/{shipment_id}", tags=["shipments"])
    def get_shipment(shipment_id: int, p: Principal = Depends(require("shipment:read"))):
        s = c.store.one("SELECT * FROM shipments WHERE shipment_id=?", (shipment_id,))
        if not s or not can_see(s, p):
            raise NotFound(f"shipment {shipment_id}")
        s.pop("order_payload", None)
        events = c.store.all("SELECT type,day,payload,ts FROM events WHERE shipment_id=? ORDER BY id", (shipment_id,))
        for e in events:
            e["payload"] = json.loads(e["payload"] or "{}")
        dec = c.store.one("SELECT recommended,explanation FROM decisions WHERE decision_id=?", (s["decision_id"],))
        return {**s, "policy": json.loads(s["policy"]), "events": events,
                "explanation": json.loads(dec["explanation"]) if dec else None}

    @app.post("/v1/shipments/{shipment_id}/reoptimize", tags=["shipments"])
    def reoptimize(shipment_id: int, p: Principal = Depends(require("shipment:manage"))):
        return c.engine.reoptimize(shipment_id, reason="manual", actor=p.username)

    # ---------------------------------------------------------------- batch
    @app.post("/v1/batch/optimize", tags=["decisions"])
    def batch(body: sc.BatchIn, p: Principal = Depends(require("shipment:manage"))):
        prof, cw = resolve(body.profile, body.custom_weights)
        weights = opt.resolve_weights(prof, cw)
        per_order = {}
        for o in body.orders:
            per_order[o.order_id] = c.engine.coverage.evaluate(o).options
        car_caps = {x.carrier_id: int(x.capacity_snapshot()["capacity"] - c.registry.raw(x.carrier_id)._load) for x in c.registry.all()}
        if body.carrier_capacity:
            car_caps.update(body.carrier_capacity)
        org_caps = {n.node_id: n.free_capacity for n in c.network.all()}
        r = opt.optimize_batch(per_order, weights, car_caps, org_caps)
        out = {"solver": r.solver_status, "solve_ms": r.solve_ms, "objective": round(r.objective, 4),
               "unassigned": r.unassigned, "carrier_usage": r.carrier_usage, "origin_usage": r.origin_usage,
               "naive_capacity_violations": r.naive_capacity_violations,
               "note": "naive = each order picks its own best plan ignoring shared capacity",
               "assignments": {k: (v.model_dump(mode="json") if v else None) for k, v in r.assignments.items()}}
        if body.apply:
            booked = []
            for o in body.orders:
                a = r.assignments.get(o.order_id)
                if a:
                    did = c.engine.adopt_plan(scoped_order(o, p), a, prof, weights, actor=p.username)
                    booked.append(c.engine.book(o.order_id, f"batch-{o.order_id}-{did}", did, actor=p.username)["status"])
            out["applied"] = {s: booked.count(s) for s in set(booked)}
        return out

    # ---------------------------------------------------------- control tower
    @app.get("/v1/control-tower", tags=["control-tower"])
    def tower(_: Principal = Depends(require("tower:read"))):
        return analytics.control_tower(c)

    @app.get("/v1/control-tower/carriers", tags=["control-tower"])
    def tower_carriers(_: Principal = Depends(require("tower:read"))):
        return analytics.carriers(c)

    @app.get("/v1/control-tower/routes", tags=["control-tower"])
    def tower_routes(_: Principal = Depends(require("tower:read"))):
        return analytics.routes(c)

    @app.get("/v1/control-tower/geo", tags=["control-tower"])
    def tower_geo(_: Principal = Depends(require("tower:read"))):
        return analytics.geo(c)

    @app.get("/v1/control-tower/sellers", tags=["control-tower"])
    def tower_sellers(p: Principal = Depends(require("shipment:read"))):
        return analytics.sellers(c, p.seller_id if p.role == "seller" else None)

    @app.get("/v1/control-tower/events", tags=["control-tower"])
    def tower_events(limit: int = Query(40, ge=1, le=200), _: Principal = Depends(require("tower:read"))):
        return c.bus.recent(limit)

    @app.get("/v1/models", tags=["control-tower"])
    def models(_: Principal = Depends(require("tower:read"))):
        cal = calibration.get()
        return {"offline_evaluation": c.predictor.report, "online_feedback": c.engine.feedback.report(),
                "real_data_benchmark": (cal.d.get("benchmark") if cal else None),
                "data_calibration": {"active": cal is not None, "checksum": cal.token if cal else None}}

    @app.get("/v1/data/evidence", tags=["control-tower"])
    def data_evidence(_: Principal = Depends(require("tower:read"))):
        return evidence.build(calibration.get())

    @app.get("/v1/network", tags=["control-tower"])
    def network(_: Principal = Depends(require("tower:read"))):
        return {"nodes": [n.model_dump() | {"free": n.free_capacity} for n in c.network.all()],
                "carriers": [x.capacity_snapshot() | {"name": x.name} for x in c.registry.all()]}

    # --------------------------------------------------------------- simulation
    def _scenario(s: sc.ScenarioIn) -> twin.Scenario:
        return twin.Scenario(**s.model_dump())

    def _run_cached(s: twin.Scenario) -> dict:
        k = json.dumps(s.__dict__, sort_keys=True, default=str)
        cache = app.state.twin_cache
        if k in cache:
            cache.move_to_end(k)
            return cache[k]
        r = twin.run(s, c.predictor, c.settings)
        cache[k] = r
        if len(cache) > 64:
            cache.popitem(last=False)
        return r

    @app.get("/v1/simulate/presets", tags=["simulation"])
    def presets(_: Principal = Depends(require("simulate"))):
        return {k: v[0] for k, v in PRESETS.items()}

    @app.post("/v1/simulate/what-if", tags=["simulation"])
    def what_if(body: sc.WhatIfIn, p: Principal = Depends(require("simulate"))):
        alt = _scenario(body.scenario)
        base = _scenario(body.baseline) if body.baseline else twin.Scenario(name="baseline", n_orders=alt.n_orders, month=alt.month, sla_days=alt.sla_days)
        b, a = _run_cached(base), _run_cached(alt)
        return {"baseline": b, "scenario": a, "impact": twin.compare(b, a)}

    @app.post("/v1/simulate/preset/{name}", tags=["simulation"])
    def preset(name: str, n_orders: int = Query(500, ge=50, le=3000), p: Principal = Depends(require("simulate"))):
        if name not in PRESETS:
            raise HTTPException(404, f"unknown preset; options: {sorted(PRESETS)}")
        label, kw = PRESETS[name]
        base = twin.Scenario(name="baseline (Balanced)", n_orders=n_orders)
        alt = twin.Scenario(name=label, n_orders=n_orders, **kw)
        b, a = _run_cached(base), _run_cached(alt)
        return {"label": label, "baseline": b, "scenario": a, "impact": twin.compare(b, a)}

    # ---------------------------------------------------------------- ops
    @app.get("/v1/ops/carriers", tags=["ops"])
    def ops_carriers(_: Principal = Depends(require("shipment:read"))):
        return [x.capacity_snapshot() | {"name": x.name, "carrier_id": x.carrier_id} for x in c.registry.all()]

    @app.post("/v1/ops/tick", tags=["ops"])
    def tick(body: sc.TickIn, p: Principal = Depends(require("ops:control"))):
        return c.engine.tick(body.days, actor=p.username)

    @app.post("/v1/ops/disrupt", tags=["ops"])
    def disrupt(body: sc.DisruptIn, p: Principal = Depends(require("ops:control"))):
        try:
            return c.engine.disrupt(body.carrier_id, body.kind, actor=p.username)
        except KeyError:
            raise HTTPException(404, "unknown carrier")

    @app.post("/v1/ops/queue/process", tags=["ops"])
    def process_queue(p: Principal = Depends(require("ops:control"))):
        return c.engine.process_queue(actor=p.username)

    @app.post("/v1/ops/seed-demo", tags=["ops"])
    def seed_demo(body: sc.SeedIn, p: Principal = Depends(require("admin"))):
        try:
            return seed.seed_demo(c, body.orders, body.advance_days, body.seed, source=body.source)
        except ValueError as e:
            raise HTTPException(422, str(e))

    # ---------------------------------------------------------------- audit
    @app.get("/v1/audit", tags=["audit"])
    def audit(entity: Optional[str] = None, entity_id: Optional[str] = None, limit: int = Query(100, ge=1, le=1000),
              _: Principal = Depends(require("audit:read"))):
        where, args = ["1=1"], []
        if entity:
            where.append("entity=?"); args.append(entity)
        if entity_id:
            where.append("entity_id=?"); args.append(entity_id)
        rows = c.store.all(f"SELECT id,ts,actor,action,entity,entity_id,details,hash FROM audit_log WHERE {' AND '.join(where)} "
                           f"ORDER BY id DESC LIMIT ?", (*args, limit))
        for r in rows:
            r["details"] = json.loads(r["details"] or "{}")
        return rows

    @app.get("/v1/audit/verify", tags=["audit"])
    def audit_verify(_: Principal = Depends(require("audit:read"))):
        return c.store.verify_audit_chain()

    # ---------------------------------------------------------------- UI
    if UI_DIR.exists():
        app.mount("/static", StaticFiles(directory=UI_DIR), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(UI_DIR / "index.html")

    return app
