from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session
from app.core.db import SessionLocal

from app.services.model_signal_summary import build_signal_label, model_confidence
from app.services.nlp_snapshots import (
    SNAPSHOT_DASHBOARD_NLP,
    SNAPSHOT_PORTFOLIO_NLP,
    SNAPSHOT_WATCHLIST_NLP,
    build_dashboard_nlp_snapshot,
    build_portfolio_nlp_snapshot as build_portfolio_news_snapshot,
    build_watchlist_nlp_snapshot,
    summarize_news_rows,
)
from app.services.portfolio_intelligence import (
    build_position_management_fields,
    build_portfolio_ai_summary,
    build_portfolio_intelligence,
)
from app.services.portfolio_book import load_portfolio_positions
from app.services.price_snapshot import load_latest_close, load_latest_closes
from app.services.repository import (
    BacktestRepository,
    DataJobRepository,
    DashboardReadRepository,
    ModelRunRepository,
    PredictionRepository,
    PredictionTradePlanRepository,
    PriceSyncStateRepository,
    SymbolRepository,
    WatchlistRepository,
    WorkspaceSnapshotRepository,
)
from app.services.screener import ScreenerService
from app.services.screener_snapshots import build_base_precompute_params, screener_snapshot_type
from app.services.template_evaluation import resolve_template_group_label
from app.services.time_utils import app_now_iso, app_today_iso


SNAPSHOT_HOME_WATCHLIST = "home_watchlist"
SNAPSHOT_HOME_PORTFOLIO = "home_portfolio"
SNAPSHOT_MODEL_CANDIDATES = "model_candidates"
SNAPSHOT_PIPELINE_STATUS = "pipeline_status"
SNAPSHOT_WATCHLIST_WORKSPACE = "watchlist_workspace"
SNAPSHOT_PORTFOLIO_WORKSPACE = "portfolio_workspace"
SNAPSHOT_CONTINUOUS_LEADERS = "continuous_leaders_workspace"
SNAPSHOT_MARKET_WORKSPACE = "market_workspace"
SNAPSHOT_MARKET_WORKSPACE_PREMARKET = "market_workspace:premarket"
SNAPSHOT_MARKET_WORKSPACE_MONITOR = "market_workspace:monitor"
SNAPSHOT_MARKET_WORKSPACE_POSTMARKET = "market_workspace:postmarket"
SNAPSHOT_MARKET_HEATMAP_WORKSPACE = "market_heatmap_workspace"


MARKET_HEATMAP_TEMPLATE_LABELS = {
    "technical_momentum": "技术动量",
    "cn_bollinger_squeeze_watch": "布林带收口",
    "cn_three_white_soldiers": "三连阳",
    "cn_volume_breakout": "底部放量突破",
    "lightgbm_top_picks": "AI优选",
    "next_tesla_swing": "成长爆发",
    "global_growth_value": "成长价值",
    "global_income_quality": "收益质量",
}

MARKET_HEATMAP_TEMPLATE_PREFIX = {
    "technical_momentum": "动量",
    "cn_bollinger_squeeze_watch": "收口",
    "cn_three_white_soldiers": "连阳",
    "cn_volume_breakout": "放量",
    "lightgbm_top_picks": "模型",
    "next_tesla_swing": "成长",
    "global_growth_value": "价值",
    "global_income_quality": "收益",
}

MARKET_HEATMAP_LABEL_ALIASES = {
    "北交所 / BSE": "北交所",
    "科创板 / STAR": "科创板",
    "创业板 / ChiNext": "创业板",
    "深主板 / SZSE Main": "深主板",
    "沪主板 / SSE Main": "沪主板",
    "A股其他 / CN Other": "A股其他",
    "美股 ETF / US ETF": "美股ETF",
    "美股医药 / US Healthcare": "美股医药",
    "美股金融 / US Financials": "美股金融",
    "美股能源材料 / US Energy & Materials": "美股能源材料",
    "美股消费出行 / US Consumer & Mobility": "美股消费出行",
    "美股科技 / US Tech": "美股科技",
    "美股综合 / US General": "美股综合",
    "Unclassified": "未分类",
}

MARKET_HEATMAP_TEMPLATE_PLAN = {
    "CN": [
        "technical_momentum",
        "cn_bollinger_squeeze_watch",
        "cn_three_white_soldiers",
        "cn_volume_breakout",
    ],
    "US": [
        "lightgbm_top_picks",
        "next_tesla_swing",
        "technical_momentum",
        "global_growth_value",
        "global_income_quality",
    ],
}

MARKET_SNAPSHOT_BOARD_DEFS = [
    {
        "key": "leaders",
        "market": "CN",
        "template": "technical_momentum",
        "title_en": "A-Share Momentum Leaders",
        "title_zh": "A股强势榜",
        "description_en": "Trend and volume leaders from the latest A-share close.",
        "description_zh": "基于 A 股最新收盘数据筛出的趋势强股。",
    },
    {
        "key": "squeeze",
        "market": "CN",
        "template": "cn_bollinger_squeeze_watch",
        "title_en": "A-Share Squeeze Watch",
        "title_zh": "A股收口榜",
        "description_en": "A-share names with compressed Bollinger Bands and coiled price action.",
        "description_zh": "A股里波动率收缩、价格待选择方向的候选股。",
    },
    {
        "key": "three_white_soldiers",
        "market": "CN",
        "template": "cn_three_white_soldiers",
        "title_en": "A-Share Three White Soldiers",
        "title_zh": "A股连阳榜",
        "description_en": "A-share stocks showing three consecutive strong bullish candles.",
        "description_zh": "A股里连续三根强势阳线、收盘逐步抬高的股票。",
    },
    {
        "key": "volume_breakout",
        "market": "CN",
        "template": "cn_volume_breakout",
        "title_en": "A-Share Volume Breakout",
        "title_zh": "A股放量榜",
        "description_en": "A-share base breakouts supported by expanding turnover.",
        "description_zh": "A股底部放量突破、量能确认更充分的候选股。",
    },
    {
        "key": "us_ai_leaders",
        "market": "US",
        "template": "lightgbm_top_picks",
        "title_en": "U.S. AI Leaders",
        "title_zh": "美股AI优选",
        "description_en": "Top U.S. names from the latest LightGBM multifactor ranking.",
        "description_zh": "来自最新 LightGBM 多因子排序的美股优选候选。",
    },
    {
        "key": "us_growth_breakout",
        "market": "US",
        "template": "next_tesla_swing",
        "title_en": "U.S. Growth Breakout",
        "title_zh": "美股成长爆发",
        "description_en": "U.S. growth setups with fast acceleration and expansion potential.",
        "description_zh": "美股里具备加速特征和扩张潜力的成长型 setup。",
    },
    {
        "key": "us_momentum",
        "market": "US",
        "template": "technical_momentum",
        "title_en": "U.S. Momentum Leaders",
        "title_zh": "美股动量强势",
        "description_en": "U.S. momentum leaders backed by price trend and volume.",
        "description_zh": "由价格趋势和量能支持的美股动量强股。",
    },
    {
        "key": "us_growth_value",
        "market": "US",
        "template": "global_growth_value",
        "title_en": "U.S. Growth + Value",
        "title_zh": "美股成长价值",
        "description_en": "Balanced U.S. names combining growth quality with valuation discipline.",
        "description_zh": "兼顾成长质量与估值纪律的美股候选。",
    },
]


def _snapshot_now_iso() -> str:
    return app_now_iso()


def _action_hint_for_score(score: float | None, *, lang: str) -> str:
    if score is None:
        return "等待数据" if lang == "zh" else "Wait for data"
    value = float(score)
    if value >= 0.18:
        return "优先跟踪" if lang == "zh" else "Prioritize"
    if value >= 0.05:
        return "加入观察" if lang == "zh" else "Watch closely"
    if value <= -0.05:
        return "降低优先级" if lang == "zh" else "Deprioritize"
    return "继续观察" if lang == "zh" else "Keep watching"


def _signal_tone_for_score(score: float | None) -> str:
    if score is None:
        return "sig-watch"
    value = float(score)
    if value >= 0.18:
        return "sig-buy"
    if value <= -0.05:
        return "sig-sell"
    if value >= 0.05:
        return "sig-watch"
    return "sig-hold"


def _watchlist_combined_analysis(model_output: dict | None) -> dict:
    score = None if model_output is None else model_output.get("score")
    label = (model_output or {}).get("signal_label") or build_signal_label(score, lang="en") or "Hold"
    normalized_label = str(label).strip().lower()
    if normalized_label == "buy":
        decision = "BUY"
    elif normalized_label == "sell":
        decision = "SELL"
    elif normalized_label == "watch":
        decision = "WATCH"
    else:
        decision = "HOLD"
    confidence = (model_output or {}).get("confidence") or model_confidence(score) or 45
    strength = (model_output or {}).get("signal_strength") or 0
    return {
        "status": "snapshot",
        "decision": decision,
        "confidence": int(confidence),
        "score": int(round(float(score or 0.0) * 10)),
        "signal_strength": int(strength),
    }


def _watchlist_decision_brief(ticker: str, model_output: dict | None, combined_analysis: dict) -> dict:
    decision = str((combined_analysis or {}).get("decision") or "HOLD").upper()
    score = model_output.get("score") if model_output else None
    if decision == "BUY":
        headline = f"{ticker} stays on the long radar"
        summary = (
            f"Model score {float(score):.3f} keeps the setup constructive."
            if score is not None
            else "Model tone stays constructive."
        )
    elif decision == "SELL":
        headline = f"{ticker} needs tighter risk control"
        summary = (
            f"Model score {float(score):.3f} reads defensive."
            if score is not None
            else "Model tone stays defensive."
        )
    elif decision == "WATCH":
        headline = f"{ticker} still needs confirmation"
        summary = "Current setup is worth monitoring, but the trigger is not clean yet."
    else:
        headline = f"{ticker} remains in monitoring mode"
        summary = "No fresh high-conviction setup yet."
    return {"status": "snapshot", "headline": headline, "summary": summary}


def _watchlist_action_hint(item: dict, *, lang: str) -> tuple[str, str]:
    combined = item.get("combined_analysis") or {}
    decision = str(combined.get("decision") or "HOLD").upper()
    confidence = int(combined.get("confidence") or 0)
    tags = [str(tag).strip() for tag in (item.get("execution_tags") or []) if str(tag).strip()]
    if decision in {"BUY", "STRONG BUY"} and confidence >= 70:
        return (
            "优先复核入场条件" if lang == "zh" else "Review entry setup first",
            "模型偏强且信心较高，适合优先检查是否接近触发位。" if lang == "zh" else "Model posture is strong with high confidence; check whether the trigger level is near.",
        )
    if decision in {"SELL", "STRONG SELL"}:
        return (
            "降低关注优先级" if lang == "zh" else "Lower priority",
            "当前模型态度偏弱，除非有新的价格结构改善，否则不宜放到前排。" if lang == "zh" else "Current model posture is weak; keep it out of the front row unless price structure improves.",
        )
    if tags:
        return (
            "先核对风险标签" if lang == "zh" else "Check risk tags first",
            "这只股票带有执行提醒，先确认风险事件再决定是否继续跟踪。" if lang == "zh" else "This name carries execution warnings, so verify those risks before continuing.",
        )
    return (
        "继续观察确认" if lang == "zh" else "Keep monitoring",
        "目前更像等待确认的观察标的，适合继续跟踪量价和模型变化。" if lang == "zh" else "This still looks like a confirmation watch, so keep tracking price, volume, and model changes.",
    )


def build_home_watchlist_snapshot(db: Session, *, lang: str = "zh") -> dict:
    watchlist_repo = WatchlistRepository(db)
    prediction_repo = PredictionRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    items = watchlist_repo.list_items(watchlist.id)
    tickers = [item["ticker"] for item in items]
    outputs = prediction_repo.get_latest_model_outputs_for_tickers(tickers)
    rows: list[dict] = []
    for item in items:
        output = prediction_repo._build_signal_decision(outputs.get(item["ticker"]) or {})
        score = output.get("score")
        confidence = int(output.get("confidence") or model_confidence(score) or 0)
        signal_label = output.get("signal_label") or build_signal_label(score, lang=lang) or ("观察" if lang == "zh" else "Watch")
        rows.append(
            {
                "ticker": item["ticker"],
                "name": item.get("name") or item["ticker"],
                "market": item.get("market") or "-",
                "score": float(score or 0.0),
                "confidence": confidence,
                "signal_label": signal_label,
                "signal_tone": _signal_tone_for_score(score),
                "priority": confidence,
                "action_hint": _action_hint_for_score(score, lang=lang),
                "tradability_status": output.get("tradability_status"),
                "target_weight": output.get("target_weight"),
                "entry_trigger": output.get("entry_trigger"),
                "invalidation_condition": output.get("invalidation_condition"),
                "time_horizon": output.get("time_horizon"),
                "max_slippage_bps": output.get("max_slippage_bps"),
                "liquidity_bucket": output.get("liquidity_bucket"),
                "stop_loss_type": output.get("stop_loss_type"),
                "execution_note": output.get("execution_note"),
                "risk_flags": output.get("risk_flags") or [],
                "summary_text": output.get("summary_text"),
                "trade_date": output.get("trade_date"),
            }
        )
    rows.sort(key=lambda item: (-item["priority"], -item["score"], item["ticker"]))
    return {
        "rows": rows[:8],
        "updated_at": _snapshot_now_iso(),
    }


def build_home_portfolio_snapshot(db: Session, *, lang: str = "zh") -> dict:
    symbol_repo = SymbolRepository(db)
    prediction_repo = PredictionRepository(db)
    intelligence = build_portfolio_intelligence(db, lang=lang)
    rows: list[dict] = []
    total_market_value = 0.0
    total_cost = 0.0
    for item in load_portfolio_positions():
        overview = symbol_repo.get_overview(item["ticker"]) or {
            "ticker": item["ticker"],
            "name": item.get("name"),
            "market": item.get("market"),
        }
        latest_signal = prediction_repo.get_latest_model_output_for_ticker(item["ticker"])
        latest_signal = prediction_repo._build_signal_decision(latest_signal or {}) if latest_signal else None
        latest_price = float(load_latest_close(item["ticker"]) or 0.0)
        quantity = float(item.get("quantity") or 0.0)
        cost_basis = float(item.get("cost_basis") or 0.0)
        market_value = latest_price * quantity
        cost_value = cost_basis * quantity
        pnl = market_value - cost_value
        pnl_pct = ((latest_price / cost_basis) - 1.0) * 100 if cost_basis else 0.0
        total_market_value += market_value
        total_cost += cost_value
        score = (latest_signal or {}).get("score")
        signal_label = build_signal_label(score, lang=lang) or ("持有" if lang == "zh" else "Hold")
        rows.append(
            {
                "ticker": item["ticker"],
                "name": overview.get("name") or item["ticker"],
                "market": overview.get("market") or item.get("market") or "-",
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "market_value": market_value,
                "signal_label": signal_label,
                "signal_tone": _signal_tone_for_score(score),
                "risk_flag": "关注回撤" if pnl_pct < -5 and lang == "zh" else ("Watch drawdown" if pnl_pct < -5 else ""),
                "action_hint": _action_hint_for_score(score, lang=lang),
                "tradability_status": (latest_signal or {}).get("tradability_status"),
                "target_weight": (latest_signal or {}).get("target_weight"),
                "entry_trigger": (latest_signal or {}).get("entry_trigger"),
                "invalidation_condition": (latest_signal or {}).get("invalidation_condition"),
                "time_horizon": (latest_signal or {}).get("time_horizon"),
                "max_slippage_bps": (latest_signal or {}).get("max_slippage_bps"),
                "liquidity_bucket": (latest_signal or {}).get("liquidity_bucket"),
                "stop_loss_type": (latest_signal or {}).get("stop_loss_type"),
                "execution_note": (latest_signal or {}).get("execution_note"),
                "risk_flags": (latest_signal or {}).get("risk_flags") or [],
            }
        )
    rows.sort(key=lambda item: (-abs(item["market_value"]), item["ticker"]))
    totals = {
        "market_value": total_market_value,
        "cost": total_cost,
        "pnl": total_market_value - total_cost,
        "pnl_pct": ((total_market_value / total_cost) - 1.0) * 100 if total_cost else 0.0,
    }
    return {
        "rows": rows[:8],
        "totals": totals,
        "meta": {
            "top_sector": intelligence["top_sector"],
            "top_market": intelligence["top_market"],
            "concentration_pct": intelligence["concentration_pct"],
            "risk_summary": intelligence["risk_summary"],
            "action_mix": intelligence["action_mix"],
            "watch_items": intelligence["watch_items"][:3],
        },
        "updated_at": _snapshot_now_iso(),
    }


def build_model_candidates_snapshot(db: Session, *, lang: str = "zh") -> dict:
    signal_repo = PredictionRepository(db)
    rows = signal_repo.list_latest_signal_decisions(limit=8)
    items = []
    for row in rows:
        score = row.get("score")
        items.append(
            {
                "ticker": row.get("ticker"),
                "name": row.get("name") or row.get("ticker"),
                "score": score,
                "confidence": int(row.get("confidence") or model_confidence(score) or 0),
                "signal_label": row.get("signal_label") or build_signal_label(score, lang=lang),
                "tradability_status": row.get("tradability_status"),
                "action_bucket": row.get("action_bucket"),
                "action_label": row.get("action_label"),
                "target_weight": row.get("target_weight"),
                "priority": row.get("priority"),
                "entry_trigger": row.get("entry_trigger"),
                "invalidation_condition": row.get("invalidation_condition"),
                "time_horizon": row.get("time_horizon"),
                "max_slippage_bps": row.get("max_slippage_bps"),
                "liquidity_bucket": row.get("liquidity_bucket"),
                "stop_loss_type": row.get("stop_loss_type"),
                "execution_note": row.get("execution_note"),
                "risk_flags": row.get("risk_flags") or [],
                "reason_summary": row.get("summary_text"),
                "trade_date": row.get("trade_date"),
            }
        )
    return {
        "rows": items,
        "updated_at": _snapshot_now_iso(),
    }


def build_watchlist_workspace_snapshot(db: Session, *, lang: str = "zh") -> dict:
    watchlist_repo = WatchlistRepository(db)
    prediction_repo = PredictionRepository(db)
    trade_plan_repo = PredictionTradePlanRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    items = watchlist_repo.list_items(watchlist.id)
    tickers = [item["ticker"] for item in items]
    outputs = prediction_repo.get_latest_model_outputs_for_tickers(tickers)
    trade_plans = trade_plan_repo.get_latest_for_tickers(tickers)
    rows: list[dict] = []
    for item in items:
        model_output = outputs.get(item["ticker"]) or {}
        execution_tags = (
            (model_output.get("trade_plan") or {}).get("execution_tags")
            or (trade_plans.get(item["ticker"]) or {}).get("execution_tags")
            or []
        )
        combined = _watchlist_combined_analysis(model_output)
        decision_brief = _watchlist_decision_brief(item["ticker"], model_output, combined)
        row = {
            "item_id": item.get("item_id"),
            "ticker": item.get("ticker"),
            "name": item.get("name") or item.get("ticker"),
            "market": item.get("market") or "-",
            "exchange": item.get("exchange") or "-",
            "sync_enabled": bool(item.get("sync_enabled")),
            "sync_status": item.get("sync_status"),
            "last_synced_date": item.get("last_synced_date"),
            "model_output": model_output,
            "execution_tags": execution_tags,
            "combined_analysis": combined,
            "decision_brief": decision_brief,
        }
        action_hint, action_reason = _watchlist_action_hint(row, lang=lang)
        row["action_hint"] = action_hint
        row["action_reason"] = action_reason
        if execution_tags:
            row["ai_brief"] = "先处理执行风险标签，再决定是否推进。" if lang == "zh" else "Resolve execution risk tags before promoting this name."
        elif str(combined.get("decision") or "").upper() == "BUY":
            row["ai_brief"] = "模型偏多，可优先检查触发位是否接近。" if lang == "zh" else "Constructive model posture; check whether price is near the trigger."
        elif str(combined.get("decision") or "").upper() == "SELL":
            row["ai_brief"] = "当前偏弱，先降低优先级。" if lang == "zh" else "Current setup is weak, so lower its priority."
        else:
            row["ai_brief"] = "等待更多量价确认后再推进。" if lang == "zh" else "Wait for more price and volume confirmation."
        rows.append(row)
    return {"rows": rows, "updated_at": _snapshot_now_iso()}


def build_portfolio_workspace_snapshot(db: Session, *, lang: str = "zh") -> dict:
    symbol_repo = SymbolRepository(db)
    prediction_repo = PredictionRepository(db)
    intelligence = build_portfolio_intelligence(db, lang=lang)
    raw_positions = load_portfolio_positions()
    tickers = [item["ticker"] for item in raw_positions]
    overviews = symbol_repo.list_overviews_for_tickers(tickers)
    latest_outputs = prediction_repo.get_latest_model_outputs_for_tickers(tickers)
    latest_prices = load_latest_closes(tickers)
    rows: list[dict] = []
    total_market_value = 0.0
    total_cost = 0.0
    position_drafts: list[dict] = []
    for item in raw_positions:
        overview = overviews.get(item["ticker"]) or {
            "ticker": item["ticker"],
            "name": item.get("name"),
            "market": item.get("market"),
        }
        latest_signal = latest_outputs.get(item["ticker"]) or {}
        latest_price = float(latest_prices.get(item["ticker"]) or 0.0)
        quantity = float(item.get("quantity") or 0.0)
        cost_basis = float(item.get("cost_basis") or 0.0)
        market_value = latest_price * quantity
        cost_value = cost_basis * quantity
        pnl = market_value - cost_value
        pnl_pct = ((latest_price / cost_basis) - 1.0) * 100 if cost_basis else 0.0
        total_market_value += market_value
        total_cost += cost_value
        position_drafts.append(
            {
                "item": item,
                "overview": overview,
                "latest_signal": latest_signal,
                "latest_price": latest_price,
                "quantity": quantity,
                "cost_basis": cost_basis,
                "market_value": market_value,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
            }
        )
    for draft in position_drafts:
        item = draft["item"]
        overview = draft["overview"]
        latest_signal = draft["latest_signal"]
        ai_summary = build_portfolio_ai_summary(
            latest_signal=latest_signal,
            pnl_pct=draft["pnl_pct"],
            cost_basis=draft["cost_basis"],
            lang=lang,
        )
        management = build_position_management_fields(
            latest_signal=latest_signal,
            pnl_pct=draft["pnl_pct"],
            market_value=draft["market_value"],
            total_market_value=total_market_value,
            cost_basis=draft["cost_basis"],
            lang=lang,
        )
        rows.append(
            {
                "ticker": item["ticker"],
                "name": overview.get("name") or item["ticker"],
                "market": overview.get("market") or item.get("market") or "-",
                "quantity": draft["quantity"],
                "cost_basis": draft["cost_basis"],
                "latest_price": draft["latest_price"],
                "market_value": draft["market_value"],
                "pnl": draft["pnl"],
                "pnl_pct": draft["pnl_pct"],
                "ai_headline": ai_summary["ai_headline"],
                "ai_verdict": ai_summary["ai_verdict"],
                "ai_strategy": ai_summary["ai_strategy"],
                "target_weight_pct": management["target_weight_pct"],
                "target_weight_text": management["target_weight_text"],
                "target_weight_source": management["target_weight_source"],
                "current_weight_pct": management["current_weight_pct"],
                "action_bucket": management["action_bucket"],
                "action_bucket_key": management["action_bucket_key"],
                "note": item.get("note") or "",
            }
        )
    return {
        "rows": rows,
        "totals": {
            "market_value": total_market_value,
            "cost": total_cost,
            "pnl": total_market_value - total_cost,
            "pnl_pct": ((total_market_value / total_cost) - 1.0) * 100 if total_cost else 0.0,
        },
        "intelligence": intelligence,
        "updated_at": _snapshot_now_iso(),
    }


def build_continuous_leaders_snapshot(db: Session, *, lang: str = "zh") -> dict:
    signal_repo = PredictionRepository(db)
    symbol_repo = SymbolRepository(db)
    snapshots = signal_repo.list_recent_prediction_snapshots(top_n=10, limit_runs=5)
    ticker_hit_counts: dict[str, int] = {}
    ticker_score_history: dict[str, list[float]] = {}
    latest_signal_map: dict[str, dict] = {}
    for snapshot in snapshots:
        for item in snapshot["items"]:
            ticker = item["ticker"]
            ticker_hit_counts[ticker] = ticker_hit_counts.get(ticker, 0) + 1
            ticker_score_history.setdefault(ticker, []).append(float(item.get("score") or 0.0))
            latest_signal_map[ticker] = item
    tickers = list(ticker_hit_counts.keys())
    latest_outputs = signal_repo.get_latest_model_outputs_for_tickers(tickers)
    rows: list[dict] = []
    for ticker, hits in ticker_hit_counts.items():
        latest_output = signal_repo._build_signal_decision(latest_outputs.get(ticker) or {})
        symbol = symbol_repo.get_by_ticker(ticker)
        latest_signal = latest_signal_map.get(ticker, {})
        rows.append(
            {
                "ticker": ticker,
                "name": (symbol.name if symbol and symbol.name else ticker),
                "market": (symbol.market if symbol and symbol.market else "OTHER").upper(),
                "hits": hits,
                "runs": len(snapshots),
                "score": round(float(latest_signal.get("score") or 0.0), 4),
                "score_history": ticker_score_history.get(ticker, []),
                "trade_date": latest_output.get("trade_date") or latest_signal.get("trade_date"),
                "signal_label": latest_output.get("signal_label"),
                "signal_strength": latest_output.get("signal_strength"),
                "confidence": latest_output.get("confidence"),
                "conviction_bucket": latest_output.get("conviction_bucket"),
                "position_size_hint": latest_output.get("position_size_hint"),
                "entry_style": latest_output.get("entry_style"),
                "percentile": latest_output.get("percentile"),
                "model_reward_risk_ratio": latest_output.get("model_reward_risk_ratio"),
                "execution_tags": list(latest_output.get("execution_tags") or []),
            }
        )
    rows.sort(key=lambda item: (-item["hits"], -item["score"], item["ticker"]))
    return {"rows": rows[:120], "updated_at": _snapshot_now_iso(), "lookback_runs": len(snapshots)}


def _load_market_board_rows(db: Session, *, template: str, market: str = "CN") -> list[dict]:
    params = build_base_precompute_params(model_template=template, universe="full_market", market=market)
    snapshot = WorkspaceSnapshotRepository(db).get_latest_snapshot(screener_snapshot_type(params))
    payload = (snapshot or {}).get("payload") if isinstance(snapshot, dict) else None
    rows = (payload or {}).get("rows") if isinstance(payload, dict) else None
    return rows if isinstance(rows, list) else []


def _market_heatmap_fallback_label(row: dict, template: str) -> str:
    template_label = MARKET_HEATMAP_TEMPLATE_LABELS.get(template, template)
    market = str(row.get("market") or "").strip().upper()
    if template == "technical_momentum":
        momentum_20 = float(row.get("momentum_20") or 0.0)
        momentum_5 = float(row.get("momentum_5") or 0.0)
        volume_ratio = float(row.get("volume_ratio") or 0.0)
        if market == "US":
            if momentum_20 >= 1.0:
                return "翻倍动量"
            if momentum_20 >= 0.45:
                return "高弹性趋势"
            if momentum_5 >= 0.6 and volume_ratio >= 4.0:
                return "放量加速"
            if momentum_5 >= 0.3:
                return "趋势延续"
            if volume_ratio >= 4.0:
                return "量价共振"
            if volume_ratio >= 1.5:
                return "活跃放量"
            return "技术动量"
        if momentum_20 >= 1.0:
            return "翻倍动量"
        if momentum_5 >= 0.25:
            return "短线加速"
        if volume_ratio >= 1.5:
            return "量能放大"
        return template_label
    if template == "cn_volume_breakout":
        volume_ratio = float(row.get("volume_ratio") or 0.0)
        if volume_ratio >= 2.0:
            return "强量突破"
        return template_label
    if template == "cn_bollinger_squeeze_watch":
        distance = row.get("distance_to_breakout_pct")
        try:
            if distance is not None and float(distance) <= 3.0:
                return "临界突破"
        except (TypeError, ValueError):
            pass
        return template_label
    if template == "next_tesla_swing":
        momentum_20 = float(row.get("momentum_20") or 0.0)
        momentum_5 = float(row.get("momentum_5") or 0.0)
        volume_ratio = float(row.get("volume_ratio") or 0.0)
        if market == "US":
            if momentum_5 >= 1.0 and volume_ratio >= 2.0:
                return "高弹性加速"
            if momentum_5 >= 0.6 and volume_ratio >= 3.0:
                return "放量突破"
            if momentum_5 >= 0.45:
                return "强势延续"
            if volume_ratio >= 2.0:
                return "量价共振"
            if volume_ratio >= 1.0:
                return "回踩转强"
            return "趋势蓄势"
        if momentum_20 >= 0.35:
            return "成长加速"
        return template_label
    if template == "lightgbm_top_picks":
        signal_strength = float(row.get("model_signal_strength") or 0.0)
        if signal_strength >= 80:
            return "模型高置信"
        return template_label
    return template_label


def _market_heatmap_display_label(*, label: str, market: str, template: str, used_fallback: bool) -> str:
    normalized = str(label or "").strip()
    if not normalized:
        return normalized
    normalized = MARKET_HEATMAP_LABEL_ALIASES.get(normalized, normalized)
    if market == "US" and used_fallback:
        prefix = MARKET_HEATMAP_TEMPLATE_PREFIX.get(template, "")
        if prefix and not normalized.startswith(f"{prefix}·"):
            return f"{prefix}·{normalized}"
    return normalized


def build_market_heatmap_snapshot(db: Session | None, *, lang: str = "zh") -> dict:
    del lang
    owns_db = db is None
    db = db or SessionLocal()
    try:
        combined_rows: list[dict] = []
        for market, templates in MARKET_HEATMAP_TEMPLATE_PLAN.items():
            for template in templates:
                for row in _load_market_board_rows(db, template=template, market=market):
                    combined_rows.append(dict(row, _template=template, market=market))
        tickers = [str(row.get("ticker") or "").strip().upper() for row in combined_rows if row.get("ticker")]
        overviews = SymbolRepository(db).list_overviews_for_tickers(tickers)
        sector_map: dict[tuple[str, str], dict] = {}
        market_counts: dict[str, int] = {}
        for row in combined_rows:
            ticker = str(row.get("ticker") or "").strip().upper()
            overview = overviews.get(ticker) or {}
            market = str(overview.get("market") or row.get("market") or "CN").upper()
            template = str(row.get("_template") or "").strip()
            if market == "US" and not str(overview.get("sector") or "").strip():
                # U.S. sector flow should be based on real sector metadata, not
                # inferred labels from ticker/name heuristics.
                continue
            label = resolve_template_group_label(
                meta=overview,
                ticker=ticker,
                market_code=market,
                name=row.get("name") or overview.get("name"),
            )
            used_fallback_label = not label or label in {"其他", "Unclassified", "美股综合 / US General", "A股其他 / CN Other"}
            if market == "US" and used_fallback_label:
                # The U.S. heatmap should represent sector/industry flow. Names without
                # sector metadata remain available in continuous-leader views instead of
                # being mixed into synthetic momentum buckets.
                continue
            if used_fallback_label:
                label = _market_heatmap_fallback_label(row, template)
            market_counts[market] = market_counts.get(market, 0) + 1
            label = _market_heatmap_display_label(
                label=label,
                market=market,
                template=template,
                used_fallback=used_fallback_label,
            )
            item = sector_map.setdefault(
                (market, label),
                {
                    "market": market,
                    "label": label,
                    "slug": label.lower().replace(" ", "-").replace("/", "-"),
                    "hits": 0,
                    "score_total": 0.0,
                    "move_5d_total": 0.0,
                    "move_5d_count": 0,
                    "positive_5d_count": 0,
                    "volume_ratio_total": 0.0,
                    "volume_ratio_count": 0,
                    "up_flow_proxy_total": 0.0,
                    "down_flow_proxy_total": 0.0,
                    "max_signal_strength": 0,
                    "buy_signal_count": 0,
                    "execution_tags": {},
                    "ticker_details": [],
                },
            )
            item["hits"] += 1
            trend_score = float(row.get("trend_score") or row.get("model_signal_strength") or 0.0)
            item["score_total"] += trend_score
            item["max_signal_strength"] = max(item["max_signal_strength"], int(row.get("model_signal_strength") or trend_score or 0))
            momentum_5 = row.get("momentum_5")
            if momentum_5 is not None:
                try:
                    move_5d = float(momentum_5) * 100.0
                    item["move_5d_total"] += move_5d
                    item["move_5d_count"] += 1
                    if move_5d > 0:
                        item["positive_5d_count"] += 1
                except (TypeError, ValueError):
                    pass
            try:
                volume_ratio = float(row.get("volume_ratio") or 0.0)
            except (TypeError, ValueError):
                volume_ratio = 0.0
            if volume_ratio > 0:
                item["volume_ratio_total"] += volume_ratio
                item["volume_ratio_count"] += 1
                flow_weight = max(0.25, volume_ratio)
                try:
                    move_for_flow = float(momentum_5 or 0.0)
                except (TypeError, ValueError):
                    move_for_flow = 0.0
                if move_for_flow > 0:
                    item["up_flow_proxy_total"] += flow_weight
                elif move_for_flow < 0:
                    item["down_flow_proxy_total"] += flow_weight
            signal_label = str(row.get("model_signal_label") or row.get("signal_label") or row.get("action_label") or "").strip().upper()
            if signal_label == "BUY":
                item["buy_signal_count"] += 1
            fallback_tags = [MARKET_HEATMAP_TEMPLATE_LABELS.get(template, template)] if template else []
            for tag in row.get("model_execution_tags") or row.get("execution_tags") or fallback_tags:
                normalized = str(tag).strip()
                if normalized:
                    item["execution_tags"][normalized] = item["execution_tags"].get(normalized, 0) + 1
            item["ticker_details"].append(
                {
                    "ticker": ticker,
                    "market": market,
                    "name": row.get("name") or overview.get("name"),
                    "score": trend_score,
                    "state": row.get("model_state"),
                    "confidence": row.get("model_confidence"),
                    "percentile": row.get("model_percentile"),
                    "target_horizon_days": row.get("model_horizon_days"),
                    "model_reward_risk_ratio": row.get("model_reward_risk_ratio"),
                    "conviction_bucket": row.get("model_conviction_bucket"),
                    "position_size_hint": row.get("model_position_size_hint"),
                    "entry_style": row.get("model_entry_style"),
                    "execution_tags": row.get("model_execution_tags") or row.get("execution_tags") or [],
                    "signal_label": signal_label,
                    "signal_strength": int(row.get("model_signal_strength") or trend_score or 0),
                }
            )
        heatmap_rows: list[dict] = []
        for item in sector_map.values():
            hits = int(item["hits"] or 0)
            avg_score = round(float(item["score_total"] or 0.0) / max(hits, 1), 3)
            avg_move_5d = (
                round(float(item["move_5d_total"] or 0.0) / max(int(item["move_5d_count"] or 0), 1), 2)
                if int(item["move_5d_count"] or 0) > 0
                else None
            )
            breadth_pct = (
                round((int(item["positive_5d_count"] or 0) / max(int(item["move_5d_count"] or 0), 1)) * 100.0, 1)
                if int(item["move_5d_count"] or 0) > 0
                else None
            )
            turnover_ratio_20d = (
                round(float(item["volume_ratio_total"] or 0.0) / max(int(item["volume_ratio_count"] or 0), 1), 2)
                if int(item["volume_ratio_count"] or 0) > 0
                else None
            )
            total_flow_proxy = float(item["up_flow_proxy_total"] or 0.0) + float(item["down_flow_proxy_total"] or 0.0)
            up_turnover_share_pct = (
                round((float(item["up_flow_proxy_total"] or 0.0) / max(total_flow_proxy, 1e-6)) * 100.0, 1)
                if total_flow_proxy > 0
                else None
            )
            signed_turnover_pct = (
                round(((float(item["up_flow_proxy_total"] or 0.0) - float(item["down_flow_proxy_total"] or 0.0)) / max(total_flow_proxy, 1e-6)) * 100.0, 1)
                if total_flow_proxy > 0
                else None
            )
            flow_proxy_score = None
            if turnover_ratio_20d is not None or signed_turnover_pct is not None or breadth_pct is not None:
                ratio_component = ((turnover_ratio_20d or 1.0) - 1.0) * 26.0
                signed_component = (signed_turnover_pct or 0.0) * 0.30
                breadth_component = ((breadth_pct or 50.0) - 50.0) * 0.32
                flow_proxy_score = round(min(100.0, max(0.0, 50.0 + ratio_component + signed_component + breadth_component)), 1)
            tags = [tag for tag, _count in sorted(item["execution_tags"].items(), key=lambda pair: (-pair[1], pair[0]))[:3]]
            intensity = min(100, 18 + hits * 12 + int(avg_score * 0.7) + int(item["max_signal_strength"] * 0.25))
            heatmap_rows.append(
                {
                    "market": item["market"],
                    "label": item["label"],
                    "slug": item["slug"],
                    "hits": hits,
                    "avg_score": avg_score,
                    "avg_move_5d": avg_move_5d,
                    "breadth_pct": breadth_pct,
                    "max_signal_strength": item["max_signal_strength"],
                    "buy_signal_count": item["buy_signal_count"],
                    "turnover_ratio_20d": turnover_ratio_20d,
                    "up_turnover_share_pct": up_turnover_share_pct,
                    "signed_turnover_pct": signed_turnover_pct,
                    "flow_proxy_score": flow_proxy_score,
                    "execution_tags": tags,
                    "ticker_details": item["ticker_details"],
                    "intensity": intensity,
                }
            )
        heatmap_rows.sort(
            key=lambda item: (
                -int(item.get("hits") or 0),
                -float(item.get("avg_score") or 0.0),
                str(item.get("market") or ""),
                item.get("label") or "",
            )
        )
        market_distribution = [
            {"market": market, "count": count}
            for market, count in sorted(market_counts.items(), key=lambda pair: (-pair[1], pair[0]))
        ]
        tracked_signal_count = len(combined_rows)
        resonance_score = round((heatmap_rows[0]["hits"] / max(tracked_signal_count, 1)) * 100.0, 1) if heatmap_rows else 0.0
        return {
            "as_of_date": app_today_iso(),
            "sector_heatmap": heatmap_rows[:24],
            "market_distribution": market_distribution,
            "tracked_signal_count": tracked_signal_count,
            "resonance_score": resonance_score,
            "updated_at": _snapshot_now_iso(),
        }
    finally:
        if owns_db:
            db.close()


def build_market_mode_snapshot(mode: str, *, lang: str = "zh", db: Session | None = None) -> dict:
    del lang
    normalized_mode = (mode or "monitor").strip().lower()
    if normalized_mode not in {"premarket", "monitor", "postmarket"}:
        normalized_mode = "monitor"
    owns_db = db is None
    db = db or SessionLocal()
    try:
        service = ScreenerService()
        boards: list[dict] = []
        for board in MARKET_SNAPSHOT_BOARD_DEFS:
            rows = [
                dict(row, market=str(row.get("market") or board["market"]).upper())
                for row in _load_market_board_rows(db, template=board["template"], market=board["market"])
            ]
            for row in rows:
                row["snapshot_score"] = service._market_snapshot_score(board["key"], row, normalized_mode)
                row["snapshot_score_breakdown"] = service._market_snapshot_score_breakdown(board["key"], row, normalized_mode)
            rows.sort(
                key=lambda item: (
                    -(item.get("snapshot_score") or 0),
                    -(item.get("trend_score") or 0),
                    -(item.get("volume_ratio") or 0),
                    item.get("ticker", ""),
                )
            )
            boards.append({**board, "rows": rows[:10], "mode": normalized_mode})
    finally:
        if owns_db:
            db.close()
    return {
        "mode": normalized_mode,
        "boards": boards,
        "updated_at": _snapshot_now_iso(),
    }


def build_market_workspace_snapshot(db: Session, *, lang: str = "zh") -> dict:
    del lang
    monitor_payload = build_market_mode_snapshot("monitor", db=db)
    boards = monitor_payload.get("boards") or []
    return {
        "rows": [dict(board, rows=(board.get("rows") or [])[:4]) for board in boards],
        "updated_at": _snapshot_now_iso(),
    }


def save_market_workspace_snapshots(
    db: Session | None = None,
    *,
    source_job_id: int | None = None,
    lang: str = "zh",
) -> dict[str, dict]:
    owns_db = db is None
    db = db or SessionLocal()
    try:
        repo = WorkspaceSnapshotRepository(db)
        snapshot_date = app_today_iso()
        payloads = {
            SNAPSHOT_MARKET_WORKSPACE: build_market_workspace_snapshot(db, lang=lang),
            SNAPSHOT_MARKET_WORKSPACE_PREMARKET: build_market_mode_snapshot("premarket", lang=lang, db=db),
            SNAPSHOT_MARKET_WORKSPACE_MONITOR: build_market_mode_snapshot("monitor", lang=lang, db=db),
            SNAPSHOT_MARKET_WORKSPACE_POSTMARKET: build_market_mode_snapshot("postmarket", lang=lang, db=db),
            SNAPSHOT_MARKET_HEATMAP_WORKSPACE: build_market_heatmap_snapshot(db, lang=lang),
        }
        created: dict[str, dict] = {}
        for snapshot_type, payload in payloads.items():
            row = repo.create_snapshot(
                snapshot_type=snapshot_type,
                snapshot_date=snapshot_date,
                payload=payload,
                source_job_id=source_job_id,
            )
            created[snapshot_type] = {
                "id": row.id,
                "snapshot_date": row.snapshot_date,
                "created_at": row.created_at,
            }
        return created
    finally:
        if owns_db:
            db.close()


def build_pipeline_status_snapshot(db: Session, *, lang: str = "zh") -> dict:
    from app.services.ai_daily_report import build_close_review_action_feed, load_ai_daily_report

    summary = DashboardReadRepository(db).load_summary_snapshot()
    recent_jobs = summary["recent_jobs"]
    latest_model = ModelRunRepository(db).get_latest_run_summary() or {}
    latest_backtest = BacktestRepository(db).get_latest_backtest_summary() or {}
    close_review_action_feed = build_close_review_action_feed(load_ai_daily_report(db=db), lang=lang)
    sync_states = PriceSyncStateRepository(db).list_states_with_symbols()
    job_repo = DataJobRepository(db)
    refresh_job = job_repo.get_latest_job(
        {"refresh_cn_market_data_lake_only", "refresh_cn_market_data_daily", "refresh_cn_market_data"}
    )
    analysis_job = job_repo.get_latest_job("watchlist_auto_analysis")
    news_job = job_repo.get_latest_job("news_enrichment")
    us_train_job = job_repo.get_latest_job({"us_signal_train", "train_us_signals"})
    guidance_job = job_repo.get_latest_job("model_selection_guidance_snapshot")
    latest_dashboard_nlp = WorkspaceSnapshotRepository(db).get_latest_snapshot(SNAPSHOT_DASHBOARD_NLP) or {}
    latest_watchlist_nlp = WorkspaceSnapshotRepository(db).get_latest_snapshot(SNAPSHOT_WATCHLIST_NLP) or {}
    nlp_payload = (latest_dashboard_nlp.get("payload") or {}) if isinstance(latest_dashboard_nlp, dict) else {}
    watchlist_nlp_payload = (latest_watchlist_nlp.get("payload") or {}) if isinstance(latest_watchlist_nlp, dict) else {}
    watchlist_nlp_rows = (watchlist_nlp_payload.get("rows") or []) if isinstance(watchlist_nlp_payload, dict) else []
    news_meta = (
        (nlp_payload.get("meta") if isinstance(nlp_payload, dict) else None)
        or (watchlist_nlp_payload.get("meta") if isinstance(watchlist_nlp_payload, dict) else None)
        or summarize_news_rows(watchlist_nlp_rows)
    )
    news_market_meta = {
        market: summarize_news_rows([row for row in watchlist_nlp_rows if str(row.get("market") or "").strip().upper() == market])
        for market in ("CN", "US")
    }
    for market, meta in news_market_meta.items():
        top_sources_list = meta.get("top_sources") or []
        primary_source = str(top_sources_list[0].get("source") or "").strip() if top_sources_list else ""
        if primary_source.startswith("TuShare:"):
            provider_label = "TuShare"
        elif primary_source in {"东方财富Choice数据", "东方财富"} or any(
            token in primary_source for token in ("证券时报", "界面新闻", "每日经济新闻", "财联社", "央广财经", "财中社")
        ):
            provider_label = "东方财富/中文财经源" if lang == "zh" else "Eastmoney/CN finance"
        elif "Polygon" in primary_source:
            provider_label = "Polygon News"
        elif primary_source:
            provider_label = "RSS fallback"
        else:
            provider_label = "No provider" if lang != "zh" else "暂无来源"
        meta["primary_provider"] = provider_label
    top_sources = ", ".join(
        f"{item.get('source')}({item.get('count')})"
        for item in (news_meta.get("top_sources") or [])[:3]
        if item.get("source")
    )
    market_summary_text = " ".join(
        (
            (
                f"{('A股' if market == 'CN' else '美股')} {meta.get('matched_ticker_count', 0)}/{meta.get('ticker_count', 0)}，{meta.get('headline_total', 0)} 条；"
                if lang == "zh"
                else f"{market} {meta.get('matched_ticker_count', 0)}/{meta.get('ticker_count', 0)}, {meta.get('headline_total', 0)} headlines;"
            )
            if meta.get("ticker_count")
            else ""
        )
        for market, meta in news_market_meta.items()
    ).strip()
    sync_success_count = sum(1 for item in sync_states if str(item.get("status") or "").lower() == "success")
    sync_total = len(sync_states)
    latest_model_at = latest_model.get("finished_at") or latest_model.get("created_at")
    latest_backtest_at = latest_backtest.get("finished_at") or latest_backtest.get("created_at")
    model_health = [
        {
            "label": "训练状态" if lang == "zh" else "Training Status",
            "value": latest_model.get("status") or ("待运行" if lang == "zh" else "Idle"),
            "meta": latest_model.get("name") or ("暂无模型" if lang == "zh" else "No model run"),
        },
        {
            "label": "最近训练时间" if lang == "zh" else "Latest Training Time",
            "value": latest_model_at or "-",
            "meta": ("模型越近，当前结论通常越可信" if lang == "zh" else "More recent training usually means fresher conclusions"),
        },
        {
            "label": "最近回测" if lang == "zh" else "Latest Backtest",
            "value": latest_backtest.get("status") or ("待运行" if lang == "zh" else "Idle"),
            "meta": latest_backtest_at or (latest_backtest.get("name") or "-"),
        },
        {
            "label": "美股训练" if lang == "zh" else "US Training",
            "value": (us_train_job or {}).get("status") or ("待运行" if lang == "zh" else "Idle"),
            "meta": (us_train_job or {}).get("message") or ("美股收盘后训练 LightGBM 多因子信号" if lang == "zh" else "Train LightGBM U.S. multifactor signals after the U.S. close."),
        },
        {
            "label": "数据完整度" if lang == "zh" else "Data Coverage",
            "value": f"{sync_success_count}/{sync_total}",
            "meta": ("同步成功股票数" if lang == "zh" else "Synced symbols"),
        },
    ]
    anomalies: list[dict] = []
    if latest_model.get("status") not in {None, "", "success"}:
        anomalies.append(
            {
                "title": "模型训练异常" if lang == "zh" else "Model Training Alert",
                "detail": latest_model.get("name") or ("最近训练状态不是 success" if lang == "zh" else "Latest training status is not success"),
            }
        )
    if latest_backtest.get("status") not in {None, "", "success"}:
        anomalies.append(
            {
                "title": "回测结果待确认" if lang == "zh" else "Backtest Needs Review",
                "detail": latest_backtest.get("name") or ("最近回测状态不是 success" if lang == "zh" else "Latest backtest status is not success"),
            }
        )
    if sync_total and sync_success_count < sync_total:
        anomalies.append(
            {
                "title": "数据覆盖不足" if lang == "zh" else "Data Coverage Gap",
                "detail": (
                    f"{sync_total - sync_success_count} {'只股票最近同步未成功' if lang == 'zh' else 'symbols did not sync successfully'}"
                ),
            }
        )
    failed_jobs = [item for item in recent_jobs if str(item.get("status") or "").lower() == "failed"]
    if failed_jobs:
        latest_failed = failed_jobs[0]
        anomalies.append(
            {
                "title": "最近任务失败" if lang == "zh" else "Recent Job Failure",
                "detail": f"{latest_failed.get('job_type') or '-'} · {latest_failed.get('message') or '-'}",
            }
        )
    pipeline = [
        {
            "step": "market_refresh",
            "label": "全市场轻刷新" if lang == "zh" else "Market Light Refresh",
            "status": (refresh_job or {}).get("status") or "idle",
            "timestamp": (refresh_job or {}).get("finished_at") or (refresh_job or {}).get("started_at"),
            "message": (refresh_job or {}).get("message")
            or ("收盘后先做 A 股轻量增量刷新，不在这一层做全市场深分析。" if lang == "zh" else "Runs a light CN refresh after close before any deeper analysis."),
        },
        {
            "step": "technical_snapshot",
            "label": "自选技术快照" if lang == "zh" else "Watchlist Technical Snapshot",
            "status": "success" if sync_success_count else "idle",
            "timestamp": sync_states[0].get("updated_at") if sync_states else None,
            "message": (
                f"{sync_success_count}/{len(sync_states)} {'只股票已完成同步，主要用于自选跟踪' if lang == 'zh' else 'symbols synced successfully for watchlist tracking'}"
                if sync_states
                else ("暂无同步记录" if lang == "zh" else "No sync history yet")
            ),
        },
        {
            "step": "model_training",
            "label": "自选深分析训练" if lang == "zh" else "Watchlist Deep Analysis",
            "status": latest_model.get("status") or "idle",
            "timestamp": latest_model.get("finished_at") or latest_model.get("created_at"),
            "message": latest_model.get("name") or ("尚未训练" if lang == "zh" else "No model run yet"),
        },
        {
            "step": "backtest",
            "label": "回测结果" if lang == "zh" else "Backtest",
            "status": latest_backtest.get("status") or "idle",
            "timestamp": latest_backtest.get("finished_at") or latest_backtest.get("created_at"),
            "message": latest_backtest.get("name") or ("暂无回测" if lang == "zh" else "No backtest yet"),
        },
        {
            "step": "us_training",
            "label": "美股训练" if lang == "zh" else "US Signal Training",
            "status": (us_train_job or {}).get("status") or "idle",
            "timestamp": (us_train_job or {}).get("finished_at") or (us_train_job or {}).get("started_at"),
            "message": (us_train_job or {}).get("message") or ("美股收盘后会把 US lake 写入统一模型结果层。" if lang == "zh" else "After the U.S. close, U.S. lake symbols are written into the unified model-output layer."),
        },
        {
            "step": "model_selection_guidance",
            "label": "模型使用指导" if lang == "zh" else "Model Selection Guidance",
            "status": (guidance_job or {}).get("status") or "idle",
            "timestamp": (guidance_job or {}).get("finished_at") or (guidance_job or {}).get("started_at"),
            "message": (guidance_job or {}).get("message") or ("收盘后把优先模型、优先组合和强票反向归因写入快照。" if lang == "zh" else "Persist priority models, priority combos, and winner traceback after the close."),
        },
        {
            "step": "ai_report",
            "label": "AI 日报" if lang == "zh" else "AI Report",
            "status": (analysis_job or {}).get("status") or "idle",
            "timestamp": (analysis_job or {}).get("finished_at") or (analysis_job or {}).get("started_at"),
            "message": (analysis_job or {}).get("message") or ("自动分析完成后会生成日报与推送。" if lang == "zh" else "Generated after automated analysis."),
        },
        {
            "step": "news_enrichment",
            "label": "新闻增强" if lang == "zh" else "News Enrichment",
            "status": (news_job or {}).get("status") or "idle",
            "timestamp": (news_job or {}).get("finished_at") or (news_job or {}).get("started_at"),
            "message": (news_job or {}).get("message")
            or (
                (
                    f"命中 {news_meta.get('matched_ticker_count', 0)}/{news_meta.get('ticker_count', 0)} 只股票，"
                    f"累计 {news_meta.get('headline_total', 0)} 条新闻。"
                    + (f" {market_summary_text}" if market_summary_text else "")
                    + (f" 主要来源：{top_sources}。" if top_sources else "")
                )
                if lang == "zh" and news_meta
                else (
                    f"Matched {news_meta.get('matched_ticker_count', 0)}/{news_meta.get('ticker_count', 0)} names with "
                    f"{news_meta.get('headline_total', 0)} total headlines."
                    + (f" {market_summary_text}" if market_summary_text else "")
                    + (f" Top sources: {top_sources}." if top_sources else "")
                )
                if news_meta
                else ("收盘后刷新自选和持仓的新闻情绪与摘要。" if lang == "zh" else "Refreshes watchlist and portfolio news sentiment after close.")
            ),
        },
    ]
    trust_score = max(
        0,
        min(
            100,
            (
                (35 if latest_model.get("status") == "success" else 10)
                + (25 if latest_backtest.get("status") == "success" else 8)
                + (20 if sync_total and sync_success_count == sync_total else 10)
                + (20 if latest_model_at else 5)
            ),
        ),
    )
    return {
        "rows": pipeline,
        "recent_jobs": recent_jobs[:6],
        "model_health": model_health,
        "trust_score": trust_score,
        "anomalies": anomalies[:4],
        "close_review_action_feed": close_review_action_feed,
        "news_meta": news_meta,
        "news_market_meta": news_market_meta,
        "updated_at": _snapshot_now_iso(),
    }


def refresh_workspace_snapshots(db: Session, *, source_job_id: int | None = None, lang: str = "zh") -> dict:
    try:
        # Snapshot builders can spend a long time assembling JSON after their
        # initial read queries. On PostgreSQL, that can otherwise trip
        # idle_in_transaction_session_timeout before the later cache writes.
        db.execute(text("SET SESSION idle_in_transaction_session_timeout = 0"))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
    repo = WorkspaceSnapshotRepository(db)
    snapshot_date = app_today_iso()
    created: dict[str, dict] = {}
    builders = {
        SNAPSHOT_HOME_WATCHLIST: build_home_watchlist_snapshot,
        SNAPSHOT_HOME_PORTFOLIO: build_home_portfolio_snapshot,
        SNAPSHOT_MODEL_CANDIDATES: build_model_candidates_snapshot,
        SNAPSHOT_PIPELINE_STATUS: build_pipeline_status_snapshot,
        SNAPSHOT_WATCHLIST_WORKSPACE: build_watchlist_workspace_snapshot,
        SNAPSHOT_PORTFOLIO_WORKSPACE: build_portfolio_workspace_snapshot,
        SNAPSHOT_CONTINUOUS_LEADERS: build_continuous_leaders_snapshot,
        SNAPSHOT_WATCHLIST_NLP: build_watchlist_nlp_snapshot,
        SNAPSHOT_PORTFOLIO_NLP: build_portfolio_news_snapshot,
        SNAPSHOT_DASHBOARD_NLP: build_dashboard_nlp_snapshot,
    }
    try:
        for snapshot_type, builder in builders.items():
            payload = builder(db, lang=lang)
            # Builders are read-heavy and can leave PostgreSQL sessions sitting
            # in an open read transaction while Python assembles large JSON
            # payloads. End that transaction before the short write transaction
            # below.
            db.rollback()
            row = repo.create_snapshot(
                snapshot_type=snapshot_type,
                snapshot_date=snapshot_date,
                payload=payload,
                source_job_id=source_job_id,
            )
            created[snapshot_type] = {
                "id": row.id,
                "snapshot_date": row.snapshot_date,
                "created_at": row.created_at,
            }
        return created
    finally:
        try:
            db.execute(text("SET SESSION idle_in_transaction_session_timeout = DEFAULT"))
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass


def load_latest_workspace_snapshot(db: Session, snapshot_type: str) -> dict | None:
    return WorkspaceSnapshotRepository(db).get_latest_snapshot(snapshot_type)
