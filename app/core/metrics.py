"""Tiny dependency-free metrics registry with Prometheus text exposition."""
from __future__ import annotations

import threading
from collections import defaultdict
from contextlib import contextmanager
import time


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[tuple[str, tuple], float] = defaultdict(float)
        self.hist: dict[tuple[str, tuple], list[float]] = defaultdict(list)

    @staticmethod
    def _key(name: str, labels: dict | None) -> tuple[str, tuple]:
        return name, tuple(sorted((labels or {}).items()))

    def inc(self, name: str, value: float = 1.0, **labels) -> None:
        with self._lock:
            self.counters[self._key(name, labels)] += value

    def observe(self, name: str, value: float, **labels) -> None:
        with self._lock:
            buf = self.hist[self._key(name, labels)]
            buf.append(value)
            if len(buf) > 5000:
                del buf[:1000]

    @contextmanager
    def timer(self, name: str, **labels):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, (time.perf_counter() - t0) * 1000, **labels)

    def get(self, name: str, **labels) -> float:
        return self.counters.get(self._key(name, labels), 0.0)

    def percentile(self, name: str, q: float, **labels) -> float:
        buf = sorted(self.hist.get(self._key(name, labels), []))
        if not buf:
            return 0.0
        return buf[min(len(buf) - 1, int(q * len(buf)))]

    def render_prometheus(self) -> str:
        def fmt(labels: tuple) -> str:
            return "{" + ",".join(f'{k}="{v}"' for k, v in labels) + "}" if labels else ""

        lines: list[str] = []
        with self._lock:
            for (name, labels), v in sorted(self.counters.items()):
                lines.append(f"olistflow_{name}{fmt(labels)} {v}")
            for (name, labels), buf in sorted(self.hist.items()):
                s = sorted(buf)
                if not s:
                    continue
                for q in (0.5, 0.95, 0.99):
                    lines.append(
                        f'olistflow_{name}_ms{fmt(labels + (("quantile", str(q)),))} {s[min(len(s)-1, int(q*len(s)))]:.3f}'
                    )
                lines.append(f"olistflow_{name}_ms_count{fmt(labels)} {len(s)}")
        return "\n".join(lines) + "\n"


metrics = Metrics()
