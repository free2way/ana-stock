"""Dependency-free score units shared by repository, screener and reports.

Ranking strength is not a probability. Keep this outside the research package
because that package eagerly loads repository-backed orchestration services.
"""
from __future__ import annotations

import math
from typing import Mapping, Any


SCORE_CONTRACT_VERSION = "selection_score_units_v1"


def first_present(row: Mapping[str, Any], *keys: str):
    for key in keys:
        if row.get(key) is not None:
            return row[key]
    return None


def finite_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not a model score")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("model score must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError("model score must be finite")
    return number


def rank_ratio(row: Mapping[str, Any]) -> float | None:
    # Canonical field: ratio. Existing repository percentile fields: percent,
    # including values below 1. Never infer a unit from the value's magnitude.
    if row.get("rank_percentile") is not None:
        value = finite_number(row["rank_percentile"])
        upper = 1.0
    else:
        value = finite_number(first_present(row, "model_percentile", "percentile"))
        upper = 100.0
    if value is None:
        return None
    if not 0 <= value <= upper:
        raise ValueError("rank percentile outside declared range")
    return value / upper


def ordering_strength(row: Mapping[str, Any]) -> tuple[float | None, str | None]:
    rank = rank_ratio(row)
    if rank is not None:
        return rank, "same_date_percentile"
    # Legacy confidence/raw_score are not calibrated probabilities. Reuse the
    # explicit estimate schema, requiring both protocol and calibration identity.
    if (row.get("estimate_schema_version") == "calibrated_estimates_v1"
        and row.get("probability_unit") == "ratio"
        and str(row.get("estimate_protocol_id") or "").strip()
        and str(row.get("calibration_version") or "").strip()):
        value = finite_number(first_present(row, "calibrated_probability", "bullish_prob"))
        if value is not None and not 0 <= value <= 1:
            raise ValueError("calibrated probability outside [0, 1]")
        if value is not None:
            return value, "calibrated_probability"
    return None, None
