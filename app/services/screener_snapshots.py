from __future__ import annotations

import hashlib
import json

from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.services.repository import (
    PredictionRepository,
    SymbolRepository,
    WorkspaceSnapshotRepository,
)
from app.services.market_lake import screen_cn_lake_momentum, screen_us_lake_momentum
from app.services.screener import MODEL_TEMPLATES, ScreenerService
from app.services.technical_patterns import TechnicalPatternService
from app.services.time_utils import app_now_iso


SCREENER_SNAPSHOT_TYPE_PREFIX = "screener_result:"

WATCHLIST_PRECOMPUTE_TEMPLATES = [
    "lightgbm_top_picks",
    "next_tesla_swing",
    "technical_momentum",
    "cn_limit_up_watch",
    "cn_volume_breakout",
    "cn_bullish_ma_stack",
    "cn_macd_underwater_cross",
    "cn_ma_cluster_breakout_watch",
    "cn_bollinger_squeeze_watch",
    "cn_three_white_soldiers",
    "cn_bullish_engulfing_reversal",
    "cn_hammer_reversal",
    "global_growth_value",
    "global_income_quality",
    "cn_growth_value",
    "cn_high_roe_steady_growth",
    "cn_low_valuation_high_dividend",
]

FULL_MARKET_CN_PRECOMPUTE_TEMPLATES = [
    "lightgbm_top_picks",
    "next_tesla_swing",
    "technical_momentum",
    "cn_limit_up_watch",
    "cn_volume_breakout",
    "cn_bullish_ma_stack",
    "cn_macd_underwater_cross",
    "cn_ma_cluster_breakout_watch",
    "cn_bollinger_squeeze_watch",
    "cn_three_white_soldiers",
    "cn_bullish_engulfing_reversal",
    "cn_hammer_reversal",
    "cn_growth_value",
    "cn_high_roe_steady_growth",
    "cn_low_valuation_high_dividend",
]

FULL_MARKET_US_PRECOMPUTE_TEMPLATES = [
    "lightgbm_top_picks",
    "next_tesla_swing",
    "technical_momentum",
    "global_growth_value",
    "global_income_quality",
]

FULL_MARKET_ALL_PRECOMPUTE_TEMPLATES = [
    "next_tesla_swing",
    "technical_momentum",
    "global_growth_value",
    "global_income_quality",
]

CORE_FULL_MARKET_CN_PRECOMPUTE_TEMPLATES = [
    "lightgbm_top_picks",
    "next_tesla_swing",
    "technical_momentum",
]

REST_FULL_MARKET_CN_PRECOMPUTE_TEMPLATES = [
    template for template in FULL_MARKET_CN_PRECOMPUTE_TEMPLATES if template not in CORE_FULL_MARKET_CN_PRECOMPUTE_TEMPLATES
]

CN_MULTI_MODEL_PRECOMPUTE_PRESETS = [
    {
        "key": "dip_confluence",
        "label": "回踩共振",
        "templates": ["lightgbm_top_picks", "next_tesla_swing", "technical_momentum", "cn_hammer_reversal", "cn_macd_underwater_cross"],
        "min_hits": 2,
        "confluence_action_filter": "buy_the_dip",
    },
    {
        "key": "breakout_confluence",
        "label": "突破共振",
        "templates": ["lightgbm_top_picks", "next_tesla_swing", "technical_momentum", "cn_volume_breakout", "cn_bullish_ma_stack"],
        "min_hits": 2,
        "confluence_action_filter": "breakout_confirmation",
    },
    {
        "key": "trend_momentum_lightgbm",
        "label": "强趋势+动量+LightGBM",
        "templates": ["lightgbm_top_picks", "next_tesla_swing", "technical_momentum"],
        "min_hits": 2,
        "confluence_action_filter": "ALL",
    },
    {
        "key": "quality_growth_confluence",
        "label": "成长质量共振",
        "templates": ["lightgbm_top_picks", "cn_growth_value", "cn_high_roe_steady_growth", "technical_momentum"],
        "min_hits": 2,
        "confluence_action_filter": "bullish_entry",
    },
]

US_MULTI_MODEL_PRECOMPUTE_PRESETS = [
    {
        "key": "us_trend_momentum_lightgbm",
        "label": "美股强趋势+动量+LightGBM",
        "templates": ["lightgbm_top_picks", "next_tesla_swing", "technical_momentum"],
        "min_hits": 2,
        "confluence_action_filter": "ALL",
        "sort_by": "model_hit_count",
    },
    {
        "key": "us_breakout_confluence",
        "label": "美股突破共振",
        "templates": ["lightgbm_top_picks", "next_tesla_swing", "technical_momentum"],
        "min_hits": 2,
        "confluence_action_filter": "breakout_confirmation",
        "sort_by": "confluence_rank",
    },
    {
        "key": "us_quality_momentum",
        "label": "美股质量成长共振",
        "templates": ["lightgbm_top_picks", "technical_momentum", "global_growth_value", "global_income_quality"],
        "min_hits": 2,
        "confluence_action_filter": "bullish_entry",
        "sort_by": "confluence_rank",
    },
]

SNAPSHOT_ROW_FIELDS = {
    "ticker",
    "name",
    "market",
    "listing_days",
    "trend_score",
    "action_label",
    "action_summary",
    "latest_close",
    "momentum_5",
    "momentum_20",
    "volume_ratio",
    "distance_to_breakout_pct",
    "snapshot_hits",
    "snapshot_runs",
    "matched_patterns",
    "selection_reason",
    "model_score",
    "model_summary",
    "model_state",
    "model_confidence",
    "model_signal_label",
    "model_signal_strength",
    "model_conviction_bucket",
    "model_position_size_hint",
    "model_entry_style",
    "model_execution_tags",
    "model_percentile",
    "model_horizon_days",
    "model_reward_risk_ratio",
    "model_expected_drawdown_20d",
    "tradability_status",
    "trade_readiness_score",
    "readiness_bucket",
    "readiness_reason",
    "block_reason",
    "preferred_entry_style",
    "suggested_watch_action",
    "target_weight",
    "priority",
    "action_bucket",
    "entry_trigger",
    "invalidation_condition",
    "time_horizon",
    "max_slippage_bps",
    "liquidity_bucket",
    "stop_loss_type",
    "execution_note",
    "risk_flags",
    "pe_ttm",
    "roe_avg_3y",
    "net_profit_yoy",
    "revenue_yoy",
    "dividend_yield",
    "debt_to_assets",
    "setup_bucket",
    "distance_to_52w_high_pct",
    "pullback_depth_pct",
}


def screener_snapshot_key(params: dict) -> str:
    return json.dumps(params, sort_keys=True, ensure_ascii=False)


def screener_snapshot_type(params: dict) -> str:
    digest = hashlib.sha1(screener_snapshot_key(params).encode("utf-8")).hexdigest()[:20]
    return f"{SCREENER_SNAPSHOT_TYPE_PREFIX}{digest}"


def load_exact_screener_snapshot_rows(params: dict) -> list[dict] | None:
    with SessionLocal() as db:
        snapshot = WorkspaceSnapshotRepository(db).get_latest_snapshot(screener_snapshot_type(params))
    if not snapshot:
        return None
    payload = snapshot.get("payload") or {}
    if payload.get("key") != screener_snapshot_key(params):
        return None
    rows = payload.get("rows")
    return list(rows) if isinstance(rows, list) else None


def _build_default_params(template_key: str, *, universe: str, market: str | None = None) -> dict:
    template = MODEL_TEMPLATES.get(template_key, {})
    defaults = template.get("defaults") or {}
    template_market = str(market or template.get("market") or "ALL").upper()
    min_trend_score = int(defaults.get("min_trend_score", 60))
    limit = 300 if universe == "watchlist" else 5000

    if universe == "full_market":
        if template_key == "lightgbm_top_picks":
            # Multi-model confluence needs a much broader LightGBM source pool than
            # the user-facing single-template default threshold.
            min_trend_score = 10
            limit = 6000
        elif template_key == "technical_momentum":
            limit = 5000
        elif template_key == "next_tesla_swing":
            limit = 5000

    return {
        "model_template": template_key,
        "universe": universe,
        "market": template_market,
        "min_trend_score": min_trend_score,
        "action_filter": "ALL",
        "min_volume_ratio": float(defaults.get("min_volume_ratio", 0.0)),
        "min_listing_days": int(defaults.get("min_listing_days", 365)),
        "pe_min": float(defaults.get("pe_min", 0.0)),
        "pe_max": float(defaults.get("pe_max", 30.0)),
        "min_roe_avg_3y": float(defaults.get("min_roe_avg_3y", 12.0)),
        "min_net_profit_yoy": float(defaults.get("min_net_profit_yoy", 20.0)),
        "min_revenue_yoy": float(defaults.get("min_revenue_yoy", 0.0)),
        "max_debt_to_assets": float(defaults.get("max_debt_to_assets", 100.0)),
        "min_dividend_yield": float(defaults.get("min_dividend_yield", 0.0)),
        "exclude_bottom_market_cap_pct": float(defaults.get("exclude_bottom_market_cap_pct", 10.0)),
        "recent_snapshot_runs": 0,
        "min_snapshot_hits": 0,
        "model_signal_filter": "ALL",
        "min_model_signal_strength": 0.0,
        "execution_tag_filter": "ALL",
        "exclude_execution_tag_filter": "ALL",
        "sort_by": "default",
        "sort_order": "desc",
        "limit": limit,
    }


def build_base_precompute_params(*, model_template: str, universe: str, market: str) -> dict:
    return _build_default_params(model_template, universe=universe, market=market)


def build_precompute_screener_params(*, markets: list[str] | None = None, include_watchlist: bool = True) -> list[dict]:
    market_set = {str(item).strip().upper() for item in (markets or ["CN"]) if str(item).strip()}
    if not market_set:
        market_set = {"CN"}
    params: list[dict] = []
    if include_watchlist:
        for template_key in WATCHLIST_PRECOMPUTE_TEMPLATES:
            params.append(_build_default_params(template_key, universe="watchlist"))
    if "CN" in market_set:
        for template_key in FULL_MARKET_CN_PRECOMPUTE_TEMPLATES:
            params.append(_build_default_params(template_key, universe="full_market", market="CN"))
    if market_set.intersection({"CN", "US"}):
        for template_key in FULL_MARKET_ALL_PRECOMPUTE_TEMPLATES:
            params.append(_build_default_params(template_key, universe="full_market", market="ALL"))
    if "US" in market_set:
        for template_key in FULL_MARKET_US_PRECOMPUTE_TEMPLATES:
            params.append(_build_default_params(template_key, universe="full_market", market="US"))
    return params


def build_lake_precompute_screener_params(*, markets: list[str] | None = None) -> list[dict]:
    market_set = {str(item).strip().upper() for item in (markets or ["CN", "US"]) if str(item).strip()}
    params: list[dict] = []
    for market in sorted(market_set):
        if market == "US":
            params.append(_build_default_params("lightgbm_top_picks", universe="full_market", market=market))
            params.append(_build_default_params("technical_momentum", universe="full_market", market=market))
            continue
        if market == "CN":
            params.append(_build_default_params("next_tesla_swing", universe="full_market", market=market))
            params.append(_build_default_params("technical_momentum", universe="full_market", market=market))
    return params


def build_multi_model_precompute_params(*, markets: list[str] | None = None, preset_keys: list[str] | None = None) -> list[dict]:
    market_set = {str(item).strip().upper() for item in (markets or ["CN"]) if str(item).strip()}
    normalized_keys = {str(item).strip() for item in (preset_keys or []) if str(item).strip()}
    params: list[dict] = []
    for market in sorted(market_set):
        if market == "CN":
            preset_source = CN_MULTI_MODEL_PRECOMPUTE_PRESETS
        elif market == "US":
            preset_source = US_MULTI_MODEL_PRECOMPUTE_PRESETS
        else:
            continue
        for preset in preset_source:
            if normalized_keys and str(preset.get("key") or "") not in normalized_keys:
                continue
            params.append(
                {
                    "preset_key": preset["key"],
                    "preset_label": preset["label"],
                    "model_template": preset["templates"][0],
                    "multi_model_templates": list(preset["templates"]),
                    "min_multi_model_hits": int(preset["min_hits"]),
                    "confluence_action_filter": str(preset.get("confluence_action_filter") or "ALL"),
                    "lang": "zh",
                    "universe": "full_market",
                    "market": market,
                    "min_trend_score": 10,
                    "action_filter": "ALL",
                    "min_volume_ratio": 0.0,
                    "min_listing_days": 365,
                    "pe_min": 0.0,
                    "pe_max": 30.0,
                    "min_roe_avg_3y": 12.0,
                    "min_net_profit_yoy": 20.0,
                    "min_revenue_yoy": 0.0,
                    "max_debt_to_assets": 100.0,
                    "min_dividend_yield": 0.0,
                    "exclude_bottom_market_cap_pct": 10.0,
                    "recent_snapshot_runs": 0,
                    "min_snapshot_hits": 0,
                    "model_signal_filter": "ALL",
                    "min_model_signal_strength": 0.0,
                    "execution_tag_filter": "ALL",
                    "exclude_execution_tag_filter": "ALL",
                    "sort_by": str(preset.get("sort_by") or "confluence_rank"),
                    "sort_order": "desc",
                    "limit": 500,
                }
            )
    return params


def _compact_snapshot_rows(rows: list[dict], *, limit: int) -> list[dict]:
    compacted: list[dict] = []
    for row in rows[:limit]:
        compacted.append({key: row.get(key) for key in SNAPSHOT_ROW_FIELDS if key in row})
    return compacted


def _normalize_multi_model_templates(values: object) -> list[str]:
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


def _normalize_action_filter(value: str | None) -> str:
    return str(value or "ALL").strip().lower()


def _action_semantic_buckets(action_label: str | None) -> list[str]:
    normalized = str(action_label or "").strip().lower().replace(" ", "_")
    if not normalized:
        return []
    if normalized == "buy_the_dip":
        return ["buy_the_dip", "bullish_entry"]
    if normalized == "wait_for_breakout":
        return ["breakout_confirmation"]
    if normalized == "pullback":
        return ["buy_the_dip", "bullish_entry"]
    if normalized == "breakout":
        return ["breakout_confirmation", "bullish_entry"]
    if normalized in {"buy", "strong_buy", "technical_pattern", "fundamental_pass"}:
        return ["bullish_entry"]
    if normalized in {"watch", "hold", "hold_and_watch", "wait", "avoid", "avoid_or_wait", "continue_to_watch"}:
        return ["watchlist"]
    return []


def _template_action_semantic_buckets(template_key: str, action_label: str | None) -> list[str]:
    buckets = list(_action_semantic_buckets(action_label))
    if template_key in {"cn_hammer_reversal", "cn_bullish_engulfing_reversal", "cn_macd_underwater_cross"}:
        for bucket in ("buy_the_dip", "bullish_entry"):
            if bucket not in buckets:
                buckets.append(bucket)
    elif template_key in {"cn_volume_breakout", "cn_bullish_ma_stack", "cn_three_white_soldiers", "tv_multi_timeframe_bullish"}:
        for bucket in ("breakout_confirmation", "bullish_entry"):
            if bucket not in buckets:
                buckets.append(bucket)
    elif template_key in {"cn_ma_cluster_breakout_watch", "cn_bollinger_squeeze_watch"}:
        if "breakout_confirmation" not in buckets:
            buckets.append("breakout_confirmation")
    elif template_key in {
        "global_growth_value",
        "global_income_quality",
        "cn_growth_value",
        "cn_high_roe_steady_growth",
        "cn_low_valuation_high_dividend",
    }:
        if "bullish_entry" not in buckets:
            buckets.append("bullish_entry")
    return buckets


def _build_multi_screen_rows_from_snapshots(params: dict) -> tuple[list[dict], dict]:
    template_keys = _normalize_multi_model_templates(params.get("multi_model_templates"))
    if len(template_keys) < 2:
        return [], {"available_templates": [], "missing_templates": []}
    template_rows: dict[str, list[dict]] = {}
    missing_templates: list[str] = []
    available_templates: list[str] = []
    for template_key in template_keys:
        local_params = _build_default_params(
            template_key,
            universe=str(params.get("universe") or "full_market"),
            market=str(params.get("market") or "CN"),
        )
        rows = load_exact_screener_snapshot_rows(local_params)
        if rows is None:
            missing_templates.append(template_key)
            continue
        template_rows[template_key] = rows
        available_templates.append(template_key)
    if not available_templates:
        return [], {"available_templates": [], "missing_templates": missing_templates}

    aggregated: dict[str, dict] = {}
    for template_key in available_templates:
        label = str((MODEL_TEMPLATES.get(template_key) or {}).get("label") or template_key)
        rows = template_rows.get(template_key) or []
        for row in rows:
            ticker = str(row.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            score = float(row.get("snapshot_score") or row.get("trend_score") or 0.0)
            existing = aggregated.get(ticker)
            if existing is None or score > float(existing.get("_best_score") or 0.0):
                base = dict(row)
                base["_best_score"] = score
                base["_template_keys"] = []
                base["_template_labels"] = []
                base["_action_labels"] = []
                base["_confluence_bucket_hits"] = {}
                base["_selection_reasons"] = []
                base["_execution_tags"] = []
                aggregated[ticker] = base
                existing = base
            if existing.get("model_score") is None and row.get("model_score") is not None:
                existing["model_score"] = row.get("model_score")
            if existing.get("model_signal_strength") is None and row.get("model_signal_strength") is not None:
                existing["model_signal_strength"] = row.get("model_signal_strength")
            if existing.get("model_confidence") is None and row.get("model_confidence") is not None:
                existing["model_confidence"] = row.get("model_confidence")
            if existing.get("model_percentile") is None and row.get("model_percentile") is not None:
                existing["model_percentile"] = row.get("model_percentile")
            existing["_template_keys"].append(template_key)
            existing["_template_labels"].append(label)
            existing["_action_labels"].append(str(row.get("action_label") or "").strip())
            row_risk_flags = {str(flag).strip().lower() for flag in (row.get("risk_flags") or []) if str(flag).strip()}
            buckets = _template_action_semantic_buckets(template_key, row.get("action_label"))
            if row_risk_flags.intersection({"rolled-over-after-spike", "do-not-chase"}):
                buckets = [bucket for bucket in buckets if bucket not in {"bullish_entry", "breakout_confirmation", "buy_the_dip"}]
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

    min_hits = max(2, int(params.get("min_multi_model_hits") or 2))
    confluence_action_filter = _normalize_action_filter(params.get("confluence_action_filter"))
    results: list[dict] = []
    for item in aggregated.values():
        template_keys_hit = list(dict.fromkeys(item.pop("_template_keys", [])))
        if len(template_keys_hit) < min_hits:
            continue
        template_labels_hit = list(dict.fromkeys(item.pop("_template_labels", [])))
        action_labels_hit = [label for label in dict.fromkeys(item.pop("_action_labels", [])) if label]
        confluence_bucket_hits = item.pop("_confluence_bucket_hits", {})
        selection_reasons = list(dict.fromkeys(item.pop("_selection_reasons", [])))
        execution_tags = list(dict.fromkeys(item.pop("_execution_tags", [])))
        item.pop("_best_score", None)
        if confluence_action_filter not in {"", "all"}:
            aligned_templates = confluence_bucket_hits.get(confluence_action_filter) or []
            if len(aligned_templates) < min_hits:
                continue
        item["model_hit_count"] = len(template_keys_hit)
        item["snapshot_hits"] = len(template_keys_hit)
        item["snapshot_runs"] = len(template_keys)
        item["matched_model_templates"] = template_keys_hit
        item["matched_model_labels"] = template_labels_hit
        item["matched_patterns"] = template_labels_hit
        item["matched_action_buckets"] = sorted(confluence_bucket_hits.keys())
        item["matched_action_bucket_hits"] = {
            key: len(value or []) for key, value in confluence_bucket_hits.items()
        }
        if confluence_action_filter not in {"", "all"}:
            item["confluence_alignment_count"] = int(item["matched_action_bucket_hits"].get(confluence_action_filter) or 0)
        else:
            item["confluence_alignment_count"] = max(
                [int(value or 0) for value in item["matched_action_bucket_hits"].values()] or [0]
            )
        item["model_execution_tags"] = execution_tags
        item["selection_reason"] = " | ".join(selection_reasons[:3]) if selection_reasons else item.get("selection_reason")
        item["model_summary"] = (
            f"{len(template_labels_hit)} model hits · " + " / ".join(template_labels_hit[:4])
            if template_labels_hit
            else item.get("model_summary")
        )
        item["model_highlights"] = [
            "Matched templates: " + " / ".join(template_labels_hit[:5]),
            "Action mix: " + " / ".join(action_labels_hit[:4]) if action_labels_hit else "",
        ]
        results.append(item)

    sort_by = str(params.get("sort_by", "default"))
    sort_order = str(params.get("sort_order", "desc"))
    reverse = sort_order != "asc"
    if sort_by in {"default", "confluence_rank"}:
        results.sort(
            key=lambda item: (
                int(item.get("model_hit_count") or 0),
                int(item.get("confluence_alignment_count") or 0),
                float(item.get("trade_readiness_score") or 0.0),
                float(item.get("model_signal_strength") or 0.0),
                float(item.get("trend_score") or 0.0),
                str(item.get("ticker") or ""),
            ),
            reverse=reverse,
        )
    else:
        results.sort(
            key=lambda item: (
                int(item.get("model_hit_count") or 0),
                float(item.get("trade_readiness_score") or 0.0),
                str(item.get("ticker") or ""),
            ),
            reverse=reverse,
        )
    return results[: int(params.get("limit", 500) or 500)], {
        "available_templates": available_templates,
        "missing_templates": missing_templates,
    }


def refresh_precomputed_screener_snapshots(
    db: Session,
    *,
    source_job_id: int | None = None,
    markets: list[str] | None = None,
    include_watchlist: bool = True,
    lake_only: bool = False,
    template_keys: list[str] | None = None,
    universes: list[str] | None = None,
) -> dict:
    created: list[dict] = []
    failed: list[dict] = []

    precompute_params = (
        build_lake_precompute_screener_params(markets=markets)
        if lake_only
        else build_precompute_screener_params(markets=markets, include_watchlist=include_watchlist)
    )
    normalized_templates = {
        str(item).strip()
        for item in (template_keys or [])
        if str(item).strip()
    }
    normalized_universes = {
        str(item).strip()
        for item in (universes or [])
        if str(item).strip()
    }
    if normalized_templates:
        precompute_params = [
            params for params in precompute_params if str(params.get("model_template") or "") in normalized_templates
        ]
    if normalized_universes:
        precompute_params = [
            params for params in precompute_params if str(params.get("universe") or "") in normalized_universes
        ]

    for params in precompute_params:
        try:
            rows = _screen_with_lake_preferred(params)
            persisted_rows = _compact_snapshot_rows(rows, limit=int(params.get("limit", 5000)))
            with SessionLocal() as snapshot_db:
                row = WorkspaceSnapshotRepository(snapshot_db).create_snapshot(
                    snapshot_type=screener_snapshot_type(params),
                    snapshot_date=app_now_iso(),
                    payload={
                        "key": screener_snapshot_key(params),
                        "rows": persisted_rows,
                        "updated_at": app_now_iso(),
                        "model_template": params["model_template"],
                        "market": params["market"],
                        "universe": params["universe"],
                        "candidate_stats": {
                            "returned_count": len(rows),
                            "persisted_count": len(persisted_rows),
                            "limit": int(params.get("limit", 5000)),
                        },
                    },
                    source_job_id=source_job_id,
                )
            created.append(
                {
                    "id": row.id,
                    "model_template": params["model_template"],
                    "universe": params["universe"],
                    "market": params["market"],
                    "rows": len(persisted_rows),
                }
            )
        except Exception as exc:
            failed.append(
                {
                    "model_template": params["model_template"],
                    "universe": params["universe"],
                    "market": params["market"],
                    "error": str(exc),
                }
            )

    return {
        "status": "success" if created else "failed",
        "snapshots_created": created,
        "count": len(created),
        "failed_templates": failed,
        "failed_count": len(failed),
    }


def refresh_precomputed_multi_screener_snapshots(
    db: Session,
    *,
    source_job_id: int | None = None,
    markets: list[str] | None = None,
    preset_keys: list[str] | None = None,
) -> dict:
    created: list[dict] = []
    failed: list[dict] = []
    for params in build_multi_model_precompute_params(markets=markets, preset_keys=preset_keys):
        preset_key = str(params.get("preset_key") or "")
        try:
            rows, meta = _build_multi_screen_rows_from_snapshots(params)
            if meta.get("missing_templates"):
                raise RuntimeError(
                    "Missing prerequisite snapshots: " + ", ".join(str(item) for item in meta.get("missing_templates") or [])
                )
            persisted_rows = _compact_snapshot_rows(rows, limit=int(params.get("limit", 500)))
            with SessionLocal() as snapshot_db:
                row = WorkspaceSnapshotRepository(snapshot_db).create_snapshot(
                    snapshot_type=screener_snapshot_type(params),
                    snapshot_date=app_now_iso(),
                    payload={
                        "key": screener_snapshot_key(params),
                        "rows": persisted_rows,
                        "updated_at": app_now_iso(),
                        "preset_key": preset_key,
                        "preset_label": params.get("preset_label"),
                        "market": params["market"],
                        "universe": params["universe"],
                        "multi_model_templates": params.get("multi_model_templates") or [],
                        "meta": meta,
                    },
                    source_job_id=source_job_id,
                )
            created.append(
                {
                    "id": row.id,
                    "preset_key": preset_key,
                    "preset_label": params.get("preset_label"),
                    "market": params["market"],
                    "universe": params["universe"],
                    "rows": len(persisted_rows),
                }
            )
        except Exception as exc:
            failed.append(
                {
                    "preset_key": preset_key,
                    "preset_label": params.get("preset_label"),
                    "market": params.get("market"),
                    "error": str(exc),
                }
            )
    return {
        "status": "success" if created else "failed",
        "snapshots_created": created,
        "count": len(created),
        "failed_presets": failed,
        "failed_count": len(failed),
    }


def _screen_with_lake_preferred(params: dict) -> list[dict]:
    if (
        str(params.get("market") or "").upper() == "CN"
        and str(params.get("universe") or "") == "full_market"
        and str(params.get("model_template") or "") == "technical_momentum"
    ):
        rows = screen_cn_lake_momentum(limit=int(params.get("limit", 160)))
        if rows:
            return rows
    if (
        str(params.get("market") or "").upper() == "US"
        and str(params.get("universe") or "") == "full_market"
        and str(params.get("model_template") or "") == "technical_momentum"
    ):
        rows = screen_us_lake_momentum(limit=int(params.get("limit", 160)))
        if rows:
            return rows
    return ScreenerService().screen(**params)


def _build_limit_up_watch_snapshot_rows(db: Session) -> list[dict]:
    symbol_repo = SymbolRepository(db)
    prediction_repo = PredictionRepository(db)
    technical_service = TechnicalPatternService()
    symbols = [item for item in symbol_repo.list_symbols() if str(item.market or "").upper() == "CN"]
    filtered: list[dict] = []
    for symbol in symbols:
        snapshot = technical_service.evaluate_ticker(symbol.ticker)
        if snapshot is None:
            continue
        matched_patterns = [str(pattern).strip() for pattern in (snapshot.matched_patterns or []) if str(pattern).strip()]
        if "今日涨停" not in matched_patterns:
            continue
        filtered.append(
            {
                "ticker": symbol.ticker,
                "name": symbol.name,
                "market": symbol.market,
                "as_of_date": snapshot.as_of_date,
                "matched_patterns": matched_patterns,
                "volume_breakout": bool(snapshot.volume_breakout),
                "bullish_ma_stack": bool(snapshot.bullish_ma_stack),
            }
        )
    filtered.sort(
        key=lambda item: (
            -int(bool(item.get("volume_breakout"))),
            -int(bool(item.get("bullish_ma_stack"))),
            -len(item.get("matched_patterns") or []),
            item.get("ticker") or "",
        )
    )
    filtered = filtered[:120]
    tickers = [item["ticker"] for item in filtered if item.get("ticker")]
    symbol_map = symbol_repo.list_overviews_for_tickers(tickers)
    model_outputs = prediction_repo.get_latest_model_outputs_for_tickers(tickers)

    rows: list[dict] = []
    for item in filtered:
        ticker = item.get("ticker")
        if not ticker:
            continue
        symbol = symbol_map.get(ticker) or {}
        model_output = model_outputs.get(ticker) or {}
        decision = prediction_repo._build_signal_decision(model_output) if model_output else {}
        matched_patterns = list(item.get("matched_patterns") or [])
        rows.append(
            {
                "ticker": ticker,
                "name": symbol.get("name") or ticker,
                "market": symbol.get("market") or "CN",
                "as_of_date": item.get("as_of_date"),
                "trend_score": None,
                "action_label": "technical_pattern",
                "action_summary": "Matched the selected technical pattern.",
                "latest_close": None,
                "momentum_5": None,
                "momentum_20": None,
                "volume_ratio": None,
                "distance_to_breakout_pct": None,
                "snapshot_hits": 0,
                "snapshot_runs": 0,
                "matched_patterns": matched_patterns,
                "selection_reason": ", ".join(matched_patterns or ["今日涨停"]),
                "model_summary": decision.get("summary_text"),
                "model_highlights": [],
                "model_state": decision.get("action_bucket"),
                "model_confidence": decision.get("confidence"),
                "model_signal_label": decision.get("signal_label"),
                "model_signal_strength": decision.get("signal_strength"),
                "model_conviction_bucket": decision.get("conviction_bucket"),
                "model_position_size_hint": decision.get("position_size_hint"),
                "model_entry_style": decision.get("entry_style"),
                "model_execution_tags": decision.get("execution_tags", []) or [],
                "model_percentile": decision.get("percentile"),
                "model_horizon_days": decision.get("target_horizon_days"),
                "model_reward_risk_ratio": decision.get("model_reward_risk_ratio"),
                "model_expected_drawdown_20d": decision.get("expected_drawdown_20d"),
                "tradability_status": decision.get("tradability_status"),
                "target_weight": decision.get("target_weight"),
                "priority": decision.get("priority"),
                "action_bucket": decision.get("action_bucket"),
                "entry_trigger": decision.get("entry_trigger"),
                "invalidation_condition": decision.get("invalidation_condition"),
                "time_horizon": decision.get("time_horizon"),
                "max_slippage_bps": decision.get("max_slippage_bps"),
                "liquidity_bucket": decision.get("liquidity_bucket"),
                "stop_loss_type": decision.get("stop_loss_type"),
                "execution_note": decision.get("execution_note"),
                "risk_flags": decision.get("risk_flags") or [],
            }
        )
    rows.sort(
        key=lambda item: (
            -(item.get("model_signal_strength") or 0),
            -(item.get("priority") or 0),
            item.get("ticker") or "",
        )
    )
    return rows
