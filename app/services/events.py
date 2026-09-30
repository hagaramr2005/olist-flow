"""Event-driven backbone. In-process by default; the Transport seam lets Kafka/RabbitMQ replace it
without touching producers or consumers (publish -> persist -> fan-out to subscribers)."""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Callable, Protocol

from app.core.logging import get_logger
from app.core.metrics import metrics
from app.services.store import Store

log = get_logger("events")
Handler = Callable[[dict], None]


class Transport(Protocol):
    def publish(self, topic: str, event: dict) -> None: ...


class EventBus:
    def __init__(self, store: Store, transport: Transport | None = None, ring_size: int = 500) -> None:
        self.store = store
        self.transport = transport                 # optional external broker (Kafka / RabbitMQ adapter)
        self._subs: dict[str, list[Handler]] = {}
        self._ring: deque[dict] = deque(maxlen=ring_size)
        self._lock = threading.Lock()

    def subscribe(self, event_type: str, handler: Handler) -> None:
        self._subs.setdefault(event_type, []).append(handler)

    def publish(self, event_type: str, shipment_id: int | None, day: float | None, payload: dict | None = None) -> dict:
        ev = {"type": event_type, "shipment_id": shipment_id, "day": day, "payload": payload or {}, "ts": round(time.time(), 3)}
        ev["id"] = self.store.execute("INSERT INTO events(shipment_id,type,day,payload,ts) VALUES(?,?,?,?,?)",
                                      (shipment_id, event_type, day, json.dumps(ev["payload"], default=str), ev["ts"]))
        with self._lock:
            self._ring.append(ev)
        metrics.inc("events", type=event_type)
        for h in list(self._subs.get(event_type, [])) + list(self._subs.get("*", [])):
            try:
                h(ev)
            except Exception:                                  # a bad consumer must never break the producer
                log.exception("event handler failed", extra={"ctx": {"event": event_type}})
        if self.transport:
            try:
                self.transport.publish(event_type, ev)
            except Exception:
                log.exception("transport publish failed")
        return ev

    def recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return list(self._ring)[-limit:][::-1]
