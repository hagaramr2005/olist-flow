"""SQLite persistence: orders, decisions, shipments, events, idempotency keys, users and a
tamper-evident (hash-chained) audit log. Swap for PostgreSQL by re-implementing this class."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS orders(order_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS decisions(
  decision_id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL, created_at REAL NOT NULL,
  kind TEXT NOT NULL, profile TEXT NOT NULL, weights TEXT NOT NULL, recommended TEXT NOT NULL,
  ranked TEXT NOT NULL, explanation TEXT NOT NULL, funnel TEXT NOT NULL, latency_ms REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_dec_order ON decisions(order_id);
CREATE TABLE IF NOT EXISTS shipments(
  shipment_id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL, decision_id INTEGER,
  seller_state TEXT, dest_state TEXT, carrier_id TEXT NOT NULL, origin_id TEXT NOT NULL, service TEXT NOT NULL,
  tracking_code TEXT NOT NULL, cost REAL NOT NULL, promised_days REAL NOT NULL, predicted_days REAL NOT NULL,
  late_risk REAL NOT NULL, status TEXT NOT NULL, severity TEXT NOT NULL DEFAULT 'normal',
  booked_day REAL NOT NULL, last_event_day REAL NOT NULL DEFAULT 0, revised_eta REAL, replan_count INTEGER DEFAULT 0,
  order_payload TEXT NOT NULL, policy TEXT NOT NULL, seller_id TEXT, active INTEGER NOT NULL DEFAULT 1,
  delivered_day REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_ship_active ON shipments(active);
CREATE INDEX IF NOT EXISTS ix_ship_order ON shipments(order_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ship_order_active ON shipments(order_id) WHERE active=1;
CREATE TABLE IF NOT EXISTS pending_cancels(
  id INTEGER PRIMARY KEY AUTOINCREMENT, carrier_id TEXT NOT NULL, tracking_code TEXT NOT NULL,
  status TEXT DEFAULT 'queued', attempts INTEGER DEFAULT 0, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id INTEGER, type TEXT NOT NULL, day REAL, payload TEXT, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS ix_evt_ship ON events(shipment_id);
CREATE TABLE IF NOT EXISTS idempotency(key TEXT NOT NULL, scope TEXT NOT NULL, response TEXT NOT NULL,
  created_at REAL NOT NULL, PRIMARY KEY(key, scope));
CREATE TABLE IF NOT EXISTS pending_bookings(
  id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL, payload TEXT NOT NULL, attempts INTEGER DEFAULT 0,
  status TEXT DEFAULT 'queued', last_error TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS feedback(
  id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id INTEGER, carrier_id TEXT, origin_state TEXT, dest_state TEXT,
  predicted_days REAL, actual_days REAL, promised_days REAL, predicted_risk REAL, was_late INTEGER, ts REAL);
CREATE TABLE IF NOT EXISTS users(username TEXT PRIMARY KEY, pw_hash TEXT NOT NULL, role TEXT NOT NULL, seller_id TEXT);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
  entity TEXT NOT NULL, entity_id TEXT, details TEXT, prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
"""


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            if path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)

    # -------------------------------------------------------------- primitives
    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            cur = self._db.execute(sql, tuple(params))
            return cur.lastrowid or 0

    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        with self._lock:
            r = self._db.execute(sql, tuple(params)).fetchone()
            return dict(r) if r else None

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, tuple(params)).fetchall()]

    def transaction(self):
        return _Tx(self)

    # ----------------------------------------------------------------- kv/clock
    def get_kv(self, k: str, default: str = "") -> str:
        r = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return r["v"] if r else default

    def set_kv(self, k: str, v: str) -> None:
        self.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

    # -------------------------------------------------------------- idempotency
    def idem_get(self, key: str, scope: str) -> Optional[dict]:
        r = self.one("SELECT response FROM idempotency WHERE key=? AND scope=?", (key, scope))
        return json.loads(r["response"]) if r else None

    def idem_put(self, key: str, scope: str, response: dict) -> bool:
        """Returns False if another request stored this key first (lost the race)."""
        try:
            self.execute("INSERT INTO idempotency(key,scope,response,created_at) VALUES(?,?,?,?)",
                         (key, scope, json.dumps(response, default=str), time.time()))
            return True
        except sqlite3.IntegrityError:
            return False

    # -------------------------------------------------------------------- audit
    def audit(self, actor: str, action: str, entity: str, entity_id: str | int | None, details: dict | None = None) -> None:
        with self._lock:
            prev = self.one("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1")
            prev_hash = prev["hash"] if prev else "GENESIS"
            ts = round(time.time(), 3)
            eid = None if entity_id is None else str(entity_id)     # stored as TEXT -> hash the same type
            body = json.dumps({"ts": ts, "actor": actor, "action": action, "entity": entity, "id": eid,
                               "details": details or {}}, sort_keys=True, default=str)
            h = hashlib.sha256((prev_hash + body).encode()).hexdigest()
            self.execute("INSERT INTO audit_log(ts,actor,action,entity,entity_id,details,prev_hash,hash) VALUES(?,?,?,?,?,?,?,?)",
                         (ts, actor, action, entity, None if entity_id is None else str(entity_id),
                          json.dumps(details or {}, default=str), prev_hash, h))

    def verify_audit_chain(self) -> dict:
        prev = "GENESIS"
        rows = self.all("SELECT * FROM audit_log ORDER BY id")
        for r in rows:
            body = json.dumps({"ts": r["ts"], "actor": r["actor"], "action": r["action"], "entity": r["entity"],
                               "id": r["entity_id"],
                               "details": json.loads(r["details"] or "{}")}, sort_keys=True, default=str)
            if r["prev_hash"] != prev or hashlib.sha256((prev + body).encode()).hexdigest() != r["hash"]:
                return {"valid": False, "broken_at": r["id"], "entries": len(rows)}
            prev = r["hash"]
        return {"valid": True, "entries": len(rows)}

    def close(self) -> None:
        with self._lock:
            self._db.close()


class _Tx:
    def __init__(self, s: Store) -> None:
        self.s = s

    def __enter__(self):
        self.s._lock.acquire()
        self.s._db.execute("BEGIN")
        return self.s

    def __exit__(self, et, ev, tb):
        try:
            self.s._db.execute("ROLLBACK" if et else "COMMIT")
        finally:
            self.s._lock.release()
        return False
