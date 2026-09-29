"""P1 date-decay and regime-matched research weights."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
import math
from typing import Iterable, Mapping, Protocol


class DatedSample(Protocol):
    sample_id: str
    feature_date: date


@dataclass(frozen=True, slots=True)
class P1SamplingConfig:
    mode: str = "fixed_window_v1"
    half_life_sessions: int = 90
    matching_regime_multiplier: float = 1.5
    minimum_matching_regime_dates: int = 20
    schema_version: str = "p1_training_sampling_v1"

    def __post_init__(self) -> None:
        if self.mode not in {
            "fixed_window_v1",
            "exponential_decay_v1",
            "decay_plus_regime_v1",
        }:
            raise ValueError("unsupported P1 sampling mode")
        if self.half_life_sessions <= 0 or self.minimum_matching_regime_dates <= 0:
            raise ValueError("sampling date settings must be positive")
        if not math.isfinite(self.matching_regime_multiplier) or self.matching_regime_multiplier < 1.0:
            raise ValueError("matching_regime_multiplier must be finite and at least one")

    def version(self) -> str:
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return f"{self.schema_version}:{hashlib.sha256(encoded.encode()).hexdigest()[:16]}"


@dataclass(frozen=True, slots=True)
class P1SamplingResult:
    weights_by_sample_id: Mapping[str, float]
    audit: Mapping[str, object]


def _effective_count(values: Iterable[float]) -> float:
    weights = list(values)
    denominator = sum(value * value for value in weights)
    return (sum(weights) ** 2 / denominator) if denominator > 0 else 0.0


def build_p1_training_weights(
    samples: Iterable[DatedSample],
    *,
    trading_dates: Iterable[date],
    prediction_date: date,
    config: P1SamplingConfig | None = None,
    historical_regime_by_date: Mapping[date, str] | None = None,
    prediction_regime: str | None = None,
) -> P1SamplingResult:
    resolved = config or P1SamplingConfig()
    rows = list(samples)
    if not rows:
        raise ValueError("P1 sampling requires at least one training sample")
    sample_ids = [str(item.sample_id) for item in rows]
    if len(sample_ids) != len(set(sample_ids)):
        raise ValueError("P1 sampling requires unique sample_id values")
    calendar = list(trading_dates)
    if calendar != sorted(calendar) or len(calendar) != len(set(calendar)):
        raise ValueError("trading_dates must be unique and ascending")
    if prediction_date not in set(calendar):
        raise ValueError("prediction_date must exist in trading_dates")
    date_index = {value: index for index, value in enumerate(calendar)}
    prediction_index = date_index[prediction_date]
    counts = Counter(item.feature_date for item in rows)
    if any(value not in date_index or date_index[value] >= prediction_index for value in counts):
        raise ValueError("training feature dates must precede prediction_date in the calendar")

    requested_mode = resolved.mode
    applied_mode = requested_mode
    fallback_reason = None
    regime_map = historical_regime_by_date or {}
    matching_dates: set[date] = set()
    if requested_mode == "decay_plus_regime_v1":
        if not str(prediction_regime or "").strip():
            raise ValueError("regime-matched sampling requires prediction_regime")
        missing_regime_dates = sorted(value for value in counts if value not in regime_map)
        if missing_regime_dates:
            raise ValueError("regime-matched sampling requires complete historical regime evidence")
        matching_dates = {
            value for value in counts if regime_map[value] == prediction_regime
        }
        if len(matching_dates) < resolved.minimum_matching_regime_dates:
            applied_mode = "exponential_decay_v1"
            fallback_reason = "insufficient_matching_regime_dates"

    raw_date_weights: dict[date, float] = {}
    for feature_date in sorted(counts):
        age_sessions = prediction_index - date_index[feature_date]
        decay = (
            1.0
            if applied_mode == "fixed_window_v1"
            else 0.5 ** (age_sessions / resolved.half_life_sessions)
        )
        regime_multiplier = (
            resolved.matching_regime_multiplier
            if applied_mode == "decay_plus_regime_v1" and feature_date in matching_dates
            else 1.0
        )
        raw_date_weights[feature_date] = decay * regime_multiplier

    # Each date receives its configured mass independent of cross-section size;
    # normalize mean row weight to one for estimator comparability.
    raw_by_id = {
        str(item.sample_id): raw_date_weights[item.feature_date] / counts[item.feature_date]
        for item in rows
    }
    normalization = len(rows) / sum(raw_by_id.values())
    weights_by_id = {
        sample_id: weight * normalization for sample_id, weight in raw_by_id.items()
    }
    normalized_date_weights = {
        feature_date: raw_date_weights[feature_date] * normalization
        for feature_date in raw_date_weights
    }
    audit = {
        "schema_version": "p1_training_sampling_audit_v1",
        "config_version": resolved.version(),
        "requested_mode": requested_mode,
        "applied_mode": applied_mode,
        "status": "FALLBACK" if fallback_reason else "READY_RESEARCH_ONLY",
        "fallback_reason": fallback_reason,
        "prediction_date": prediction_date.isoformat(),
        "prediction_regime": prediction_regime,
        "sample_count": len(rows),
        "date_count": len(counts),
        "matching_regime_date_count": len(matching_dates),
        "effective_sample_count": _effective_count(weights_by_id.values()),
        "effective_date_count": _effective_count(normalized_date_weights.values()),
        "regime_date_counts": dict(sorted(Counter(regime_map.get(value, "missing") for value in counts).items())),
        "dates": [
            {
                "date": feature_date.isoformat(),
                "sample_count": counts[feature_date],
                "age_sessions": prediction_index - date_index[feature_date],
                "regime": regime_map.get(feature_date),
                "matching_prediction_regime": feature_date in matching_dates,
                "total_weight": normalized_date_weights[feature_date],
                "row_weight": normalized_date_weights[feature_date] / counts[feature_date],
            }
            for feature_date in sorted(counts)
        ],
    }
    return P1SamplingResult(weights_by_sample_id=weights_by_id, audit=audit)


__all__ = ["P1SamplingConfig", "P1SamplingResult", "build_p1_training_weights"]
