"""Authentication (JWT), authorisation (RBAC), password hashing and rate limiting."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.logging import get_logger
from app.services.store import Store

log = get_logger("security")
bearer = HTTPBearer(auto_error=False)

ROLES = ("admin", "ops_manager", "seller", "analyst")
# permission -> roles allowed
PERMISSIONS: dict[str, set[str]] = {
    "order:create":     {"admin", "ops_manager", "seller"},
    "shipment:read":    {"admin", "ops_manager", "seller", "analyst"},
    "shipment:manage":  {"admin", "ops_manager"},
    "tower:read":       {"admin", "ops_manager", "analyst"},
    "simulate":         {"admin", "ops_manager", "analyst"},
    "policy:read":      {"admin", "ops_manager", "analyst", "seller"},
    "policy:write":     {"admin", "ops_manager"},
    "ops:control":      {"admin", "ops_manager"},
    "audit:read":       {"admin", "analyst"},
    "admin":            {"admin"},
    "metrics:read":     {"admin", "analyst"},
}


# ------------------------------------------------------------------ passwords
def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, dk_hex = stored.split("$")
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


def seed_default_users(store: Store) -> None:
    """Creates one account per role. Passwords come from OLISTFLOW_<ROLE>_PASSWORD; a random one is
    generated (and logged once) if unset in production, a fixed dev password otherwise."""
    dev = os.environ.get("OLISTFLOW_ENV", "dev") != "production"
    for role, username, seller in (("admin", "admin", None), ("ops_manager", "ops", None),
                                   ("analyst", "analyst", None), ("seller", "seller", "S001")):
        if store.one("SELECT 1 FROM users WHERE username=?", (username,)):
            continue
        pw = os.environ.get(f"OLISTFLOW_{username.upper()}_PASSWORD") or ("olistflow-dev" if dev else secrets.token_urlsafe(12))
        store.execute("INSERT INTO users(username,pw_hash,role,seller_id) VALUES(?,?,?,?)",
                      (username, hash_password(pw), role, seller))
        if dev:
            log.warning("dev credentials created", extra={"ctx": {"username": username, "note": "password=olistflow-dev (dev only)"}})
        elif f"OLISTFLOW_{username.upper()}_PASSWORD" not in os.environ:
            log.warning("generated one-time password", extra={"ctx": {"username": username, "password": pw}})


# ------------------------------------------------------------------------ JWT
@dataclass
class Principal:
    username: str
    role: str
    seller_id: Optional[str]

    def can(self, perm: str) -> bool:
        return self.role in PERMISSIONS.get(perm, set())


def issue_token(settings, user: dict) -> tuple[str, int]:
    now = int(time.time())
    ttl = settings.jwt_ttl_minutes * 60
    token = jwt.encode({"sub": user["username"], "role": user["role"], "sid": user["seller_id"], "iat": now,
                        "exp": now + ttl, "jti": uuid.uuid4().hex}, settings.jwt_secret, algorithm="HS256")
    return token, ttl


def current_principal(request: Request, creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> Principal:
    if not creds:
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = jwt.decode(creds.credentials, request.app.state.c.settings.jwt_secret, algorithms=["HS256"],
                            options={"require": ["exp", "sub", "role"]})
    except jwt.PyJWTError:
        raise HTTPException(401, "invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    if claims["role"] not in ROLES:
        raise HTTPException(401, "invalid role")
    p = Principal(claims["sub"], claims["role"], claims.get("sid"))
    request.state.principal = p
    return p


def require(perm: str):
    def dep(p: Principal = Depends(current_principal)) -> Principal:
        if not p.can(perm):
            raise HTTPException(403, f"role '{p.role}' lacks permission '{perm}'")
        return p
    return dep


# --------------------------------------------------------------- rate limiting
class RateLimiter:
    """Token bucket per key. `capacity` tokens refill evenly over 60 seconds."""

    def __init__(self, per_minute: int) -> None:
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self._b: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, cost: float = 1.0) -> tuple[bool, float]:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._b.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens >= cost:
                self._b[key] = (tokens - cost, now)
                return True, 0.0
            self._b[key] = (tokens, now)
            return False, (cost - tokens) / self.rate
