"""Coverage Engine: which (origin x carrier x service) plans are even feasible?

Infeasible options are removed *before* optimisation and their reasons are kept so the
decision can be explained ("7 carriers connected -> 4 serve route -> 3 meet SLA").
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from app.core.geo import distance_km
from app.core.logging import get_logger
from app.core.models import OrderIn, Option, Origin, ServiceLevel
from app.integration.base import CarrierError, CoverageError
from app.integration.registry import CarrierRegistry, ResilientCarrier
from app.intelligence.network import FulfillmentNetwork
from app.intelligence.prediction import PredictionEngine

log = get_logger("coverage")
_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="coverage")


@dataclass
class Rejection:
    origin_id: str
    carrier_id: str
    service: str | None
    stage: str          # coverage | capacity | availability | sla
    reason: str


@dataclass
class CoverageOutcome:
    options: list[Option] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    funnel: dict = field(default_factory=dict)


class CoverageEngine:
    def __init__(self, registry: CarrierRegistry, network: FulfillmentNetwork, predictor: PredictionEngine) -> None:
        self.registry, self.network, self.predictor = registry, network, predictor

    def _probe(self, node: Origin, carrier: ResilientCarrier, order: OrderIn):
        """Network-facing half: coverage + capacity + quotes. Returns (quotes, rejections).
        Prediction happens afterwards in ONE vectorised call for all candidates."""
        quotes, rej = [], []
        if not carrier.healthy:
            return quotes, [Rejection(node.node_id, carrier.carrier_id, None, "availability", "circuit open (carrier API down)")]
        try:
            cov = carrier.check_coverage(node.state, order)
        except CarrierError as e:
            return quotes, [Rejection(node.node_id, carrier.carrier_id, None, "availability", f"carrier unavailable: {e}")]
        if not cov.covered:
            return quotes, [Rejection(node.node_id, carrier.carrier_id, None, "coverage", cov.reason)]
        snap = carrier.capacity_snapshot()
        if snap["free_ratio"] <= 0:
            return quotes, [Rejection(node.node_id, carrier.carrier_id, None, "capacity", "carrier at capacity")]
        if node.free_capacity <= 0:
            return quotes, [Rejection(node.node_id, carrier.carrier_id, None, "capacity", f"{node.node_id} at capacity")]
        for svc in ServiceLevel:
            try:
                q = carrier.get_quote(node.state, order, svc)
            except CoverageError:
                continue                                   # service not offered: not an interesting rejection
            except CarrierError as e:
                rej.append(Rejection(node.node_id, carrier.carrier_id, svc.value, "availability", f"quote failed: {e}"))
                continue
            quotes.append((node, carrier, svc, q, snap))
        return quotes, rej

    def evaluate(self, order: OrderIn) -> CoverageOutcome:
        month = order.ordered_month or 6
        nodes = self.network.nodes_for(order)
        carriers = self.registry.all()
        futures = [_pool.submit(self._probe, n, c, order) for n in nodes for c in carriers]
        out = CoverageOutcome()
        quotes = []
        for f in futures:
            qs, r = f.result()
            quotes.extend(qs)
            out.rejections.extend(r)
        preds = self.predictor.predict_many([
            (c.carrier_id, n.state, order.customer_state, svc.value, month, order.weight_kg, q.promised_days, order.sla_days)
            for n, c, svc, q, _ in quotes])
        for (node, carrier, svc, q, snap), pred in zip(quotes, preds):
            feasible, reasons = True, []
            if order.sla_days is not None and q.promised_days > order.sla_days:
                feasible = False
                reasons.append(f"promise {q.promised_days:.0f}d exceeds SLA {order.sla_days:.0f}d")
                out.rejections.append(Rejection(node.node_id, carrier.carrier_id, svc.value, "sla", reasons[-1]))
            out.options.append(Option(
                option_id=f"{node.node_id}|{carrier.carrier_id}|{svc.value}", origin_id=node.node_id,
                origin_state=node.state, carrier_id=carrier.carrier_id, service=svc, cost=q.cost,
                promised_days=q.promised_days, predicted_days=pred.predicted_days,
                # late_risk = risk of missing what the customer needs (SLA if given, else the carrier promise)
                late_risk=pred.sla_risk if pred.sla_risk is not None else pred.late_risk,
                reliability=pred.reliability, distance_km=round(distance_km(node.state, order.customer_state), 1),
                capacity_free_ratio=snap["free_ratio"],
                origin_capacity_free_ratio=round(node.free_capacity / node.capacity_per_day, 3),
                feasible=feasible, reasons=reasons))
        # Funnel = the story told in the demo (distinct carriers surviving each gate).
        all_c = {c.carrier_id for c in carriers}
        stage_reject = lambda st: {r.carrier_id for r in out.rejections if r.stage == st}
        serving = {o.carrier_id for o in out.options}
        meeting_sla = {o.carrier_id for o in out.options if o.feasible}
        out.funnel = {
            "carriers_connected": len(all_c),
            "origins_with_stock": len(nodes),
            "carriers_serving_route": len(serving),
            "carriers_meeting_sla": len(meeting_sla),
            "feasible_plans": sum(1 for o in out.options if o.feasible),
            "rejected_plans": len(out.rejections),
            "carriers_unavailable": len(stage_reject("availability")),
        }
        return out
