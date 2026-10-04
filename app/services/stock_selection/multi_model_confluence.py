from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.services.model_signal_summary import model_rank_strength
from app.services.screener import MODEL_TEMPLATES
from app.services.stock_selection.selection_policy import apply_snapshot_strategy_profile, normalize_action_filter


_MODEL_CONTEXT_FIELDS = (
    "model_score",
    "model_signal_strength",
    "model_confidence",
    "model_percentile",
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


def _sort_multi_model_rows(rows: list[dict], *, sort_by: str, sort_order: str) -> list[dict]:
    reverse = sort_order != "asc"
    if sort_by in {"default", "confluence_rank"}:
        return sorted(
            rows,
            key=lambda item: (
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
    if not apply_limit:
        return results, meta
    return results[: int(params.get("limit") or 500)], meta
