from __future__ import annotations

from urllib.parse import urlencode


def _reason_screen_params(*, reason: str | None, status: str | None, market: str | None, lang: str) -> dict[str, object]:
    normalized_reason = str(reason or "").strip().lower()
    normalized_status = str(status or "").strip().lower()
    market_code = str(market or "CN").strip().upper()
    if market_code not in {"CN", "US", "HK", "ALL"}:
        market_code = "CN"
    cn_core_templates = [
        "lightgbm_top_picks",
        "next_tesla_swing",
        "technical_momentum",
        "cn_volume_breakout",
        "cn_bullish_ma_stack",
    ]
    us_core_templates = [
        "lightgbm_top_picks",
        "next_tesla_swing",
        "technical_momentum",
        "tv_multi_timeframe_bullish",
        "global_growth_value",
    ]
    market_templates = cn_core_templates if market_code == "CN" else us_core_templates
    params: dict[str, object] = {
        "lang": lang,
        "run": 1,
        "model_template": "lightgbm_top_picks" if market_code in {"CN", "US", "ALL"} else "technical_momentum",
        "universe": "full_market",
        "market": market_code,
        "min_trend_score": 0,
        "sort_by": "trade_readiness_score",
        "sort_order": "asc",
    }
    key = normalized_reason or normalized_status
    if key in {"extended_after_sharp_move", "do_not_chase"}:
        params.update(
            {
                "model_template": "next_tesla_swing",
                "multi_model_templates": market_templates[:3],
                "min_multi_model_hits": 2,
                "confluence_action_filter": "breakout_confirmation",
                "sort_by": "momentum_5",
                "sort_order": "desc",
                "action_filter": "wait_for_breakout",
            }
        )
    elif key == "too_far_from_pullback_zone":
        params.update(
            {
                "model_template": "next_tesla_swing",
                "multi_model_templates": market_templates[:3],
                "min_multi_model_hits": 2,
                "confluence_action_filter": "buy_the_dip",
                "sort_by": "trade_readiness_score",
                "sort_order": "asc",
                "action_filter": "buy_the_dip",
            }
        )
    elif key == "signal_not_actionable":
        params.update(
            {
                "model_template": "technical_momentum",
                "multi_model_templates": market_templates[:3],
                "min_multi_model_hits": 2,
                "confluence_action_filter": "watchlist",
                "sort_by": "model_signal_strength",
                "sort_order": "asc",
                "action_filter": "hold_and_watch",
            }
        )
    elif key == "missing_latest_price":
        params.update(
            {
                "sort_by": "trade_readiness_score",
                "sort_order": "asc",
            }
        )
    elif key == "too_many_risk_flags":
        params.update(
            {
                "model_template": "lightgbm_top_picks",
                "multi_model_templates": market_templates,
                "min_multi_model_hits": 2,
                "confluence_action_filter": "ALL",
                "sort_by": "trade_readiness_score",
                "sort_order": "asc",
            }
        )
    elif key == "low_trade_readiness":
        params.update(
            {
                "model_template": "lightgbm_top_picks",
                "multi_model_templates": market_templates[:3],
                "min_multi_model_hits": 2,
                "confluence_action_filter": "ALL",
                "sort_by": "trade_readiness_score",
                "sort_order": "asc",
            }
        )
    return params


def reason_screen_href(*, reason: str | None, status: str | None, market: str | None, lang: str) -> str:
    return "/screeners?" + urlencode(
        _reason_screen_params(reason=reason, status=status, market=market, lang=lang),
        doseq=True,
    )
