"""Shared price-basis resolution for evaluation and label paths (A1/R2).

The versioned adjusted view stores its OHLC under namespaced ``adjusted_*``
fields (see ``app.services.trainer._attach_adjusted_basis``).  Evaluation
paths historically read the raw ``close`` column directly, which reintroduces
the ex-date jumps that the trainer-side label fix (A1) already removed
(audit residual R2).  This module centralises the
``adjusted_* -> adj_close (legacy alias) -> raw`` precedence so every
consumer resolves the same underlying series.

``adj_close`` is kept only as a fallback alias: the rebuilt view is the
trustworthy source and ``adj_close`` predates it (the legacy lake column is
explicitly flagged as untrustworthy in ``trainer`` for label construction).
"""
from __future__ import annotations

import math
from typing import Any, Mapping


def _to_float(value: Any) -> float | None:
    """Finite float or ``None``; non-numeric / missing / NaN are unusable."""

    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def preferred_price(row: Mapping[str, Any], field: str) -> float | None:
    """Adjusted-view price for ``field`` when present, else raw.

    Precedence: ``adjusted_<field>`` -> ``<field>``.  ``close`` additionally
    accepts the legacy ``adj_close`` alias between the two.  Non-numeric or
    missing candidates are skipped rather than coerced to ``0``; when every
    candidate is unusable the result is ``None``.
    """

    if not isinstance(row, Mapping):
        return None
    normalized = str(field or "").strip()
    if not normalized:
        return None
    candidates = [f"adjusted_{normalized}"]
    if normalized == "close":
        candidates.append("adj_close")
    candidates.append(normalized)
    for key in candidates:
        value = _to_float(row.get(key))
        if value is not None:
            return value
    return None


def preferred_close(row: Mapping[str, Any]) -> float | None:
    """Adjusted-view close first, then legacy ``adj_close``, then raw ``close``."""

    return preferred_price(row, "close")
