from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from math import isnan

from app.core.db import SessionLocal
from app.services.ai_analysis import AIAnalysisService
from app.services.portfolio_book import load_portfolio_positions
from app.services.portfolio_intelligence import build_portfolio_ai_summary, build_position_management_fields
from app.services.price_snapshot import load_latest_closes
from app.services.repository import AppSettingRepository, PredictionRepository, SymbolRepository, WatchlistRepository, WorkspaceSnapshotRepository
from app.services.screener_snapshots import build_base_precompute_params, screener_snapshot_type
from app.services.social_signals import social_signal_summary
from app.services.template_evaluation import (
    build_lightgbm_prediction_evaluation,
    build_pattern_template_evaluation,
    resolve_template_group_label,
)
from app.services.time_utils import app_now_iso, app_today_iso


AI_DAILY_REPORT_KEY = "ai_daily_report"
AI_DAILY_REPORT_SNAPSHOT_TYPE = "ai_daily_report_history"
MARKET_HEATMAP_SNAPSHOT_TYPE = "market_heatmap_workspace"
DEFAULT_AI_DAILY_REPORT_MARKETS = ["CN"]
BUY_THE_DIP_LIMIT = 10
FULL_MARKET_REPORT_TEMPLATES = [
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
        if tradability == "BLOCKED":
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
        recommendation_limit = 5
        excluded_tickers = _load_owned_or_watched_tickers(watchlist_repo)
        rows, market_recommendation_meta = _build_market_recommendation_rows(
            db=db,
            symbol_repo=symbol_repo,
            prediction_repo=prediction_repo,
            market=market,
            excluded_tickers=excluded_tickers,
            recommendation_limit=recommendation_limit,
            prefer_snapshot=True,
        )
        us_model_rows, us_market_recommendation_meta = _build_market_recommendation_rows(
            db=db,
            symbol_repo=symbol_repo,
            prediction_repo=prediction_repo,
            market="US",
            excluded_tickers=excluded_tickers,
            recommendation_limit=recommendation_limit,
            prefer_snapshot=False,
        )
        market_heatmap_snapshot = (
            WorkspaceSnapshotRepository(db).get_latest_snapshot(MARKET_HEATMAP_SNAPSHOT_TYPE)
            if market == "CN"
            else None
        )
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
    return {
        "status": "success",
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
        "market_recommendations": rows,
        "market_recommendations_meta": market_recommendation_meta,
        "market_structure": market_structure,
        "market_template_attribution": market_template_attribution,
        "lightgbm_execution_bias": lightgbm_execution_bias,
        "us_model_recommendations": us_model_rows,
        "us_model_recommendations_meta": us_market_recommendation_meta,
        "us_market_structure": us_market_structure,
        "rows": rows,
        "buy_the_dip_rows": buy_the_dip_rows,
    }


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
    candidate_limit = max(recommendation_limit * 14, 80)
    candidates: list[dict] = []
    candidate_meta = {
        "market": market,
        "source": "none",
        "status": "not_ready" if prefer_snapshot else "ready",
        "ready": not prefer_snapshot,
        "used_today_snapshot": False,
        "snapshot_templates_considered": 0,
        "snapshot_templates_ready": 0,
        "snapshot_rows": 0,
        "candidate_count": 0,
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
            candidate_meta.update(
                {
                    "source": "fresh_snapshot",
                    "status": "ready",
                    "ready": True,
                    "used_today_snapshot": True,
                    "note": _render_market_candidate_note(
                        source="fresh_snapshot",
                        market=market,
                        candidate_count=len(candidates),
                        snapshot_templates_ready=int(snapshot_meta.get("snapshot_templates_ready") or 0),
                    ),
                }
            )
    if not candidates:
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
                ),
            }
        )

    ranked_candidates: list[dict] = []
    for candidate in candidates:
        ticker = str(candidate.get("ticker") or "").strip().upper()
        if not ticker:
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
) -> str:
    market_label = {"CN": "A股", "US": "美股", "HK": "港股"}.get(str(market or "").upper(), str(market or "市场"))
    if source == "fresh_snapshot":
        return f"{market_label} Top 5 使用今天的全市场快照候选；已命中 {candidate_count} 个可排序候选，快照模板 {snapshot_templates_ready} 个已就绪。"
    if source == "predictions_fallback":
        return f"{market_label} 今日全市场快照未完全就绪，当前已降级到最新模型预测候选；可用候选 {candidate_count} 个。"
    return f"{market_label} 今日全市场候选尚未就绪，当前没有可用于日报的候选。"


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
    enriched_payload.setdefault("report_date", app_today_iso())
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


def render_ai_daily_report_message(report: dict | None) -> str:
    payload = report or {}
    strategy = payload.get("strategy") or {}
    portfolio_summary = payload.get("portfolio_summary") or {}
    portfolio_rows = payload.get("portfolio_rows") or []
    market_rows = payload.get("market_recommendations") or payload.get("rows") or []
    market_meta = payload.get("market_recommendations_meta") or {}
    market_structure = payload.get("market_structure") or {}
    market_template_attribution = payload.get("market_template_attribution") or {}
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
        risk_flags = ", ".join(item.get("risk_flags") or []) or "-"
        lines.extend(
            [
                f"{index}. {item.get('ticker')} {item.get('name') or ''}".strip(),
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
            "二、全市场扫描 Top 5",
        "以下候选来自收盘后全市场模型扫描，优先选择量化分高、触发/失效条件清楚、可交易性更容易验证的股票。",
        _render_market_meta_line(market_meta),
        "",
        f"策略主线：{strategy.get('headline') or '-'}",
        f"执行建议：{strategy.get('playbook') or '-'}",
        f"{lightgbm_execution_bias.get('title') or 'LightGBM：今天先观察'}",
        f"{lightgbm_execution_bias.get('summary') or '-'}",
        "",
    ]
    )
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
    for index, item in enumerate(market_rows[:5], start=1):
        buy_zone = item.get("buy_zone") or {}
        take_profit = item.get("take_profit") or {}
        risk_flags = ", ".join(item.get("risk_flags") or []) or "-"
        lines.extend(
            [
                f"{index}. {item.get('ticker')} {item.get('name') or ''}".strip(),
                f"量化分：{item.get('quant_rank') or '-'} | 验证分：{item.get('verification_score') or '-'} | 模型分：{_fmt_number(item.get('model_score'))} | 趋势分：{item.get('trend_score') or '-'}",
                f"结论：{item.get('verdict') or '-'} | 置信度：{item.get('confidence') or '-'} | 策略：{item.get('strategy') or '-'}",
                f"可交易性：{item.get('tradability_status') or '-'} | 建议仓位：{item.get('target_weight') or '-'} | 执行备注：{item.get('execution_note') or '-'}",
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
    if us_model_rows:
        lines.extend(
            [
                "三、美股模型 Top 5",
                "以下候选来自最新美股模型训练结果，优先展示验证分高、交易条件清楚、且未进入当前持仓/自选的名字。",
                _render_market_meta_line(us_market_meta),
                "",
            ]
        )
        lines.extend(_render_market_structure_lines(us_market_structure, title="固定结构：美股强方向 / 风险清单"))
        for index, item in enumerate(us_model_rows[:5], start=1):
            buy_zone = item.get("buy_zone") or {}
            take_profit = item.get("take_profit") or {}
            risk_flags = ", ".join(item.get("risk_flags") or []) or "-"
            lines.extend(
                [
                    f"{index}. {item.get('ticker')} {item.get('name') or ''}".strip(),
                    f"量化分：{item.get('quant_rank') or '-'} | 验证分：{item.get('verification_score') or '-'} | 模型分：{_fmt_number(item.get('model_score'))} | 趋势分：{item.get('trend_score') or '-'}",
                    f"结论：{item.get('verdict') or '-'} | 置信度：{item.get('confidence') or '-'} | 策略：{item.get('strategy') or '-'}",
                    f"可交易性：{item.get('tradability_status') or '-'} | 建议仓位：{item.get('target_weight') or '-'} | 执行备注：{item.get('execution_note') or '-'}",
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
                "四、X 账户社交信号验证",
                "以下只作为社交观点和模型共振参考，不直接作为买卖依据。",
                "",
            ]
        )
        for index, item in enumerate(social_rows[:5], start=1):
            lines.extend(
                [
                    f"{index}. {item.get('ticker')} {item.get('name') or ''}".strip(),
                    f"来源：{item.get('handle') or '-'} | 观点：{item.get('social_view') or '-'} | 验证分：{item.get('validation_score') or 0}",
                    f"模型：{item.get('model_signal_label') or '-'} | 动作：{item.get('system_action') or '-'}",
                    f"理由：{' / '.join(item.get('validation_reasons') or []) or '-'}",
                    "",
                ]
            )
    if us_hotspot_rows:
        lines.extend(
            [
                "五、X 热点美股验证",
                "以下是 X 提及美股与美股模型候选快照的交叉验证，仅作为复核清单。",
                "",
            ]
        )
        for index, item in enumerate(us_hotspot_rows[:5], start=1):
            lines.extend(
                [
                    f"{index}. {item.get('ticker')} {item.get('name') or ''}".strip(),
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
            "title": "AI 日报 2/2：明日推荐购买 Top 5",
            "body": _render_market_top5_push_message(payload),
        },
    ]
    if payload.get("us_model_recommendations"):
        messages[0]["title"] = "AI 日报 1/3：持仓股总结"
        messages[1]["title"] = "AI 日报 2/3：A股明日推荐 Top 5"
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
        risk_flags = ", ".join(item.get("risk_flags") or []) or "-"
        lines.extend(
            [
                f"{index}. {item.get('ticker')} {item.get('name') or ''}".strip(),
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
    market_meta = payload.get("market_recommendations_meta") or {}
    market_structure = payload.get("market_structure") or {}
    market_template_attribution = payload.get("market_template_attribution") or {}
    lightgbm_execution_bias = payload.get("lightgbm_execution_bias") or {}
    lines = [
        "二、明日推荐购买 Top 5",
        "以下候选来自收盘后全市场模型扫描，不包含当前自选股和持仓股；建议只在触发条件满足时执行。",
        "",
        _render_market_meta_line(market_meta),
        f"市场状态：{payload.get('mood') or '-'}",
        f"策略主线：{strategy.get('headline') or '-'}",
        f"执行建议：{strategy.get('playbook') or '-'}",
        f"{lightgbm_execution_bias.get('title') or 'LightGBM：今天先观察'}",
        f"{lightgbm_execution_bias.get('summary') or '-'}",
        "",
    ]
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
        lines.append("暂无满足条件的全市场 Top 5 候选。")
        return "\n".join(lines).strip()
    for index, item in enumerate(market_rows[:5], start=1):
        buy_zone = item.get("buy_zone") or {}
        take_profit = item.get("take_profit") or {}
        risk_flags = ", ".join(item.get("risk_flags") or []) or "-"
        lines.extend(
            [
                f"{index}. {item.get('ticker')} {item.get('name') or ''}".strip(),
                f"量化分：{item.get('quant_rank') or '-'} | 验证分：{item.get('verification_score') or '-'} | 趋势分：{item.get('trend_score') or '-'}",
                f"结论：{item.get('verdict') or '-'} | 置信度：{item.get('confidence') or '-'} | 策略：{item.get('strategy') or '-'}",
                f"可交易性：{item.get('tradability_status') or '-'} | 建议仓位：{item.get('target_weight') or '-'}",
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
        risk_flags = ", ".join(item.get("risk_flags") or []) or "-"
        lines.extend(
            [
                f"{index}. {item.get('ticker')} {item.get('name') or ''}".strip(),
                f"量化分：{item.get('quant_rank') or '-'} | 验证分：{item.get('verification_score') or '-'} | 趋势分：{item.get('trend_score') or '-'}",
                f"结论：{item.get('verdict') or '-'} | 置信度：{item.get('confidence') or '-'} | 策略：{item.get('strategy') or '-'}",
                f"可交易性：{item.get('tradability_status') or '-'} | 建议仓位：{item.get('target_weight') or '-'}",
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
        risk_flags = ", ".join(item.get("risk_flags") or []) or "-"
        lines.extend(
            [
                f"{index}. {item.get('ticker')} {item.get('name') or ''}".strip(),
                f"量化分：{item.get('quant_rank') or '-'} | 模型分：{_fmt_number(item.get('model_score'))} | 趋势分：{item.get('trend_score') or '-'} | Setup：{item.get('setup_label') or '-'}",
                f"结论：{item.get('verdict') or '-'} | 置信度：{item.get('confidence') or '-'} | 策略：{item.get('strategy') or '-'}",
                f"可交易性：{item.get('tradability_status') or '-'} | 建议仓位：{item.get('target_weight') or '-'} | 执行备注：{item.get('execution_note') or '-'}",
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
            risk_flags = ", ".join(item.get("risk_flags") or []) or "-"
            lines.extend(
                [
                    f"{index}. {item.get('ticker')} {item.get('name') or ''}".strip(),
                    f"量化分：{item.get('quant_rank') or '-'} | 模型分：{_fmt_number(item.get('model_score'))} | 趋势分：{item.get('trend_score') or '-'}",
                    f"结论：{item.get('verdict') or '-'} | Setup：{item.get('setup_label') or '-'}",
                    f"可交易性：{item.get('tradability_status') or '-'} | 建议仓位：{item.get('target_weight') or '-'} | 风险标记：{risk_flags}",
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
            risk_flags = ", ".join(item.get("risk_flags") or []) or "-"
            lines.append(
                f"- {item.get('ticker')}: {item.get('tradability_status') or '-'} | {risk_flags} | {item.get('headline') or '-'}"
            )
        lines.append("")
    return lines


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
    setup_label = str((combined or {}).get("setup_label") or "")
    signal_label = str(candidate.get("signal_label") or "").upper()
    score = quant_rank
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
    if signal.get("full_market_template"):
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


def _load_full_market_report_candidates(
    *,
    db,
    market: str,
    excluded_tickers: set[str],
    limit: int,
    with_meta: bool = False,
) -> list[dict] | tuple[list[dict], dict]:
    snapshot_repo = WorkspaceSnapshotRepository(db)
    candidate_map: dict[str, dict] = {}
    repeat_counts = _recent_daily_report_repeat_counts(db=db, market=market)
    today = app_today_iso()
    meta = {
        "market": market,
        "snapshot_templates_considered": len(FULL_MARKET_REPORT_TEMPLATES),
        "snapshot_templates_ready": 0,
        "snapshot_rows": 0,
    }
    for template in FULL_MARKET_REPORT_TEMPLATES:
        params = build_base_precompute_params(model_template=template, universe="full_market", market=market)
        snapshot = snapshot_repo.get_latest_snapshot(screener_snapshot_type(params))
        snapshot_day = str((snapshot or {}).get("snapshot_date") or "")[:10]
        if snapshot_day != today:
            continue
        payload = (snapshot or {}).get("payload") if isinstance(snapshot, dict) else None
        rows = (payload or {}).get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            continue
        meta["snapshot_templates_ready"] = int(meta.get("snapshot_templates_ready") or 0) + 1
        meta["snapshot_rows"] = int(meta.get("snapshot_rows") or 0) + len(rows)
        for row in rows:
            ticker = str(row.get("ticker") or "").strip().upper()
            if not ticker or ticker in excluded_tickers:
                continue
            candidate = _candidate_from_full_market_row(row, template=template, market=market)
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
            -float(item.get("full_market_rank_score") or 0.0),
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


def _candidate_from_full_market_row(row: dict, *, template: str, market: str) -> dict:
    trend_score = _safe_float(row.get("trend_score"))
    model_score = row.get("model_score")
    if model_score is None:
        model_score = _parse_model_score(row.get("model_summary"))
    signal_strength = _safe_float(row.get("model_signal_strength"))
    percentile = _safe_float(row.get("model_percentile"))
    confidence = _safe_float(row.get("model_confidence"))
    reward_risk = _safe_float(row.get("model_reward_risk_ratio"))
    volume_ratio = _safe_float(row.get("volume_ratio"))
    snapshot_score = _safe_float(row.get("snapshot_score"))
    rank_score = (
        snapshot_score * 1.1
        + trend_score
        + signal_strength * 0.7
        + percentile * 0.25
        + confidence * 0.25
        + min(volume_ratio, 8.0) * 2.0
        + min(reward_risk, 4.0) * 6.0
    )
    execution_tags = row.get("model_execution_tags") or row.get("execution_tags") or []
    tradability_status = row.get("tradability_status") or row.get("model_tradability_status")
    if not tradability_status:
        tradability_status = "REVIEW" if execution_tags else "READY"
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
        "setup_label": row.get("setup_label") or row.get("action_label") or template,
        "full_market_template": template,
        "full_market_rank_score": round(rank_score, 1),
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
            overview = symbol_repo.get_overview(ticker)
            if overview is None:
                continue
            combined = service.insight_engine.get_insight(ticker, lang="zh")
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
