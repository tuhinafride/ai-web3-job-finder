"""Tiny in-memory token-bucket rate limiter (per process; good enough for one Render instance)."""
from __future__ import annotations

import threading
import time


class TokenBucket:
    def __init__(self, per_minute: int, burst: int | None = None, max_keys: int = 50_000):
        self.rate = max(1, per_minute) / 60.0
        self.capacity = float(burst or max(1, per_minute))
        self.max_keys = max_keys
        self._b: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def configure(self, per_minute: int, burst: int | None = None):
        with self._lock:
            self.rate = max(1, per_minute) / 60.0
            self.capacity = float(burst or max(1, per_minute))
            self._b.clear()

    def allow(self, key: str, cost: float = 1.0) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, ts = self._b.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - ts) * self.rate)
            ok = tokens >= cost
            if ok:
                tokens -= cost
            if len(self._b) >= self.max_keys and key not in self._b:
                # drop the oldest half to bound memory
                for k, _ in sorted(self._b.items(), key=lambda kv: kv[1][1])[: self.max_keys // 2]:
                    self._b.pop(k, None)
            self._b[key] = (tokens, now)
            return ok

    def reset(self):
        with self._lock:
            self._b.clear()
