"""FlowEngine: the control loop  Predict -> Decide -> Execute -> Observe -> Learn."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Optional

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.metrics import metrics
from app.core.models import OrderIn, Option, ServiceLevel, Severity, ShipmentStatus
from app.integration.base import CapacityExceeded, CarrierError, CarrierUnavailable, CoverageError
from app.integration.registry import CarrierRegistry
from app.intelligence import exceptions as exc
from app.intelligence import optimizer as opt
from app.intelligence.coverage import CoverageEngine
from app.intelligence.explain import explain
from app.intelligence.network import FulfillmentNetwork
from app.intelligence.prediction import PredictionEngine
from app.services.events import EventBus
from app.services.feedback import FeedbackLoop
from app.services.store import Store

log = get_logger("engine")
TOP_N = 8


class ConflictError(Exception):
    """Order already has an active shipment (double-booking guard)."""


class NotFound(Exception):
    pass


class FlowEngine:
    def __init__(self, settings: Settings, store: Store, registry: CarrierRegistry, network: FulfillmentNetwork,
                 predictor: PredictionEngine, bus: EventBus) -> None:
        self.s, self.store, self.registry, self.network, self.predictor, self.bus = settings, store, registry, network, predictor, bus
        self.coverage = CoverageEngine(registry, network, predictor)
        self.feedback = FeedbackLoop(store, predictor)
        self._order_locks = [threading.Lock() for _ in range(64)]      # striped per-order locks

    # ------------------------------------------------------------------ clock
    @property
    def sim_day(self) -> float:
        return float(self.store.get_kv("sim_day", "0"))

    def _set_day(self, d: float) -> None:
        self.store.set_kv("sim_day", repr(round(d, 4)))

    # ---------------------------------------------------------------- decide
    def decide(self, order: OrderIn, profile: str = "balanced", custom_weights: Optional[dict] = None,
               max_late_risk: Optional[float] = None, actor: str = "system", kind: str = "initial",
               exclude_carriers: Optional[set[str]] = None, persist: bool = True) -> dict:
        with metrics.timer("decide_total"):
            cov = self.coverage.evaluate(order)
            decision = opt.optimize(cov.options, profile, custom_weights, order.priority, max_late_risk, exclude_carriers)
            expl = explain(decision, cov, order.sla_days)
        top = decision.ranked[:TOP_N]
        result = {
            "order_id": order.order_id, "kind": kind, "profile": decision.profile, "weights": decision.weights,
            "recommended": decision.recommended.model_dump(mode="json"),
            "alternatives": [o.model_dump(mode="json") for o in top],
            "pareto_ids": sorted(decision.pareto_ids), "funnel": cov.funnel, "explanation": expl,
            "latency_ms": decision.latency_ms,
        }
        if persist:
            self.store.execute("INSERT OR REPLACE INTO orders(order_id,payload,created_at) VALUES(?,?,?)",
                               (order.order_id, order.model_dump_json(), time.time()))
            result["decision_id"] = self.store.execute(
                "INSERT INTO decisions(order_id,created_at,kind,profile,weights,recommended,ranked,explanation,funnel,latency_ms)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (order.order_id, time.time(), kind, decision.profile, json.dumps(decision.weights),
                 json.dumps(result["recommended"]), json.dumps(result["alternatives"]),
                 json.dumps(expl), json.dumps(cov.funnel), decision.latency_ms))
            b = decision.recommended
            self.store.audit(actor, f"decision.{kind}", "order", order.order_id, {
                "decision_id": result["decision_id"], "policy": decision.profile, "weights": decision.weights,
                "selected": b.option_id, "cost": b.cost, "predicted_days": b.predicted_days,
                "late_risk": b.late_risk, "reliability": b.reliability, "feasible_plans": cov.funnel["feasible_plans"]})
            metrics.inc("decisions", kind=kind, profile=decision.profile)
        return result

    # ------------------------------------------------------------------ book
    def _load_decision(self, order_id: str, decision_id: Optional[int]) -> dict:
        row = (self.store.one("SELECT * FROM decisions WHERE decision_id=? AND order_id=?", (decision_id, order_id))
               if decision_id else
               self.store.one("SELECT * FROM decisions WHERE order_id=? ORDER BY decision_id DESC LIMIT 1", (order_id,)))
        if not row:
            raise NotFound(f"no decision for order {order_id}")
        return row

    def _try_book(self, order: OrderIn, options: list[Option], idem_key: str, max_attempts: int = 3) -> tuple[Option, dict, list[dict]]:
        """Walk the ranked plans until one books. Returns (option, booking, fallbacks_tried)."""
        tried: list[dict] = []
        for o in options:
            if len(tried) >= max_attempts:
                break
            carrier = self.registry.get(o.carrier_id)
            if not carrier.healthy:
                tried.append({"option": o.option_id, "error": "circuit open"})
                continue
            if not self.network.reserve(o.origin_id):
                tried.append({"option": o.option_id, "error": "origin at capacity"})
                continue
            try:
                b = carrier.book_shipment(o.origin_state, order, o.service, f"{idem_key}:{o.option_id}")
                return o, b.model_dump(mode="json"), tried
            except (CapacityExceeded, CarrierUnavailable, CoverageError, CarrierError) as e:
                self.network.release(o.origin_id)
                tried.append({"option": o.option_id, "error": f"{type(e).__name__}: {e}"})
                metrics.inc("booking_failures", carrier=o.carrier_id)
        raise CarrierError(f"all {len(tried)} candidate plans failed: {tried}")

    def book(self, order_id: str, idempotency_key: str, decision_id: Optional[int] = None, actor: str = "system") -> dict:
        prior = self.store.idem_get(idempotency_key, "book")
        if prior:
            prior["replayed"] = True
            return prior
        # Serialise attempts for the SAME order so concurrent double-submits never reach the carrier twice.
        # (Across processes, the unique index + compensating cancel below is the safety net.)
        with self._order_locks[hash(order_id) % len(self._order_locks)]:
            prior = self.store.idem_get(idempotency_key, "book")
            if prior:
                prior["replayed"] = True
                return prior
            return self._book_locked(order_id, idempotency_key, decision_id, actor)

    def _book_locked(self, order_id: str, idempotency_key: str, decision_id: Optional[int], actor: str) -> dict:
        order = OrderIn.model_validate_json(self._order_row(order_id)["payload"])
        active = self.store.one("SELECT shipment_id FROM shipments WHERE order_id=? AND active=1", (order_id,))
        if active:
            raise ConflictError(f"order {order_id} already has active shipment {active['shipment_id']}")
        dec = self._load_decision(order_id, decision_id)
        ranked = [Option.model_validate(o) for o in json.loads(dec["ranked"])]
        try:
            with metrics.timer("booking"):
                chosen, booking, tried = self._try_book(order, ranked, idempotency_key)
        except CarrierError as e:
            qid = self.store.execute("INSERT INTO pending_bookings(order_id,payload,attempts,status,last_error,created_at)"
                                     " VALUES(?,?,?,?,?,?)", (order_id, json.dumps({"key": idempotency_key, "decision_id": dec["decision_id"]}),
                                                              1, "queued", str(e)[:300], time.time()))
            self.store.audit(actor, "booking.queued", "order", order_id, {"queue_id": qid, "error": str(e)[:300]})
            self.bus.publish("BookingQueued", None, self.sim_day, {"order_id": order_id, "queue_id": qid})
            return {"status": "queued", "queue_id": qid, "order_id": order_id, "reason": str(e)[:300]}
        try:
            sid = self._insert_shipment(order, chosen, booking, dec, idempotency_key)
        except sqlite3.IntegrityError:                      # another instance won the race: undo OUR carrier booking
            self.network.release(chosen.origin_id)
            self._cancel_or_queue(chosen.carrier_id, booking["tracking_code"])
            self.store.audit(actor, "booking.race_compensated", "order", order_id, {"cancelled": booking["tracking_code"]})
            ex = self.store.one("SELECT * FROM shipments WHERE order_id=? AND active=1", (order_id,))
            return {"status": "booked", "shipment_id": ex["shipment_id"], "tracking_code": ex["tracking_code"],
                    "replayed": True, "order_id": order_id}
        resp = {"status": "booked", "shipment_id": sid, "order_id": order_id, "tracking_code": booking["tracking_code"],
                "carrier_id": chosen.carrier_id, "service": chosen.service.value, "origin_id": chosen.origin_id,
                "cost": chosen.cost, "promised_days": chosen.promised_days, "fallbacks_tried": tried, "replayed": False,
                "label": self._label(chosen.carrier_id, booking["tracking_code"])}
        self.store.idem_put(idempotency_key, "book", resp)
        self.store.audit(actor, "shipment.booked", "shipment", sid, {"order_id": order_id, "option": chosen.option_id,
                                                                     "tracking_code": booking["tracking_code"], "fallbacks": tried})
        self.bus.publish(ShipmentStatus.CREATED.value, sid, self.sim_day, {"carrier": chosen.carrier_id, "tracking_code": booking["tracking_code"]})
        return resp

    def _cancel_or_queue(self, carrier_id: str, tracking_code: str) -> None:
        try:
            self.registry.get(carrier_id).cancel_shipment(tracking_code)
        except CarrierError:                                   # carrier down: retry later from the queue
            self.store.execute("INSERT INTO pending_cancels(carrier_id,tracking_code,created_at) VALUES(?,?,?)",
                               (carrier_id, tracking_code, time.time()))

    def _label(self, carrier_id: str, tracking_code: str) -> Optional[str]:
        """Best-effort: a label failure must never undo or hide a shipment that is already booked."""
        try:
            return self.registry.get(carrier_id).generate_label(tracking_code)
        except CarrierError:
            log.warning("label generation failed; retry later", extra={"ctx": {"carrier": carrier_id, "tracking": tracking_code}})
            return None

    def _order_row(self, order_id: str) -> dict:
        r = self.store.one("SELECT * FROM orders WHERE order_id=?", (order_id,))
        if not r:
            raise NotFound(f"unknown order {order_id}")
        return r

    def _insert_shipment(self, order: OrderIn, o: Option, booking: dict, dec: dict, key: str) -> int:
        now = time.time()
        return self.store.execute(
            "INSERT INTO shipments(order_id,decision_id,seller_state,dest_state,carrier_id,origin_id,service,tracking_code,cost,"
            "promised_days,predicted_days,late_risk,status,booked_day,last_event_day,order_payload,policy,seller_id,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order.order_id, dec["decision_id"], o.origin_state, order.customer_state, o.carrier_id, o.origin_id, o.service.value,
             booking["tracking_code"], booking["cost"], booking["promised_days"], o.predicted_days, o.late_risk,
             ShipmentStatus.CREATED.value, self.sim_day, 0.0, order.model_dump_json(),
             json.dumps({"profile": dec["profile"], "weights": json.loads(dec["weights"])}), order.seller_id, now, now))

    # ------------------------------------------------------------- queue retry
    def process_queue(self, actor: str = "system") -> dict:
        """Retry queued bookings and pending carrier cancellations (resilience: Queue)."""
        done = failed = cancels = 0
        for q in self.store.all("SELECT * FROM pending_bookings WHERE status='queued' ORDER BY id"):
            p = json.loads(q["payload"])
            try:
                order = OrderIn.model_validate_json(self._order_row(q["order_id"])["payload"])
                fresh = self.decide(order, actor=actor, kind="retry")
                r = self.book(q["order_id"], p["key"] + f":retry{q['attempts']}", fresh["decision_id"], actor)
                if r["status"] == "booked":
                    self.store.execute("UPDATE pending_bookings SET status='done' WHERE id=?", (q["id"],))
                    done += 1
                    continue
            except (opt.NoFeasibleOption, ConflictError, NotFound) as e:
                self.store.execute("UPDATE pending_bookings SET status='failed', last_error=? WHERE id=?", (str(e)[:300], q["id"]))
                failed += 1
                continue
            self.store.execute("UPDATE pending_bookings SET attempts=attempts+1 WHERE id=?", (q["id"],))
        for c in self.store.all("SELECT * FROM pending_cancels WHERE status='queued'"):
            try:
                self.registry.get(c["carrier_id"]).cancel_shipment(c["tracking_code"])
                self.store.execute("UPDATE pending_cancels SET status='done' WHERE id=?", (c["id"],))
                cancels += 1
            except CarrierError:
                self.store.execute("UPDATE pending_cancels SET attempts=attempts+1 WHERE id=?", (c["id"],))
        return {"bookings_done": done, "bookings_failed": failed, "cancellations_done": cancels}

    # ------------------------------------------------------- tracking / monitor
    def tick(self, days: float = 0.5, actor: str = "system") -> dict:
        """Advance the simulated clock, ingest tracking events, classify exceptions, act."""
        new_day = self.sim_day + days
        self._set_day(new_day)
        summary = {"sim_day": round(new_day, 2), "events": 0, "delivered": 0, "escalations": 0, "reoptimized": 0}
        for sh in self.store.all("SELECT * FROM shipments WHERE active=1"):
            self._monitor(sh, new_day, summary, actor)
        return summary

    def _monitor(self, sh: dict, now: float, summary: dict, actor: str) -> None:
        carrier = self.registry.get(sh["carrier_id"])
        rel = now - sh["booked_day"]
        tracking_ok = True
        events = []
        try:
            events = carrier.track_shipment(sh["tracking_code"], rel)
        except CarrierError:
            tracking_ok = False
            metrics.inc("tracking_failures", carrier=sh["carrier_id"])
        last_day = sh["last_event_day"]
        status = ShipmentStatus(sh["status"])
        for e in events:
            if e.day > last_day + 1e-9:
                self.bus.publish(e.status.value, sh["shipment_id"], sh["booked_day"] + e.day, {"hub": e.hub, "rel_day": e.day})
                summary["events"] += 1
                last_day = max(last_day, e.day)
                status = e.status
        active = 1
        delivered_day = None
        if status == ShipmentStatus.DELIVERED:
            active = 0
            delivered_day = sh["booked_day"] + last_day
            fb = self.feedback.record(sh, last_day)
            self.network.release(sh["origin_id"])
            self.store.audit(actor, "shipment.delivered", "shipment", sh["shipment_id"], {"actual_days": last_day, **fb})
            summary["delivered"] += 1
        snap = exc.ShipmentSnapshot(
            status=status, day_now=rel, last_event_day=last_day, promised_days=sh["promised_days"],
            predicted_days=sh["predicted_days"], late_risk=sh["late_risk"], handling_days=self._handling(sh["carrier_id"]),
            carrier_api_healthy=carrier.healthy and tracking_ok,
            carrier_capacity_ok=carrier.capacity_snapshot()["free_ratio"] > 0 or status != ShipmentStatus.CREATED)
        rep = exc.evaluate(snap)
        prev_sev = Severity(sh["severity"])
        if exc.RANK[rep.severity] > exc.RANK[prev_sev]:
            summary["escalations"] += 1
            self.bus.publish("ExceptionRaised", sh["shipment_id"], now, {
                "severity": rep.severity.value, "findings": [f.code for f in rep.findings], "actions": rep.actions,
                "revised_eta": rep.revised_eta_days})
            for a in rep.actions:
                self.bus.publish("ActionTriggered", sh["shipment_id"], now, {"action": a, "order_id": sh["order_id"]})
        self.store.execute("UPDATE shipments SET status=?, last_event_day=?, severity=?, revised_eta=?, active=?, delivered_day=?,"
                           " updated_at=? WHERE shipment_id=?", (status.value, last_day, rep.severity.value,
                                                                rep.revised_eta_days, active, delivered_day, time.time(), sh["shipment_id"]))
        if rep.can_reoptimize and active:
            try:
                r = self.reoptimize(sh["shipment_id"], reason=",".join(f.code for f in rep.findings), actor=actor)
                if r.get("changed"):
                    summary["reoptimized"] += 1
            except (opt.NoFeasibleOption, CarrierError):
                log.warning("reoptimization failed", extra={"ctx": {"shipment": sh["shipment_id"]}})

    def _handling(self, carrier_id: str) -> float:
        from app.integration.world import PROFILE_BY_ID
        return PROFILE_BY_ID[carrier_id].handling_days if carrier_id in PROFILE_BY_ID else 1.5

    # ------------------------------------------------------- re-optimization
    def reoptimize(self, shipment_id: int, reason: str = "manual", actor: str = "system") -> dict:
        sh = self.store.one("SELECT * FROM shipments WHERE shipment_id=?", (shipment_id,))
        if not sh:
            raise NotFound(f"shipment {shipment_id}")
        if not sh["active"]:
            return {"changed": False, "reason": "shipment closed"}
        if sh["status"] != ShipmentStatus.CREATED.value:
            return {"changed": False, "reason": f"already {sh['status']}; cannot re-plan after pickup - intervention recommended"}
        order = OrderIn.model_validate_json(sh["order_payload"])
        policy = json.loads(sh["policy"])
        elapsed = self.sim_day - sh["booked_day"]
        if order.sla_days is not None:
            order = order.model_copy(update={"sla_days": max(1.0, order.sla_days - elapsed)})
        with metrics.timer("reoptimize"):
            try:
                fresh = self.decide(order, policy["profile"] if policy["profile"] != "custom" else "balanced",
                                    policy["weights"] if policy["profile"] == "custom" else None,
                                    actor=actor, kind="reoptimization", persist=False)
            except opt.NoFeasibleOption:
                order = order.model_copy(update={"sla_days": None})          # relax SLA rather than strand the parcel
                fresh = self.decide(order, policy["profile"] if policy["profile"] != "custom" else "balanced",
                                    None, actor=actor, kind="reoptimization", persist=False)
                fresh["explanation"]["reasons"].append("SLA relaxed: no plan could still meet it")
        best = Option.model_validate(fresh["recommended"])
        if (best.carrier_id, best.service.value, best.origin_id) == (sh["carrier_id"], sh["service"], sh["origin_id"]):
            return {"changed": False, "reason": "current plan is still optimal"}
        # Persist the new decision, then book the replacement *before* releasing the old plan.
        self.store.execute("INSERT OR REPLACE INTO orders(order_id,payload,created_at) VALUES(?,?,?)",
                           (sh["order_id"], sh["order_payload"], time.time()))
        ranked = [Option.model_validate(o) for o in fresh["alternatives"]]
        new_dec = self.store.execute(
            "INSERT INTO decisions(order_id,created_at,kind,profile,weights,recommended,ranked,explanation,funnel,latency_ms)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (sh["order_id"], time.time(), "reoptimization", fresh["profile"], json.dumps(fresh["weights"]),
             json.dumps(fresh["recommended"]), json.dumps(fresh["alternatives"]), json.dumps(fresh["explanation"]),
             json.dumps(fresh["funnel"]), fresh["latency_ms"]))
        n = sh["replan_count"] + 1
        chosen, booking, tried = self._try_book(order, ranked, f"reopt:{shipment_id}:{n}")
        old = {"carrier": sh["carrier_id"], "origin": sh["origin_id"], "service": sh["service"], "cost": sh["cost"],
               "tracking_code": sh["tracking_code"]}
        self._cancel_or_queue(sh["carrier_id"], sh["tracking_code"])           # old carrier down -> queued
        self.network.release(sh["origin_id"])
        self.store.execute(
            "UPDATE shipments SET decision_id=?, carrier_id=?, origin_id=?, seller_state=?, service=?, tracking_code=?, cost=?,"
            " promised_days=?, predicted_days=?, late_risk=?, status=?, booked_day=?, last_event_day=0, severity='normal',"
            " revised_eta=NULL, replan_count=?, updated_at=? WHERE shipment_id=?",
            (new_dec, chosen.carrier_id, chosen.origin_id, chosen.origin_state, chosen.service.value, booking["tracking_code"],
             booking["cost"], booking["promised_days"], chosen.predicted_days, chosen.late_risk, ShipmentStatus.CREATED.value,
             self.sim_day, n, time.time(), shipment_id))
        change = {"changed": True, "shipment_id": shipment_id, "order_id": sh["order_id"], "reason": reason, "from": old,
                  "to": {"carrier": chosen.carrier_id, "origin": chosen.origin_id, "service": chosen.service.value,
                         "cost": chosen.cost, "tracking_code": booking["tracking_code"]},
                  "explanation": fresh["explanation"], "fallbacks_tried": tried}
        self.store.audit(actor, "shipment.reoptimized", "shipment", shipment_id,
                         {"reason": reason, "from": old, "to": change["to"], "decision_id": new_dec})
        self.bus.publish("PlanReoptimized", shipment_id, self.sim_day, {"from": old["carrier"], "to": chosen.carrier_id, "reason": reason})
        metrics.inc("reoptimizations")
        return change

    # ------------------------------------------------------------ disruptions
    def disrupt(self, carrier_id: str, kind: str, actor: str = "system") -> dict:
        adapter = self.registry.raw(carrier_id)
        if not hasattr(adapter, "outage"):
            raise ValueError("carrier does not support fault injection")
        if kind == "outage":
            adapter.outage = True
        elif kind == "capacity":
            adapter.capacity_cut = True
        elif kind == "latency":
            adapter.latency_s = self.s.carrier_timeout_s * 2
        elif kind == "stall":
            for sh in self.store.all("SELECT tracking_code FROM shipments WHERE active=1 AND carrier_id=?", (carrier_id,)):
                adapter.stalled.add(sh["tracking_code"])
        elif kind == "clear":
            adapter.outage = adapter.capacity_cut = False
            adapter.latency_s = 0.0
            adapter.stalled.clear()
            self.registry.get(carrier_id).breaker.on_success()
        else:
            raise ValueError("kind must be outage|capacity|latency|stall|clear")
        self.store.audit(actor, f"disruption.{kind}", "carrier", carrier_id)
        self.bus.publish("DisruptionInjected", None, self.sim_day, {"carrier": carrier_id, "kind": kind})
        changes = []
        if kind in ("outage", "capacity", "latency"):
            # Probe once so the breaker/health reflect reality, then re-plan every parcel still with the seller.
            for sh in self.store.all("SELECT shipment_id FROM shipments WHERE active=1 AND carrier_id=? AND status=?",
                                     (carrier_id, ShipmentStatus.CREATED.value)):
                try:
                    r = self.reoptimize(sh["shipment_id"], reason=f"{kind}:{carrier_id}", actor=actor)
                    if r.get("changed"):
                        changes.append(r)
                except (opt.NoFeasibleOption, CarrierError) as e:
                    changes.append({"changed": False, "shipment_id": sh["shipment_id"], "error": str(e)[:200]})
        return {"carrier_id": carrier_id, "kind": kind, "reoptimized": [c for c in changes if c.get("changed")],
                "failed": [c for c in changes if not c.get("changed")]}

    # -------------------------------------------------------- batch adoption
    def adopt_plan(self, order: OrderIn, option: Option, profile: str, weights: dict, kind: str = "batch",
                   actor: str = "system") -> int:
        """Persist an externally-optimised plan (e.g. from the batch MILP) as a decision so it can be booked and audited."""
        self.store.execute("INSERT OR REPLACE INTO orders(order_id,payload,created_at) VALUES(?,?,?)",
                           (order.order_id, order.model_dump_json(), time.time()))
        rec = option.model_dump(mode="json")
        did = self.store.execute(
            "INSERT INTO decisions(order_id,created_at,kind,profile,weights,recommended,ranked,explanation,funnel,latency_ms)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (order.order_id, time.time(), kind, profile, json.dumps(weights), json.dumps(rec), json.dumps([rec]),
             json.dumps({"headline": f"Batch-optimal plan: {option.option_id}", "reasons": ["Assigned by MILP under shared capacity constraints"]}),
             json.dumps({}), 0.0))
        self.store.audit(actor, f"decision.{kind}", "order", order.order_id,
                         {"decision_id": did, "policy": profile, "selected": option.option_id, "cost": option.cost})
        return did
