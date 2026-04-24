from __future__ import annotations

import json

from app.services.runtime_cache import get_or_set
from app.services.technical_patterns import TechnicalPatternService
from app.services.tradingview_client import TradingViewClient


def build_combined_analysis_payload(
    *,
    overview: dict,
    latest_signal: dict | None,
    technical_rating: dict | None,
    multi_timeframe: dict | None,
    bollinger: dict | None,
    candlestick: dict | None,
) -> dict:
    score = 0
    reasons: list[str] = []

    daily_reco = str((technical_rating or {}).get("recommendation") or "").upper()
    if daily_reco in {"BUY", "STRONG_BUY"}:
        score += 2
        reasons.append(f"daily {daily_reco}")
    elif daily_reco in {"SELL", "STRONG_SELL"}:
        score -= 2
        reasons.append(f"daily {daily_reco}")

    alignment = str((multi_timeframe or {}).get("alignment") or "").lower()
    if alignment == "bullish_alignment":
        score += 3
        reasons.append("multi-timeframe aligned")
    elif alignment == "bullish_bias":
        score += 1
        reasons.append("multi-timeframe bullish bias")
    elif alignment == "bearish_alignment":
        score -= 3
        reasons.append("multi-timeframe bearish")
    elif alignment == "bearish_bias":
        score -= 1
        reasons.append("multi-timeframe weak")

    bollinger_signal = str((bollinger or {}).get("signal") or "").lower()
    if bollinger_signal in {"upper_band_strength", "bullish_bias"}:
        score += 1
        reasons.append(f"bollinger {bollinger_signal}")
    elif bollinger_signal in {"lower_band_pressure", "bearish_bias"}:
        score -= 1
        reasons.append(f"bollinger {bollinger_signal}")
    if (bollinger or {}).get("squeeze"):
        score += 1
        reasons.append("bollinger squeeze")

    matched_patterns = list((candlestick or {}).get("patterns") or [])
    if matched_patterns:
        score += min(len(matched_patterns), 2)
        reasons.append(" / ".join(matched_patterns[:2]))

    latest_score = latest_signal.get("score") if latest_signal else None
    if latest_score is not None:
        numeric_score = float(latest_score)
        if numeric_score >= 0.6:
            score += 2
            reasons.append("model signal strong")
        elif numeric_score <= 0.4:
            score -= 1
            reasons.append("model signal weak")

    decision = "HOLD"
    if score >= 5:
        decision = "STRONG BUY"
    elif score >= 3:
        decision = "BUY"
    elif score <= -4:
        decision = "STRONG SELL"
    elif score <= -2:
        decision = "SELL"

    confidence = min(100, max(35, 50 + abs(score) * 8))
    return {
        "ticker": overview["ticker"],
        "status": "success",
        "decision": decision,
        "confidence": confidence,
        "score": score,
        "reasons": reasons,
        "technical_rating": technical_rating,
        "multi_timeframe": multi_timeframe,
        "bollinger_band": bollinger,
        "candlestick_patterns": candlestick,
    }


def safe_symbol_analysis(overview: dict, latest_signal: dict | None) -> dict:
    signal_key = None
    if latest_signal:
        signal_key = {
            "trade_date": latest_signal.get("trade_date"),
            "score": latest_signal.get("score"),
            "rank_value": latest_signal.get("rank_value"),
        }
    cache_key = json.dumps(
        {
            "ticker": overview.get("ticker"),
            "market": overview.get("market"),
            "exchange": overview.get("exchange"),
            "signal": signal_key,
        },
        sort_keys=True,
        ensure_ascii=False,
    )

    def _load() -> dict:
        tradingview = TradingViewClient()
        patterns = TechnicalPatternService()
        technical_rating = tradingview.get_technical_rating(
            ticker=overview["ticker"],
            market=overview.get("market"),
            exchange=overview.get("exchange"),
            interval="1d",
        )
        multi_timeframe = tradingview.get_multi_timeframe_analysis(
            ticker=overview["ticker"],
            market=overview.get("market"),
            exchange=overview.get("exchange"),
        )
        bollinger = patterns.get_bollinger_band_analysis(overview["ticker"])
        candlestick = patterns.get_candlestick_patterns(overview["ticker"])
        return build_combined_analysis_payload(
            overview=overview,
            latest_signal=latest_signal,
            technical_rating=technical_rating,
            multi_timeframe=multi_timeframe,
            bollinger=bollinger,
            candlestick=candlestick,
        )

    return get_or_set("safe_symbol_analysis", cache_key, ttl_seconds=90.0, loader=_load)
