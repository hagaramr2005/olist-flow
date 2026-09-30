"""Fulfilment network: sellers/warehouses that can supply an order, with live capacity."""
from __future__ import annotations

import hashlib
import threading

from app.core.models import OrderIn, Origin

DEFAULT_NODES = [
    ("SP-1", "Sao Paulo Hub", "SP", 900), ("SP-2", "Campinas FC", "SP", 500),
    ("RJ-1", "Rio Partner FC", "RJ", 350), ("MG-1", "Minas FC", "MG", 300),
    ("PR-1", "Curitiba FC", "PR", 250), ("SC-1", "Santa Catarina FC", "SC", 200),
    ("DF-1", "Brasilia FC", "DF", 120), ("BA-1", "Salvador FC", "BA", 100),
]


class FulfillmentNetwork:
    def __init__(self, nodes=DEFAULT_NODES) -> None:
        self._nodes = {n[0]: Origin(node_id=n[0], name=n[1], state=n[2], capacity_per_day=n[3]) for n in nodes}
        self._lock = threading.Lock()

    def all(self) -> list[Origin]:
        return list(self._nodes.values())

    def get(self, node_id: str) -> Origin:
        return self._nodes[node_id]

    def nodes_for(self, order: OrderIn) -> list[Origin]:
        """Nodes that hold stock for this order. The seller's own node always does; a deterministic
        subset of other nodes holds replicated stock (multi-node inventory)."""
        if order.allowed_origins:
            pinned = [self._nodes[i] for i in order.allowed_origins if i in self._nodes and self._nodes[i].stock_ok]
            if pinned:
                return pinned
        stocked: list[Origin] = []
        for n in self._nodes.values():
            if not n.stock_ok:
                continue
            if order.seller_hint and n.state == order.seller_hint:
                stocked.append(n)
                continue
            h = int(hashlib.md5(f"{order.category}:{n.node_id}".encode()).hexdigest(), 16) % 100
            if h < 45:
                stocked.append(n)
        if not stocked:                                   # never return an empty network
            stocked = [n for n in self._nodes.values() if n.stock_ok][:1]
        return stocked

    def reserve(self, node_id: str) -> bool:
        with self._lock:
            n = self._nodes[node_id]
            if n.load >= n.capacity_per_day:
                return False
            n.load += 1
            return True

    def release(self, node_id: str) -> None:
        with self._lock:
            n = self._nodes[node_id]
            n.load = max(0, n.load - 1)

    def set_stock(self, node_id: str, ok: bool) -> None:
        self._nodes[node_id].stock_ok = ok
