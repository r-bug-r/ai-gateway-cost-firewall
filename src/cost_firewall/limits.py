"""Per-project rate and concurrency limiting.

Both limiters are process-local. Budget enforcement is *not* process-local: it is
transactional in SQLite, so multiple gateway processes share one hard cap. Limits
here are a request-shaping control, and the operations docs say so explicitly.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    """Sliding-window request limiter that recovers as the window advances."""

    def __init__(self, *, limit: int, window_seconds: float = 60.0):
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self._limit = limit
        self._window_ms = int(window_seconds * 1000)
        self._hits: dict[str, deque[int]] = {}
        self._lock = threading.Lock()

    def _prune(self, project_id: str, now: int) -> deque[int]:
        hits = self._hits.setdefault(project_id, deque())
        cutoff = now - self._window_ms
        while hits and hits[0] <= cutoff:
            hits.popleft()
        return hits

    def acquire(self, project_id: str, *, now: int | None = None) -> int | None:
        """Return None when allowed, otherwise seconds to wait before retrying."""
        timestamp = now if now is not None else int(time.time() * 1000)
        with self._lock:
            hits = self._prune(project_id, timestamp)
            if len(hits) >= self._limit:
                wait_ms = hits[0] + self._window_ms - timestamp
                return max(1, -(-wait_ms // 1000))
            hits.append(timestamp)
            return None

    def snapshot(self, project_id: str, *, now: int | None = None) -> int:
        timestamp = now if now is not None else int(time.time() * 1000)
        with self._lock:
            return len(self._prune(project_id, timestamp))


class ConcurrencyLimiter:
    """Bounded in-flight counter. Over-limit requests are rejected, never queued."""

    def __init__(self, limits: dict[str, int]):
        self._limits = limits
        self._in_flight: dict[str, int] = {}
        self._lock = threading.Lock()

    def try_acquire(self, project_id: str) -> bool:
        with self._lock:
            limit = self._limits.get(project_id, 1)
            current = self._in_flight.get(project_id, 0)
            if current >= limit:
                return False
            self._in_flight[project_id] = current + 1
            return True

    def release(self, project_id: str) -> None:
        with self._lock:
            current = self._in_flight.get(project_id, 0)
            self._in_flight[project_id] = max(0, current - 1)

    def in_flight(self, project_id: str) -> int:
        with self._lock:
            return self._in_flight.get(project_id, 0)
