from __future__ import annotations

import json
from math import isnan

from app.core.db import SessionLocal
from app.services.ai_analysis import AIAnalysisService
from app.services.repository import AppSettingRepository, PredictionRepository, SymbolRepository, WatchlistRepository


AI_DAILY_REPORT_KEY = "ai_daily_report"
DEFAULT_AI_DAILY_REPORT_MARKETS = ["CN"]
BUY_THE_DIP_LIMIT = 10


def build_ai_daily_report(*, limit: int = 8, tickers: list[str] | None = None, markets: list[str] | None = None) -> dict:
    with SessionLocal() as db:
        watchlist_repo = WatchlistRepository(db)
        symbol_repo = SymbolRepository(db)
        prediction_repo = PredictionRepository(db)
        normalized_tickers = {str(item).strip().upper() for item in (tickers or []) if str(item).strip()}
        effective_markets = markets if markets is not None else DEFAULT_AI_DAILY_REPORT_MARKETS
        normalized_markets = {str(item).strip().upper() for item in (effective_markets or []) if str(item).strip()}
        service = AIAnalysisService()

        items: list[dict]
        if normalized_tickers:
            watchlist = watchlist_repo.get_or_create_default()
            items = watchlist_repo.list_items(watchlist.id)
            items = [item for item in items if str(item.get("ticker") or "").upper() in normalized_tickers]
            if normalized_markets:
                items = [item for item in items if str(item.get("market") or "").upper() in normalized_markets]
        else:
            market = next(iter(normalized_markets), "CN")
            candidate_limit = max(limit * 8, 40)
            candidates = prediction_repo.list_latest_predictions_for_market(market, limit=candidate_limit)
            ranked_candidates: list[dict] = []
            for candidate in candidates:
                overview = symbol_repo.get_overview(candidate["ticker"])
                if overview is None:
                    continue
                combined = service.insight_engine.get_insight(candidate["ticker"], lang="zh")
                if combined is None:
                    continue
                quant_rank = _candidate_quant_score(candidate, combined)
                ranked_candidates.append(
                    {
                        "ticker": candidate["ticker"],
                        "name": candidate.get("name") or candidate["ticker"],
                        "market": candidate.get("market") or market,
                        "overview": overview,
                        "latest_signal": candidate,
                        "combined": combined,
                        "quant_rank": quant_rank,
                    }
                )
            ranked_candidates.sort(
                key=lambda item: (
                    -(item.get("quant_rank") or 0.0),
                    -float((item.get("latest_signal") or {}).get("score") or 0.0),
                    item["ticker"],
                )
            )
            items = ranked_candidates[:limit]

        rows: list[dict] = []
        for item in items[:limit]:
            overview = item.get("overview") or symbol_repo.get_overview(item["ticker"])
            if overview is None:
                continue
            latest_signal = item.get("latest_signal")
            if latest_signal is None:
                predictions = prediction_repo.list_symbol_predictions(item["ticker"], limit=1, latest_run_only=True)
                latest_signal = predictions[0] if predictions else None
            combined = item.get("combined") or service.insight_engine.get_insight(item["ticker"], lang="zh")
            # Reuse the full AI route path by building from combined stack when available.
            # If local insight is the only context available, the service still degrades gracefully.
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
                    "headline": analysis.get("headline"),
                    "verdict": analysis.get("verdict"),
                    "confidence": analysis.get("confidence"),
                    "strategy": analysis.get("strategy"),
                    "quant_rank": round(float(item.get("quant_rank") or _candidate_quant_score(latest_signal or {}, combined or {})), 1),
                    "model_score": (None if latest_signal is None else latest_signal.get("score")),
                    "model_signal_strength": (None if latest_signal is None else latest_signal.get("signal_strength")),
                    "trend_score": (None if combined is None else combined.get("trend_score")),
                    "setup_label": (None if combined is None else combined.get("setup_label")),
                    "buy_zone": analysis.get("buy_zone"),
                    "stop_loss": analysis.get("stop_loss"),
                    "take_profit": analysis.get("take_profit"),
                    "summary": analysis.get("summary"),
                }
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
    return {
        "status": "success",
        "count": len(rows),
        "mood": mood,
        "headline": f"今日 A股 AI 量化复盘偏{mood}",
        "scope": "cn_full_market_top_picks" if not normalized_tickers else "custom_tickers",
        "strategy": strategy,
        "rows": rows,
        "buy_the_dip_rows": buy_the_dip_rows,
    }


def save_ai_daily_report(payload: dict, *, db=None) -> None:
    if db is None:
        with SessionLocal() as own_db:
            save_ai_daily_report(payload, db=own_db)
        return
    AppSettingRepository(db).set(AI_DAILY_REPORT_KEY, json.dumps(payload, ensure_ascii=False))


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


def render_ai_daily_report_message(report: dict | None) -> str:
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
        lines.extend(
            [
                f"{index}. {item.get('ticker')} {item.get('name') or ''}".strip(),
                f"量化分：{item.get('quant_rank') or '-'} | 模型分：{_fmt_number(item.get('model_score'))} | 趋势分：{item.get('trend_score') or '-'} | Setup：{item.get('setup_label') or '-'}",
                f"结论：{item.get('verdict') or '-'} | 置信度：{item.get('confidence') or '-'} | 策略：{item.get('strategy') or '-'}",
                f"Headline：{item.get('headline') or '-'}",
                f"Summary：{item.get('summary') or '-'}",
                f"买入区：{buy_zone.get('low', '-')} - {buy_zone.get('high', '-')}",
                f"止损位：{item.get('stop_loss', '-')}",
                f"止盈区：{take_profit.get('low', '-')} - {take_profit.get('high', '-')}",
                "",
            ]
        )
    buy_the_dip_rows = payload.get("buy_the_dip_rows") or []
    if buy_the_dip_rows:
        lines.extend(["Buy The Dip 候选:", ""])
        for index, item in enumerate(buy_the_dip_rows, start=1):
            buy_zone = item.get("buy_zone") or {}
            lines.extend(
                [
                    f"{index}. {item.get('ticker')} {item.get('name') or ''}".strip(),
                    f"量化分：{item.get('quant_rank') or '-'} | 模型分：{_fmt_number(item.get('model_score'))} | 趋势分：{item.get('trend_score') or '-'}",
                    f"结论：{item.get('verdict') or '-'} | Setup：{item.get('setup_label') or '-'}",
                    f"回踩区：{buy_zone.get('low', '-')} - {buy_zone.get('high', '-')}",
                    "",
                ]
            )
    return "\n".join(lines).strip()


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
        candidates = prediction_repo.list_latest_predictions_for_market(market, limit=200)
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
                    "trend_score": combined.get("trend_score"),
                    "setup_label": combined.get("setup_label"),
                    "buy_zone": analysis.get("buy_zone"),
                    "stop_loss": analysis.get("stop_loss"),
                    "take_profit": analysis.get("take_profit"),
                    "summary": analysis.get("summary"),
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


def _fmt_number(value) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "-"
