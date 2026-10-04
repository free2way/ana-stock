"""In-process TTL cache for runtime/presentation data.

Guarantees required by the quant-remediation S-11 acceptance item:

* **Bounded LRU** — at most ``max_entries`` keys are retained; the least
  recently used entry is evicted first.
* **Negative caching** — a ``None`` returned by a loader is a real cached
  value with its own TTL, so a missing/empty result is not re-queried on every
  request.
* **Single flight** — concurrent ``get_or_set`` calls for the same key run the
  loader exactly once; other callers wait for and share its result.

The public API (``get_cached`` / ``set_cached`` / ``get_or_set`` /
``clear_namespace``) is unchanged for the ~70 existing call sites.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from time import monotonic


def _default_max_entries() -> int:
    raw = os.environ.get("PQW_RUNTIME_CACHE_MAX_ENTRIES", "2048")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 2048
    return max(16, value)


_CACHE: "OrderedDict[tuple[str, str], tuple[float, object]]" = OrderedDict()
_LOCK = threading.RLock()
_MAX_ENTRIES = _default_max_entries()

# Distinguishes "key absent" from "key cached as None" for internal lookups.
_MISS = object()


class _Flight:
    """Tracks the single in-progress loader for one cache key."""

    __slots__ = ("event", "owner", "error")

    def __init__(self, owner: int) -> None:
        self.event = threading.Event()
        self.owner = owner
        self.error: BaseException | None = None


_INFLIGHT: dict[tuple[str, str], _Flight] = {}


def _record_hit(key: tuple[str, str]) -> None:
    _CACHE.move_to_end(key)


def _store(key: tuple[str, str], *, expires_at: float, value: object) -> None:
    _CACHE[key] = (expires_at, value)
    _CACHE.move_to_end(key)
    while len(_CACHE) > _MAX_ENTRIES:
        _CACHE.popitem(last=False)


def _lookup(key: tuple[str, str]) -> object:
    """Return the cached value, or ``_MISS`` when absent/expired."""

    existing = _CACHE.get(key)
    if existing is None:
        return _MISS
    expires_at, value = existing
    if expires_at <= monotonic():
        _CACHE.pop(key, None)
        return _MISS
    _record_hit(key)
    return value


def get_cached(namespace: str, key: str):
    """Return a cached value or ``None`` when absent/expired."""

    with _LOCK:
        value = _lookup((namespace, key))
    return None if value is _MISS else value


def set_cached(namespace: str, key: str, value, *, ttl_seconds: float) -> None:
    with _LOCK:
        _store(
            (namespace, key),
            expires_at=monotonic() + max(0.0, float(ttl_seconds)),
            value=value,
        )


def get_or_set(namespace: str, key: str, *, ttl_seconds: float, loader):
    """Return the cached value or compute it once, with single-flight semantics.

    ``None`` is cached like any other value (negative caching) and expires
    after ``ttl_seconds``. Concurrent callers for the same key share the first
    loader result instead of each re-running the loader.
    """

    cache_key = (namespace, key)
    while True:
        with _LOCK:
            cached = _lookup(cache_key)
            if cached is not _MISS:
                return cached
            flight = _INFLIGHT.get(cache_key)
            if flight is None:
                flight = _Flight(threading.get_ident())
                _INFLIGHT[cache_key] = flight
                leader = True
            else:
                leader = False

        if leader or flight.owner == threading.get_ident():
            # The owner re-entered the same key (loader recursion); run the
            # loader directly instead of dead-locking on our own wait.
            break
        flight.event.wait()
        # Loop: the leader stored a value, or errored and left the key open.

    try:
        value = loader()
    except BaseException as exc:
        with _LOCK:
            flight.error = exc
            if _INFLIGHT.get(cache_key) is flight:
                _INFLIGHT.pop(cache_key, None)
        flight.event.set()
        raise
    with _LOCK:
        _store(
            cache_key,
            expires_at=monotonic() + max(0.0, float(ttl_seconds)),
            value=value,
        )
        if _INFLIGHT.get(cache_key) is flight:
            _INFLIGHT.pop(cache_key, None)
    flight.event.set()
    return value


def clear_namespace(namespace: str) -> None:
    with _LOCK:
        doomed = [key for key in _CACHE if key[0] == namespace]
        for key in doomed:
            _CACHE.pop(key, None)


def cache_size() -> int:
    """Number of live entries (including negative-cached ``None`` values)."""

    with _LOCK:
        return len(_CACHE)


def cache_max_entries() -> int:
    with _LOCK:
        return _MAX_ENTRIES


def configure_cache(*, max_entries: int | None = None) -> None:
    """Adjust the LRU capacity (primarily for tests/ops tuning)."""

    global _MAX_ENTRIES
    with _LOCK:
        if max_entries is not None:
            _MAX_ENTRIES = max(1, int(max_entries))
        while len(_CACHE) > _MAX_ENTRIES:
            _CACHE.popitem(last=False)


def clear_all() -> None:
    with _LOCK:
        _CACHE.clear()
