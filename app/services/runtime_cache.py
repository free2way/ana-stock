from __future__ import annotations

from time import monotonic


_CACHE: dict[tuple[str, str], tuple[float, object]] = {}


def get_or_set(namespace: str, key: str, *, ttl_seconds: float, loader):
    cache_key = (namespace, key)
    now = monotonic()
    existing = _CACHE.get(cache_key)
    if existing is not None:
        expires_at, value = existing
        if expires_at > now:
            return value
    value = loader()
    _CACHE[cache_key] = (now + ttl_seconds, value)
    return value


def clear_namespace(namespace: str) -> None:
    doomed = [key for key in _CACHE if key[0] == namespace]
    for key in doomed:
        _CACHE.pop(key, None)
