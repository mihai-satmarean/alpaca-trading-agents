"""One set of broker reads shared by every dashboard viewer.

Streamlit re-runs the whole script per SESSION, so read cost scales with the
size of the audience rather than with the data. On 2026-09-08, seven viewers
at one render per 15 seconds, each render making roughly fourteen account,
position and order calls, put the dashboard past Alpaca's 200 requests per
minute; the page died with a rate-limit traceback mid-demo.

Caching here bounds the call rate by a TTL instead of by how many people are
watching. The wrapper is returned from a cache_resource-decorated factory, so
all sessions in the process share one instance and therefore one cache.

READS ONLY, AND DISPLAY ONLY. This must never reach a trading engine: a stale
position read inside a trading loop is the exact defect that turned a hedge
into a doubled directional bet on prediction-market-arb in June. The engines
construct their own client and never import this module, which is enforced by
a test rather than by this comment.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

# Reads that are safe to serve from a short-lived cache on a display surface.
CACHED_READS = ("get_account", "get_positions", "get_orders")

# Anything that changes broker state invalidates the read cache, so the next
# render reflects the action rather than the window before it.
MUTATORS = (
    "market_order", "limit_order", "close_position", "close_all_positions",
    "cancel_order", "cancel_all_orders",
)


class ReadThroughCache:
    """Delegate everything to the wrapped client, memoizing only the reads."""

    def __init__(self, inner: Any, ttl: float = 10.0,
                 clock: Callable[[], float] | None = None):
        self._inner = inner
        self._ttl = float(ttl)
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._entries: dict[tuple, tuple[float, Any]] = {}
        self.calls = 0          # reads that actually reached the broker
        self.served = 0         # reads answered from cache

    def invalidate(self) -> None:
        with self._lock:
            self._entries.clear()

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr
        if name in CACHED_READS:
            return self._memoized(name, attr)
        if name in MUTATORS:
            return self._invalidating(attr)
        return attr

    def _memoized(self, name: str, attr: Callable) -> Callable:
        def call(*args, **kwargs):
            key = (name, args, tuple(sorted(kwargs.items())))
            now = self._clock()
            with self._lock:
                hit = self._entries.get(key)
                if hit is not None and now - hit[0] < self._ttl:
                    self.served += 1
                    return hit[1]
            # Deliberately outside the lock: a slow broker call must not block
            # every other viewer's thread.
            value = attr(*args, **kwargs)
            with self._lock:
                self._entries[key] = (self._clock(), value)
                self.calls += 1
            return value
        return call

    def _invalidating(self, attr: Callable) -> Callable:
        def call(*args, **kwargs):
            try:
                return attr(*args, **kwargs)
            finally:
                self.invalidate()
        return call
