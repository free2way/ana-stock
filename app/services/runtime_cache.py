from __future__ import annotations

from time import monotonic


_CACHE: dict[tuple[str, str], tuple[float, object]] = {}


def get_cached(namespace: str, key: str):
    cache_key = (namespace, key)
    existing = _CACHE.get(cache_key)
    if existing is None:
        return None
    expires_at, value = existing
    if expires_at <= monotonic():
        _CACHE.pop(cache_key, None)
        return None
    return value


def set_cached(namespace: str, key: str, value, *, ttl_seconds: float) -> None:
    _CACHE[(namespace, key)] = (monotonic() + max(0.0, float(ttl_seconds)), value)


def get_or_set(namespace: str, key: str, *, ttl_seconds: float, loader):
    cache_key = (namespace, key)
    now = monotonic()
    cached = get_cached(namespace, key)
    if cached is not None:
        return cached
    value = loader()
    _CACHE[cache_key] = (now + ttl_seconds, value)
    return value


def clear_namespace(namespace: str) -> None:
    doomed = [key for key in _CACHE if key[0] == namespace]
    for key in doomed:
        _CACHE.pop(key, None)
