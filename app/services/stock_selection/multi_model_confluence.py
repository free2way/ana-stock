from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence

from app.services.model_signal_summary import (
    CALIBRATION_STATUS_CALIBRATED,
    CALIBRATION_STATUS_MISSING_SCORE,
    expected_hit_probability,
    model_rank_strength,
    resolve_probability_calibrator,
)
from app.services.screener import MODEL_TEMPLATES
from app.services.stock_selection.selection_policy import apply_snapshot_strategy_profile, normalize_action_filter


_MODEL_CONTEXT_FIELDS = (
    "model_score",
    "model_signal_strength",
    "model_confidence",
    "model_percentile",
)

# Fallback chain used to read a model's ex-ante reliability off its own rows
# when no explicit weights were supplied.  ``metric`` selects the transform:
# hit rates are already probabilities, IC is signed so its magnitude is used.
_RELIABILITY_ROW_FIELDS: tuple[tuple[str, str], ...] = (
    ("model_oos_hit_rate", "oos_hit_rate"),
    ("model_oos_ic", "oos_ic"),
    ("model_reliability", "reliability"),
)

_EQUAL_WEIGHT_SOURCE = "equal_weight_fallback"
_PROVIDED_WEIGHT_SOURCE = "provided_model_reliability_weights"

# Same order the fusion consumes; the first populated field wins.
_CALIBRATION_SCORE_FALLBACK_FIELDS = (
    "model_score",
    "snapshot_score",
    "trend_score",
    "confluence_score_mean",
)


def normalize_multi_model_templates(values: object) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw_values = [item.strip() for item in values.split(",")]
    else:
        raw_values = [str(item or "").strip() for item in list(values)]
    normalized: list[str] = []
    for item in raw_values:
        if not item or item not in MODEL_TEMPLATES or item in normalized:
            continue
        normalized.append(item)
    return normalized


def action_semantic_buckets(action_label: str | None) -> list[str]:
    normalized = str(action_label or "").strip().lower().replace(" ", "_")
    if not normalized:
        return []
    if normalized in {"buy_the_dip", "pullback"}:
        return ["buy_the_dip", "bullish_entry"]
    if normalized == "wait_for_breakout":
        return ["breakout_confirmation"]
    if normalized == "breakout":
        return ["breakout_confirmation", "bullish_entry"]
    if normalized in {"buy", "strong_buy", "technical_pattern", "fundamental_pass"}:
        return ["bullish_entry"]
    if normalized in {"watch", "hold", "hold_and_watch", "wait", "avoid", "avoid_or_wait", "continue_to_watch"}:
        return ["watchlist"]
    return []


def template_action_semantic_buckets(template_key: str, action_label: str | None) -> list[str]:
    buckets = list(action_semantic_buckets(action_label))
    if template_key in {"cn_hammer_reversal", "cn_bullish_engulfing_reversal", "cn_macd_underwater_cross"}:
        required = ("buy_the_dip", "bullish_entry")
    elif template_key in {
        "cn_volume_breakout",
        "cn_bullish_ma_stack",
        "cn_three_white_soldiers",
        "tv_multi_timeframe_bullish",
    }:
        required = ("breakout_confirmation", "bullish_entry")
    elif template_key in {"cn_ma_cluster_breakout_watch", "cn_bollinger_squeeze_watch"}:
        required = ("breakout_confirmation",)
    elif template_key in {
        "global_growth_value",
        "global_income_quality",
        "cn_growth_value",
        "cn_high_roe_steady_growth",
        "cn_low_valuation_high_dividend",
    }:
        required = ("bullish_entry",)
    else:
        required = ()
    for bucket in required:
        if bucket not in buckets:
            buckets.append(bucket)
    return buckets


def _template_label(template_key: str) -> str:
    return str((MODEL_TEMPLATES.get(template_key) or {}).get("label") or template_key)


def _unique(values: Sequence[object]) -> list:
    return list(dict.fromkeys(values))


def _numeric(value: object) -> float:
    try:
        return float(value) if value is not None else float("-inf")
    except (TypeError, ValueError):
        return float("-inf")


def _coerce_reliability_weight(value: object, *, metric: str) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if metric == "oos_ic":
        number = abs(number)
    return number


def _normalize_model_weights(raw: Mapping[str, float], template_keys: Sequence[str]) -> dict[str, float]:
    """Clamp to non-negative, rescale percent-style inputs, fill partial gaps.

    A missing model gets the mean of the supplied weights so it is neither
    silently zeroed (which would erase an unmeasured model) nor allowed to
    dominate.  A degenerate all-zero map falls back to equal weight.
    """
    cleaned = {key: max(0.0, float(value)) for key, value in raw.items() if value is not None}
    if not cleaned:
        return {key: 1.0 for key in template_keys}
    maximum = max(cleaned.values())
    if maximum > 1.0:
        cleaned = {key: value / maximum for key, value in cleaned.items()}
    mean = sum(cleaned.values()) / len(cleaned)
    weights = {key: cleaned.get(key, mean) for key in template_keys}
    if sum(weights.values()) <= 0.0:
        return {key: 1.0 for key in template_keys}
    return weights


def resolve_model_weights(
    template_keys: Sequence[str],
    *,
    params: Mapping[str, object],
    template_rows: Mapping[str, Sequence[dict]],
) -> tuple[dict[str, float], str]:
    """Resolve per-model reliability weights, newest reliable source first.

    Priority:
      1. ``params["model_reliability_weights"]`` (explicit mapping, optionally
         ``{template: {"metric": ..., "value": ...}}``).
      2. Per-model row metadata (``model_oos_hit_rate`` / ``model_oos_ic`` /
         ``model_reliability``) carried by the prediction rows.
      3. Equal weights, clearly labelled ``equal_weight_fallback``.
    """
    active = [key for key in template_keys if key in template_rows] or list(template_keys)
    provided = params.get("model_reliability_weights")
    if isinstance(provided, Mapping) and provided:
        raw: dict[str, float] = {}
        for key in active:
            entry = provided.get(key)
            if entry is None:
                continue
            if isinstance(entry, Mapping):
                metric = str(entry.get("metric") or entry.get("weight_source") or "reliability")
                value = entry.get("value", entry.get("weight"))
            else:
                metric = "reliability"
                value = entry
            number = _coerce_reliability_weight(value, metric=metric)
            if number is not None:
                raw[key] = number
        if raw:
            return _normalize_model_weights(raw, active), _PROVIDED_WEIGHT_SOURCE

    raw = {}
    metric_used: str | None = None
    for key in active:
        for field, metric in _RELIABILITY_ROW_FIELDS:
            found = next(
                (row.get(field) for row in (template_rows.get(key) or []) if row.get(field) is not None),
                None,
            )
            if found is None:
                continue
            number = _coerce_reliability_weight(found, metric=metric)
            if number is not None:
                raw[key] = number
                metric_used = metric_used or metric
                break
    if raw:
        return _normalize_model_weights(raw, active), f"row_metadata:{metric_used or 'reliability'}"

    return {key: 1.0 for key in active}, _EQUAL_WEIGHT_SOURCE


def _weight_field_name(template_key: str) -> str:
    sanitized = re.sub(r"[^0-9a-zA-Z]+", "_", str(template_key)).strip("_").lower()
    return f"model_weight_{sanitized or 'model'}"


def _calibration_score(row: Mapping[str, object], score_field: str | None) -> object | None:
    fields = [score_field] if score_field else []
    fields.extend(field for field in _CALIBRATION_SCORE_FALLBACK_FIELDS if field != score_field)
    for field in fields:
        value = row.get(field) if field else None
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return None


def apply_probability_gate(
    rows: Sequence[dict],
    *,
    min_hit_probability: float | None,
    outcome_key: str | None = None,
) -> tuple[list[dict], dict]:
    """Optional abstention gate on calibrated ``expected_hit_probability``.

    Disabled by default (``min_hit_probability=None``).  When enabled, rows
    whose probability is missing (uncalibrated) or below the threshold abstain.
    Coverage falls as the threshold rises; when a truth ``outcome_key`` is
    present the realized precision of the retained subset is reported too.
    """
    total = len(rows)
    probabilities = [row.get("expected_hit_probability") for row in rows]
    uncalibrated = sum(1 for value in probabilities if value is None)
    if min_hit_probability is None:
        present = [float(value) for value in probabilities if value is not None]
        report = {
            "enabled": False,
            "threshold": None,
            "total_candidates": total,
            "selected_count": total,
            "excluded_count": 0,
            "excluded_uncalibrated": uncalibrated,
            "coverage": 1.0 if total else 0.0,
            "mean_expected_hit_probability": (
                round(sum(present) / len(present), 6) if present else None
            ),
            "precision": None,
        }
        return list(rows), report

    threshold = float(min_hit_probability)
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise ValueError("min_hit_probability must be finite and in [0, 1]")
    kept: list[dict] = []
    for row in rows:
        value = row.get("expected_hit_probability")
        if value is None or float(value) < threshold:
            continue
        kept.append(row)
    precision: float | None = None
    if outcome_key:
        outcomes = [
            float(row[outcome_key])
            for row in kept
            if row.get(outcome_key) is not None and _numeric(row.get(outcome_key)) != float("-inf")
        ]
        if outcomes:
            precision = sum(1 for value in outcomes if value > 0) / len(outcomes)
    selected_probabilities = [float(row["expected_hit_probability"]) for row in kept]
    report = {
        "enabled": True,
        "threshold": threshold,
        "total_candidates": total,
        "selected_count": len(kept),
        "excluded_count": total - len(kept),
        "excluded_uncalibrated": uncalibrated,
        "coverage": (len(kept) / total) if total else 0.0,
        "mean_expected_hit_probability": (
            round(sum(selected_probabilities) / len(selected_probabilities), 6)
            if selected_probabilities
            else None
        ),
        "precision": round(precision, 6) if precision is not None else None,
    }
    return kept, report


def _sort_multi_model_rows(rows: list[dict], *, sort_by: str, sort_order: str) -> list[dict]:
    reverse = sort_order != "asc"
    if sort_by in {"default", "confluence_rank"}:
        return sorted(
            rows,
            key=lambda item: (
                # Reliability-weighted agreement leads; hit count stays a
                # transparent tie-break so ``min_hits`` semantics are unchanged.
                _numeric(item.get("weighted_score")),
                int(item.get("model_hit_count") or 0),
                int(item.get("confluence_alignment_count") or 0),
                float(item.get("trade_readiness_score") or 0.0),
                model_rank_strength(item.get("model_score")),
                float(item.get("trend_score") or 0.0),
                str(item.get("ticker") or ""),
            ),
            reverse=reverse,
        )
    if sort_by in {"model_hit_count", "confluence_alignment_count"}:
        primary_key = sort_by
        return sorted(
            rows,
            key=lambda item: (
                int(item.get(primary_key) or 0),
                _numeric(item.get("weighted_score")),
                int(item.get("model_hit_count") or 0),
                int(item.get("confluence_alignment_count") or 0),
                float(item.get("snapshot_score") or 0.0),
                float(item.get("trend_score") or 0.0),
                str(item.get("ticker") or ""),
            ),
            reverse=reverse,
        )
    numeric_fields = {
        "trend_score",
        "latest_close",
        "momentum_5",
        "momentum_20",
        "volume_ratio",
        "pe_ttm",
        "roe_avg_3y",
        "net_profit_yoy",
        "revenue_yoy",
        "dividend_yield",
        "debt_to_assets",
        "snapshot_hits",
        "model_signal_strength",
        "trade_readiness_score",
        "weighted_score",
        "weighted_score_normalized",
        "expected_hit_probability",
    }
    if sort_by == "model_signal_strength":
        return sorted(
            rows,
            key=lambda item: (
                model_rank_strength(item.get("model_score")),
                _numeric(item.get("model_signal_strength")),
                str(item.get("ticker") or ""),
            ),
            reverse=reverse,
        )
    if sort_by in numeric_fields:
        return sorted(
            rows,
            key=lambda item: (_numeric(item.get(sort_by)), str(item.get("ticker") or "")),
            reverse=reverse,
        )
    return sorted(rows, key=lambda item: str(item.get(sort_by) or "").lower(), reverse=reverse)


def aggregate_multi_model_rows(
    template_rows: Mapping[str, Sequence[dict]],
    *,
    template_keys: Sequence[str],
    params: Mapping[str, object],
    apply_limit: bool = True,
) -> tuple[list[dict], dict]:
    """Aggregate model rows once for both precompute and online fallback paths."""
    normalized_keys = normalize_multi_model_templates(template_keys)
    available_templates = [key for key in normalized_keys if key in template_rows]
    missing_templates = [key for key in normalized_keys if key not in template_rows]
    meta = {
        "available_templates": available_templates,
        "missing_templates": missing_templates,
    }
    if len(normalized_keys) < 2 or not available_templates:
        return [], meta

    model_weights, weight_source = resolve_model_weights(
        normalized_keys,
        params=params,
        template_rows=template_rows,
    )
    calibrator, calibration_reason = resolve_probability_calibrator(params)
    meta["model_weights"] = dict(model_weights)
    meta["weight_source"] = weight_source
    meta["calibration_status"] = (
        CALIBRATION_STATUS_CALIBRATED if calibrator is not None else calibration_reason
    )
    if calibrator is not None:
        meta["calibration_source"] = calibrator.source
        meta["calibration_version"] = calibrator.version
    total_active_weight = sum(model_weights.values())

    aggregated: dict[str, dict] = {}
    for template_key in available_templates:
        label = _template_label(template_key)
        for source_row in template_rows.get(template_key) or []:
            ticker = str(source_row.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            row = dict(source_row)
            score = float(row.get("snapshot_score") or row.get("trend_score") or 0.0)
            existing = aggregated.get(ticker)
            if existing is None or score > float(existing.get("_best_score") or 0.0):
                previous_meta = {
                    "_template_keys": list((existing or {}).get("_template_keys") or []),
                    "_template_labels": list((existing or {}).get("_template_labels") or []),
                    "_action_labels": list((existing or {}).get("_action_labels") or []),
                    "_confluence_bucket_hits": {
                        key: list(value or [])
                        for key, value in dict((existing or {}).get("_confluence_bucket_hits") or {}).items()
                    },
                    "_selection_reasons": list((existing or {}).get("_selection_reasons") or []),
                    "_execution_tags": list((existing or {}).get("_execution_tags") or []),
                    "_scores": list((existing or {}).get("_scores") or []),
                }
                previous_context = {
                    field: (existing or {}).get(field)
                    for field in _MODEL_CONTEXT_FIELDS
                    if (existing or {}).get(field) is not None
                }
                base = row
                base["_best_score"] = score
                base.update(previous_meta)
                for field, value in previous_context.items():
                    if base.get(field) is None:
                        base[field] = value
                aggregated[ticker] = base
                existing = base
            for field in _MODEL_CONTEXT_FIELDS:
                if existing.get(field) is None and row.get(field) is not None:
                    existing[field] = row.get(field)
            existing["_template_keys"].append(template_key)
            existing["_template_labels"].append(label)
            existing["_action_labels"].append(str(row.get("action_label") or "").strip())
            risk_flags = {
                str(flag).strip().lower()
                for flag in (row.get("risk_flags") or [])
                if str(flag).strip()
            }
            buckets = template_action_semantic_buckets(template_key, row.get("action_label"))
            if risk_flags.intersection({"rolled-over-after-spike", "do-not-chase"}):
                buckets = [
                    bucket
                    for bucket in buckets
                    if bucket not in {"bullish_entry", "breakout_confirmation", "buy_the_dip"}
                ]
                if "watchlist" not in buckets:
                    buckets.append("watchlist")
            for bucket in buckets:
                hits = existing["_confluence_bucket_hits"].setdefault(bucket, [])
                if template_key not in hits:
                    hits.append(template_key)
            reason = str(row.get("selection_reason") or "").strip()
            if reason:
                existing["_selection_reasons"].append(f"{label}: {reason}")
            for tag in row.get("model_execution_tags") or []:
                clean_tag = str(tag).strip()
                if clean_tag:
                    existing["_execution_tags"].append(clean_tag)
            existing["_scores"].append(score)

    min_hits = max(2, int(params.get("min_multi_model_hits") or 2))
    confluence_action_filter = normalize_action_filter(str(params.get("confluence_action_filter") or "ALL"))
    results: list[dict] = []
    for item in aggregated.values():
        template_keys_hit = _unique(item.pop("_template_keys", []))
        if len(template_keys_hit) < min_hits:
            continue
        template_labels_hit = _unique(item.pop("_template_labels", []))
        action_labels_hit = [label for label in _unique(item.pop("_action_labels", [])) if label]
        confluence_bucket_hits = item.pop("_confluence_bucket_hits", {})
        selection_reasons = _unique(item.pop("_selection_reasons", []))
        execution_tags = _unique(item.pop("_execution_tags", []))
        scores = item.pop("_scores", [])
        item.pop("_best_score", None)
        if confluence_action_filter not in {"", "all"}:
            aligned_templates = confluence_bucket_hits.get(confluence_action_filter) or []
            if len(aligned_templates) < min_hits:
                continue
        item["model_hit_count"] = len(template_keys_hit)
        item["snapshot_hits"] = len(template_keys_hit)
        item["snapshot_runs"] = len(normalized_keys)
        item["matched_model_templates"] = template_keys_hit
        item["matched_model_labels"] = template_labels_hit
        item["matched_patterns"] = template_labels_hit
        item["matched_action_labels"] = action_labels_hit
        item["matched_action_buckets"] = sorted(confluence_bucket_hits)
        item["matched_action_bucket_hits"] = {
            key: len(value or [])
            for key, value in confluence_bucket_hits.items()
        }
        if confluence_action_filter not in {"", "all"}:
            alignment_count = item["matched_action_bucket_hits"].get(confluence_action_filter) or 0
        else:
            alignment_count = max([int(value or 0) for value in item["matched_action_bucket_hits"].values()] or [0])
        item["confluence_alignment_count"] = int(alignment_count)
        item["model_execution_tags"] = execution_tags
        item["selection_reason"] = (
            " | ".join(selection_reasons[:3])
            if selection_reasons
            else item.get("selection_reason")
        )
        item["model_summary"] = (
            f"{len(template_labels_hit)} model hits · " + " / ".join(template_labels_hit[:4])
            if template_labels_hit
            else item.get("model_summary")
        )
        item["model_highlights"] = [
            text
            for text in (
                "Matched templates: " + " / ".join(template_labels_hit[:5]),
                "Action mix: " + " / ".join(action_labels_hit[:4]) if action_labels_hit else "",
                *selection_reasons[:2],
            )
            if text
        ]
        item["confluence_score_mean"] = (
            round(sum(float(score or 0.0) for score in scores) / len(scores), 2)
            if scores
            else None
        )
        # Reliability weighting: a high-reliability model's vote counts more
        # than a low-reliability one, while ``model_hit_count`` keeps the raw
        # AND-style agreement count used by ``min_multi_model_hits``.
        matched_weights = {key: float(model_weights.get(key) or 0.0) for key in template_keys_hit}
        weighted_score = sum(matched_weights.values())
        item["weighted_score"] = round(weighted_score, 6)
        item["weighted_score_normalized"] = (
            round(weighted_score / total_active_weight, 6) if total_active_weight > 0 else None
        )
        item["model_weights"] = {key: round(value, 6) for key, value in matched_weights.items()}
        for key, value in matched_weights.items():
            item[_weight_field_name(key)] = round(value, 6)
        item["weight_source"] = weight_source
        calibration_score = _calibration_score(item, calibrator.score_field if calibrator else None)
        probability = expected_hit_probability(calibrator, calibration_score)
        item["expected_hit_probability"] = probability
        if calibrator is not None and probability is not None:
            item["calibration_status"] = CALIBRATION_STATUS_CALIBRATED
            item["calibration_source"] = calibrator.source
            item["calibration_method"] = calibrator.method
        elif calibrator is not None:
            item["calibration_status"] = CALIBRATION_STATUS_MISSING_SCORE
        else:
            item["calibration_status"] = calibration_reason
        results.append(item)

    results = _sort_multi_model_rows(
        results,
        sort_by=str(params.get("sort_by") or "default"),
        sort_order=str(params.get("sort_order") or "desc"),
    )
    results = apply_snapshot_strategy_profile(
        results,
        profile=str(params.get("strategy_profile") or ""),
    )
    min_hit_probability = params.get("min_hit_probability")
    if min_hit_probability is not None:
        min_hit_probability = float(min_hit_probability)
    results, probability_gate = apply_probability_gate(
        results,
        min_hit_probability=min_hit_probability,
        outcome_key=str(params.get("probability_outcome_key") or "") or None,
    )
    meta["probability_gate"] = probability_gate
    if not apply_limit:
        return results, meta
    return results[: int(params.get("limit") or 500)], meta
