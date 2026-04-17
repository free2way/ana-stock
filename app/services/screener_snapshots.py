from __future__ import annotations

import hashlib
import json

from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.services.repository import (
    PredictionRepository,
    SymbolRepository,
    TechnicalSnapshotRepository,
    WorkspaceSnapshotRepository,
)
from app.services.market_lake import screen_cn_lake_momentum, screen_us_lake_momentum
from app.services.screener import MODEL_TEMPLATES, ScreenerService
from app.services.time_utils import app_now_iso


SCREENER_SNAPSHOT_TYPE_PREFIX = "screener_result:"

WATCHLIST_PRECOMPUTE_TEMPLATES = [
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
]

FULL_MARKET_US_PRECOMPUTE_TEMPLATES = [
    "next_tesla_swing",
    "technical_momentum",
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


def _build_default_params(template_key: str, *, universe: str, market: str | None = None) -> dict:
    template = MODEL_TEMPLATES.get(template_key, {})
    defaults = template.get("defaults") or {}
    template_market = str(market or template.get("market") or "ALL").upper()
    return {
        "model_template": template_key,
        "universe": universe,
        "market": template_market,
        "min_trend_score": int(defaults.get("min_trend_score", 60)),
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
        "limit": 300 if universe == "watchlist" else 160,
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
    if "US" in market_set:
        for template_key in FULL_MARKET_US_PRECOMPUTE_TEMPLATES:
            params.append(_build_default_params(template_key, universe="full_market", market="US"))
    return params


def build_lake_precompute_screener_params(*, markets: list[str] | None = None) -> list[dict]:
    market_set = {str(item).strip().upper() for item in (markets or ["CN", "US"]) if str(item).strip()}
    params: list[dict] = []
    for market in sorted(market_set):
        if market in {"CN", "US"}:
            params.append(_build_default_params("next_tesla_swing", universe="full_market", market=market))
            params.append(_build_default_params("technical_momentum", universe="full_market", market=market))
    return params


def _compact_snapshot_rows(rows: list[dict], *, limit: int) -> list[dict]:
    compacted: list[dict] = []
    for row in rows[:limit]:
        compacted.append({key: row.get(key) for key in SNAPSHOT_ROW_FIELDS if key in row})
    return compacted


def refresh_precomputed_screener_snapshots(
    db: Session,
    *,
    source_job_id: int | None = None,
    markets: list[str] | None = None,
    include_watchlist: bool = True,
    lake_only: bool = False,
) -> dict:
    created: list[dict] = []
    failed: list[dict] = []

    precompute_params = (
        build_lake_precompute_screener_params(markets=markets)
        if lake_only
        else build_precompute_screener_params(markets=markets, include_watchlist=include_watchlist)
    )

    for params in precompute_params:
        try:
            rows = _screen_with_lake_preferred(params)
            persisted_rows = _compact_snapshot_rows(rows, limit=int(params.get("limit", 160)))
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


def _screen_with_lake_preferred(params: dict) -> list[dict]:
    if (
        str(params.get("market") or "").upper() == "CN"
        and str(params.get("universe") or "") == "full_market"
        and str(params.get("model_template") or "") in {"technical_momentum", "next_tesla_swing"}
    ):
        rows = screen_cn_lake_momentum(limit=int(params.get("limit", 160)))
        if rows:
            return rows
    if (
        str(params.get("market") or "").upper() == "US"
        and str(params.get("universe") or "") == "full_market"
        and str(params.get("model_template") or "") in {"technical_momentum", "next_tesla_swing"}
    ):
        rows = screen_us_lake_momentum(limit=int(params.get("limit", 160)))
        if rows:
            return rows
    return ScreenerService().screen(**params)


def _build_limit_up_watch_snapshot_rows(db: Session) -> list[dict]:
    snapshot_repo = TechnicalSnapshotRepository(db)
    symbol_repo = SymbolRepository(db)
    prediction_repo = PredictionRepository(db)

    snapshots = snapshot_repo.list_latest_for_market(market="CN")
    filtered = [item for item in snapshots if item.get("limit_up_yesterday")]
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
                "selection_reason": ", ".join(matched_patterns or ["昨日涨停"]),
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
