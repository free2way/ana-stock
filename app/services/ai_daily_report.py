from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from math import isnan

from app.core.db import SessionLocal
from app.services.ai_analysis import AIAnalysisService
from app.services.market_context import load_market_context_snapshot
from app.services.market_lake import get_latest_lake_trade_date
from app.services.portfolio_book import load_portfolio_positions
from app.services.portfolio_intelligence import build_portfolio_ai_summary, build_position_management_fields
from app.services.price_snapshot import load_latest_closes
from app.services.recommendation_regression import load_or_build_recommendation_regression
from app.services.repository import AppSettingRepository, PredictionRepository, SymbolRepository, WatchlistRepository, WorkspaceSnapshotRepository
from app.services.model_selection_guidance import load_model_selection_guidance_snapshot, summarize_model_selection_guidance
from app.services.screener_snapshots import (
    build_base_precompute_params,
    build_multi_model_precompute_params,
    screener_snapshot_type,
)
from app.services.social_signals import social_signal_summary
from app.services.template_evaluation import (
    build_lightgbm_prediction_evaluation,
    build_pattern_template_evaluation,
    resolve_template_group_label,
)
from app.services.time_utils import app_now_iso, app_today_iso
from app.services.tradability_filter import evaluate_candidate_tradability


AI_DAILY_REPORT_KEY = "ai_daily_report"
AI_DAILY_REPORT_SNAPSHOT_TYPE = "ai_daily_report_history"
MARKET_HEATMAP_SNAPSHOT_TYPE = "market_heatmap_workspace"
DEFAULT_AI_DAILY_REPORT_MARKETS = ["CN"]
ACTIONABLE_MAX_BUY_ZONE_DEVIATION_PCT = 8.0
BUY_THE_DIP_LIMIT = 10
FULL_MARKET_REPORT_TEMPLATES = [
    "lightgbm_top_picks",
    "technical_momentum",
    "cn_bollinger_squeeze_watch",
    "cn_three_white_soldiers",
    "cn_volume_breakout",
]
US_HOTSPOT_TEMPLATES = [
    "next_tesla_swing",
    "technical_momentum",
    "global_growth_value",
    "global_income_quality",
]
RECOMMENDATION_MIN_READINESS = 55.0
RECOMMENDATION_MAX_RISK_FLAGS = 2
RECOMMENDATION_CHASE_MOMENTUM_5 = 18.0
RECOMMENDATION_MAX_PULLBACK_DISTANCE = 8.0


def _reason_label_map(lang: str) -> dict[str, str]:
    if lang == "zh":
        return {
            "signal_not_actionable": "模型信号还不具备可执行性",
            "missing_latest_price": "缺少最新行情，暂时不能推荐",
            "low_trade_readiness": "交易就绪度偏低，先观察等待",
            "too_many_risk_flags": "风险标记过多，先不纳入推荐",
            "extended_after_sharp_move": "短线涨幅过大，避免追高",
            "do_not_chase": "短线涨幅过大，避免追高",
            "too_far_from_pullback_zone": "已经偏离理想回踩区，买点不划算",
            "model_score": "模型分在加分",
            "signal_strength": "信号强度在加分",
            "trend": "趋势结构在加分",
            "reward_risk": "盈亏比在加分",
            "model_confluence": "多模型共振在加分",
            "missing_price": "缺少价格信息",
            "drawdown_risk": "回撤风险偏高",
            "chase_risk": "追高风险偏高",
            "far_from_trigger": "距离理想触发位偏远",
            "weak_market_breakout": "市场偏弱，突破型机会先不要硬追",
            "weak_market": "当前市场环境偏弱",
            "weak_breadth": "市场上涨广度偏弱",
            "broad_participation": "市场参与度较好",
            "market_tailwind": "市场环境有顺风",
            "crowded_theme": "题材过于拥挤，容易后排接力",
            "confirmation_needed": "市场还在确认阶段，先等二次确认",
            "portfolio_risk_budget": "当前组合风险预算偏紧",
            "balanced_setup": "形态中性，继续观察",
            "rolled_over_after_spike": "冲高后转弱，先按观察处理",
        }
    return {
        "signal_not_actionable": "Model signal is not actionable yet",
        "missing_latest_price": "Latest price is missing, so the idea is blocked",
        "low_trade_readiness": "Trade readiness is too low; keep it on watch",
        "too_many_risk_flags": "Too many risk flags; keep it out of top picks",
        "extended_after_sharp_move": "The move is already extended; do not chase",
        "do_not_chase": "The move is already extended; do not chase",
        "too_far_from_pullback_zone": "Price is too far from the preferred pullback zone",
        "model_score": "Model score is supportive",
        "signal_strength": "Signal strength is supportive",
        "trend": "Trend structure is supportive",
        "reward_risk": "Reward/risk is supportive",
        "model_confluence": "Multi-model confluence is supportive",
        "missing_price": "Missing price context",
        "drawdown_risk": "Drawdown risk is elevated",
        "chase_risk": "Chasing risk is elevated",
        "far_from_trigger": "Price is too far from the ideal trigger",
        "weak_market_breakout": "The tape is weak, so breakout trades should wait",
        "weak_market": "The market backdrop is weak",
        "weak_breadth": "Market breadth is weak",
        "broad_participation": "Participation is broad",
        "market_tailwind": "The market backdrop is supportive",
        "crowded_theme": "Theme crowding is elevated",
        "confirmation_needed": "The tape still needs confirmation",
        "portfolio_risk_budget": "Portfolio risk budget is already tight",
        "balanced_setup": "Balanced setup; keep monitoring",
        "rolled_over_after_spike": "Momentum faded after the spike; keep it on watch",
    }


def format_risk_flags(flags: list[str] | tuple[str, ...] | set[str] | None, *, lang: str = "zh") -> str:
    if not flags:
        return "-"
    label_map = {
        "zh": {
            "drawdown-risk": "回撤风险",
            "low-conviction": "低置信度",
            "weak-signal-strength": "信号偏弱",
            "missing-model-score": "缺少模型分",
            "do-not-chase": "不要追高",
            "rolled-over-after-spike": "冲高后转弱",
            "chase-risk": "追高风险",
            "far-from-trigger": "偏离触发位",
        },
        "en": {
            "drawdown-risk": "Drawdown risk",
            "low-conviction": "Low conviction",
            "weak-signal-strength": "Weak signal",
            "missing-model-score": "Missing model score",
            "do-not-chase": "Do not chase",
            "rolled-over-after-spike": "Rolled over after spike",
            "chase-risk": "Chasing risk",
            "far-from-trigger": "Far from trigger",
        },
    }
    localized = label_map.get(lang, label_map["en"])
    values = [localized.get(str(flag).strip(), str(flag).strip().replace("_", " ").replace("-", " ")) for flag in flags if str(flag).strip()]
    if not values:
        return "-"
    return "，".join(values) if lang == "zh" else ", ".join(values)


def format_trade_gate_reason(reason: str | None, *, lang: str = "zh") -> str:
    value = str(reason or "").strip()
    if not value:
        return "-"
    labels = _reason_label_map(lang)
    if "," in value:
        parts = [labels.get(part.strip(), part.strip().replace("_", " ")) for part in value.split(",") if part.strip()]
        return "，".join(parts[:4]) if lang == "zh" else ", ".join(parts[:4])
    return labels.get(value, value.replace("_", " "))


def format_trade_status(status: str | None, *, lang: str = "zh") -> str:
    normalized = str(status or "").strip().upper()
    if lang == "zh":
        return {
            "READY": "可执行",
            "REVIEW": "待复核",
            "DEFER": "等更好买点",
            "BLOCKED": "禁止交易",
            "DO_NOT_CHASE": "不要追高",
        }.get(normalized, normalized or "-")
    return {
        "READY": "Ready",
        "REVIEW": "Review",
        "DEFER": "Wait",
        "BLOCKED": "Blocked",
        "DO_NOT_CHASE": "Do Not Chase",
    }.get(normalized, normalized or "-")


def build_trade_explain_text(candidate: dict, *, lang: str = "zh") -> str:
    status = str(candidate.get("tradability_status") or "").strip().upper()
    block_reason = str(candidate.get("block_reason") or "").strip()
    readiness_reason = str(candidate.get("readiness_reason") or "").strip()
    if status in {"BLOCKED", "DO_NOT_CHASE"} or block_reason:
        return format_trade_gate_reason(block_reason or status.lower(), lang=lang)
    if readiness_reason:
        return format_trade_gate_reason(readiness_reason, lang=lang)
    return "形态通过，可以继续跟踪" if lang == "zh" else "Setup is acceptable; keep tracking it"


def build_trade_summary_text(candidate: dict, *, lang: str = "zh", include_execution_note: bool = False) -> str:
    status_text = format_trade_status(candidate.get("tradability_status"), lang=lang)
    readiness_score = candidate.get("trade_readiness_score")
    readiness_bucket = candidate.get("readiness_bucket") or "-"
    parts = [
        f"{'可交易性' if lang == 'zh' else 'Tradability'}：{status_text}",
        f"{'就绪度' if lang == 'zh' else 'Readiness'}：{readiness_score or '-'} / {readiness_bucket}",
        f"{'原因' if lang == 'zh' else 'Reason'}：{build_trade_explain_text(candidate, lang=lang)}",
    ]
    if include_execution_note:
        parts.append(f"{'执行备注' if lang == 'zh' else 'Execution'}：{candidate.get('execution_note') or '-'}")
    separator = " | " if lang == "zh" else " | "
    return separator.join(parts)


def build_close_review_action_feed(report: dict | None, *, lang: str = "zh", limit: int = 5) -> dict:
    payload = report or {}
    rows = list(payload.get("rows") or [])
    buy_the_dip_rows = list(payload.get("buy_the_dip_rows") or [])

    actionable: list[dict] = []
    blocked: list[dict] = []
    risk_reduction: list[dict] = []

    for item in rows:
        tradability = str(item.get("tradability_status") or "").upper()
        verdict = str(item.get("verdict") or "").upper()
        enriched = {
            "ticker": item.get("ticker"),
            "name": item.get("name") or item.get("ticker"),
            "verdict": item.get("verdict") or "-",
            "strategy": item.get("strategy") or "-",
            "tradability_status": tradability or "-",
            "trade_readiness_score": item.get("trade_readiness_score"),
            "readiness_bucket": item.get("readiness_bucket"),
            "latest_close": item.get("latest_close"),
            "latest_price": item.get("latest_price"),
            "target_weight": item.get("target_weight"),
            "entry_trigger": item.get("entry_trigger") or "-",
            "invalidation_condition": item.get("invalidation_condition") or "-",
            "execution_note": item.get("execution_note") or "-",
            "risk_flags": item.get("risk_flags") or [],
            "time_horizon": item.get("time_horizon") or "-",
            "liquidity_bucket": item.get("liquidity_bucket") or "-",
            "max_slippage_bps": item.get("max_slippage_bps"),
            "quant_rank": float(item.get("quant_rank") or 0.0),
        }
        gate = _recommendation_gate(item)
        if not gate["allowed"]:
            enriched["tradability_status"] = gate["status"]
            enriched["block_reason"] = gate["reason"]
            blocked.append(enriched)
        elif verdict in {"SELL", "STRONG SELL"} or tradability in {"REVIEW", "DEFER"}:
            risk_reduction.append(enriched)
        else:
            actionable.append(enriched)

    for item in buy_the_dip_rows:
        actionable.append(
            {
                "ticker": item.get("ticker"),
                "name": item.get("name") or item.get("ticker"),
                "verdict": item.get("verdict") or ("BUY" if lang == "en" else "买入"),
                "strategy": item.get("strategy") or ("Buy The Dip"),
                "tradability_status": item.get("tradability_status") or "-",
                "trade_readiness_score": item.get("trade_readiness_score"),
                "readiness_bucket": item.get("readiness_bucket"),
                "latest_close": item.get("latest_close"),
                "latest_price": item.get("latest_price"),
                "target_weight": item.get("target_weight"),
                "entry_trigger": item.get("entry_trigger") or "-",
                "invalidation_condition": item.get("invalidation_condition") or "-",
                "execution_note": item.get("execution_plan") or item.get("execution_note") or "-",
                "risk_flags": item.get("risk_flags") or [],
                "time_horizon": item.get("time_horizon") or "-",
                "liquidity_bucket": item.get("liquidity_bucket") or "-",
                "max_slippage_bps": item.get("max_slippage_bps"),
                "quant_rank": float(item.get("quant_rank") or 0.0),
            }
        )

    filtered_actionable: list[dict] = []
    for item in actionable:
        gate = _recommendation_gate(item)
        if gate["allowed"]:
            filtered_actionable.append(item)
        else:
            item["tradability_status"] = gate["status"]
            item["block_reason"] = gate["reason"]
            blocked.append(item)
    actionable = filtered_actionable

    actionable.sort(key=lambda item: (-float(item.get("quant_rank") or 0.0), item.get("ticker") or ""))
    blocked.sort(key=lambda item: (-float(item.get("quant_rank") or 0.0), item.get("ticker") or ""))
    risk_reduction.sort(key=lambda item: (-float(item.get("quant_rank") or 0.0), item.get("ticker") or ""))

    if lang == "zh":
        summary = (
            f"可执行 {len(actionable)} / 减风险 {len(risk_reduction)} / 受阻 {len(blocked)}"
            if actionable or risk_reduction or blocked
            else "暂无结构化复盘动作"
        )
    else:
        summary = (
            f"Ready {len(actionable)} / Reduce Risk {len(risk_reduction)} / Blocked {len(blocked)}"
            if actionable or risk_reduction or blocked
            else "No structured close-review actions yet"
        )

    return {
        "mood": payload.get("mood") or "-",
        "headline": payload.get("headline") or "-",
        "summary": summary,
        "actionable": actionable[:limit],
        "risk_reduction": risk_reduction[:limit],
        "blocked": blocked[:limit],
    }


def _entry_style_value(candidate: dict) -> str:
    for key in ("preferred_entry_style", "entry_style", "model_entry_style", "action_label", "setup_label"):
        value = str(candidate.get(key) or "").strip().lower()
        if value:
            return value
    return ""


def _candidate_latest_price(candidate: dict) -> float:
    return _safe_float(
        candidate.get("latest_close")
        or candidate.get("latest_price")
        or candidate.get("close")
        or candidate.get("price")
    )


def _candidate_risk_flags(candidate: dict) -> list[str]:
    raw = candidate.get("risk_flags") or candidate.get("model_execution_tags") or []
    if isinstance(raw, (list, tuple, set)):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw or "").strip()
    if not text:
        return []
    return [item.strip() for item in text.replace(";", ",").split(",") if item.strip()]


def _candidate_board_profile(candidate: dict) -> str:
    ticker = str(candidate.get("ticker") or "").strip().upper()
    name = str(candidate.get("name") or "").strip().upper().replace(" ", "")
    limit_band = _safe_float(candidate.get("limit_band_pct"))
    code = ticker.split(".", 1)[0]
    if name.startswith(("ST", "*ST", "S*ST", "PT")) or limit_band <= 5.5:
        return "st"
    if limit_band >= 29.0 or ticker.endswith(".BJ") or code.startswith(("4", "8")):
        return "bse"
    if code.startswith(("688", "689")):
        return "star"
    if code.startswith(("300", "301")):
        return "chinext"
    return "main"


def _recommendation_gate_config(candidate: dict) -> dict[str, float | int | str | None]:
    market_context = candidate.get("market_context") if isinstance(candidate.get("market_context"), dict) else {}
    regime = str(
        (market_context or {}).get("regime_label")
        or (market_context or {}).get("market_regime")
        or (market_context or {}).get("risk_regime")
        or ""
    ).lower()
    breadth = _safe_float(
        (market_context or {}).get("up_ratio")
        or (market_context or {}).get("advance_decline_ratio")
        or (market_context or {}).get("breadth_score")
    )
    limit_band = _safe_float(candidate.get("limit_band_pct"))
    entry_style = _entry_style_value(candidate)
    board_profile = _candidate_board_profile(candidate)
    crowded_theme = bool((market_context or {}).get("crowded_theme"))
    breakout_tailwind = bool((market_context or {}).get("breakout_tailwind"))

    min_readiness = RECOMMENDATION_MIN_READINESS
    max_risk_flags = RECOMMENDATION_MAX_RISK_FLAGS
    chase_momentum_5 = RECOMMENDATION_CHASE_MOMENTUM_5
    max_pullback_distance = RECOMMENDATION_MAX_PULLBACK_DISTANCE

    strong_market = any(token in regime for token in ("bull", "risk_on", "strong", "trend", "强"))
    weak_market = any(token in regime for token in ("weak", "bear", "risk_off", "cautious", "defensive", "弱", "谨慎"))
    if breadth >= 65:
        strong_market = True
    elif 0 < breadth <= 42:
        weak_market = True

    if strong_market:
        min_readiness -= 4.0
        chase_momentum_5 += 4.0
        max_pullback_distance += 2.0
    if weak_market:
        min_readiness += 6.0
        chase_momentum_5 -= 3.0
        max_risk_flags = max(1, max_risk_flags - 1)
        if entry_style in {"breakout", "breakout_confirmation", "momentum_breakout"}:
            min_readiness += 3.0

    if breakout_tailwind and not weak_market and entry_style in {"breakout", "breakout_confirmation", "momentum_breakout"}:
        min_readiness -= 2.0
        chase_momentum_5 += 2.0
    if crowded_theme:
        max_risk_flags = max(1, max_risk_flags - 1)
        if entry_style in {"breakout", "breakout_confirmation", "momentum_breakout"}:
            min_readiness += 2.0
            chase_momentum_5 -= 2.0

    if board_profile in {"chinext", "star"}:
        chase_momentum_5 += 3.0 if strong_market else 1.0
        max_pullback_distance += 1.0
        if weak_market:
            min_readiness += 2.0
    elif board_profile == "bse":
        chase_momentum_5 += 5.0 if strong_market else 2.0
        max_pullback_distance += 2.0
        max_risk_flags = min(max_risk_flags, 1 if weak_market else 2)
        if weak_market:
            min_readiness += 4.0
    elif board_profile == "st":
        min_readiness += 8.0
        max_risk_flags = 1
        chase_momentum_5 -= 6.0
    elif limit_band >= 19.0:
        chase_momentum_5 += 2.0
        max_pullback_distance += 1.0

    return {
        "min_readiness": round(min_readiness, 1),
        "max_risk_flags": int(max_risk_flags),
        "chase_momentum_5": round(chase_momentum_5, 1),
        "max_pullback_distance": round(max_pullback_distance, 1),
        "regime": regime or None,
        "breadth": round(breadth, 1) if breadth else None,
        "limit_band_pct": round(limit_band, 1) if limit_band else None,
        "board_profile": board_profile,
        "crowded_theme": crowded_theme,
        "breakout_tailwind": breakout_tailwind,
    }


def _recommendation_gate(candidate: dict) -> dict[str, object]:
    config = _recommendation_gate_config(candidate)
    tradability = str(candidate.get("tradability_status") or "").upper()
    readiness = _safe_float(candidate.get("trade_readiness_score"))
    readiness_bucket = str(candidate.get("readiness_bucket") or "").upper()
    risk_flags = _candidate_risk_flags(candidate)
    entry_style = _entry_style_value(candidate)
    latest_price = _candidate_latest_price(candidate)
    momentum_5 = _safe_float(candidate.get("momentum_5"))
    distance_to_breakout = _safe_float(candidate.get("distance_to_breakout_pct"))

    if tradability == "BLOCKED":
        return {"allowed": False, "status": "BLOCKED", "reason": str(candidate.get("block_reason") or "signal_not_actionable"), "config": config}
    if latest_price <= 0:
        return {"allowed": False, "status": "BLOCKED", "reason": "missing_latest_price", "config": config}
    if "missing-latest-price" in risk_flags:
        return {"allowed": False, "status": "BLOCKED", "reason": "missing_latest_price", "config": config}
    if readiness_bucket in {"LOW", "BLOCKED"} or (readiness and readiness < float(config["min_readiness"] or RECOMMENDATION_MIN_READINESS)):
        return {"allowed": False, "status": "BLOCKED", "reason": "low_trade_readiness", "config": config}
    if len(risk_flags) > int(config["max_risk_flags"] or RECOMMENDATION_MAX_RISK_FLAGS):
        return {"allowed": False, "status": "BLOCKED", "reason": "too_many_risk_flags", "config": config}
    if momentum_5 >= float(config["chase_momentum_5"] or RECOMMENDATION_CHASE_MOMENTUM_5) and entry_style not in {"pullback", "buy_the_dip", "support_hold", "pullback_reentry"}:
        return {"allowed": False, "status": "DO_NOT_CHASE", "reason": "extended_after_sharp_move", "config": config}
    if (
        distance_to_breakout >= float(config["max_pullback_distance"] or RECOMMENDATION_MAX_PULLBACK_DISTANCE)
        and entry_style in {"pullback", "buy_the_dip", "support_hold", "pullback_reentry"}
    ):
        return {"allowed": False, "status": "BLOCKED", "reason": "too_far_from_pullback_zone", "config": config}
    return {"allowed": True, "status": tradability or "READY", "reason": "", "config": config}


def build_ai_daily_report(*, limit: int = 8, tickers: list[str] | None = None, markets: list[str] | None = None) -> dict:
    with SessionLocal() as db:
        watchlist_repo = WatchlistRepository(db)
        symbol_repo = SymbolRepository(db)
        prediction_repo = PredictionRepository(db)
        effective_markets = markets if markets is not None else DEFAULT_AI_DAILY_REPORT_MARKETS
        normalized_markets = {str(item).strip().upper() for item in (effective_markets or []) if str(item).strip()}
        service = AIAnalysisService()
        portfolio_rows, portfolio_summary = _build_portfolio_report_rows(
            db=db,
            symbol_repo=symbol_repo,
            prediction_repo=prediction_repo,
        )
        social_summary = social_signal_summary(db)
        us_hotspot_validation = _build_us_hotspot_validation(db=db, social_summary=social_summary)

        market = next(iter(normalized_markets), "CN")
        market_report_date = get_latest_lake_trade_date(market=market)
        recommendation_limit = 5
        evaluation_limit = max(recommendation_limit * 4, 20)
        excluded_tickers = _load_owned_or_watched_tickers(watchlist_repo)
        rows, market_recommendation_meta = _build_market_recommendation_rows(
            db=db,
            symbol_repo=symbol_repo,
            prediction_repo=prediction_repo,
            market=market,
            excluded_tickers=excluded_tickers,
            recommendation_limit=evaluation_limit,
            prefer_snapshot=True,
        )
        us_model_rows, us_market_recommendation_meta = _build_market_recommendation_rows(
            db=db,
            symbol_repo=symbol_repo,
            prediction_repo=prediction_repo,
            market="US",
            excluded_tickers=excluded_tickers,
            recommendation_limit=evaluation_limit,
            prefer_snapshot=False,
        )
        us_report_date = get_latest_lake_trade_date(market="US")
        market_heatmap_snapshot = (
            WorkspaceSnapshotRepository(db).get_latest_snapshot(MARKET_HEATMAP_SNAPSHOT_TYPE)
            if market == "CN"
            else None
        )
        model_selection_guidance = load_model_selection_guidance_snapshot(db, market=market, allow_fallback=True)
        model_selection_guidance_summary = summarize_model_selection_guidance(model_selection_guidance, lang="zh")
        recommendation_regression = load_or_build_recommendation_regression(db=db)
        market_structure_rows = (
            _load_full_market_report_candidates(
                db=db,
                market=market,
                excluded_tickers=set(),
                limit=max(recommendation_limit * 24, 120),
            )
            if market == "CN"
            else rows
        )
        _hydrate_security_names(
            symbol_repo,
            portfolio_rows,
            rows,
            us_model_rows,
            social_summary.get("actionable") or [],
            us_hotspot_validation,
            market_structure_rows or [],
        )

    buy_the_dip_rows = _build_buy_the_dip_rows(rows=rows, markets=sorted(normalized_markets) if normalized_markets else ["CN"])
    bullish = sum(1 for row in rows if str(row.get("verdict") or "").upper() in {"BUY", "STRONG BUY"})
    cautious = sum(1 for row in rows if str(row.get("verdict") or "").upper() in {"SELL", "STRONG SELL"})
    mood = "均衡观察"
    if bullish >= max(2, len(rows) // 2):
        mood = "偏进攻"
    elif cautious >= max(2, len(rows) // 3):
        mood = "偏防守"
    strategy = _build_market_strategy(rows=rows, mood=mood)
    market_structure = _build_market_structure(
        rows=market_structure_rows or rows,
        market=market,
        lang="zh",
        heatmap_payload=(market_heatmap_snapshot or {}).get("payload") if isinstance(market_heatmap_snapshot, dict) else None,
    )
    market_template_attribution = _build_market_template_attribution(rows=rows, market=market, lang="zh")
    us_market_structure = _build_market_structure(rows=us_model_rows, market="US", lang="zh")
    lightgbm_execution_bias = _build_lightgbm_execution_bias(lang="zh")
    actionable_rows, watch_rows, report_pool_meta = _split_market_recommendation_rows(
        rows,
        actionable_limit=recommendation_limit,
        watch_limit=recommendation_limit,
        regression_policy=(recommendation_regression or {}).get("policy") or {},
        lightgbm_execution_bias=lightgbm_execution_bias,
    )
    with SessionLocal() as db:
        symbol_repo = SymbolRepository(db)
        _hydrate_security_names(
            symbol_repo,
            actionable_rows,
            watch_rows,
            buy_the_dip_rows,
        )
    market_recommendation_meta.update(report_pool_meta)
    market_recommendation_meta["note"] = (
        f"{market_recommendation_meta.get('note') or ''} "
        f"可执行买入池 {report_pool_meta.get('actionable_count') or 0} 只，强势观察池 {report_pool_meta.get('watch_count') or 0} 只。"
    ).strip()
    report_date = (
        str(market_recommendation_meta.get("target_snapshot_date") or "").strip()
        or str(market_report_date or "").strip()
        or str(us_market_recommendation_meta.get("target_snapshot_date") or "").strip()
        or str(us_report_date or "").strip()
        or app_today_iso()
    )
    return {
        "status": "success",
        "report_date": report_date,
        "count": len(rows),
        "mood": mood,
        "headline": f"今日 AI 日报：先复核持仓库，再从主市场与美股模型里筛出可验证候选。",
        "scope": "portfolio_plus_cn_full_market_top5",
        "strategy": strategy,
        "portfolio_summary": portfolio_summary,
        "portfolio_rows": portfolio_rows,
        "social_signal_summary": {
            "accounts": social_summary.get("accounts") or [],
            "actionable": social_summary.get("actionable") or [],
        },
        "us_hotspot_validation": us_hotspot_validation,
        "market_recommendations": actionable_rows,
        "market_watch_recommendations": watch_rows,
        "market_candidates_all": rows,
        "market_recommendations_meta": market_recommendation_meta,
        "market_structure": market_structure,
        "market_template_attribution": market_template_attribution,
        "model_selection_guidance": model_selection_guidance,
        "model_selection_guidance_summary": model_selection_guidance_summary,
        "lightgbm_execution_bias": lightgbm_execution_bias,
        "recommendation_regression": recommendation_regression,
        "us_model_recommendations": us_model_rows,
        "us_model_recommendations_meta": us_market_recommendation_meta,
        "us_market_structure": us_market_structure,
        "rows": rows,
        "buy_the_dip_rows": buy_the_dip_rows,
    }


def _hydrate_security_names(symbol_repo: SymbolRepository, *row_groups: list[dict]) -> None:
    tickers: list[str] = []
    for rows in row_groups:
        for item in rows or []:
            ticker = str(item.get("ticker") or "").strip().upper()
            if ticker:
                tickers.append(ticker)
    if not tickers:
        return
    overviews = symbol_repo.list_overviews_for_tickers(list(dict.fromkeys(tickers)))
    for rows in row_groups:
        for item in rows or []:
            ticker = str(item.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            overview = overviews.get(ticker) or {}
            candidate_name = str(item.get("name") or "").strip()
            resolved_name = str(overview.get("name") or "").strip()
            if resolved_name and (not candidate_name or candidate_name == ticker):
                item["name"] = resolved_name


def _build_lightgbm_execution_bias(*, lang: str = "zh") -> dict:
    evaluation = build_lightgbm_prediction_evaluation(market="ALL", recent_runs=8, top_n=40)
    windows = evaluation.get("windows") or {}
    sample_count = int(evaluation.get("sample_count") or 0)
    ranked = sorted(
        [
            (
                int(((windows.get("breakout") or {}).get(1) or {}).get("count") or 0),
                float(((windows.get("breakout") or {}).get(1) or {}).get("hit_rate") or 0.0),
                "breakout",
            ),
            (
                int(((windows.get("pullback") or {}).get(1) or {}).get("count") or 0),
                float(((windows.get("pullback") or {}).get(1) or {}).get("hit_rate") or 0.0),
                "pullback",
            ),
            (
                int(((windows.get("watch") or {}).get(1) or {}).get("count") or 0),
                float(((windows.get("watch") or {}).get(1) or {}).get("hit_rate") or 0.0),
                "watch",
            ),
        ],
        key=lambda item: (-item[0], -item[1], item[2]),
    )
    lead_count, lead_hit, lead_key = ranked[0]
    if sample_count <= 0 or lead_count <= 0:
        if lang == "zh":
            return {
                "title": "LightGBM：今天先观察",
                "summary": "当前还没有足够成熟的次日样本，先把 LightGBM 当作观察面板。",
                "action": "watch",
                "sample_count": sample_count,
                "hit_rate_1d": None,
            }
        return {
            "title": "LightGBM: Observe First",
            "summary": "There are not enough mature next-day samples yet, so treat LightGBM as an observation panel.",
            "action": "watch",
            "sample_count": sample_count,
            "hit_rate_1d": None,
        }
    if lead_key == "breakout":
        if lang == "zh":
            return {
                "title": "LightGBM：今天更偏突破确认",
                "summary": f"优先看放量突破的名字；同类 1D 命中率 {lead_hit:.1f}%。",
                "action": "breakout",
                "sample_count": sample_count,
                "hit_rate_1d": round(lead_hit, 1),
            }
        return {
            "title": "LightGBM: Lean Breakout Today",
            "summary": f"Prioritize names with cleaner breakout confirmation; peer 1D hit rate {lead_hit:.1f}%.",
            "action": "breakout",
            "sample_count": sample_count,
            "hit_rate_1d": round(lead_hit, 1),
        }
    if lead_key == "pullback":
        if lang == "zh":
            return {
                "title": "LightGBM：今天更偏回踩布局",
                "summary": f"优先看回踩企稳的名字；同类 1D 命中率 {lead_hit:.1f}%。",
                "action": "pullback",
                "sample_count": sample_count,
                "hit_rate_1d": round(lead_hit, 1),
            }
        return {
            "title": "LightGBM: Lean Pullbacks Today",
            "summary": f"Prioritize names resetting into support; peer 1D hit rate {lead_hit:.1f}%.",
            "action": "pullback",
            "sample_count": sample_count,
            "hit_rate_1d": round(lead_hit, 1),
        }
    if lang == "zh":
        return {
            "title": "LightGBM：今天先观察",
            "summary": f"当前 Watch 信号更占优，先把它当观察名单；同类 1D 命中率 {lead_hit:.1f}%。",
            "action": "watch",
            "sample_count": sample_count,
            "hit_rate_1d": round(lead_hit, 1),
        }
    return {
        "title": "LightGBM: Watch First",
        "summary": f"Watch signals currently lead, so treat it as a monitored list first; peer 1D hit rate {lead_hit:.1f}%.",
        "action": "watch",
        "sample_count": sample_count,
        "hit_rate_1d": round(lead_hit, 1),
    }


def _build_market_recommendation_rows(
    *,
    db,
    symbol_repo: SymbolRepository,
    prediction_repo: PredictionRepository,
    market: str,
    excluded_tickers: set[str],
    recommendation_limit: int,
    prefer_snapshot: bool,
) -> tuple[list[dict], dict]:
    service = AIAnalysisService()
    market_snapshot_context = load_market_context_snapshot(db, market=market)
    candidate_limit = max(recommendation_limit * 14, 80)
    candidates: list[dict] = []
    candidate_meta = {
        "market": market,
        "source": "none",
        "status": "not_ready" if prefer_snapshot else "ready",
        "ready": not prefer_snapshot,
        "used_today_snapshot": False,
        "target_snapshot_date": None,
        "snapshot_templates_considered": 0,
        "snapshot_templates_ready": 0,
        "snapshot_rows": 0,
        "candidate_count": 0,
        "blocked_candidates": 0,
        "note": "",
    }
    if prefer_snapshot:
        candidates, snapshot_meta = _load_full_market_report_candidates(
            db=db,
            market=market,
            excluded_tickers=excluded_tickers,
            limit=candidate_limit,
            with_meta=True,
        )
        candidate_meta.update(snapshot_meta)
        if candidates:
            target_snapshot_date = str(snapshot_meta.get("target_snapshot_date") or "").strip()
            candidate_meta.update(
                {
                    "source": "fresh_snapshot",
                    "status": "ready",
                    "ready": True,
                    "used_today_snapshot": bool(target_snapshot_date and target_snapshot_date == app_today_iso()),
                    "note": _render_market_candidate_note(
                        source="fresh_snapshot",
                        market=market,
                        candidate_count=len(candidates),
                        snapshot_templates_ready=int(snapshot_meta.get("snapshot_templates_ready") or 0),
                        snapshot_date=target_snapshot_date or None,
                    ),
                }
            )
    if not candidates and not prefer_snapshot:
        fallback_candidates = [
            item
            for item in prediction_repo.list_latest_signal_decisions(limit=candidate_limit, market=market)
            if str(item.get("ticker") or "").upper() not in excluded_tickers
        ]
        candidates = fallback_candidates
        if fallback_candidates:
            candidate_meta.update(
                {
                    "source": "predictions_fallback",
                    "status": "fallback",
                    "ready": False,
                    "used_today_snapshot": False,
                    "note": _render_market_candidate_note(
                        source="predictions_fallback",
                        market=market,
                        candidate_count=len(fallback_candidates),
                        snapshot_templates_ready=int(candidate_meta.get("snapshot_templates_ready") or 0),
                        snapshot_date=str(candidate_meta.get("target_snapshot_date") or "").strip() or None,
                    ),
                }
            )
        else:
            candidate_meta.update(
                {
                    "source": "none",
                    "status": "not_ready" if prefer_snapshot else "empty",
                    "ready": False,
                    "used_today_snapshot": False,
                    "note": _render_market_candidate_note(
                        source="none",
                        market=market,
                        candidate_count=0,
                        snapshot_templates_ready=int(candidate_meta.get("snapshot_templates_ready") or 0),
                        snapshot_date=str(candidate_meta.get("target_snapshot_date") or "").strip() or None,
                    ),
                }
            )
    elif not candidates and prefer_snapshot:
        blocked_candidates = int(candidate_meta.get("blocked_candidates") or 0)
        snapshot_rows = int(candidate_meta.get("snapshot_rows") or 0)
        candidate_meta.update(
            {
                "source": "all_blocked" if snapshot_rows > 0 or blocked_candidates > 0 else "snapshot_required",
                "status": "blocked",
                "ready": False,
                "used_today_snapshot": False,
                "note": (
                    f"最近交易日快照已生成，但 {blocked_candidates or snapshot_rows} 个候选全部被交易纪律拦截。"
                    if market == "CN" and (snapshot_rows > 0 or blocked_candidates > 0)
                    else (
                        "最近交易日模型快照未就绪，已停止生成全市场推荐。"
                        if market == "CN"
                        else (
                            f"Latest-trading-day snapshots are ready, but all {blocked_candidates or snapshot_rows} candidates were filtered by trading rules."
                            if snapshot_rows > 0 or blocked_candidates > 0
                            else "Latest-trading-day snapshots are not ready, so market recommendations are paused."
                        )
                    )
                ),
            }
        )
    elif not prefer_snapshot:
        candidate_meta.update(
            {
                "source": "predictions_fallback",
                "status": "ready",
                "ready": True,
                "used_today_snapshot": False,
                "note": _render_market_candidate_note(
                    source="predictions_fallback",
                    market=market,
                    candidate_count=len(candidates),
                    snapshot_templates_ready=0,
                    snapshot_date=str(candidate_meta.get("target_snapshot_date") or "").strip() or None,
                ),
            }
        )

    ranked_candidates: list[dict] = []
    for candidate in candidates:
        ticker = str(candidate.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        gate = _recommendation_gate(candidate)
        if not gate["allowed"]:
            candidate_meta["blocked_candidates"] = int(candidate_meta.get("blocked_candidates") or 0) + 1
            continue
        overview = symbol_repo.get_overview(ticker)
        if overview is None:
            continue
        combined = service.insight_engine.get_insight(ticker, lang="zh")
        if combined is None:
            continue
        quant_rank = _candidate_quant_score(candidate, combined)
        verification_score = _candidate_verification_score(candidate, combined)
        ranked_candidates.append(
            {
                "ticker": ticker,
                "name": candidate.get("name") or ticker,
                "market": candidate.get("market") or market,
                "overview": overview,
                "latest_signal": candidate,
                "combined": combined,
                "quant_rank": quant_rank,
                "verification_score": verification_score,
            }
        )
    ranked_candidates.sort(
        key=lambda item: (
            0 if (item.get("latest_signal") or {}).get("score") is not None else 1,
            0 if "missing-model-score" not in {
                str(flag).strip().lower() for flag in (((item.get("latest_signal") or {}).get("risk_flags") or [])) if str(flag).strip()
            } else 1,
            0
            if str(((item.get("latest_signal") or {}).get("tradability_status") or "")).upper() == "READY"
            else 1
            if str(((item.get("latest_signal") or {}).get("tradability_status") or "")).upper() == "DEFER"
            else 2
            if str(((item.get("latest_signal") or {}).get("tradability_status") or "")).upper() == "REVIEW"
            else 3,
            -float(((item.get("latest_signal") or {}).get("trade_readiness_score") or 0.0)),
            -(item.get("verification_score") or 0.0),
            -(item.get("quant_rank") or 0.0),
            -float((item.get("latest_signal") or {}).get("score") or 0.0),
            item["ticker"],
        )
    )

    rows: list[dict] = []
    for item in ranked_candidates[:recommendation_limit]:
        overview = item.get("overview") or symbol_repo.get_overview(item["ticker"])
        if overview is None:
            continue
        latest_signal = item.get("latest_signal")
        if latest_signal is None:
            latest_signal = prediction_repo.get_latest_model_output_for_ticker(item["ticker"])
            latest_signal = prediction_repo._build_signal_decision(latest_signal or {}) if latest_signal else None
        combined = item.get("combined") or service.insight_engine.get_insight(item["ticker"], lang="zh")
        if latest_signal is not None and (
            latest_signal.get("trade_readiness_score") is None
            or not str(latest_signal.get("tradability_status") or "").strip()
        ):
            decision = evaluate_candidate_tradability(
                latest_signal,
                market_snapshot=market_snapshot_context,
            )
            latest_signal = {
                **latest_signal,
                "tradability_status": decision.tradability_status,
                "block_reason": decision.block_reason,
                "trade_readiness_score": decision.trade_readiness_score,
                "readiness_bucket": decision.readiness_bucket,
                "readiness_reason": decision.readiness_reason,
                "suggested_watch_action": decision.suggested_watch_action,
                "preferred_entry_style": decision.preferred_entry_style,
                "risk_flags": decision.risk_flags,
                "liquidity_bucket": decision.liquidity_bucket,
                "suggested_participation_rate": decision.suggested_participation_rate,
                "entry_trigger": decision.entry_trigger,
                "invalidation_condition": decision.invalidation_condition,
                "time_horizon": decision.time_horizon,
                "max_slippage_bps": decision.max_slippage_bps,
                "stop_loss_type": decision.stop_loss_type,
                "execution_note": decision.execution_note,
            }
        analysis = service.analyze_symbol(
            overview=overview,
            latest_signal=latest_signal,
            combined_analysis={
                "decision": "WATCH" if combined is None else "BUY" if (combined.get("trend_label") == "bullish") else "HOLD",
                "confidence": 55 if combined is None else int(round(float(combined.get("confidence") or 0.55) * 100)),
                "score": 0 if combined is None else int(round(((combined.get("trend_score") or 50) - 50) / 10)),
                "reasons": list((combined or {}).get("explanation") or [])[:3],
                "technical_rating": {},
                "multi_timeframe": {},
                "bollinger_band": {},
                "candlestick_patterns": {},
            },
            lang="zh",
        )
        rows.append(
            {
                "ticker": item["ticker"],
                "name": item.get("name") or item["ticker"],
                "market": item.get("market"),
                "sector": overview.get("sector"),
                "industry": overview.get("industry"),
                "headline": analysis.get("headline"),
                "verdict": analysis.get("verdict"),
                "confidence": analysis.get("confidence"),
                "strategy": analysis.get("strategy"),
                "quant_rank": round(float(item.get("quant_rank") or _candidate_quant_score(latest_signal or {}, combined or {})), 1),
                "verification_score": round(float(item.get("verification_score") or _candidate_verification_score(latest_signal or {}, combined or {})), 1),
                "model_score": (None if latest_signal is None else latest_signal.get("score")),
                "model_signal_strength": (None if latest_signal is None else latest_signal.get("signal_strength")),
                "tradability_status": (None if latest_signal is None else latest_signal.get("tradability_status")),
                "block_reason": (None if latest_signal is None else latest_signal.get("block_reason")),
                "trade_readiness_score": (None if latest_signal is None else latest_signal.get("trade_readiness_score")),
                "readiness_bucket": (None if latest_signal is None else latest_signal.get("readiness_bucket")),
                "readiness_reason": (None if latest_signal is None else latest_signal.get("readiness_reason")),
                "suggested_watch_action": (None if latest_signal is None else latest_signal.get("suggested_watch_action")),
                "target_weight": (None if latest_signal is None else latest_signal.get("target_weight")),
                "suggested_participation_rate": (
                    None if latest_signal is None else latest_signal.get("suggested_participation_rate")
                ),
                "entry_trigger": (None if latest_signal is None else latest_signal.get("entry_trigger")),
                "invalidation_condition": (
                    None if latest_signal is None else latest_signal.get("invalidation_condition")
                ),
                "time_horizon": (None if latest_signal is None else latest_signal.get("time_horizon")),
                "max_slippage_bps": (None if latest_signal is None else latest_signal.get("max_slippage_bps")),
                "liquidity_bucket": (None if latest_signal is None else latest_signal.get("liquidity_bucket")),
                "stop_loss_type": (None if latest_signal is None else latest_signal.get("stop_loss_type")),
                "execution_note": (None if latest_signal is None else latest_signal.get("execution_note")),
                "risk_flags": ([] if latest_signal is None else latest_signal.get("risk_flags") or []),
                "trend_score": (None if combined is None else combined.get("trend_score")),
                "setup_label": (None if combined is None else combined.get("setup_label")),
                "full_market_template": (None if latest_signal is None else latest_signal.get("full_market_template")),
                "full_market_rank_score": (None if latest_signal is None else latest_signal.get("full_market_rank_score")),
                "report_source_kind": (None if latest_signal is None else latest_signal.get("report_source_kind")),
                "report_source_label": (None if latest_signal is None else latest_signal.get("report_source_label")),
                "recommendation_gate_config": (None if latest_signal is None else latest_signal.get("recommendation_gate_config")),
                "latest_close": (
                    None
                    if latest_signal is None and combined is None
                    else (None if latest_signal is None else latest_signal.get("latest_close")) or (combined or {}).get("latest_close")
                ),
                "latest_price": (
                    None
                    if latest_signal is None and combined is None
                    else (None if latest_signal is None else latest_signal.get("latest_price"))
                    or (None if latest_signal is None else latest_signal.get("latest_close"))
                    or (combined or {}).get("latest_close")
                ),
                "buy_zone": analysis.get("buy_zone"),
                "stop_loss": analysis.get("stop_loss"),
                "take_profit": analysis.get("take_profit"),
                "summary": analysis.get("summary"),
                "verification_note": _build_verification_note(latest_signal=latest_signal, combined=combined),
                "execution_plan": _build_execution_plan(latest_signal=latest_signal, analysis=analysis),
            }
        )
    candidate_meta["candidate_count"] = len(rows)
    return rows, candidate_meta


def _render_market_candidate_note(
    *,
    source: str,
    market: str,
    candidate_count: int,
    snapshot_templates_ready: int,
    snapshot_date: str | None = None,
) -> str:
    market_label = {"CN": "A股", "US": "美股", "HK": "港股"}.get(str(market or "").upper(), str(market or "市场"))
    snapshot_hint = f"{snapshot_date} 收盘后" if snapshot_date else "最近交易日"
    if source == "fresh_snapshot":
        return f"{market_label} Top 5 使用 {snapshot_hint} 的全市场快照候选；已命中 {candidate_count} 个可排序候选，快照模板 {snapshot_templates_ready} 个已就绪。"
    if source == "predictions_fallback":
        return f"{market_label} 全市场快照未完全就绪，当前已降级到最新模型预测候选；可用候选 {candidate_count} 个。"
    return f"{market_label} 全市场候选尚未就绪，当前没有可用于日报的候选。"


def _close_vs_buy_zone_high_pct(row: dict) -> float | None:
    buy_zone = row.get("buy_zone") or {}
    buy_zone_high = _safe_float(buy_zone.get("high"))
    latest_price = _candidate_latest_price(row)
    if latest_price <= 0 or buy_zone_high <= 0:
        return None
    return round(((latest_price - buy_zone_high) / buy_zone_high) * 100.0, 2)


def _maybe_float(value) -> float | None:
    try:
        if value in (None, ""):
            return None
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if isnan(numeric):
        return None
    return numeric


def _recommendation_regression_board_profile(row: dict) -> str:
    ticker = str(row.get("ticker") or "").strip().upper()
    name = str(row.get("name") or "").strip().upper().replace(" ", "")
    code = ticker.split(".", 1)[0]
    limit_band = _maybe_float(row.get("limit_band_pct"))
    if name.startswith(("ST", "*ST", "S*ST", "PT")) or (limit_band is not None and limit_band <= 5.5):
        return "st"
    if ticker.endswith(".BJ") or code.startswith(("4", "8")) or (limit_band is not None and limit_band >= 29):
        return "bse"
    if code.startswith(("688", "689")):
        return "star"
    if code.startswith(("300", "301")):
        return "chinext"
    return "main"


def _recommendation_regression_reason_label(reason: str | None, *, lang: str = "zh") -> str:
    normalized = str(reason or "").strip()
    if lang == "zh":
        return {
            "missing_model_score_regression": "历史回归显示缺少完整模型分的候选胜率偏低，先降级观察。",
            "risk_flag_regression:missing-model-score": "历史回归显示 missing-model-score 候选次日表现偏弱，先降级观察。",
            "st_regression": "历史回归显示 ST 候选隔夜/盘中质量不稳，默认只观察。",
            "buy_zone_deviation_regression": "历史回归显示当前偏离买点过大，先等回踩确认。",
            "watch_bias_regression": "LightGBM 当前偏观察，历史回归要求压缩可执行池。",
        }.get(normalized, "")
    return {
        "missing_model_score_regression": "Historical regression shows incomplete model scores have weaker hit rates, so downgrade to watch.",
        "risk_flag_regression:missing-model-score": "Historical regression shows missing-model-score setups underperform, so downgrade to watch.",
        "st_regression": "Historical regression shows ST setups have unstable execution quality, so keep them on watch.",
        "buy_zone_deviation_regression": "Historical regression shows this is too far above the buy zone; wait for a pullback.",
        "watch_bias_regression": "LightGBM is watch-biased, so historical regression caps the actionable pool.",
    }.get(normalized, "")


def _recommendation_regression_downgrade_reason(
    row: dict,
    *,
    deviation: float | None,
    risk_flags: set[str],
    regression_policy: dict | None,
) -> str:
    policy = regression_policy or {}
    if not policy:
        return ""

    board_profile = _recommendation_regression_board_profile(row)
    excluded_boards = {str(item).strip().lower() for item in (policy.get("exclude_actionable_board_profiles") or [])}
    if board_profile in excluded_boards:
        return f"{board_profile}_regression"

    if policy.get("downgrade_model_score_missing") and row.get("model_score") is None and row.get("score") is None:
        return "missing_model_score_regression"

    downgraded_flags = {str(item).strip().lower() for item in (policy.get("downgrade_risk_flags") or [])}
    for flag in sorted(risk_flags):
        if flag in downgraded_flags:
            return f"risk_flag_regression:{flag}"

    max_deviation = _maybe_float(policy.get("max_actionable_buy_zone_deviation_pct"))
    if max_deviation is not None and deviation is not None and deviation > max_deviation:
        return "buy_zone_deviation_regression"

    return ""


def _market_pool_reason(row: dict, *, lang: str = "zh") -> str:
    regression_reason = _recommendation_regression_reason_label(row.get("regression_downgrade_reason"), lang=lang)
    if regression_reason:
        return regression_reason
    status = str(row.get("tradability_status") or "").strip().upper()
    deviation = _safe_float(row.get("close_vs_buy_zone_high_pct"))
    risk_flags = {str(item).strip().lower() for item in (row.get("risk_flags") or []) if str(item).strip()}
    if status != "READY":
        return "当前仍待复核，更适合先观察承接。" if lang == "zh" else "Still a review setup; watch it first."
    if deviation is not None and deviation > ACTIONABLE_MAX_BUY_ZONE_DEVIATION_PCT:
        return (
            f"收盘价已高出买入区上沿 {deviation:.1f}%，更像强势观察票，不适合直接追。"
            if lang == "zh"
            else f"Close is already {deviation:.1f}% above the buy zone; better as a watch name than a chase."
        )
    if "missing-model-score" in risk_flags:
        return (
            "当前模型分还不完整，但位置仍接近计划买点，适合先用小仓位跟踪执行。"
            if lang == "zh"
            else "The full model score is still incomplete, but the setup remains near the planned buy zone, so it can stay in the starter bucket."
        )
    return "位置仍接近计划买点，且交易状态已就绪，可进入可执行买入池。" if lang == "zh" else "Still close to the planned buy zone and marked ready."


def _split_market_recommendation_rows(
    rows: list[dict],
    *,
    actionable_limit: int = 5,
    watch_limit: int = 5,
    regression_policy: dict | None = None,
    lightgbm_execution_bias: dict | None = None,
) -> tuple[list[dict], list[dict], dict]:
    actionable: list[dict] = []
    watch: list[dict] = []
    policy = regression_policy or {}
    active_policy_notes = list(policy.get("notes") or []) if isinstance(policy, dict) else []
    max_deviation = _maybe_float((policy or {}).get("max_actionable_buy_zone_deviation_pct"))
    actionable_deviation_cap = max_deviation if max_deviation is not None else ACTIONABLE_MAX_BUY_ZONE_DEVIATION_PCT
    for row in rows:
        enriched = {**row}
        deviation = _close_vs_buy_zone_high_pct(enriched)
        enriched["close_vs_buy_zone_high_pct"] = deviation
        status = str(enriched.get("tradability_status") or "").strip().upper()
        risk_flags = {str(item).strip().lower() for item in (enriched.get("risk_flags") or []) if str(item).strip()}
        regression_reason = _recommendation_regression_downgrade_reason(
            enriched,
            deviation=deviation,
            risk_flags=risk_flags,
            regression_policy=policy,
        )
        is_actionable = (
            status == "READY"
            and (deviation is None or deviation <= actionable_deviation_cap)
            and not regression_reason
        )
        enriched["regression_policy_applied"] = bool(regression_reason)
        enriched["regression_downgrade_reason"] = regression_reason
        enriched["report_pool"] = "actionable" if is_actionable else "watch"
        enriched["report_pool_reason"] = _market_pool_reason(enriched, lang="zh")
        if is_actionable:
            actionable.append(enriched)
        else:
            watch.append(enriched)

    watch_bias_limit = (policy or {}).get("watch_bias_actionable_limit")
    try:
        watch_bias_limit_value = int(watch_bias_limit) if watch_bias_limit is not None else None
    except (TypeError, ValueError):
        watch_bias_limit_value = None
    bias_action = str((lightgbm_execution_bias or {}).get("action") or "").strip().lower()
    if (
        watch_bias_limit_value is not None
        and watch_bias_limit_value >= 0
        and bias_action == "watch"
        and len(actionable) > watch_bias_limit_value
    ):
        excess = actionable[watch_bias_limit_value:]
        actionable = actionable[:watch_bias_limit_value]
        for item in excess:
            item["report_pool"] = "watch"
            item["regression_policy_applied"] = True
            item["regression_downgrade_reason"] = "watch_bias_regression"
            item["report_pool_reason"] = _market_pool_reason(item, lang="zh")
        watch = excess + watch

    return (
        actionable[:actionable_limit],
        watch[:watch_limit],
        {
            "actionable_count": len(actionable),
            "watch_count": len(watch),
            "actionable_limit": actionable_limit,
            "watch_limit": watch_limit,
            "regression_policy_applied": bool(active_policy_notes),
            "regression_policy_notes": active_policy_notes[:5],
            "actionable_deviation_cap_pct": actionable_deviation_cap,
        },
    )


def _build_market_structure(
    *,
    rows: list[dict],
    market: str,
    lang: str = "zh",
    heatmap_payload: dict | None = None,
) -> dict:
    market_label = {"CN": "A股", "US": "美股", "HK": "港股"}.get(str(market or "").upper(), str(market or "市场"))
    normalized_market = str(market or "").upper()
    if normalized_market == "CN" and isinstance(heatmap_payload, dict):
        heatmap_rows = list(heatmap_payload.get("sector_heatmap") or [])
        informative_heatmap = [
            item for item in heatmap_rows
            if str(item.get("label") or "").strip() and str(item.get("label") or "").strip() != "其他"
        ]
        if informative_heatmap:
            strong_sectors = [
                {
                    "label": item.get("label") or f"{market_label}综合",
                    "count": int(item.get("hits") or 0),
                    "avg_strength": round(float(item.get("avg_score") or 0.0), 1),
                    "avg_risk": float(len(item.get("execution_tags") or [])) * 8.0,
                    "tickers": [str(detail.get("ticker") or "") for detail in (item.get("ticker_details") or []) if detail.get("ticker")][:5],
                    "breadth_pct": item.get("breadth_pct"),
                    "execution_tags": item.get("execution_tags") or [],
                }
                for item in informative_heatmap[:3]
            ]
            weak_sectors = sorted(
                [
                    {
                        "label": item.get("label") or f"{market_label}综合",
                        "count": int(item.get("hits") or 0),
                        "avg_strength": round(float(item.get("avg_score") or 0.0), 1),
                        "avg_risk": float(len(item.get("execution_tags") or [])) * 8.0,
                        "tickers": [str(detail.get("ticker") or "") for detail in (item.get("ticker_details") or []) if detail.get("ticker")][:5],
                        "breadth_pct": item.get("breadth_pct"),
                        "execution_tags": item.get("execution_tags") or [],
                    }
                    for item in informative_heatmap
                ],
                key=lambda item: (
                    -float(item.get("avg_risk") or 0.0),
                    float(item.get("avg_strength") or 0.0),
                    str(item.get("label") or ""),
                ),
            )[:3]
            risk_watch = []
            for item in informative_heatmap:
                execution_tags = [str(tag).strip() for tag in (item.get("execution_tags") or []) if str(tag).strip()]
                if not execution_tags:
                    continue
                risk_watch.append(
                    {
                        "ticker": " / ".join([str(detail.get("ticker") or "") for detail in (item.get("ticker_details") or [])[:2] if detail.get("ticker")]) or "-",
                        "name": item.get("label") or f"{market_label}综合",
                        "sector": item.get("label") or f"{market_label}综合",
                        "tradability_status": "HEATMAP",
                        "risk_flags": execution_tags,
                        "verification_score": item.get("avg_score"),
                        "headline": f"命中 {int(item.get('hits') or 0)} | 广度 {_fmt_number(item.get('breadth_pct'))}% | 标签 {' / '.join(execution_tags[:3])}",
                    }
                )
            risk_watch = sorted(
                risk_watch,
                key=lambda item: (
                    -len(item.get("risk_flags") or []),
                    -float(item.get("verification_score") or 0.0),
                    str(item.get("sector") or ""),
                ),
            )[:4]
            strong_labels = " / ".join(item["label"] for item in strong_sectors) or "-"
            weak_labels = " / ".join(item["label"] for item in weak_sectors if float(item.get("avg_risk") or 0.0) > 0) or "暂无明显风险集中方向"
            breadth_hint = next(
                (
                    f"领先方向上涨广度 {_fmt_number(item.get('breadth_pct'))}%"
                    for item in strong_sectors
                    if item.get("breadth_pct") is not None
                ),
                None,
            )
            headline = f"{market_label} 当前偏强方向：{strong_labels}；风险更多集中在：{weak_labels}。"
            if breadth_hint:
                headline = f"{headline[:-1]}；{breadth_hint}。"
            return {
                "market": normalized_market,
                "headline": headline,
                "strong_sectors": strong_sectors,
                "weak_sectors": weak_sectors,
                "risk_watch": risk_watch,
                "source": "market_heatmap_snapshot",
            }
    if not rows:
        return {
            "market": normalized_market,
            "headline": f"{market_label} 暂无可用结构化候选。",
            "strong_sectors": [],
            "weak_sectors": [],
            "risk_watch": [],
            "source": "recommendation_rows",
        }

    sector_map: dict[str, dict] = {}
    for row in rows:
        label = _market_structure_label_for_row(row, market=normalized_market, market_label=market_label)
        trend_score = _safe_float(row.get("trend_score"))
        verification_score = _safe_float(row.get("verification_score"))
        quant_rank = _safe_float(row.get("quant_rank"))
        tradability = str(row.get("tradability_status") or "").upper()
        risk_flags = [str(item).strip() for item in (row.get("risk_flags") or []) if str(item).strip()]
        risk_penalty = len(risk_flags) * 12.0
        if tradability == "BLOCKED":
            risk_penalty += 28.0
        elif tradability in {"REVIEW", "DEFER"}:
            risk_penalty += 12.0
        strength_score = verification_score + trend_score * 0.5 + quant_rank * 0.08 - risk_penalty
        bucket = sector_map.setdefault(
            label,
            {
                "label": label,
                "count": 0,
                "strength_total": 0.0,
                "risk_total": 0.0,
                "tickers": [],
            },
        )
        bucket["count"] += 1
        bucket["strength_total"] += strength_score
        bucket["risk_total"] += risk_penalty
        if row.get("ticker"):
            bucket["tickers"].append(str(row.get("ticker")))

    ranked = []
    for item in sector_map.values():
        count = max(1, int(item.get("count") or 0))
        ranked.append(
            {
                "label": item["label"],
                "count": count,
                "avg_strength": round(float(item.get("strength_total") or 0.0) / count, 1),
                "avg_risk": round(float(item.get("risk_total") or 0.0) / count, 1),
                "tickers": item.get("tickers") or [],
            }
        )
    strong_sectors = sorted(
        ranked,
        key=lambda item: (-float(item.get("avg_strength") or 0.0), float(item.get("avg_risk") or 0.0), item.get("label") or ""),
    )[:3]
    weak_sectors = sorted(
        ranked,
        key=lambda item: (-float(item.get("avg_risk") or 0.0), float(item.get("avg_strength") or 0.0), item.get("label") or ""),
    )[:3]
    risk_watch = sorted(
        [
            {
                "ticker": row.get("ticker"),
                "name": row.get("name"),
                "sector": _market_structure_label_for_row(row, market=normalized_market, market_label=market_label),
                "tradability_status": row.get("tradability_status"),
                "risk_flags": row.get("risk_flags") or [],
                "verification_score": row.get("verification_score"),
                "headline": row.get("headline") or row.get("summary"),
            }
            for row in rows
            if (row.get("risk_flags") or []) or str(row.get("tradability_status") or "").upper() in {"BLOCKED", "REVIEW", "DEFER"}
        ],
        key=lambda item: (
            -(
                len(item.get("risk_flags") or []) * 10
                + (25 if str(item.get("tradability_status") or "").upper() == "BLOCKED" else 10 if str(item.get("tradability_status") or "").upper() in {"REVIEW", "DEFER"} else 0)
            ),
            float(item.get("verification_score") or 0.0),
            item.get("ticker") or "",
        ),
    )[:4]
    strong_labels = " / ".join(item["label"] for item in strong_sectors) or "-"
    weak_labels = " / ".join(item["label"] for item in weak_sectors if float(item.get("avg_risk") or 0.0) > 0) or "暂无明显风险集中方向"
    headline = f"{market_label} 当前偏强方向：{strong_labels}；风险更多集中在：{weak_labels}。"
    return {
        "market": normalized_market,
        "headline": headline,
        "strong_sectors": strong_sectors,
        "weak_sectors": weak_sectors,
        "risk_watch": risk_watch,
        "source": "recommendation_rows",
    }


def _build_market_template_attribution(*, rows: list[dict], market: str, lang: str = "zh") -> dict:
    if not rows:
        return {
            "headline": "当前没有可归因的日报候选。" if lang == "zh" else "No report candidates available for attribution.",
            "leaders": [],
        }
    template_labels = {
        "technical_momentum": "技术动量",
        "cn_bollinger_squeeze_watch": "布林带收口待突破",
        "cn_three_white_soldiers": "三连阳强势延续",
        "cn_volume_breakout": "底部放量突破",
        "lightgbm_top_picks": "LightGBM 多因子优选",
    }
    buckets: dict[str, dict] = {}
    for row in rows[:5]:
        template_key = str(row.get("full_market_template") or "").strip() or "unknown"
        bucket = buckets.setdefault(
            template_key,
            {
                "template": template_key,
                "label": template_labels.get(template_key, template_key or "-"),
                "count": 0,
                "tickers": [],
                "avg_quant_rank": 0.0,
            },
        )
        bucket["count"] += 1
        if row.get("ticker"):
            bucket["tickers"].append(str(row.get("ticker")))
        bucket["avg_quant_rank"] += _safe_float(row.get("quant_rank"))
    leaders = []
    for item in buckets.values():
        count = max(1, int(item.get("count") or 0))
        template_key = str(item.get("template") or "").strip()
        eval_payload = build_pattern_template_evaluation(
            template_key=template_key,
            market=market,
            lookback_snapshots=15,
            top_n=40,
        ) if template_key.startswith("cn_") else None
        eval_windows = (eval_payload or {}).get("windows") or {}

        def _best_window(window: int) -> dict:
            ranked = []
            for action_key in ("buy_the_dip", "wait_for_breakout", "hold_and_watch"):
                stats = (eval_windows.get(action_key) or {}).get(window) or {}
                ranked.append(
                    (
                        int(stats.get("count") or 0),
                        float(stats.get("hit_rate") or 0.0),
                        float(stats.get("avg_return") or 0.0),
                        stats,
                    )
                )
            ranked.sort(key=lambda value: (-value[0], -value[1], -value[2]))
            _count, _hit, _avg, stats = ranked[0]
            return {
                "count": int(stats.get("count") or 0),
                "hit_rate": round(float(stats.get("hit_rate") or 0.0), 1) if stats.get("hit_rate") is not None else None,
                "avg_return": round(float(stats.get("avg_return") or 0.0), 2) if stats.get("avg_return") is not None else None,
            }

        leaders.append(
            {
                **item,
                "avg_quant_rank": round(float(item.get("avg_quant_rank") or 0.0) / count, 1),
                "stats_1d": _best_window(1),
                "stats_3d": _best_window(3),
                "stats_5d": _best_window(5),
            }
        )
    leaders.sort(
        key=lambda item: (
            -int(item.get("count") or 0),
            -float(item.get("avg_quant_rank") or 0.0),
            str(item.get("label") or ""),
        )
    )
    headline = (
        "当前 Top 5 主要由这些模板推上来，可直接看出今天是动量、放量突破还是形态延续在主导。"
        if lang == "zh"
        else "These template families are currently driving the Top 5, showing whether momentum, breakout, or continuation is really in control."
    )
    return {
        "headline": headline,
        "leaders": leaders[:4],
    }


def _market_structure_label_for_row(row: dict, *, market: str, market_label: str) -> str:
    sector = str(row.get("sector") or "").strip()
    if sector and sector != "其他":
        return sector
    industry = str(row.get("industry") or "").strip()
    if industry and industry != "其他":
        return industry
    ticker = str(row.get("ticker") or "").strip().upper()
    if ticker:
        fallback_label = resolve_template_group_label(
            meta={
                "sector": row.get("sector"),
                "industry": row.get("industry"),
                "exchange": row.get("exchange"),
                "name": row.get("name"),
            },
            ticker=ticker,
            market_code=market,
            name=row.get("name"),
        )
        if fallback_label and fallback_label not in {"Unclassified", "A股其他 / CN Other", "美股综合 / US General"}:
            return fallback_label
    template = str(row.get("full_market_template") or "").strip()
    if template:
        template_labels = {
            "technical_momentum": "动量趋势",
            "cn_bollinger_squeeze_watch": "波动收敛",
            "cn_three_white_soldiers": "K线转强",
            "cn_volume_breakout": "放量突破",
            "next_tesla_swing": "强趋势二次启动",
            "global_growth_value": "成长价值",
            "global_income_quality": "质量分红",
        }
        return template_labels.get(template, template.replace("_", " ").strip() or f"{market_label}综合")
    return f"{market_label}综合"


def save_ai_daily_report(payload: dict, *, db=None) -> None:
    if db is None:
        with SessionLocal() as own_db:
            save_ai_daily_report(payload, db=own_db)
        return
    enriched_payload = dict(payload or {})
    default_report_date = (
        str(enriched_payload.get("market_recommendations_meta", {}).get("target_snapshot_date") or "").strip()
        or get_latest_lake_trade_date(market="CN")
        or app_today_iso()
    )
    enriched_payload.setdefault("report_date", default_report_date)
    enriched_payload["saved_at"] = app_now_iso()
    AppSettingRepository(db).set(AI_DAILY_REPORT_KEY, json.dumps(enriched_payload, ensure_ascii=False))
    WorkspaceSnapshotRepository(db).create_snapshot(
        snapshot_type=AI_DAILY_REPORT_SNAPSHOT_TYPE,
        snapshot_date=str(enriched_payload.get("report_date") or app_today_iso()),
        payload=enriched_payload,
    )


def load_ai_daily_report(*, db=None) -> dict | None:
    if db is None:
        with SessionLocal() as own_db:
            return load_ai_daily_report(db=own_db)
    raw = AppSettingRepository(db).get(AI_DAILY_REPORT_KEY)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def list_ai_daily_report_history(*, limit: int = 30, db=None) -> list[dict]:
    if db is None:
        with SessionLocal() as own_db:
            return list_ai_daily_report_history(limit=limit, db=own_db)
    return WorkspaceSnapshotRepository(db).list_snapshots(AI_DAILY_REPORT_SNAPSHOT_TYPE, limit=limit)


def load_ai_daily_report_history_item(snapshot_id: int, *, db=None) -> dict | None:
    if db is None:
        with SessionLocal() as own_db:
            return load_ai_daily_report_history_item(snapshot_id, db=own_db)
    return WorkspaceSnapshotRepository(db).get_snapshot(snapshot_id, snapshot_type=AI_DAILY_REPORT_SNAPSHOT_TYPE)


def _render_recommendation_regression_lines(payload: dict) -> list[str]:
    regression = payload.get("recommendation_regression") or {}
    policy = regression.get("policy") or {}
    notes = [str(item).strip() for item in (policy.get("notes") or []) if str(item).strip()]
    summary = regression.get("summary") or {}
    actionable = summary.get("actionable") or {}
    sample_count = int(regression.get("sample_count") or 0)
    if not notes and sample_count <= 0:
        return []
    lines = ["历史回归调参："]
    if sample_count > 0:
        lines.append(
            "已回放最近 "
            f"{sample_count} 条日报候选；可执行池次日收盘胜率 "
            f"{actionable.get('close_hit_rate') if actionable.get('close_hit_rate') is not None else '-'}%，"
            f"次日可执行命中率 {actionable.get('execution_hit_rate') if actionable.get('execution_hit_rate') is not None else '-'}%。"
        )
    if notes:
        lines.extend([f"- {item}" for item in notes[:3]])
    lines.append("")
    return lines


def render_ai_daily_report_message(report: dict | None) -> str:
    payload = report or {}
    strategy = payload.get("strategy") or {}
    portfolio_summary = payload.get("portfolio_summary") or {}
    portfolio_rows = payload.get("portfolio_rows") or []
    market_rows = payload.get("market_recommendations") or payload.get("rows") or []
    market_watch_rows = payload.get("market_watch_recommendations") or []
    market_meta = payload.get("market_recommendations_meta") or {}
    market_structure = payload.get("market_structure") or {}
    market_template_attribution = payload.get("market_template_attribution") or {}
    guidance_summary = payload.get("model_selection_guidance_summary") or {}
    lightgbm_execution_bias = payload.get("lightgbm_execution_bias") or {}
    us_model_rows = payload.get("us_model_recommendations") or []
    us_market_meta = payload.get("us_model_recommendations_meta") or {}
    us_market_structure = payload.get("us_market_structure") or {}
    social_payload = payload.get("social_signal_summary") or {}
    social_rows = social_payload.get("actionable") or []
    us_hotspot_rows = payload.get("us_hotspot_validation") or []
    lines = [
        f"AI 每日复盘",
        f"市场状态：{payload.get('mood') or '-'}",
        f"摘要：{payload.get('headline') or '-'}",
        "",
        "一、持仓库总结",
        portfolio_summary.get("headline") or "-",
        portfolio_summary.get("action_note") or "-",
        "",
    ]
    for index, item in enumerate(portfolio_rows, start=1):
        risk_flags = format_risk_flags(item.get("risk_flags") or [], lang="zh")
        lines.extend(
            [
                f"{index}. {_report_security_label(item)}",
                f"持仓：{item.get('quantity') or '-'} 股 | 成本：{_fmt_number(item.get('cost_basis'))} | 最新价：{_fmt_number(item.get('latest_price'))}",
                f"浮动盈亏：{_fmt_number(item.get('pnl'))} | 收益率：{_fmt_number(item.get('pnl_pct'))}%",
                f"AI建议：{item.get('ai_verdict') or '-'} | {item.get('ai_headline') or '-'}",
                f"动作桶：{item.get('action_bucket') or '-'} | 目标仓位：{item.get('target_weight_text') or '-'} | 风险：{risk_flags}",
                f"操作备注：{item.get('ai_strategy') or '-'}",
                "",
            ]
        )
    lines.extend(
        [
        "二、明日可执行买入池",
        "以下候选来自收盘后全市场模型扫描，只保留更接近计划买点、且交易状态更适合次日执行的股票。",
        _render_market_meta_line(market_meta),
        "",
        f"策略主线：{strategy.get('headline') or '-'}",
        f"执行建议：{strategy.get('playbook') or '-'}",
        f"{guidance_summary.get('top_model_summary') or '优先模型：当前样本还不够，先继续观察。'}",
        f"{guidance_summary.get('top_combo_summary') or '优先组合：组合样本还不够，暂不强推。'}",
        f"{lightgbm_execution_bias.get('title') or 'LightGBM：今天先观察'}",
        f"{lightgbm_execution_bias.get('summary') or '-'}",
        "",
    ]
    )
    lines.extend(_render_recommendation_regression_lines(payload))
    lines.extend(_render_market_structure_lines(market_structure, title="固定结构：强方向 / 弱方向 / 风险清单"))
    if market_template_attribution.get("leaders"):
        lines.append("来源归因：")
        lines.append(market_template_attribution.get("headline") or "-")
        for item in (market_template_attribution.get("leaders") or [])[:4]:
            tickers = " / ".join(item.get("tickers") or []) or "-"
            lines.append(f"- {item.get('label') or '-'}：{int(item.get('count') or 0)} 只 · 量化均分 {item.get('avg_quant_rank') or '-'} · {tickers}")
        lines.append("")
    if strategy.get("bullets"):
        lines.extend([f"- {item}" for item in strategy.get("bullets")[:4]])
        lines.append("")
    if not market_rows:
        lines.append("当前没有满足条件的可执行买入池，今天更适合少做或只观察。")
        lines.append("")
    for index, item in enumerate(market_rows[:5], start=1):
        buy_zone = item.get("buy_zone") or {}
        take_profit = item.get("take_profit") or {}
        risk_flags = format_risk_flags(item.get("risk_flags") or [], lang="zh")
        lines.extend(
            [
                f"{index}. {_report_security_label(item)}",
                f"量化分：{item.get('quant_rank') or '-'} | 验证分：{item.get('verification_score') or '-'} | 模型分：{_fmt_number(item.get('model_score'))} | 趋势分：{item.get('trend_score') or '-'}",
                f"结论：{item.get('verdict') or '-'} | 置信度：{item.get('confidence') or '-'} | 策略：{item.get('strategy') or '-'}",
                build_trade_summary_text(item, lang="zh", include_execution_note=True) + f" | 建议仓位：{item.get('target_weight') or '-'}",
                f"验证依据：{item.get('verification_note') or '-'}",
                f"触发条件：{item.get('entry_trigger') or '-'}",
                f"失效条件：{item.get('invalidation_condition') or '-'}",
                f"持有周期：{item.get('time_horizon') or '-'} | 流动性桶：{item.get('liquidity_bucket') or '-'} | 最大滑点：{item.get('max_slippage_bps') or '-'}bps",
                f"风险标记：{risk_flags}",
                f"买入区：{buy_zone.get('low', '-')} - {buy_zone.get('high', '-')}",
                f"止损位：{item.get('stop_loss', '-')} | 止损类型：{item.get('stop_loss_type') or '-'}",
                f"止盈区：{take_profit.get('low', '-')} - {take_profit.get('high', '-')}",
                f"Summary：{item.get('summary') or '-'}",
                f"分池原因：{item.get('report_pool_reason') or '-'}",
                "",
            ]
        )
    lines.extend(
        [
            "三、强势观察池",
            "以下股票更像强势观察对象：通常已经脱离计划买点，或者仍处于 REVIEW 状态，不适合直接追。",
            "",
        ]
    )
    if not market_watch_rows:
        lines.append("当前没有需要单独列出的强势观察池股票。")
        lines.append("")
    for index, item in enumerate(market_watch_rows[:5], start=1):
        buy_zone = item.get("buy_zone") or {}
        risk_flags = format_risk_flags(item.get("risk_flags") or [], lang="zh")
        lines.extend(
            [
                f"{index}. {_report_security_label(item)}",
                f"结论：{item.get('verdict') or '-'} | 可交易性：{format_trade_status(item.get('tradability_status'), lang='zh')} | 趋势分：{item.get('trend_score') or '-'}",
                f"当前价：{_fmt_number(item.get('latest_price') or item.get('latest_close'))} | 买入区：{buy_zone.get('low', '-')} - {buy_zone.get('high', '-')}",
                f"偏离买点：{_fmt_number(item.get('close_vs_buy_zone_high_pct'))}% | 风险：{risk_flags}",
                f"观察理由：{item.get('report_pool_reason') or '-'}",
                "",
            ]
        )
    if us_model_rows:
        lines.extend(
            [
                "四、美股模型 Top 5",
                "以下候选来自最新美股模型训练结果，优先展示验证分高、交易条件清楚、且未进入当前持仓/自选的名字。",
                _render_market_meta_line(us_market_meta),
                "",
            ]
        )
        lines.extend(_render_market_structure_lines(us_market_structure, title="固定结构：美股强方向 / 风险清单"))
        for index, item in enumerate(us_model_rows[:5], start=1):
            buy_zone = item.get("buy_zone") or {}
            take_profit = item.get("take_profit") or {}
            risk_flags = format_risk_flags(item.get("risk_flags") or [], lang="zh")
            lines.extend(
                [
                    f"{index}. {_report_security_label(item)}",
                    f"量化分：{item.get('quant_rank') or '-'} | 验证分：{item.get('verification_score') or '-'} | 模型分：{_fmt_number(item.get('model_score'))} | 趋势分：{item.get('trend_score') or '-'}",
                    f"结论：{item.get('verdict') or '-'} | 置信度：{item.get('confidence') or '-'} | 策略：{item.get('strategy') or '-'}",
                    build_trade_summary_text(item, lang="zh", include_execution_note=True) + f" | 建议仓位：{item.get('target_weight') or '-'}",
                    f"验证依据：{item.get('verification_note') or '-'}",
                    f"触发条件：{item.get('entry_trigger') or '-'}",
                    f"失效条件：{item.get('invalidation_condition') or '-'}",
                    f"持有周期：{item.get('time_horizon') or '-'} | 流动性桶：{item.get('liquidity_bucket') or '-'} | 最大滑点：{item.get('max_slippage_bps') or '-'}bps",
                    f"风险标记：{risk_flags}",
                    f"买入区：{buy_zone.get('low', '-')} - {buy_zone.get('high', '-')}",
                    f"止损位：{item.get('stop_loss', '-')} | 止损类型：{item.get('stop_loss_type') or '-'}",
                    f"止盈区：{take_profit.get('low', '-')} - {take_profit.get('high', '-')}",
                    f"Summary：{item.get('summary') or '-'}",
                    "",
                ]
            )
    if social_rows:
        lines.extend(
            [
                "五、X 账户社交信号验证",
                "以下只作为社交观点和模型共振参考，不直接作为买卖依据。",
                "",
            ]
        )
        for index, item in enumerate(social_rows[:5], start=1):
            lines.extend(
                [
                    f"{index}. {_report_security_label(item)}",
                    f"来源：{item.get('handle') or '-'} | 观点：{item.get('social_view') or '-'} | 验证分：{item.get('validation_score') or 0}",
                    f"模型：{item.get('model_signal_label') or '-'} | 动作：{item.get('system_action') or '-'}",
                    f"理由：{' / '.join(item.get('validation_reasons') or []) or '-'}",
                    "",
                ]
            )
    if us_hotspot_rows:
        lines.extend(
            [
                "六、X 热点美股验证",
                "以下是 X 提及美股与美股模型候选快照的交叉验证，仅作为复核清单。",
                "",
            ]
        )
        for index, item in enumerate(us_hotspot_rows[:5], start=1):
            lines.extend(
                [
                    f"{index}. {_report_security_label(item)}",
                    f"来源：{item.get('handle') or '-'} | X观点：{item.get('social_view') or '-'} | 社交验证分：{item.get('validation_score') or 0}",
                    f"美股模型：{item.get('template') or '-'} | Top排名：{item.get('us_rank') or '-'} | 趋势分：{item.get('trend_score') or '-'}",
                    f"结论：{item.get('cross_validation_note') or '-'}",
                    "",
                ]
            )
    return "\n".join(lines).strip()


def render_ai_daily_report_push_messages(report: dict | None) -> list[dict]:
    payload = report or {}
    messages = [
        {
            "title": "AI 日报 1/2：持仓股总结",
            "body": _render_portfolio_push_message(payload),
        },
        {
            "title": "AI 日报 2/2：可执行买入池 + 强势观察池",
            "body": _render_market_top5_push_message(payload),
        },
    ]
    if payload.get("us_model_recommendations"):
        messages[0]["title"] = "AI 日报 1/3：持仓股总结"
        messages[1]["title"] = "AI 日报 2/3：A股可执行买入池 + 观察池"
        messages.append(
            {
                "title": "AI 日报 3/3：美股模型 Top 5",
                "body": _render_us_market_top5_push_message(payload),
            }
        )
    return messages


def _render_portfolio_push_message(payload: dict) -> str:
    portfolio_summary = payload.get("portfolio_summary") or {}
    portfolio_rows = payload.get("portfolio_rows") or []
    lines = [
        "一、持仓股总结",
        portfolio_summary.get("headline") or "-",
        portfolio_summary.get("action_note") or "-",
        "",
    ]
    if not portfolio_rows:
        lines.append("当前持仓库为空，暂无需要复核的持仓。")
        return "\n".join(lines).strip()
    for index, item in enumerate(portfolio_rows, start=1):
        risk_flags = format_risk_flags(item.get("risk_flags") or [], lang="zh")
        lines.extend(
            [
                f"{index}. {_report_security_label(item)}",
                f"持仓：{item.get('quantity') or '-'} 股 | 成本：{_fmt_number(item.get('cost_basis'))} | 最新价：{_fmt_number(item.get('latest_price'))}",
                f"浮动盈亏：{_fmt_number(item.get('pnl'))} | 收益率：{_fmt_number(item.get('pnl_pct'))}%",
                f"AI建议：{item.get('ai_verdict') or '-'} | {item.get('ai_headline') or '-'}",
                f"动作桶：{item.get('action_bucket') or '-'} | 目标仓位：{item.get('target_weight_text') or '-'} | 风险：{risk_flags}",
                f"操作备注：{item.get('ai_strategy') or '-'}",
                "",
            ]
        )
    return "\n".join(lines).strip()


def _render_market_top5_push_message(payload: dict) -> str:
    strategy = payload.get("strategy") or {}
    market_rows = payload.get("market_recommendations") or payload.get("rows") or []
    market_watch_rows = payload.get("market_watch_recommendations") or []
    market_meta = payload.get("market_recommendations_meta") or {}
    market_structure = payload.get("market_structure") or {}
    market_template_attribution = payload.get("market_template_attribution") or {}
    guidance_summary = payload.get("model_selection_guidance_summary") or {}
    lightgbm_execution_bias = payload.get("lightgbm_execution_bias") or {}
    lines = [
        "二、明日可执行买入池",
        "以下候选来自收盘后全市场模型扫描，不包含当前自选股和持仓股；仅保留更接近计划买点、且适合次日执行的名字。",
        "",
        _render_market_meta_line(market_meta),
        f"市场状态：{payload.get('mood') or '-'}",
        f"策略主线：{strategy.get('headline') or '-'}",
        f"执行建议：{strategy.get('playbook') or '-'}",
        f"{guidance_summary.get('top_model_summary') or '优先模型：当前样本还不够，先继续观察。'}",
        f"{guidance_summary.get('top_combo_summary') or '优先组合：组合样本还不够，暂不强推。'}",
        f"{lightgbm_execution_bias.get('title') or 'LightGBM：今天先观察'}",
        f"{lightgbm_execution_bias.get('summary') or '-'}",
        "",
    ]
    lines.extend(_render_recommendation_regression_lines(payload))
    lines.extend(_render_market_structure_lines(market_structure, title="固定结构：强方向 / 弱方向 / 风险清单"))
    if market_template_attribution.get("leaders"):
        lines.append("来源归因：")
        for item in (market_template_attribution.get("leaders") or [])[:3]:
            tickers = " / ".join(item.get("tickers") or []) or "-"
            lines.append(f"- {item.get('label') or '-'}：{int(item.get('count') or 0)} 只 · {tickers}")
        lines.append("")
    if strategy.get("bullets"):
        lines.extend([f"- {item}" for item in strategy.get("bullets")[:3]])
        lines.append("")
    if not market_rows:
        lines.append("当前没有满足条件的可执行买入池，今天更适合少做或只观察。")
        lines.append("")
    for index, item in enumerate(market_rows[:5], start=1):
        buy_zone = item.get("buy_zone") or {}
        take_profit = item.get("take_profit") or {}
        risk_flags = format_risk_flags(item.get("risk_flags") or [], lang="zh")
        lines.extend(
            [
                f"{index}. {_report_security_label(item)}",
                f"量化分：{item.get('quant_rank') or '-'} | 验证分：{item.get('verification_score') or '-'} | 趋势分：{item.get('trend_score') or '-'}",
                f"结论：{item.get('verdict') or '-'} | 置信度：{item.get('confidence') or '-'} | 策略：{item.get('strategy') or '-'}",
                build_trade_summary_text(item, lang="zh") + f" | 建议仓位：{item.get('target_weight') or '-'}",
                f"触发条件：{item.get('entry_trigger') or '-'}",
                f"失效条件：{item.get('invalidation_condition') or '-'}",
                f"买入区：{buy_zone.get('low', '-')} - {buy_zone.get('high', '-')} | 止损：{item.get('stop_loss', '-')}",
                f"止盈区：{take_profit.get('low', '-')} - {take_profit.get('high', '-')}",
                f"风险：{risk_flags}",
                f"验证依据：{item.get('verification_note') or '-'}",
                f"入池原因：{item.get('report_pool_reason') or '-'}",
                "",
            ]
        )
    lines.extend(
        [
            "三、强势观察池",
            "以下股票更像强势观察对象：要么仍待复核，要么已经偏离买点，不适合直接追。",
            "",
        ]
    )
    if not market_watch_rows:
        lines.append("当前没有需要单独列出的强势观察池股票。")
        return "\n".join(lines).strip()
    for index, item in enumerate(market_watch_rows[:5], start=1):
        buy_zone = item.get("buy_zone") or {}
        risk_flags = format_risk_flags(item.get("risk_flags") or [], lang="zh")
        lines.extend(
            [
                f"{index}. {_report_security_label(item)}",
                f"结论：{item.get('verdict') or '-'} | 可交易性：{format_trade_status(item.get('tradability_status'), lang='zh')}",
                f"当前价：{_fmt_number(item.get('latest_price') or item.get('latest_close'))} | 买入区：{buy_zone.get('low', '-')} - {buy_zone.get('high', '-')}",
                f"偏离买点：{_fmt_number(item.get('close_vs_buy_zone_high_pct'))}% | 风险：{risk_flags}",
                f"观察理由：{item.get('report_pool_reason') or '-'}",
                "",
            ]
        )
    return "\n".join(lines).strip()


def _render_us_market_top5_push_message(payload: dict) -> str:
    us_rows = payload.get("us_model_recommendations") or []
    us_market_meta = payload.get("us_model_recommendations_meta") or {}
    us_market_structure = payload.get("us_market_structure") or {}
    lines = [
        "三、美股模型 Top 5",
        "以下候选来自最新美股模型训练结果，不包含当前自选股和持仓股；建议只在触发条件满足时执行。",
        "",
        _render_market_meta_line(us_market_meta),
    ]
    lines.extend(_render_market_structure_lines(us_market_structure, title="固定结构：美股强方向 / 风险清单"))
    if not us_rows:
        lines.append("暂无满足条件的美股模型 Top 5 候选。")
        return "\n".join(lines).strip()
    for index, item in enumerate(us_rows[:5], start=1):
        buy_zone = item.get("buy_zone") or {}
        take_profit = item.get("take_profit") or {}
        risk_flags = format_risk_flags(item.get("risk_flags") or [], lang="zh")
        lines.extend(
            [
                f"{index}. {_report_security_label(item)}",
                f"量化分：{item.get('quant_rank') or '-'} | 验证分：{item.get('verification_score') or '-'} | 趋势分：{item.get('trend_score') or '-'}",
                f"结论：{item.get('verdict') or '-'} | 置信度：{item.get('confidence') or '-'} | 策略：{item.get('strategy') or '-'}",
                build_trade_summary_text(item, lang="zh") + f" | 建议仓位：{item.get('target_weight') or '-'}",
                f"触发条件：{item.get('entry_trigger') or '-'}",
                f"失效条件：{item.get('invalidation_condition') or '-'}",
                f"买入区：{buy_zone.get('low', '-')} - {buy_zone.get('high', '-')} | 止损：{item.get('stop_loss', '-')}",
                f"止盈区：{take_profit.get('low', '-')} - {take_profit.get('high', '-')}",
                f"风险：{risk_flags}",
                f"验证依据：{item.get('verification_note') or '-'}",
                "",
            ]
        )
    return "\n".join(lines).strip()


def _render_legacy_ai_daily_report_message(report: dict | None) -> str:
    payload = report or {}
    strategy = payload.get("strategy") or {}
    lines = [
        f"A股 AI 每日决策面板",
        f"市场状态：{payload.get('mood') or '-'}",
        f"摘要：{payload.get('headline') or '-'}",
        f"策略主线：{strategy.get('headline') or '-'}",
        f"执行建议：{strategy.get('playbook') or '-'}",
        "",
    ]
    if strategy.get("bullets"):
        lines.extend([f"- {item}" for item in strategy.get("bullets")[:4]])
        lines.append("")
    for index, item in enumerate(payload.get("rows") or [], start=1):
        buy_zone = item.get("buy_zone") or {}
        take_profit = item.get("take_profit") or {}
        risk_flags = format_risk_flags(item.get("risk_flags") or [], lang="zh")
        lines.extend(
            [
                f"{index}. {_report_security_label(item)}",
                f"量化分：{item.get('quant_rank') or '-'} | 模型分：{_fmt_number(item.get('model_score'))} | 趋势分：{item.get('trend_score') or '-'} | Setup：{item.get('setup_label') or '-'}",
                f"结论：{item.get('verdict') or '-'} | 置信度：{item.get('confidence') or '-'} | 策略：{item.get('strategy') or '-'}",
                build_trade_summary_text(item, lang="zh", include_execution_note=True) + f" | 建议仓位：{item.get('target_weight') or '-'}",
                f"参与率：{_fmt_percent(item.get('suggested_participation_rate'))} | 执行计划：{item.get('execution_plan') or '-'}",
                f"触发条件：{item.get('entry_trigger') or '-'}",
                f"失效条件：{item.get('invalidation_condition') or '-'}",
                f"持有周期：{item.get('time_horizon') or '-'} | 流动性桶：{item.get('liquidity_bucket') or '-'} | 最大滑点：{item.get('max_slippage_bps') or '-'}bps",
                f"风险标记：{risk_flags}",
                f"Headline：{item.get('headline') or '-'}",
                f"Summary：{item.get('summary') or '-'}",
                f"买入区：{buy_zone.get('low', '-')} - {buy_zone.get('high', '-')}",
                f"止损位：{item.get('stop_loss', '-')} | 止损类型：{item.get('stop_loss_type') or '-'}",
                f"止盈区：{take_profit.get('low', '-')} - {take_profit.get('high', '-')}",
                "",
            ]
        )
    buy_the_dip_rows = payload.get("buy_the_dip_rows") or []
    if buy_the_dip_rows:
        lines.extend(["Buy The Dip 候选:", ""])
        for index, item in enumerate(buy_the_dip_rows, start=1):
            buy_zone = item.get("buy_zone") or {}
            risk_flags = format_risk_flags(item.get("risk_flags") or [], lang="zh")
            lines.extend(
                [
                    f"{index}. {_report_security_label(item)}",
                    f"量化分：{item.get('quant_rank') or '-'} | 模型分：{_fmt_number(item.get('model_score'))} | 趋势分：{item.get('trend_score') or '-'}",
                    f"结论：{item.get('verdict') or '-'} | Setup：{item.get('setup_label') or '-'}",
                    build_trade_summary_text(item, lang="zh") + f" | 建议仓位：{item.get('target_weight') or '-'} | 风险标记：{risk_flags}",
                    f"参与率：{_fmt_percent(item.get('suggested_participation_rate'))} | 执行计划：{item.get('execution_plan') or '-'}",
                    f"触发条件：{item.get('entry_trigger') or '-'} | 失效条件：{item.get('invalidation_condition') or '-'}",
                    f"持有周期：{item.get('time_horizon') or '-'} | 流动性桶：{item.get('liquidity_bucket') or '-'} | 最大滑点：{item.get('max_slippage_bps') or '-'}bps",
                    f"止损位：{item.get('stop_loss', '-')} | 止损类型：{item.get('stop_loss_type') or '-'}",
                    f"回踩区：{buy_zone.get('low', '-')} - {buy_zone.get('high', '-')}",
                    "",
                ]
            )
    return "\n".join(lines).strip()


def _render_market_structure_lines(structure: dict, *, title: str) -> list[str]:
    payload = structure or {}
    lines = [title, payload.get("headline") or "-", ""]
    strong = payload.get("strong_sectors") or []
    weak = payload.get("weak_sectors") or []
    risk_watch = payload.get("risk_watch") or []
    if strong:
        lines.append("强方向：")
        for item in strong[:3]:
            lines.append(
                f"- {item.get('label')}: {item.get('count')} 只，均强度 {item.get('avg_strength') or '-'}，代表 {' / '.join((item.get('tickers') or [])[:3]) or '-'}"
            )
        lines.append("")
    if weak:
        lines.append("弱方向 / 风险集中：")
        for item in weak[:3]:
            lines.append(
                f"- {item.get('label')}: 风险均值 {item.get('avg_risk') or '-'}，涉及 {' / '.join((item.get('tickers') or [])[:3]) or '-'}"
            )
        lines.append("")
    if risk_watch:
        lines.append("风险清单：")
        for item in risk_watch[:4]:
            risk_flags = format_risk_flags(item.get("risk_flags") or [], lang="zh")
            lines.append(
                f"- {_report_security_label(item)}: {item.get('tradability_status') or '-'} | {risk_flags} | {item.get('headline') or '-'}"
            )
        lines.append("")
    return lines


def _report_security_label(item: dict | None) -> str:
    payload = item or {}
    ticker = str(payload.get("ticker") or "").strip()
    name = str(payload.get("name") or "").strip()
    if name and ticker and name != ticker:
        return f"{name}（{ticker}）"
    return name or ticker or "-"


def _render_market_meta_line(meta: dict | None) -> str:
    payload = meta or {}
    status = str(payload.get("status") or "").strip().lower()
    note = str(payload.get("note") or "").strip()
    if note:
        if status == "fallback":
            return f"候选状态：降级中。{note}"
        if status == "not_ready":
            return f"候选状态：未就绪。{note}"
        return f"候选状态：已就绪。{note}"
    if status == "fallback":
        return "候选状态：今日快照未完全就绪，当前使用最新模型预测降级候选。"
    if status == "not_ready":
        return "候选状态：今日全市场候选尚未就绪。"
    return "候选状态：已使用今日全市场候选。"


def _build_market_strategy(*, rows: list[dict], mood: str) -> dict:
    bullish_rows = [row for row in rows if str(row.get("verdict") or "").upper() in {"BUY", "STRONG BUY"}]
    cautious_rows = [row for row in rows if str(row.get("verdict") or "").upper() in {"SELL", "STRONG SELL"}]
    top_buy = bullish_rows[:2]
    top_caution = cautious_rows[:2]
    if mood == "偏进攻":
        headline = "市场更适合围绕强势股做顺势交易"
        playbook = "优先做高置信度 BUY 标的，入场更看回踩承接与突破确认，不建议分散开太多低质量仓位。"
    elif mood == "偏防守":
        headline = "市场更适合防守和等待确认"
        playbook = "先控制仓位，把重点放在风险位和止损纪律，宁可错过，也不要在弱势结构里强行抄底。"
    else:
        headline = "市场处于均衡观察阶段"
        playbook = "以观察和候选池管理为主，优先跟踪最强的 1-2 个方向，等待更清晰的共振。"

    bullets: list[str] = []
    if top_buy:
        bullets.append("优先跟踪: " + " / ".join(row.get("ticker") or "-" for row in top_buy))
    if top_caution:
        bullets.append("谨慎对待: " + " / ".join(row.get("ticker") or "-" for row in top_caution))
    if bullish_rows and not cautious_rows:
        bullets.append("当前日报里偏多结论明显更多，说明短线环境对趋势延续更友好。")
    elif cautious_rows and not bullish_rows:
        bullets.append("当前日报里偏谨慎结论更集中，说明环境更偏向防守与等待。")
    else:
        bullets.append("多空信号并存，更适合集中火力处理少数高质量机会。")

    return {
        "headline": headline,
        "playbook": playbook,
        "bullets": bullets[:4],
    }


def _candidate_quant_score(candidate: dict, combined: dict) -> float:
    score = _safe_float(candidate.get("score"))
    confidence = _safe_float(candidate.get("confidence"))
    signal_strength = _safe_float(candidate.get("signal_strength"))
    reward_risk = _safe_float(candidate.get("model_reward_risk_ratio"))
    percentile = _safe_float(candidate.get("percentile"))
    trend_score = _safe_float((combined or {}).get("trend_score"))
    setup_label = str((combined or {}).get("setup_label") or "")

    quant_rank = 0.0
    quant_rank += score * 1200.0
    quant_rank += confidence * 35.0
    quant_rank += signal_strength * 0.45
    quant_rank += trend_score * 0.8
    quant_rank += percentile * 0.08
    quant_rank += min(4.0, reward_risk) * 8.0
    if setup_label == "pullback_buy":
        quant_rank += 6.0
    elif setup_label == "breakout_watch":
        quant_rank += 4.0
    return round(quant_rank, 1)


def _candidate_verification_score(candidate: dict, combined: dict) -> float:
    quant_rank = _candidate_quant_score(candidate, combined)
    tradability = str(candidate.get("tradability_status") or "").upper()
    readiness = _safe_float(candidate.get("trade_readiness_score"))
    readiness_bucket = str(candidate.get("readiness_bucket") or "").upper()
    setup_label = str((combined or {}).get("setup_label") or "")
    signal_label = str(candidate.get("signal_label") or "").upper()
    score = quant_rank
    if readiness > 0:
        score += readiness * 0.9
    if readiness_bucket == "HIGH":
        score += 18.0
    elif readiness_bucket == "MEDIUM":
        score += 8.0
    elif readiness_bucket in {"LOW", "BLOCKED"}:
        score -= 24.0
    if tradability == "READY":
        score += 28.0
    elif tradability in {"REVIEW", "DEFER"}:
        score += 10.0
    elif tradability == "BLOCKED":
        score -= 45.0
    if setup_label in {"pullback_buy", "breakout_watch"}:
        score += 12.0
    if signal_label == "BUY":
        score += 12.0
    risk_flags = candidate.get("risk_flags") or []
    score -= min(24.0, len(risk_flags) * 8.0)
    if candidate.get("entry_trigger"):
        score += 8.0
    if candidate.get("invalidation_condition"):
        score += 8.0
    if candidate.get("target_weight") is not None:
        score += 4.0
    return round(score, 1)


def _build_verification_note(*, latest_signal: dict | None, combined: dict | None) -> str:
    signal = latest_signal or {}
    setup = str((combined or {}).get("setup_label") or "").strip()
    trigger = signal.get("entry_trigger")
    invalidation = signal.get("invalidation_condition")
    parts: list[str] = []
    if setup:
        parts.append(f"形态: {setup}")
    if trigger:
        parts.append(f"触发: {trigger}")
    if invalidation:
        parts.append(f"失效: {invalidation}")
    if signal.get("liquidity_bucket"):
        parts.append(f"流动性: {signal.get('liquidity_bucket')}")
    if signal.get("report_source_label"):
        parts.append(f"来源榜单: {signal.get('report_source_label')}")
    elif signal.get("full_market_template"):
        parts.append(f"来源榜单: {signal.get('full_market_template')}")
    return "；".join(parts) or "等待模型触发、失效位和流动性条件进一步确认。"


def _build_portfolio_report_rows(*, db, symbol_repo: SymbolRepository, prediction_repo: PredictionRepository) -> tuple[list[dict], dict]:
    positions = load_portfolio_positions()
    tickers = [str(item.get("ticker") or "").strip().upper() for item in positions if item.get("ticker")]
    latest_closes = load_latest_closes(tickers)
    latest_outputs = prediction_repo.get_latest_model_outputs_for_tickers(tickers)
    total_market_value = 0.0
    base_rows: list[dict] = []
    for position in positions:
        ticker = str(position.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        quantity = _safe_float(position.get("quantity"))
        cost_basis = _safe_float(position.get("cost_basis"))
        latest_close = latest_closes.get(ticker)
        latest_price = _safe_float(latest_close)
        market_value = latest_price * quantity if latest_price and quantity else 0.0
        cost_value = cost_basis * quantity if cost_basis and quantity else 0.0
        pnl = market_value - cost_value if cost_value else 0.0
        pnl_pct = ((latest_price / cost_basis) - 1.0) * 100.0 if latest_price and cost_basis else 0.0
        total_market_value += market_value
        overview = symbol_repo.get_overview(ticker) or {}
        latest_signal = latest_outputs.get(ticker) or {}
        base_rows.append(
            {
                "ticker": ticker,
                "name": position.get("name") or overview.get("name") or ticker,
                "market": position.get("market") or overview.get("market"),
                "quantity": quantity,
                "cost_basis": cost_basis,
                "latest_price": latest_price if latest_price else None,
                "market_value": market_value,
                "cost_value": cost_value,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "latest_signal": latest_signal,
            }
        )

    rows: list[dict] = []
    for item in base_rows:
        ai_summary = build_portfolio_ai_summary(
            latest_signal=item.get("latest_signal"),
            pnl_pct=float(item.get("pnl_pct") or 0.0),
            cost_basis=float(item.get("cost_basis") or 0.0),
            lang="zh",
        )
        management = build_position_management_fields(
            latest_signal=item.get("latest_signal"),
            pnl_pct=float(item.get("pnl_pct") or 0.0),
            market_value=float(item.get("market_value") or 0.0),
            total_market_value=total_market_value,
            cost_basis=float(item.get("cost_basis") or 0.0),
            lang="zh",
        )
        latest_signal = item.get("latest_signal") or {}
        rows.append(
            {
                **{key: value for key, value in item.items() if key != "latest_signal"},
                **ai_summary,
                **management,
                "model_score": latest_signal.get("score"),
                "signal_label": latest_signal.get("signal_label"),
                "signal_strength": latest_signal.get("signal_strength"),
                "entry_trigger": latest_signal.get("entry_trigger"),
                "invalidation_condition": latest_signal.get("invalidation_condition"),
                "risk_flags": latest_signal.get("risk_flags") or [],
            }
        )

    rows.sort(key=lambda item: (-abs(float(item.get("pnl_pct") or 0.0)), -float(item.get("market_value") or 0.0), item.get("ticker") or ""))
    total_cost = sum(float(item.get("cost_value") or 0.0) for item in rows)
    total_pnl = sum(float(item.get("pnl") or 0.0) for item in rows)
    total_pnl_pct = (total_pnl / total_cost * 100.0) if total_cost else 0.0
    risk_count = sum(1 for item in rows if str(item.get("action_bucket_key") or "") in {"risk_reduction", "profit_protection", "complete_cost"})
    summary = {
        "position_count": len(rows),
        "total_market_value": total_market_value,
        "total_cost": total_cost,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "risk_count": risk_count,
        "headline": (
            f"当前持仓 {len(rows)} 只，总浮动盈亏 {total_pnl:.2f}，收益率 {total_pnl_pct:.2f}%。"
            if rows
            else "当前持仓库为空，日报持仓部分暂无可复核对象。"
        ),
        "action_note": (
            f"优先复核 {risk_count} 只需要风控、止盈或补成本信息的持仓。"
            if risk_count
            else "持仓暂无明显高优先级风险动作，继续跟踪模型信号和仓位漂移。"
        ),
    }
    return rows, summary


def _load_owned_or_watched_tickers(watchlist_repo: WatchlistRepository) -> set[str]:
    excluded: set[str] = set()
    try:
        watchlist = watchlist_repo.get_or_create_default()
        excluded.update(str(item.get("ticker") or "").strip().upper() for item in watchlist_repo.list_items(watchlist.id))
    except Exception:
        pass
    excluded.update(str(item.get("ticker") or "").strip().upper() for item in load_portfolio_positions())
    return {ticker for ticker in excluded if ticker}


def _recent_daily_report_repeat_counts(*, db, market: str, lookback_days: int = 5, history_limit: int = 12) -> dict[str, int]:
    market_code = str(market or "").upper()
    counts: dict[str, int] = {}
    today = app_today_iso()
    try:
        today_date = datetime.fromisoformat(today).date()
    except ValueError:
        today_date = None
    for item in list_ai_daily_report_history(limit=history_limit, db=db):
        payload = item.get("payload") or {}
        created_at = str(item.get("created_at") or "")
        created_day = created_at[:10] if len(created_at) >= 10 else ""
        report_day = str(payload.get("report_date") or item.get("snapshot_date") or "")[:10]
        if report_day == today:
            continue
        if today_date and created_day:
            try:
                created_date = datetime.fromisoformat(created_day).date()
            except ValueError:
                created_date = None
            if created_date and (today_date - created_date) > timedelta(days=lookback_days):
                continue
        if market_code == "US":
            rows = payload.get("us_model_recommendations") or []
        else:
            rows = payload.get("market_recommendations") or payload.get("rows") or []
        if not isinstance(rows, list):
            continue
        seen_in_report: set[str] = set()
        for row in rows[:5]:
            ticker = str((row or {}).get("ticker") or "").strip().upper()
            if not ticker or ticker in seen_in_report:
                continue
            counts[ticker] = counts.get(ticker, 0) + 1
            seen_in_report.add(ticker)
    return counts


def _full_market_report_snapshot_sources(*, db, market: str) -> list[dict]:
    sources: list[dict] = []
    seen_snapshot_types: set[str] = set()

    def add_source(*, kind: str, label: str, template: str, params: dict) -> None:
        snapshot_type = screener_snapshot_type(params)
        if snapshot_type in seen_snapshot_types:
            return
        seen_snapshot_types.add(snapshot_type)
        sources.append(
            {
                "kind": kind,
                "label": label,
                "template": template,
                "params": params,
                "snapshot_type": snapshot_type,
            }
        )

    for template in FULL_MARKET_REPORT_TEMPLATES:
        add_source(
            kind="fixed_template",
            label=template,
            template=template,
            params=build_base_precompute_params(model_template=template, universe="full_market", market=market),
        )

    try:
        guidance = load_model_selection_guidance_snapshot(db, market=market, allow_fallback=True)
    except Exception:
        guidance = {}

    for item in (guidance.get("recommendations") or [])[:3]:
        template = str(item.get("template") or "").strip()
        if not template:
            continue
        add_source(
            kind="guidance_template",
            label=str(item.get("title") or template),
            template=template,
            params=build_base_precompute_params(model_template=template, universe="full_market", market=market),
        )

    for combo in (guidance.get("combos") or [])[:4]:
        preset_key = str(combo.get("key") or "").strip()
        if not preset_key:
            continue
        for params in build_multi_model_precompute_params(markets=[market], preset_keys=[preset_key]):
            combo_label = combo.get("label") or {}
            label = str(
                (combo_label.get("zh") if isinstance(combo_label, dict) else combo_label)
                or params.get("preset_label")
                or preset_key
            )
            add_source(
                kind="guidance_combo",
                label=label,
                template=str(params.get("preset_key") or preset_key),
                params=params,
            )

    return sources


def _load_full_market_report_candidates(
    *,
    db,
    market: str,
    excluded_tickers: set[str],
    limit: int,
    with_meta: bool = False,
) -> list[dict] | tuple[list[dict], dict]:
    snapshot_repo = WorkspaceSnapshotRepository(db)
    market_snapshot_context = load_market_context_snapshot(db, market=market)
    candidate_map: dict[str, dict] = {}
    repeat_counts = _recent_daily_report_repeat_counts(db=db, market=market)
    latest_trade_date = get_latest_lake_trade_date(market=market)
    snapshot_sources = _full_market_report_snapshot_sources(db=db, market=market)
    meta = {
        "market": market,
        "snapshot_templates_considered": len(snapshot_sources),
        "snapshot_templates_ready": 0,
        "snapshot_rows": 0,
        "blocked_candidates": 0,
        "target_snapshot_date": None,
        "latest_trade_date": latest_trade_date,
        "snapshot_sources": [
            {"kind": item.get("kind"), "label": item.get("label"), "template": item.get("template")}
            for item in snapshot_sources
        ],
    }
    snapshot_batches: list[tuple[dict, dict, str, list[dict]]] = []
    for source in snapshot_sources:
        params = dict(source.get("params") or {})
        snapshot = snapshot_repo.get_latest_snapshot(screener_snapshot_type(params))
        snapshot_day = str((snapshot or {}).get("snapshot_date") or "")[:10]
        payload = (snapshot or {}).get("payload") if isinstance(snapshot, dict) else None
        rows = (payload or {}).get("rows") if isinstance(payload, dict) else None
        if not snapshot_day or not isinstance(rows, list):
            continue
        snapshot_batches.append((source, snapshot or {}, snapshot_day, rows))
    available_snapshot_days = {snapshot_day for _source, _snapshot, snapshot_day, _rows in snapshot_batches}
    if latest_trade_date and latest_trade_date in available_snapshot_days:
        target_snapshot_date = latest_trade_date
    elif snapshot_batches:
        target_snapshot_date = max(snapshot_day for _source, _snapshot, snapshot_day, _rows in snapshot_batches)
        meta["target_snapshot_date"] = target_snapshot_date
    else:
        target_snapshot_date = ""
    if target_snapshot_date:
        meta["target_snapshot_date"] = target_snapshot_date
    for source, snapshot, snapshot_day, rows in snapshot_batches:
        if target_snapshot_date and snapshot_day != target_snapshot_date:
            continue
        meta["snapshot_templates_ready"] = int(meta.get("snapshot_templates_ready") or 0) + 1
        meta["snapshot_rows"] = int(meta.get("snapshot_rows") or 0) + len(rows)
        for row in rows:
            ticker = str(row.get("ticker") or "").strip().upper()
            if not ticker or ticker in excluded_tickers:
                continue
            candidate = _candidate_from_full_market_row(
                row,
                template=str(source.get("template") or source.get("label") or ""),
                market=market,
                market_snapshot=market_snapshot_context,
            )
            candidate["report_source_kind"] = source.get("kind")
            candidate["report_source_label"] = source.get("label")
            gate = _recommendation_gate(candidate)
            candidate["recommendation_gate_config"] = gate.get("config")
            if not gate["allowed"]:
                meta["blocked_candidates"] = int(meta.get("blocked_candidates") or 0) + 1
                continue
            repeat_count = int(repeat_counts.get(ticker) or 0)
            if repeat_count > 0:
                candidate["recent_report_repeat_count"] = repeat_count
                candidate["full_market_rank_score"] = round(
                    float(candidate.get("full_market_rank_score") or 0.0) - (repeat_count * 18.0),
                    1,
                )
            existing = candidate_map.get(ticker)
            if existing is None or float(candidate.get("full_market_rank_score") or 0.0) > float(existing.get("full_market_rank_score") or 0.0):
                candidate_map[ticker] = candidate
    candidates = list(candidate_map.values())
    candidates.sort(
        key=lambda item: (
            0 if item.get("score") is not None else 1,
            0 if "missing-model-score" not in {
                str(flag).strip().lower() for flag in (item.get("risk_flags") or []) if str(flag).strip()
            } else 1,
            -float(item.get("full_market_rank_score") or 0.0),
            -float(item.get("trade_readiness_score") or 0.0),
            -float(item.get("trend_score") or 0.0),
            -float(item.get("signal_strength") or 0.0),
            item.get("ticker") or "",
        )
    )
    trimmed = candidates[:limit]
    if with_meta:
        return trimmed, meta
    return trimmed


def _build_us_hotspot_validation(*, db, social_summary: dict) -> list[dict]:
    social_mentions = [
        item
        for item in (social_summary.get("mentions") or [])
        if str(item.get("market") or "").upper() == "US"
    ]
    if not social_mentions:
        return []
    us_snapshot_rows = _load_us_precomputed_top_rows(db=db, limit=25)
    snapshot_by_ticker = {str(item.get("ticker") or "").upper(): item for item in us_snapshot_rows}
    rows: list[dict] = []
    for mention in social_mentions:
        ticker = str(mention.get("ticker") or "").upper()
        if not ticker:
            continue
        snapshot = snapshot_by_ticker.get(ticker)
        if not snapshot:
            continue
        validation_score = int(mention.get("validation_score") or 0)
        us_rank = int(snapshot.get("us_rank") or 0)
        trend_score = _safe_float(snapshot.get("trend_score"))
        cross_score = validation_score + max(0, 30 - min(us_rank, 30)) + min(25, trend_score / 4)
        rows.append(
            {
                "ticker": ticker,
                "name": mention.get("name") or snapshot.get("name") or ticker,
                "handle": mention.get("handle"),
                "social_view": mention.get("social_view"),
                "validation_score": validation_score,
                "system_action": mention.get("system_action"),
                "template": snapshot.get("model_template"),
                "us_rank": us_rank,
                "trend_score": snapshot.get("trend_score"),
                "latest_close": snapshot.get("latest_close"),
                "action_label": snapshot.get("action_label"),
                "model_signal_label": snapshot.get("model_signal_label"),
                "model_signal_strength": snapshot.get("model_signal_strength"),
                "selection_reason": snapshot.get("selection_reason"),
                "cross_score": round(cross_score, 1),
                "cross_validation_note": _build_us_hotspot_note(mention=mention, snapshot=snapshot),
            }
        )
    rows.sort(key=lambda item: (-float(item.get("cross_score") or 0.0), int(item.get("us_rank") or 999), item.get("ticker") or ""))
    return rows[:8]


def _load_us_precomputed_top_rows(*, db, limit: int = 25) -> list[dict]:
    snapshot_repo = WorkspaceSnapshotRepository(db)
    rows_by_ticker: dict[str, dict] = {}
    rank_counter = 0
    for template in US_HOTSPOT_TEMPLATES:
        params = build_base_precompute_params(model_template=template, universe="full_market", market="US")
        snapshot = snapshot_repo.get_latest_snapshot(screener_snapshot_type(params))
        payload = (snapshot or {}).get("payload") if isinstance(snapshot, dict) else None
        rows = (payload or {}).get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows[:limit]:
            ticker = str(row.get("ticker") or "").upper()
            if not ticker:
                continue
            rank_counter += 1
            candidate = {**row, "model_template": template, "us_rank": rank_counter}
            existing = rows_by_ticker.get(ticker)
            if existing is None or _safe_float(candidate.get("trend_score")) > _safe_float(existing.get("trend_score")):
                rows_by_ticker[ticker] = candidate
    rows = list(rows_by_ticker.values())
    rows.sort(key=lambda item: (int(item.get("us_rank") or 999), -_safe_float(item.get("trend_score")), item.get("ticker") or ""))
    return rows[:limit]


def _build_us_hotspot_note(*, mention: dict, snapshot: dict) -> str:
    parts: list[str] = []
    if mention.get("social_view"):
        parts.append(f"X观点 {mention.get('social_view')}")
    if snapshot.get("action_label"):
        parts.append(f"模型动作 {snapshot.get('action_label')}")
    if snapshot.get("trend_score") is not None:
        parts.append(f"趋势分 {snapshot.get('trend_score')}")
    if snapshot.get("selection_reason"):
        parts.append(str(snapshot.get("selection_reason")))
    return "；".join(parts[:4]) or "X提及与美股模型候选重合，建议进入人工复核。"


def _candidate_from_full_market_row(
    row: dict,
    *,
    template: str,
    market: str,
    market_snapshot: dict | None = None,
) -> dict:
    if row.get("trade_readiness_score") is None:
        decision = evaluate_candidate_tradability(
            {
                **row,
                "score": row.get("model_score") or row.get("model_confidence"),
                "signal_label": row.get("model_signal_label") or row.get("signal_label"),
                "signal_strength": row.get("model_signal_strength") or row.get("trend_score"),
                "expected_drawdown_20d": row.get("model_expected_drawdown_20d"),
                "entry_style": row.get("model_entry_style") or row.get("action_label"),
                "risk_flags": row.get("risk_flags") or row.get("model_execution_tags") or [],
            },
            market_snapshot=market_snapshot,
        )
        row = {
            **row,
            "tradability_status": decision.tradability_status,
            "trade_readiness_score": decision.trade_readiness_score,
            "readiness_bucket": decision.readiness_bucket,
            "readiness_reason": decision.readiness_reason,
            "preferred_entry_style": decision.preferred_entry_style,
            "suggested_watch_action": decision.suggested_watch_action,
            "risk_flags": decision.risk_flags,
            "entry_trigger": row.get("entry_trigger") or decision.entry_trigger,
            "invalidation_condition": row.get("invalidation_condition") or decision.invalidation_condition,
            "time_horizon": row.get("time_horizon") or decision.time_horizon,
            "max_slippage_bps": row.get("max_slippage_bps") or decision.max_slippage_bps,
            "liquidity_bucket": row.get("liquidity_bucket") or decision.liquidity_bucket,
            "stop_loss_type": row.get("stop_loss_type") or decision.stop_loss_type,
            "execution_note": row.get("execution_note") or decision.execution_note,
        }
    trend_score = _safe_float(row.get("trend_score"))
    model_score = row.get("model_score")
    if model_score is None:
        model_score = _parse_model_score(row.get("model_summary"))
    signal_strength = _safe_float(row.get("model_signal_strength"))
    if signal_strength is None:
        signal_strength = trend_score
    percentile = _safe_float(row.get("model_percentile"))
    confidence = _safe_float(row.get("model_confidence"))
    reward_risk = _safe_float(row.get("model_reward_risk_ratio"))
    volume_ratio = _safe_float(row.get("volume_ratio"))
    snapshot_score = _safe_float(row.get("snapshot_score"))
    readiness = _safe_float(row.get("trade_readiness_score"))
    readiness_bucket = row.get("readiness_bucket")
    rank_score = (
        snapshot_score * 1.1
        + trend_score
        + signal_strength * 0.7
        + percentile * 0.25
        + confidence * 0.25
        + readiness * 1.05
        + min(volume_ratio, 8.0) * 2.0
        + min(reward_risk, 4.0) * 6.0
    )
    execution_tags = row.get("model_execution_tags") or row.get("execution_tags") or row.get("risk_flags") or []
    tradability_status = row.get("tradability_status") or row.get("model_tradability_status")
    if not tradability_status:
        tradability_status = "REVIEW" if execution_tags else "READY"
    if str(readiness_bucket or "").upper() == "HIGH":
        rank_score += 18.0
    elif str(readiness_bucket or "").upper() == "MEDIUM":
        rank_score += 8.0
    elif str(readiness_bucket or "").upper() in {"LOW", "BLOCKED"}:
        rank_score -= 35.0
    if str(tradability_status).upper() == "BLOCKED":
        rank_score -= 80.0
    entry_trigger = row.get("entry_trigger") or row.get("model_entry_trigger") or _default_entry_trigger(row, template=template)
    invalidation = row.get("invalidation_condition") or row.get("model_invalidation_condition") or _default_invalidation_condition(row)
    return {
        "ticker": str(row.get("ticker") or "").strip().upper(),
        "name": row.get("name") or row.get("ticker"),
        "market": row.get("market") or market,
        "score": model_score,
        "rank_value": row.get("rank_value"),
        "confidence": confidence or None,
        "signal_label": row.get("model_signal_label") or row.get("signal_label"),
        "signal_strength": signal_strength,
        "expected_drawdown_20d": row.get("model_expected_drawdown_20d"),
        "model_reward_risk_ratio": reward_risk or None,
        "percentile": percentile or None,
        "conviction_bucket": row.get("model_conviction_bucket"),
        "position_size_hint": row.get("model_position_size_hint"),
        "entry_style": row.get("model_entry_style"),
        "tradability_status": tradability_status,
        "block_reason": row.get("block_reason"),
        "trade_readiness_score": readiness or None,
        "readiness_bucket": readiness_bucket,
        "readiness_reason": row.get("readiness_reason"),
        "preferred_entry_style": row.get("preferred_entry_style"),
        "suggested_watch_action": row.get("suggested_watch_action"),
        "target_weight": row.get("target_weight") or row.get("model_target_weight"),
        "suggested_participation_rate": row.get("suggested_participation_rate"),
        "entry_trigger": entry_trigger,
        "invalidation_condition": invalidation,
        "time_horizon": row.get("model_horizon_days") or row.get("time_horizon"),
        "max_slippage_bps": row.get("max_slippage_bps"),
        "liquidity_bucket": row.get("liquidity_bucket"),
        "stop_loss_type": row.get("stop_loss_type"),
        "execution_note": row.get("action_summary") or row.get("execution_note") or row.get("selection_reason"),
        "risk_flags": execution_tags,
        "summary_text": row.get("model_summary") or row.get("selection_reason"),
        "trend_score": trend_score,
        "latest_close": row.get("latest_close"),
        "momentum_5": row.get("momentum_5"),
        "distance_to_breakout_pct": row.get("distance_to_breakout_pct"),
        "setup_label": row.get("setup_label") or row.get("action_label") or template,
        "full_market_template": template,
        "full_market_rank_score": round(rank_score, 1),
        "market_context": market_snapshot or {},
        "limit_band_pct": (
            row.get("limit_band_pct")
            or _cn_limit_band_pct(
                str(row.get("ticker") or ""),
                name=str(row.get("name") or ""),
            )
        )
        if str(market or "").upper() == "CN"
        else None,
    }


def _parse_model_score(value) -> float | None:
    text = str(value or "")
    match = re.search(r"model\s+(-?\d+(?:\.\d+)?)", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _default_entry_trigger(row: dict, *, template: str) -> str:
    close = row.get("latest_close")
    breakout = row.get("distance_to_breakout_pct")
    if template == "cn_volume_breakout":
        return "放量突破后，观察次日是否继续站稳突破区。"
    if template == "cn_bollinger_squeeze_watch":
        return "布林带收口后，等待放量突破或回踩不破。"
    if template == "cn_three_white_soldiers":
        return "三连阳后等待缩量回踩承接，避免直接追高。"
    if breakout is not None:
        return f"距离突破位约 {breakout}%，等待突破或回踩确认。"
    if close:
        return f"围绕最新价 {close} 附近观察承接和量能。"
    return "等待价格触发与量能确认。"


def _default_invalidation_condition(row: dict) -> str:
    close = row.get("latest_close")
    if close:
        try:
            return f"跌破最新价下方约 5%（参考 {float(close) * 0.95:.2f}）则候选失效。"
        except (TypeError, ValueError):
            pass
    return "跌破近期支撑或量价结构转弱则候选失效。"


def _build_buy_the_dip_rows(*, rows: list[dict], markets: list[str]) -> list[dict]:
    market = (markets or ["CN"])[0]
    existing = {
        str(item.get("ticker") or "").upper(): item
        for item in rows
        if str(item.get("ticker") or "").strip()
    }
    with SessionLocal() as db:
        symbol_repo = SymbolRepository(db)
        prediction_repo = PredictionRepository(db)
        service = AIAnalysisService()
        candidates = prediction_repo.list_latest_signal_decisions(limit=200, market=market)
        ranked: list[dict] = []
        for candidate in candidates:
            ticker = str(candidate.get("ticker") or "").upper()
            combined = service.insight_engine.get_insight(ticker, lang="zh")
            merged_candidate = {
                **candidate,
                "latest_close": (combined or {}).get("latest_close"),
                "momentum_5": (combined or {}).get("momentum_5"),
                "distance_to_breakout_pct": (combined or {}).get("distance_to_breakout_pct"),
                "setup_label": (combined or {}).get("setup_label"),
            }
            gate = _recommendation_gate(merged_candidate)
            if not gate["allowed"]:
                continue
            overview = symbol_repo.get_overview(ticker)
            if overview is None:
                continue
            if combined is None or str(combined.get("setup_label") or "") != "pullback_buy":
                continue
            quant_rank = _candidate_quant_score(candidate, combined)
            analysis = service.analyze_symbol(
                overview=overview,
                latest_signal=candidate,
                combined_analysis={
                    "decision": "BUY" if combined.get("trend_label") == "bullish" else "HOLD",
                    "confidence": int(round(float(combined.get("confidence") or 0.55) * 100)),
                    "score": int(round(((combined.get("trend_score") or 50) - 50) / 10)),
                    "reasons": list((combined or {}).get("explanation") or [])[:3],
                    "technical_rating": {},
                    "multi_timeframe": {},
                    "bollinger_band": {},
                    "candlestick_patterns": {},
                },
                lang="zh",
            )
            ranked.append(
                {
                    "ticker": ticker,
                    "name": candidate.get("name") or ticker,
                    "market": candidate.get("market") or market,
                    "headline": analysis.get("headline"),
                    "verdict": analysis.get("verdict"),
                    "confidence": analysis.get("confidence"),
                    "strategy": analysis.get("strategy"),
                    "quant_rank": round(float(existing.get(ticker, {}).get("quant_rank") or quant_rank), 1),
                    "model_score": candidate.get("score"),
                    "model_signal_strength": candidate.get("signal_strength"),
                    "tradability_status": candidate.get("tradability_status"),
                    "trade_readiness_score": candidate.get("trade_readiness_score"),
                    "readiness_bucket": candidate.get("readiness_bucket"),
                    "target_weight": candidate.get("target_weight"),
                    "suggested_participation_rate": candidate.get("suggested_participation_rate"),
                    "entry_trigger": candidate.get("entry_trigger"),
                    "invalidation_condition": candidate.get("invalidation_condition"),
                    "time_horizon": candidate.get("time_horizon"),
                    "max_slippage_bps": candidate.get("max_slippage_bps"),
                    "liquidity_bucket": candidate.get("liquidity_bucket"),
                    "stop_loss_type": candidate.get("stop_loss_type"),
                    "execution_note": candidate.get("execution_note"),
                    "risk_flags": candidate.get("risk_flags") or [],
                    "trend_score": combined.get("trend_score"),
                    "setup_label": combined.get("setup_label"),
                    "buy_zone": analysis.get("buy_zone"),
                    "stop_loss": analysis.get("stop_loss"),
                    "take_profit": analysis.get("take_profit"),
                    "summary": analysis.get("summary"),
                    "execution_plan": _build_execution_plan(latest_signal=candidate, analysis=analysis),
                }
            )
    ranked.sort(
        key=lambda item: (
            -(item.get("quant_rank") or 0.0),
            -_safe_float(item.get("model_score")),
            item.get("ticker") or "",
        )
    )
    return ranked[:BUY_THE_DIP_LIMIT]


def _safe_float(value) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if isnan(numeric):
        return 0.0
    return numeric


def _cn_limit_band_pct(ticker: str, *, name: str | None = None) -> float:
    normalized = str(ticker or "").upper()
    normalized_name = str(name or "").strip().upper().replace(" ", "")
    code = normalized.split(".", 1)[0]
    suffix = normalized.split(".", 1)[1] if "." in normalized else ""
    if normalized_name.startswith(("ST", "*ST", "S*ST", "PT")):
        return 5.0
    if suffix == "BJ" or code.startswith(("4", "8")):
        return 30.0
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    return 10.0


def _fmt_percent(value) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "-"


def _build_execution_plan(*, latest_signal: dict | None, analysis: dict | None) -> str:
    signal = latest_signal or {}
    buy_zone = (analysis or {}).get("buy_zone") or {}
    trigger = signal.get("entry_trigger")
    target_weight = signal.get("target_weight")
    participation = signal.get("suggested_participation_rate")
    zone_low = buy_zone.get("low")
    zone_high = buy_zone.get("high")
    if trigger and target_weight is not None and participation is not None:
        zone_text = f"，优先在 {zone_low}-{zone_high} 附近" if zone_low is not None and zone_high is not None else ""
        return f"先按 {_fmt_percent(participation)} 参与率试单，确认后逐步加到 {_fmt_percent(target_weight)}{zone_text}。"
    if trigger and target_weight is not None:
        return f"触发后先建试探仓，逐步向 {_fmt_percent(target_weight)} 靠拢。"
    if trigger:
        return f"以“{trigger}”为前提，先小仓验证，再决定是否扩张。"
    return "等待更清晰的触发与流动性确认后再执行。"


def _fmt_number(value) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "-"
