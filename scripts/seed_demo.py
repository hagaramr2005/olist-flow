"""Seed a running server with demo orders:  python scripts/seed_demo.py [base_url] [orders]"""
import json
import os
import sys
import urllib.request

base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
n = int(sys.argv[2]) if len(sys.argv) > 2 else 80
pw = os.environ.get("OLISTFLOW_ADMIN_PASSWORD", "olistflow-dev")


def call(path, body, token=None):
    req = urllib.request.Request(base + path, json.dumps(body).encode(), {"Content-Type": "application/json",
                                 **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


tok = call("/auth/login", {"username": "admin", "password": pw})["access_token"]
print(call("/v1/ops/seed-demo", {"orders": n, "advance_days": 4.0, "seed": 5}, tok))
