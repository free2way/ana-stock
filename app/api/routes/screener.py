import html
import json
import csv
from io import StringIO
from urllib.parse import urlencode
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.db import SessionLocal, get_db_session
from app.services.auth import is_authenticated, login_redirect
from app.services.market_intelligence import build_market_sentiment_snapshot
from app.services.market_context import load_market_context_snapshot
from app.models.schema import SymbolCreate
from app.services.market_sync import sync_market_data
from app.services.repository import AppSettingRepository, SymbolRepository, WatchlistRepository, WorkspaceSnapshotRepository
from app.services.runtime_cache import get_or_set
from app.services.ai_daily_report import build_trade_explain_text, format_trade_gate_reason, format_trade_status
from app.services.model_selection_guidance import load_model_selection_guidance_snapshot, summarize_model_selection_guidance
from app.services.recommendation_regression import (
    load_or_build_recommendation_regression,
    summarize_recommendation_regression,
)
from app.services.selection_quality import (
    load_or_build_selection_quality,
    save_selection_quality_snapshot,
)
from app.services.screener import MODEL_TEMPLATES, ScreenerService
from app.services.screener_snapshots import (
    build_base_precompute_params,
    exact_screener_snapshot_exists,
    load_exact_screener_snapshot,
    load_exact_screener_snapshot_rows,
    screener_snapshot_key,
    screener_snapshot_type,
)
from app.services.template_evaluation import (
    build_lightgbm_evaluation,
    build_lightgbm_prediction_evaluation,
    build_next_tesla_evaluation,
    build_pattern_template_evaluation,
    build_technical_momentum_evaluation,
    lightgbm_bias,
    lightgbm_maturity,
    normalize_lightgbm_action,
    normalize_lightgbm_prediction_action,
    next_tesla_market_bias,
    next_tesla_maturity,
    pattern_template_bias,
    pattern_template_maturity,
    technical_momentum_bias,
    technical_momentum_maturity,
)
from app.services.focus_pool import add_to_today_focus_pool, enrich_focus_pool_with_symbols, load_today_focus_pool
from app.services.kronos_validation import annotate_rows_with_kronos, load_latest_kronos_validation
from app.services.factor_experiments import (
    get_factor_strategy,
    get_factor_experiment_run,
    list_factor_definitions,
    list_factor_experiment_runs,
    list_factor_strategies,
    refresh_factor_experiment_run,
    run_factor_experiment,
    save_factor_strategy,
)
from app.services.ui_lang import resolve_request_lang
from app.services.workspace_nav import WORKSPACE_COMPACT_STYLE, WORKSPACE_SIDEBAR_STYLE, render_workspace_nav_html
from app.services.workspace_snapshots import (
    SNAPSHOT_MARKET_WORKSPACE_MONITOR,
    SNAPSHOT_MARKET_WORKSPACE_POSTMARKET,
    SNAPSHOT_MARKET_WORKSPACE_PREMARKET,
    load_latest_workspace_snapshot,
)
from app.services.workspace_snapshots import refresh_workspace_snapshots
from app.services.time_utils import app_now_iso


router = APIRouter(prefix="/screeners", tags=["screeners"])


SCREENER_SNAPSHOT_TTL = timedelta(days=7)


ACTION_OPTIONS = [
    ("ALL", "All setups"),
    ("buy_the_dip", "Buy The Dip"),
    ("wait_for_breakout", "Wait For Breakout"),
    ("hold_and_watch", "Hold And Watch"),
    ("wait", "Wait"),
]

CONFLUENCE_ACTION_OPTIONS = [
    ("ALL", {"en": "Any confluence", "zh": "任意共振动作"}),
    ("buy_the_dip", {"en": "Buy The Dip", "zh": "回踩买点"}),
    ("breakout_confirmation", {"en": "Breakout Confirmation", "zh": "突破确认"}),
    ("bullish_entry", {"en": "Bullish Entry", "zh": "偏多入场"}),
    ("watchlist", {"en": "Watch / Observe", "zh": "观察等待"}),
]

CONFLUENCE_BUCKET_LABELS = {value: labels for value, labels in CONFLUENCE_ACTION_OPTIONS if value != "ALL"}

MODEL_SIGNAL_OPTIONS = [
    ("ALL", {"en": "All signals", "zh": "全部信号"}),
    ("BUY", {"en": "Buy", "zh": "买点"}),
    ("WATCH", {"en": "Watch", "zh": "观察"}),
    ("SELL", {"en": "Sell", "zh": "卖点"}),
    ("HOLD", {"en": "Hold", "zh": "持有"}),
]

SORT_BY_OPTIONS = [
    ("default", {"en": "Default", "zh": "默认排序"}),
    ("confluence_rank", {"en": "Confluence Rank", "zh": "共振排行榜"}),
    ("model_hit_count", {"en": "Model Hits", "zh": "模型命中数"}),
    ("confluence_alignment_count", {"en": "Action Alignment", "zh": "动作一致数"}),
    ("trend_score", {"en": "Trend Score", "zh": "趋势分"}),
    ("latest_close", {"en": "Latest Close", "zh": "最新价"}),
    ("model_signal_strength", {"en": "Model Signal", "zh": "模型信号"}),
    ("kronos_score", {"en": "Kronos Score", "zh": "Kronos 分"}),
    ("trade_readiness_score", {"en": "Trade Readiness", "zh": "交易就绪度"}),
    ("watchlist_state", {"en": "Watchlist State", "zh": "自选状态"}),
    ("snapshot_hits", {"en": "Snapshot Hits", "zh": "命中数"}),
    ("momentum_5", {"en": "5D Momentum", "zh": "5日动量"}),
    ("momentum_20", {"en": "20D Momentum", "zh": "20日动量"}),
    ("volume_ratio", {"en": "Volume Ratio", "zh": "量比"}),
    ("pe_ttm", {"en": "PE", "zh": "市盈率"}),
    ("roe_avg_3y", {"en": "ROE 3Y", "zh": "三年ROE"}),
    ("net_profit_yoy", {"en": "Profit YoY", "zh": "利润同比"}),
    ("dividend_yield", {"en": "Dividend Yield", "zh": "股息率"}),
]

SORT_ORDER_OPTIONS = [
    ("desc", {"en": "High to Low", "zh": "从高到低"}),
    ("asc", {"en": "Low to High", "zh": "从低到高"}),
]

SCREENERS_PRESETS_KEY = "screener_saved_presets"

LANG_OPTIONS = [("en", "English"), ("zh", "中文")]

SCREEN_TEXT = {
    "en": {
        "back_to_dashboard": "Back to dashboard",
        "open_watchlist": "Open Watchlist",
        "sync_cn_fundamentals": "Sync CN Fundamentals",
        "open_focus_pool": "Open Today Focus",
        "open_market_snapshot": "Open Market Snapshot",
        "quant_screener": "Quant Screener",
        "market_snapshot": "Market Snapshot",
        "title": "Rule-Based Stock Selection",
        "rules": "Rules",
        "results": "Results",
        "saved_strategies": "Saved Strategies",
        "model_template": "Model Template",
        "universe": "Universe",
        "market": "Market",
        "min_trend_score": "Minimum Trend Score",
        "action_filter": "Action Filter",
        "min_volume_strength": "Minimum Volume Strength",
        "cn_rules": "Fundamental Rules",
        "min_listing_days": "Minimum Listing Days",
        "pe_range": "PE Range",
        "min_roe_3y": "Minimum 3Y Avg ROE (%)",
        "min_profit_yoy": "Minimum Net Profit YoY (%)",
        "min_revenue_yoy": "Minimum Revenue YoY (%)",
        "max_debt": "Maximum Debt To Assets (%)",
        "min_dividend": "Minimum Dividend Yield (%)",
        "exclude_bottom_cap": "Exclude Bottom Market Cap (%)",
        "recent_snapshot_runs": "Recent Snapshot Window",
        "min_snapshot_hits": "Minimum Snapshot Hits",
        "model_signal_filter": "Model Signal",
        "min_model_signal_strength": "Minimum Signal Strength",
        "execution_tag_filter": "Execution Tag",
        "exclude_execution_tag_filter": "Exclude Tag",
        "run_screener": "Run Screener",
        "save_strategy": "Save Current Strategy",
        "strategy_name": "My strategy name",
        "save_as_strategy": "Save As My Strategy",
        "export_csv": "Export CSV",
        "only_add_top_n": "Only add top N results (0 = all)",
        "auto_enable_sync": "Auto-enable Sync for added stocks",
        "add_current_results": "Add Current Results To Watchlist",
        "focus_top_n": "Add top N to today's focus (0 = all)",
        "add_current_results_to_focus": "Add Current Results To Today Focus",
        "add_to_today_focus": "Add To Today Focus",
        "no_results_to_add": "No Results To Add",
        "stocks_matched": "stocks matched your current rules.",
        "ticker": "Ticker",
        "name": "Name",
        "trend": "Trend",
        "action": "Action",
        "close": "Close",
        "model": "Model",
        "technical_rating": "Technical Rating",
        "why_selected": "Why Selected",
        "watchlist": "Watchlist",
        "insight": "Insight",
        "last_sync": "Last Sync",
        "ready": "Ready",
        "waiting": "Waiting",
        "off": "Off",
        "sync_on": "Sync On",
        "in_watchlist": "In Watchlist",
        "sync_now": "Sync Now",
        "add_to_watchlist": "Add To Watchlist",
        "open_insight": "Open Insight",
        "no_match": "No stocks matched the current rules.",
        "no_saved": "No saved strategies yet.",
        "load": "Load",
        "rename": "Rename",
        "delete": "Delete",
        "run_strategy": "Run Strategy",
        "view_evaluation": "View Evaluation",
        "new_name": "New name",
        "run_receipt": "Strategy Run Receipt",
        "summary": "Summary",
        "hits": "Hits",
        "review_sync_settings": "Review Sync Settings",
        "language": "Language",
        "sync_top_n_now": "Sync Top N Results Now",
        "sync_top_n_help": "Sync top N current results (0 = all in watchlist results)",
        "drag_hint": "Drag the bar below to see more columns",
        "risk_overview": "Risk Overview",
        "tagged_names": "Tagged Names",
        "common_risks": "Common Risks",
        "risk_examples": "Examples",
        "no_execution_risks": "No execution warnings in the current screener view.",
        "today_focus_pool": "Today Focus Pool",
        "pattern_hits": "Pattern Hits",
        "snapshot_empty": "No candidates are available in this board yet.",
        "added_to_focus_message": "Added {ticker} to today focus pool.",
        "snapshot_score": "Snapshot Score",
        "score_breakdown": "Score Drivers",
        "market_sentiment": "Market Sentiment",
        "view_mode": "View Mode",
        "mode_premarket": "Premarket",
        "mode_monitor": "Monitor",
        "mode_postmarket": "Postmarket",
        "template_read": "Template Read",
        "template_bias": "Current Bias",
        "template_takeaway": "Takeaway",
        "snapshot_pending": "This screener snapshot is still being prepared in the background. Please refresh shortly.",
        "snapshot_pending_short": "Snapshot pending",
        "snapshot_pending_export": "Snapshot is still being prepared. Export will be available after the background job finishes.",
    },
    "zh": {
        "back_to_dashboard": "返回总览",
        "open_watchlist": "打开自选股",
        "sync_cn_fundamentals": "同步A股基本面",
        "open_focus_pool": "打开今日重点盯盘池",
        "open_market_snapshot": "打开市场快照榜单",
        "quant_screener": "量化选股器",
        "market_snapshot": "市场快照榜单",
        "title": "基于规则的选股",
        "rules": "筛选条件",
        "results": "结果",
        "saved_strategies": "已保存策略",
        "model_template": "模型模板",
        "universe": "股票池",
        "market": "市场",
        "min_trend_score": "最低趋势分",
        "action_filter": "形态筛选",
        "min_volume_strength": "最低量能强度",
        "cn_rules": "基本面规则",
        "min_listing_days": "最少上市天数",
        "pe_range": "市盈率区间",
        "min_roe_3y": "三年平均ROE下限 (%)",
        "min_profit_yoy": "净利润同比下限 (%)",
        "min_revenue_yoy": "营收同比下限 (%)",
        "max_debt": "资产负债率上限 (%)",
        "min_dividend": "股息率下限 (%)",
        "exclude_bottom_cap": "剔除底部市值比例 (%)",
        "recent_snapshot_runs": "最近快照窗口",
        "min_snapshot_hits": "最少连续入选次数",
        "model_signal_filter": "模型信号",
        "min_model_signal_strength": "最低信号强度",
        "execution_tag_filter": "执行提醒标签",
        "exclude_execution_tag_filter": "排除标签",
        "run_screener": "开始选股",
        "save_strategy": "保存当前策略",
        "strategy_name": "我的策略名称",
        "save_as_strategy": "保存为我的策略",
        "export_csv": "导出 CSV",
        "only_add_top_n": "只加入前 N 名（0 代表全部）",
        "auto_enable_sync": "加入后自动开启同步",
        "add_current_results": "将当前结果加入自选",
        "focus_top_n": "加入今日重点盯盘池前 N 名（0 代表全部）",
        "add_current_results_to_focus": "将当前结果加入今日重点盯盘池",
        "add_to_today_focus": "加入今日重点盯盘池",
        "no_results_to_add": "当前没有可加入结果",
        "stocks_matched": "只股票符合当前规则。",
        "ticker": "代码",
        "name": "名称",
        "trend": "趋势",
        "action": "动作",
        "close": "收盘价",
        "model": "模型",
        "technical_rating": "技术评级",
        "why_selected": "入选原因",
        "watchlist": "自选状态",
        "insight": "分析页",
        "last_sync": "最近同步",
        "ready": "已就绪",
        "waiting": "同步中",
        "off": "未开启",
        "sync_on": "同步已开",
        "in_watchlist": "已在自选",
        "sync_now": "立即同步",
        "add_to_watchlist": "加入自选",
        "open_insight": "打开分析页",
        "no_match": "当前没有股票符合筛选规则。",
        "no_saved": "还没有保存的策略。",
        "load": "加载",
        "rename": "重命名",
        "delete": "删除",
        "run_strategy": "运行策略",
        "view_evaluation": "查看评测",
        "new_name": "新名称",
        "run_receipt": "策略运行收据",
        "summary": "摘要",
        "hits": "命中数",
        "review_sync_settings": "检查同步设置",
        "language": "语言",
        "sync_top_n_now": "立即同步前 N 个结果",
        "sync_top_n_help": "同步当前结果里的前 N 个（0 代表全部自选结果）",
        "drag_hint": "可拖动底部滚动条查看更多列",
        "risk_overview": "风险概览",
        "tagged_names": "带提醒股票数",
        "common_risks": "常见提醒",
        "risk_examples": "示例股票",
        "no_execution_risks": "当前选股结果里没有执行提醒。",
        "today_focus_pool": "今日重点盯盘池",
        "pattern_hits": "命中形态",
        "snapshot_empty": "这个榜单里暂时还没有候选股。",
        "added_to_focus_message": "已将 {ticker} 加入今日重点盯盘池。",
        "snapshot_score": "快照分",
        "score_breakdown": "分数驱动",
        "market_sentiment": "市场情绪",
        "view_mode": "查看模式",
        "mode_premarket": "盘前",
        "mode_monitor": "盘中观察",
        "mode_postmarket": "盘后复盘",
        "template_read": "模板解读",
        "template_bias": "当前偏向",
        "template_takeaway": "当前结论",
        "snapshot_pending": "这个选股快照还在后台预计算，请稍后刷新。",
        "snapshot_pending_short": "快照生成中",
        "snapshot_pending_export": "选股快照仍在后台生成，待任务完成后即可导出。",
    },
}

TEMPLATE_LABELS = {
    "lightgbm_top_picks": {"en": "LightGBM Top Picks", "zh": "LightGBM 多因子优选"},
    "next_tesla_swing": {"en": "Next Tesla Swing", "zh": "强趋势二次启动"},
    "technical_momentum": {"en": "Technical Momentum", "zh": "技术动量"},
    "cn_limit_up_watch": {"en": "Today Limit-Up Watch", "zh": "今日涨停观察"},
    "cn_volume_breakout": {"en": "Volume Breakout From Base", "zh": "底部放量突破"},
    "cn_bullish_ma_stack": {"en": "Bullish Moving Average Stack", "zh": "均线多头排列"},
    "cn_macd_underwater_cross": {"en": "MACD Underwater Golden Cross", "zh": "MACD水下金叉"},
    "cn_ma_cluster_breakout_watch": {"en": "MA Cluster Compression", "zh": "均线密集待突破"},
    "cn_bollinger_squeeze_watch": {"en": "Bollinger Squeeze Watch", "zh": "布林带收口待突破"},
    "cn_three_white_soldiers": {"en": "Three White Soldiers", "zh": "三连阳强势延续"},
    "cn_bullish_engulfing_reversal": {"en": "Bullish Engulfing Reversal", "zh": "看涨吞没反转"},
    "cn_hammer_reversal": {"en": "Hammer Reversal", "zh": "锤子线反转"},
    "tv_multi_timeframe_bullish": {"en": "TradingView Multi-Timeframe Bullish", "zh": "TradingView多周期共振"},
    "global_growth_value": {"en": "Global Growth at Reasonable Value", "zh": "全球成长合理估值"},
    "global_income_quality": {"en": "Global Income and Quality", "zh": "全球高质量股息"},
    "cn_growth_value": {"en": "High Growth, Reasonable Value", "zh": "高成长低估值"},
    "cn_high_roe_steady_growth": {"en": "High ROE Steady Growth", "zh": "高ROE稳增长"},
    "cn_low_valuation_high_dividend": {"en": "Low Valuation High Dividend", "zh": "低估值高分红"},
}

PATTERN_EVALUATION_TEMPLATES = {
    "cn_limit_up_watch",
    "cn_volume_breakout",
    "cn_bullish_ma_stack",
    "cn_macd_underwater_cross",
    "cn_ma_cluster_breakout_watch",
    "cn_bollinger_squeeze_watch",
    "cn_three_white_soldiers",
    "cn_bullish_engulfing_reversal",
    "cn_hammer_reversal",
}

MARKET_SECTION_LABELS = {
    "en": {"CN": "A-Shares", "HK": "Hong Kong", "US": "U.S. Stocks", "OTHER": "Other"},
    "zh": {"CN": "A股", "HK": "港股", "US": "美股", "OTHER": "其他"},
}


def _lang_text(lang: str, key: str) -> str:
    language = "zh" if lang == "zh" else "en"
    return SCREEN_TEXT[language][key]


def _template_label(template_key: str, fallback: str, lang: str) -> str:
    return TEMPLATE_LABELS.get(template_key, {}).get(lang, fallback)


def _compact_text(value: str | None, limit: int = 28) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


def _market_section_label(market: str | None, lang: str) -> str:
    language = "zh" if lang == "zh" else "en"
    return MARKET_SECTION_LABELS[language].get((market or "").upper(), MARKET_SECTION_LABELS[language]["OTHER"])


def _confluence_bucket_label(bucket: str, lang: str) -> str:
    if str(bucket or "").upper() == "ALL":
        return {"zh": "任意共振动作", "en": "Any confluence"}.get(lang, "Any confluence")
    return CONFLUENCE_BUCKET_LABELS.get(bucket, {}).get(lang, bucket)


def _number_badge(value: float | int | None, *, suffix: str = "", higher_is_good: bool = True) -> str:
    if value is None:
        return "-"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    bg = "#f3f4f6"
    fg = "#374151"
    if higher_is_good:
        if numeric >= 20:
            bg, fg = "#dcfce7", "#166534"
        elif numeric >= 10:
            bg, fg = "#ecfccb", "#3f6212"
        elif numeric < 0:
            bg, fg = "#fee2e2", "#991b1b"
    else:
        if numeric <= 12:
            bg, fg = "#dcfce7", "#166534"
        elif numeric <= 25:
            bg, fg = "#fef3c7", "#92400e"
        else:
            bg, fg = "#fee2e2", "#991b1b"
    return (
        f"<span style='display:inline-flex;align-items:center;padding:4px 8px;border-radius:999px;"
        f"background:{bg};color:{fg};font-weight:700;font-size:12px;'>{numeric:.1f}{suffix}</span>"
    )


def _price_badge(value: float | int | None) -> str:
    if value is None:
        return "-"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    return (
        "<span style='display:inline-flex;align-items:center;padding:5px 10px;border-radius:999px;"
        "background:#f8fafc;color:#0f172a;font-weight:800;font-size:12px;border:1px solid #e5e7eb;'>"
        f"{numeric:.2f}"
        "</span>"
    )


def _change_chip(value: float | int | None) -> str:
    if value is None:
        return "-"
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    if numeric > 0:
        bg, fg, prefix = "#dcfce7", "#166534", "+"
    elif numeric < 0:
        bg, fg, prefix = "#fee2e2", "#991b1b", ""
    else:
        bg, fg, prefix = "#f3f4f6", "#374151", ""
    return (
        f"<span style='display:inline-flex;align-items:center;padding:5px 9px;border-radius:999px;"
        f"background:{bg};color:{fg};font-weight:800;font-size:12px;'>{prefix}{numeric:.1f}%</span>"
    )


def _trend_badge(score: float | int | None) -> str:
    if score is None:
        return "-"
    value = float(score)
    bg = "#f3f4f6"
    fg = "#374151"
    if value >= 80:
        bg, fg = "#dcfce7", "#166534"
    elif value >= 65:
        bg, fg = "#ecfccb", "#3f6212"
    elif value >= 50:
        bg, fg = "#fef3c7", "#92400e"
    else:
        bg, fg = "#fee2e2", "#991b1b"
    return (
        f"<span style='display:inline-flex;align-items:center;padding:5px 9px;border-radius:999px;"
        f"background:{bg};color:{fg};font-weight:800;font-size:12px;'>{int(value)}</span>"
    )


def _action_badge(action_label: str | None, lang: str) -> str:
    if not action_label:
        return "-"
    label = action_label
    bg = "#f3f4f6"
    fg = "#374151"
    action_key = action_label.lower().replace(" ", "_")
    if "buy" in action_key or "dip" in action_key:
        bg, fg = "#dcfce7", "#166534"
    elif "breakout" in action_key:
        bg, fg = "#dbeafe", "#1d4ed8"
    elif "hold" in action_key:
        bg, fg = "#fef3c7", "#92400e"
    elif "wait" in action_key:
        bg, fg = "#fee2e2", "#991b1b"
    return (
        f"<span style='display:inline-flex;align-items:center;padding:5px 9px;border-radius:999px;"
        f"background:{bg};color:{fg};font-weight:700;font-size:12px;white-space:nowrap;'>{label}</span>"
    )


def _why_selected_cell(reason: str | None, lang: str) -> str:
    if not reason:
        return "-"
    compact = reason if len(reason) <= 56 else f"{reason[:56].rstrip()}..."
    details_label = "Details" if lang == "en" else "展开"
    return (
        "<details style='min-width:180px;'>"
        f"<summary style='cursor:pointer;color:#0f766e;font-weight:700;list-style:none;'>{compact}</summary>"
        f"<div style='margin-top:8px;color:#4b5563;line-height:1.5;'>{reason}</div>"
        f"<div style='margin-top:6px;font-size:12px;color:#6b7280;'>{details_label}</div>"
        "</details>"
    )


def _sync_status_badge(existing: dict | None, lang: str) -> str:
    if not existing:
        label = _lang_text(lang, "off")
        bg, fg = "#f3f4f6", "#6b7280"
    elif existing.get("sync_enabled") and existing.get("sync_status") == "success":
        label = _lang_text(lang, "ready")
        bg, fg = "#dcfce7", "#166534"
    elif existing.get("sync_enabled"):
        label = _lang_text(lang, "waiting")
        bg, fg = "#fef3c7", "#92400e"
    else:
        label = _lang_text(lang, "off")
        bg, fg = "#f3f4f6", "#6b7280"
    return (
        f"<span style='display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;"
        f"background:{bg};color:{fg};font-weight:800;font-size:12px;white-space:nowrap;'>{label}</span>"
    )


def _sync_state_rank(existing: dict | None) -> int:
    if not existing:
        return 0
    if existing.get("sync_enabled") and existing.get("sync_status") == "success":
        return 3
    if existing.get("sync_enabled"):
        return 2
    return 1


def _highlight_chip(text: str) -> str:
    tone = "#0f766e"
    bg = "#eef8f5"
    lowered = text.lower()
    if "-" in text or "risk" in lowered or "debt" in lowered or "weak" in lowered:
        tone, bg = "#991b1b", "#fee2e2"
    elif "volume" in lowered or "ma20" in lowered or "move" in lowered:
        tone, bg = "#1d4ed8", "#dbeafe"
    return (
        f"<span style='display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;"
        f"background:{bg};color:{tone};font-weight:700;font-size:12px;line-height:1.2;'>{text}</span>"
    )


def _execution_tag_chip(text: str) -> str:
    return (
        "<span style='display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;"
        "background:#fff7ed;color:#c2410c;font-weight:700;font-size:12px;line-height:1.2;"
        "border:1px solid #fed7aa;'>"
        f"{text}"
        "</span>"
    )


def _risk_flag_chip(flag: str, lang: str) -> str:
    normalized = str(flag or "").strip().lower()
    if not normalized:
        return ""
    labels = {
        "rolled-over-after-spike": ("冲高转弱", "Rolled Over"),
        "do-not-chase": ("不要追高", "No Chase"),
        "drawdown-risk": ("回撤风险", "Drawdown Risk"),
        "low-conviction": ("低置信度", "Low Conviction"),
        "weak-signal-strength": ("信号偏弱", "Weak Signal"),
        "confirmation-needed": ("等待确认", "Need Confirmation"),
        "needs-better-entry": ("买点一般", "Need Better Entry"),
    }
    zh, en = labels.get(normalized, (normalized.replace("-", " "), normalized.replace("-", " ")))
    text = zh if lang == "zh" else en.title()
    tone = "#7c2d12"
    bg = "#ffedd5"
    border = "#fdba74"
    if normalized == "rolled-over-after-spike":
        tone, bg, border = "#991b1b", "#fee2e2", "#fca5a5"
    elif normalized in {"drawdown-risk", "weak-signal-strength"}:
        tone, bg, border = "#9a3412", "#ffedd5", "#fdba74"
    elif normalized in {"confirmation-needed", "needs-better-entry", "low-conviction"}:
        tone, bg, border = "#92400e", "#fef3c7", "#fcd34d"
    return (
        "<span style='display:inline-flex;align-items:center;padding:4px 8px;border-radius:999px;"
        f"background:{bg};color:{tone};border:1px solid {border};font-weight:800;font-size:11px;line-height:1.1;'>"
        f"{html.escape(text)}"
        "</span>"
    )


def _pseudo_strong_signal_html(item: dict, lang: str) -> str:
    flags = [str(flag).strip() for flag in (item.get("risk_flags") or []) if str(flag).strip()]
    focus_flags = [
        flag
        for flag in flags
        if flag.lower() in {"rolled-over-after-spike", "do-not-chase", "drawdown-risk", "confirmation-needed"}
    ]
    if not focus_flags:
        return ""
    lead = (
        "<span style='display:inline-flex;align-items:center;padding:4px 8px;border-radius:999px;"
        "background:#111827;color:#f8fafc;font-weight:900;font-size:11px;line-height:1.1;'>"
        + ("伪强势" if lang == "zh" else "False Strength")
        + "</span>"
    )
    chips = "".join(_risk_flag_chip(flag, lang) for flag in focus_flags[:3])
    return f"<div class='detail-chip-row' style='margin-top:6px;'>{lead}{chips}</div>"


def _watchlist_summary(existing: dict | None, lang: str) -> str:
    chips: list[str] = []
    if existing:
        chips.append(
            "<span style='display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;"
            f"background:#dff5ef;color:#0f766e;font-weight:700;font-size:12px;'>{_lang_text(lang, 'in_watchlist')}</span>"
        )
        if existing.get("sync_enabled"):
            chips.append(
                "<span style='display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;"
                f"background:#eef8f5;color:#0f766e;font-weight:700;font-size:12px;'>{_lang_text(lang, 'sync_on')}</span>"
            )
    else:
        chips.append(
            "<span style='display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;"
            f"background:#f3f4f6;color:#6b7280;font-weight:700;font-size:12px;'>{_lang_text(lang, 'off')}</span>"
        )
    return "<div class='detail-chip-row'>" + "".join(chips) + "</div>"


def _fmt_number(value: object, *, suffix: str = "", digits: int = 2) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "-"


def _template_interpretation_card(*, model_template: str, results: list[dict], lang: str) -> str:
    if model_template != "next_tesla_swing":
        return ""
    action_counts: dict[str, int] = {}
    for item in results:
        key = _normalize_action_filter(item.get("action_label"))
        if key:
            action_counts[key] = action_counts.get(key, 0) + 1
    buy_the_dip_count = int(action_counts.get("buy_the_dip", 0))
    breakout_count = int(action_counts.get("wait_for_breakout", 0))
    total_count = len(results)
    if lang == "zh":
        if total_count == 0:
            bias = "暂无有效候选"
            takeaway = "这套模板要求强趋势、20日动量和干净结构同时成立；当前市场暂时没有满足条件的股票。"
        elif buy_the_dip_count == 0 and breakout_count > 0:
            bias = "偏向突破确认"
            takeaway = (
                f"当前共筛出 {total_count} 只，全部是等突破确认，没有回踩买点。"
                " 说明强势股更接近新高或压力位，现阶段更适合等放量站上，而不是等回踩承接。"
            )
        elif buy_the_dip_count > 0 and breakout_count == 0:
            bias = "偏向回踩布局"
            takeaway = (
                f"当前共筛出 {total_count} 只，其中 {buy_the_dip_count} 只是回踩买点。"
                " 说明强势股已经开始回踩支撑，更适合等回踩稳住后分批观察。"
            )
        else:
            bias = "回踩与突破并存"
            takeaway = (
                f"当前共筛出 {total_count} 只，其中回踩买点 {buy_the_dip_count} 只，突破确认 {breakout_count} 只。"
                " 执行时要把回踩承接和突破跟随分成两套动作，不要混着做。"
            )
    else:
        if total_count == 0:
            bias = "No qualified setup"
            takeaway = "This template needs strong trend, valid 20-day momentum, and a clean structure. None of the current names clear that bar."
        elif buy_the_dip_count == 0 and breakout_count > 0:
            bias = "Breakout-confirmation market"
            takeaway = (
                f"{total_count} names qualified and all of them are breakout watches. "
                "The stronger names are pressing into resistance instead of retracing into support."
            )
        elif buy_the_dip_count > 0 and breakout_count == 0:
            bias = "Pullback-entry market"
            takeaway = (
                f"{total_count} names qualified and {buy_the_dip_count} are buy-the-dip setups. "
                "The stronger names are already retracing into support, so patience on pullbacks matters more than chasing."
            )
        else:
            bias = "Mixed pullback and breakout tape"
            takeaway = (
                f"{total_count} names qualified, with {buy_the_dip_count} buy-the-dip setups and {breakout_count} breakout watches. "
                "Treat pullback entries and breakout entries as separate playbooks."
            )
    return (
        "<article class='card' style='background:#f6f8f7;border-color:#d9e5df;'>"
        f"<div class='eyebrow'>{_lang_text(lang, 'template_read')}</div>"
        "<div style='display:flex;flex-wrap:wrap;gap:16px;align-items:flex-start;justify-content:space-between;'>"
        "<div>"
        f"<div style='font-size:22px;font-weight:800;color:#0f172a;margin-bottom:8px;'>{_template_label(model_template, MODEL_TEMPLATES[model_template]['label'], lang)}</div>"
        f"<div class='muted' style='margin-bottom:10px;'>{_lang_text(lang, 'template_bias')}: <strong style='color:#0f172a;'>{bias}</strong></div>"
        "<div style='display:flex;gap:12px;flex-wrap:wrap;align-items:center;'>"
        f"<span>{_action_badge('Buy The Dip', lang)} <span class='muted'>{buy_the_dip_count}</span></span>"
        f"<span>{_action_badge('Wait For Breakout', lang)} <span class='muted'>{breakout_count}</span></span>"
        "</div>"
        "</div>"
        "<div style='min-width:260px;max-width:720px;'>"
        f"<div class='muted' style='font-weight:700;margin-bottom:6px;'>{_lang_text(lang, 'template_takeaway')}</div>"
        f"<div style='color:#334155;line-height:1.6;'>{takeaway}</div>"
        "</div>"
        "</div>"
        "</article>"
    )
def _next_tesla_evaluation_card(*, market: str, lang: str) -> str:
    evaluation = build_next_tesla_evaluation(market=market, lookback_snapshots=15, top_n=20)
    maturity = next_tesla_maturity(evaluation, lang=lang)
    per_market = evaluation.get("per_market") or {}
    windows = evaluation.get("windows") or {}
    sector_windows = evaluation.get("sector_windows") or {}
    sector_counts = evaluation.get("sector_counts") or {}
    dip = windows.get("buy_the_dip") or {}
    breakout = windows.get("wait_for_breakout") or {}
    dip_5 = dip.get(5) or {}
    breakout_5 = breakout.get(5) or {}
    dip_count = int(dip_5.get("count") or 0)
    breakout_count = int(breakout_5.get("count") or 0)
    snapshot_total = int(evaluation.get("snapshot_total") or 0)
    clean_snapshot_total = int(evaluation.get("clean_snapshot_total") or 0)
    if lang == "zh":
        if dip_count <= 0 and breakout_count <= 0:
            takeaway = "历史快照里还没有足够样本，先把它当成观察模块，不要据此下结论。"
        elif dip_count > 0 and breakout_count <= 0:
            takeaway = "当前只有回踩样本可评测，先重点盯 Buy The Dip 的胜率和平均收益。"
        elif breakout_count > 0 and dip_count <= 0:
            takeaway = "当前只有突破样本可评测，说明这套模板最近更多在给突破确认而不是回踩布局。"
        else:
            dip_hit = float(dip_5.get("hit_rate") or 0.0)
            breakout_hit = float(breakout_5.get("hit_rate") or 0.0)
            dip_avg = float(dip_5.get("avg_return") or 0.0)
            breakout_avg = float(breakout_5.get("avg_return") or 0.0)
            if dip_hit >= breakout_hit + 5 and dip_avg >= breakout_avg - 1:
                takeaway = "回踩买点最近更稳，说明强势股回踩承接后的赔率更好。"
            elif breakout_hit >= dip_hit + 5 and breakout_avg >= dip_avg - 1:
                takeaway = "突破确认最近更稳，现阶段更适合等站稳再跟，而不是提前埋伏回踩。"
            else:
                takeaway = "两类打法都还能做，但要把回踩承接和突破跟随分开执行，不要混用。"
        labels = {
            "buy_the_dip": "Buy The Dip",
            "wait_for_breakout": "Wait For Breakout",
        }
        helper = "先看 5 日盈利率和平均收益，再决定这套模板当前更偏回踩还是突破。"
        samples_label = "近端样本"
        sample_note = f"本模块回看最近 {snapshot_total} 个快照，其中可用于这套模板 clean 评测的快照 {clean_snapshot_total} 个。"
    else:
        if dip_count <= 0 and breakout_count <= 0:
            takeaway = "There are not enough historical snapshot samples yet, so treat this as an observation module rather than a decision tool."
        elif dip_count > 0 and breakout_count <= 0:
            takeaway = "Only pullback samples are measurable right now, so focus on the win rate and average return of Buy The Dip setups."
        elif breakout_count > 0 and dip_count <= 0:
            takeaway = "Only breakout samples are measurable right now, which suggests this template has recently leaned toward breakout confirmation rather than pullback entries."
        else:
            dip_hit = float(dip_5.get("hit_rate") or 0.0)
            breakout_hit = float(breakout_5.get("hit_rate") or 0.0)
            dip_avg = float(dip_5.get("avg_return") or 0.0)
            breakout_avg = float(breakout_5.get("avg_return") or 0.0)
            if dip_hit >= breakout_hit + 5 and dip_avg >= breakout_avg - 1:
                takeaway = "Buy-the-dip has been steadier lately, which suggests stronger pullback support follow-through."
            elif breakout_hit >= dip_hit + 5 and breakout_avg >= dip_avg - 1:
                takeaway = "Breakout confirmation has been steadier lately, so waiting for confirmation looks cleaner than buying the pullback early."
            else:
                takeaway = "Both playbooks still work, but pullback entries and breakout entries should be handled as separate playbooks."
        labels = {
            "buy_the_dip": "Buy The Dip",
            "wait_for_breakout": "Wait For Breakout",
        }
        helper = "Use the 5-day hit rate and average return first, then decide whether the tape currently favors pullbacks or confirmation entries."
        samples_label = "Recent samples"
        sample_note = f"This module reviews the latest {snapshot_total} snapshots, and {clean_snapshot_total} of them are clean enough for this template evaluation."

    def _metric_rows(action_key: str) -> str:
        payload = windows.get(action_key) or {}
        return "".join(
            "<tr>"
            f"<td>{window}D</td>"
            f"<td>{int((payload.get(window) or {}).get('count') or 0)}</td>"
            f"<td>{_fmt_number((payload.get(window) or {}).get('avg_return'), suffix='%', digits=2)}</td>"
            f"<td>{_fmt_number((payload.get(window) or {}).get('hit_rate'), suffix='%', digits=1)}</td>"
            f"<td>{_fmt_number((payload.get(window) or {}).get('strong_hit_rate'), suffix='%', digits=1)}</td>"
            f"<td>{_fmt_number((payload.get(window) or {}).get('miss_rate'), suffix='%', digits=1)}</td>"
            "</tr>"
            for window in (3, 5, 10)
        )

    def _sample_rows(action_key: str) -> str:
        return "".join(
            f"<div class='muted'>• {html.escape(str(item.get('trade_date') or '-'))} · {html.escape(str(item.get('ticker') or '-'))} · {html.escape(str(item.get('sector') or '-'))} · "
            f"{_fmt_number(item.get('return_5d'), suffix='%', digits=2)} / {_fmt_number(item.get('return_10d'), suffix='%', digits=2)}</div>"
            for item in (evaluation.get("samples") or {}).get(action_key, [])[:4]
        ) or f"<div class='muted'>-</div>"

    def _sector_rows(action_key: str) -> str:
        groups = sector_windows.get(action_key) or {}
        counts = sector_counts.get(action_key) or {}
        ranked = sorted(
            set(groups.keys()) | set(counts.keys()),
            key=lambda pair: (
                -int(counts.get(pair, 0)),
                -int(((groups.get(pair) or {}).get(5) or {}).get("count") or 0),
                str(pair or ""),
            ),
        )[:3]
        return "".join(
            f"<div class='muted'>• {html.escape(str(sector or '-'))} · "
            f"{int(counts.get(sector, 0))} {'次出现' if lang == 'zh' else 'hits'}"
            + (
                f" · {_fmt_number((((groups.get(sector) or {}).get(5) or {}).get('avg_return')), suffix='%', digits=2)} / {_fmt_number((((groups.get(sector) or {}).get(5) or {}).get('hit_rate')), suffix='%', digits=1)}"
                if int((((groups.get(sector) or {}).get(5) or {}).get('count') or 0)) > 0
                else ""
            )
            + "</div>"
            for sector in ranked
        ) or f"<div class='muted'>-</div>"

    def _market_split_html() -> str:
        market_codes = [code for code in ("CN", "US") if code in per_market]
        if len(market_codes) <= 1:
            return ""
        return (
            "<div style='display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));margin-bottom:12px;'>"
            + "".join(
                (
                    "<div style='border:1px solid #d9e5df;border-radius:18px;padding:14px;background:rgba(255,255,255,0.68);'>"
                    f"<div style='font-size:16px;font-weight:800;color:#0f172a;margin-bottom:6px;'>{'A股' if code == 'CN' and lang == 'zh' else '美股' if code == 'US' and lang == 'zh' else code}</div>"
                    f"<div class='muted'>{html.escape(str(next_tesla_maturity(per_market.get(code) or {}, lang=lang).get('level') or '-'))}</div>"
                    f"<div class='muted' style='margin-top:6px;'>{'当前偏向' if lang == 'zh' else 'Current bias'}: {html.escape(next_tesla_market_bias(per_market.get(code) or {}, lang=lang))}</div>"
                    f"<div class='muted' style='margin-top:6px;'>{'快照' if lang == 'zh' else 'Snapshots'} {int((per_market.get(code) or {}).get('snapshot_total') or 0)} · {'clean 样本' if lang == 'zh' else 'Clean samples'} {int((per_market.get(code) or {}).get('clean_snapshot_total') or 0)}</div>"
                    "</div>"
                )
                for code in market_codes
            )
            + "</div>"
        )

    return (
        "<article class='card' style='background:#f7faf8;border-color:#dce8e1;'>"
        f"<div class='eyebrow'>{'模型评测' if lang == 'zh' else 'Template Evaluation'}</div>"
        f"<div class='muted' style='margin-bottom:10px;'>{helper}</div>"
        f"<div style='display:inline-flex;align-items:center;padding:8px 12px;border-radius:999px;margin-bottom:12px;"
        + (
            "background:#dcfce7;color:#166534;"
            if str(maturity.get('tone')) == 'good'
            else "background:#fef3c7;color:#92400e;"
            if str(maturity.get('tone')) == 'mid'
            else "background:#e5eef7;color:#37516b;"
        )
        + f"font-weight:800;font-size:12px;'>{html.escape(str(maturity.get('level') or '-'))}</div>"
        + _market_split_html()
        + "<div style='display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));'>"
        + "".join(
            (
                "<div style='border:1px solid #d9e5df;border-radius:18px;padding:16px;background:rgba(255,255,255,0.68);'>"
                f"<div style='font-size:18px;font-weight:800;color:#0f172a;margin-bottom:8px;'>{labels[action_key]}</div>"
                "<div style='overflow-x:auto;border:1px solid #e2e8f0;border-radius:12px;background:white;'>"
                "<table style='width:100%;min-width:520px;border-collapse:collapse;font-size:13px;'>"
                f"<thead><tr><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>窗口</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{'样本' if lang == 'zh' else 'Samples'}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{'平均收益' if lang == 'zh' else 'Avg Return'}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{'盈利率' if lang == 'zh' else 'Hit Rate'}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{'强命中' if lang == 'zh' else 'Strong Hit'}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{'失效率' if lang == 'zh' else 'Miss Rate'}</th></tr></thead>"
                f"<tbody>{_metric_rows(action_key)}</tbody>"
                "</table></div>"
                f"<div style='margin-top:10px;font-weight:700;color:#334155;'>{samples_label}</div>"
                f"{_sample_rows(action_key)}"
                f"<div style='margin-top:10px;font-weight:700;color:#334155;'>{'高频板块' if lang == 'zh' else 'Most Frequent Sectors'}</div>"
                f"{_sector_rows(action_key)}"
                "</div>"
            )
            for action_key in ("buy_the_dip", "wait_for_breakout")
        )
        + "</div>"
        f"<div class='muted' style='margin-top:10px;'>{sample_note}</div>"
        f"<div class='muted' style='margin-top:8px;'>{html.escape(str(maturity.get('summary') or ''))}</div>"
        f"<div class='muted' style='margin-top:12px;font-weight:700;'>{'结论' if lang == 'zh' else 'Takeaway'}: {takeaway}</div>"
        "</article>"
    )


def _template_overview_brief_html(*, model_template: str, market: str, lang: str) -> str:
    def _maturity_rank(level: str | None) -> int:
        value = str(level or "").strip().lower()
        if value in {"可比较", "comparable"}:
            return 2
        if value in {"初步参考", "early read"}:
            return 1
        return 0

    def _market_label(code: str) -> str:
        if code == "CN":
            return "A股" if lang == "zh" else "CN"
        if code == "US":
            return "美股" if lang == "zh" else "US"
        return code

    if model_template == "next_tesla_swing":
        evaluation = build_next_tesla_evaluation(market=market, lookback_snapshots=15, top_n=20)
        maturity = next_tesla_maturity(evaluation, lang=lang)
        per_market = evaluation.get("per_market") or {}
        sample_count = int(evaluation.get("clean_snapshot_total") or 0)
        summary = str(maturity.get("summary") or "")
        total_rank = _maturity_rank(str(maturity.get("level") or "")) * 100 + sample_count
        focus_value = (
            f"{maturity.get('level') or '-'} · clean {sample_count}"
            if lang == "zh"
            else f"{maturity.get('level') or '-'} · clean {sample_count}"
        )
        focus_copy = (
            f"当前这套模板累计 {sample_count} 个 clean 样本，先看 Buy The Dip 和 Wait For Breakout 谁更稳。"
            if lang == "zh"
            else f"This template currently has {sample_count} clean samples, so start by comparing Buy The Dip versus Wait For Breakout."
        )
        if market == "ALL":
            cn_eval = per_market.get("CN") or {}
            us_eval = per_market.get("US") or {}
            cn_score = _maturity_rank(str(next_tesla_maturity(cn_eval, lang=lang).get("level") or "")) * 100 + int(cn_eval.get("clean_snapshot_total") or 0)
            us_score = _maturity_rank(str(next_tesla_maturity(us_eval, lang=lang).get("level") or "")) * 100 + int(us_eval.get("clean_snapshot_total") or 0)
            if cn_score >= us_score + 8:
                market_value = "A股更有参考价值" if lang == "zh" else "CN is more informative"
                market_copy = (
                    "A股这边的 clean 样本沉淀更多，先在 A股里看回踩和突破的节奏更稳。"
                    if lang == "zh"
                    else "CN has the stronger clean-sample base, so it is the better place to study pullback versus breakout behavior first."
                )
            elif us_score >= cn_score + 8:
                market_value = "美股更有参考价值" if lang == "zh" else "US is more informative"
                market_copy = (
                    "美股这边的 clean 样本更完整，先在美股里看这套模板的动作差异更有意义。"
                    if lang == "zh"
                    else "US has the stronger clean-sample base, so it is the more useful market for reading this template right now."
                )
            else:
                market_value = "A股和美股目前接近" if lang == "zh" else "CN and US are currently close"
                market_copy = (
                    "两个市场都还在样本沉淀期，暂时不适合只因为市场不同就下强判断。"
                    if lang == "zh"
                    else "Both markets are still accumulating samples, so it is too early to force a strong market-level preference."
                )
        else:
            market_value = f"当前范围：{_market_label(market)}" if lang == "zh" else f"Current scope: {_market_label(market)}"
            market_copy = (
                "当前页面已经只看这个市场，先在该市场里比较回踩和突破，再回头做跨市场判断。"
                if lang == "zh"
                else "This page is already scoped to one market, so compare pullback versus breakout here before making cross-market judgments."
            )
        if total_rank <= 0:
            verdict_value = "先观察，不急着下结论" if lang == "zh" else "Observe first, do not force a verdict"
            verdict_copy = (
                "当前更适合作为观察面板，重点是持续留样，而不是立刻判断哪种动作一定更赚钱。"
                if lang == "zh"
                else "This is better used as an observation panel for now, with sample collection taking priority over forcing a winner."
            )
        elif total_rank < 200:
            verdict_value = "可以初步参考" if lang == "zh" else "Good for an early read"
            verdict_copy = (
                "已经可以开始观察回踩和突破谁更稳，但还不适合把它当成高置信度评判面板。"
                if lang == "zh"
                else "It is now useful for an early read on pullback versus breakout, but still too early for a high-confidence scorecard."
            )
        else:
            verdict_value = "样本已经可比较" if lang == "zh" else "Samples are now comparable"
            verdict_copy = (
                "当前可以更认真地比较回踩与突破的胜率和板块集中度。"
                if lang == "zh"
                else "You can now compare pullback versus breakout with more confidence, including sector concentration."
            )
    elif model_template == "technical_momentum":
        evaluation = build_technical_momentum_evaluation(market=market, lookback_snapshots=15, top_n=40)
        maturity = technical_momentum_maturity(evaluation, lang=lang)
        per_market = evaluation.get("per_market") or {}
        sample_count = int(evaluation.get("labeled_snapshot_total") or 0)
        summary = str(maturity.get("summary") or "")
        total_rank = _maturity_rank(str(maturity.get("level") or "")) * 100 + sample_count
        focus_value = (
            f"{maturity.get('level') or '-'} · 标签样本 {sample_count}"
            if lang == "zh"
            else f"{maturity.get('level') or '-'} · labeled {sample_count}"
        )
        focus_copy = (
            "当前更适合先看 BUY 和 WATCH 谁更稳，再决定这套动量模板该更激进还是更保守。"
            if lang == "zh"
            else "Start by comparing BUY versus WATCH, then decide whether this momentum template currently deserves a more aggressive or more patient read."
        )
        if market == "ALL":
            cn_eval = per_market.get("CN") or {}
            us_eval = per_market.get("US") or {}
            cn_score = _maturity_rank(str(technical_momentum_maturity(cn_eval, lang=lang).get("level") or "")) * 100 + int(cn_eval.get("labeled_snapshot_total") or 0)
            us_score = _maturity_rank(str(technical_momentum_maturity(us_eval, lang=lang).get("level") or "")) * 100 + int(us_eval.get("labeled_snapshot_total") or 0)
            if cn_score >= us_score + 8:
                market_value = "A股更有参考价值" if lang == "zh" else "CN is more informative"
                market_copy = (
                    "A股这边的带标签样本更完整，先在 A股里看 BUY / WATCH 的节奏更有意义。"
                    if lang == "zh"
                    else "CN currently has the better labeled-sample base, so it is the more useful place to read BUY versus WATCH behavior."
                )
            elif us_score >= cn_score + 8:
                market_value = "美股更有参考价值" if lang == "zh" else "US is more informative"
                market_copy = (
                    "美股这边的带标签样本更完整，先在美股里看动量确认是否更顺。"
                    if lang == "zh"
                    else "US currently has the better labeled-sample base, so it is the better place to inspect momentum follow-through."
                )
            else:
                market_value = "A股和美股目前接近" if lang == "zh" else "CN and US are currently close"
                market_copy = (
                    "两个市场都还在积累样本，先持续观察，不要急着把胜负归因到市场差异。"
                    if lang == "zh"
                    else "Both markets are still accumulating samples, so keep observing instead of forcing a strong market-level conclusion."
                )
        else:
            market_value = f"当前范围：{_market_label(market)}" if lang == "zh" else f"Current scope: {_market_label(market)}"
            market_copy = (
                "当前页面已经只看这个市场，适合先在这里比较 BUY 和 WATCH，再回头做跨市场判断。"
                if lang == "zh"
                else "This page is already scoped to one market, so compare BUY versus WATCH here before making cross-market judgments."
            )
        if total_rank <= 0:
            verdict_value = "先观察，不急着下结论" if lang == "zh" else "Observe first, do not force a verdict"
            verdict_copy = (
                "当前更适合作为观察面板，重点是继续积累 BUY / WATCH 的成熟窗口。"
                if lang == "zh"
                else "This is still better used as an observation panel while more mature BUY / WATCH windows accumulate."
            )
        elif total_rank < 200:
            verdict_value = "可以初步参考" if lang == "zh" else "Good for an early read"
            verdict_copy = (
                "已经可以开始观察 BUY 和 WATCH 的差异，但还不适合把它当成高置信度评分卡。"
                if lang == "zh"
                else "It is useful for an early read on BUY versus WATCH, but still too early for a high-confidence scorecard."
            )
        else:
            verdict_value = "样本已经可比较" if lang == "zh" else "Samples are now comparable"
            verdict_copy = (
                "当前可以更认真地比较 BUY / WATCH 的胜率和主导板块。"
                if lang == "zh"
                else "You can now compare BUY versus WATCH more seriously, including their dominant sector mix."
            )
    elif model_template == "lightgbm_top_picks":
        evaluation = build_lightgbm_prediction_evaluation(market=market, recent_runs=8, top_n=40)
        sample_count = int(evaluation.get("sample_count") or 0)
        per_market = evaluation.get("per_market") or {}
        latest_trade_date = str(evaluation.get("latest_trade_date") or "")
        summary = (
            f"最近直接回看 {int(evaluation.get('run_count') or 0)} 个成功 LightGBM run，累计样本 {sample_count} 条；最新交易日 {latest_trade_date or '-'}。"
            if lang == "zh"
            else f"Directly reviewing the latest {int(evaluation.get('run_count') or 0)} successful LightGBM runs with {sample_count} samples; latest trade date {latest_trade_date or '-'}."
        )
        windows = evaluation.get("windows") or {}
        breakout_1d = (windows.get("breakout") or {}).get(1) or {}
        pullback_1d = (windows.get("pullback") or {}).get(1) or {}
        watch_1d = (windows.get("watch") or {}).get(1) or {}
        maturity_level = "可比较" if sample_count >= 120 else "初步参考" if sample_count >= 40 else "观察期"
        focus_value = (
            f"{maturity_level} · 历史样本 {sample_count}"
            if lang == "zh"
            else f"{maturity_level} · samples {sample_count}"
        )
        focus_copy = (
            "这块直接回答 LightGBM 次日、3日、5日到底好不好用，更适合拿来判断第二天操作。"
            if lang == "zh"
            else "This directly answers whether LightGBM is usable over the next 1, 3, and 5 sessions, which is more aligned with next-day execution."
        )
        if market == "ALL":
            cn_eval = per_market.get("CN") or {}
            us_eval = per_market.get("US") or {}
            cn_score = int(cn_eval.get("sample_count") or 0)
            us_score = int(us_eval.get("sample_count") or 0)
            if cn_score >= us_score + 20:
                market_value = "A股更有参考价值" if lang == "zh" else "CN is more informative"
                market_copy = (
                    "当前历史验证几乎都来自 A股，先按 A股的次日 / 3日 / 5日节奏来读这套模型。"
                    if lang == "zh"
                    else "Historical validation is currently concentrated in CN, so read this model primarily through the CN 1D / 3D / 5D lens."
                )
            elif us_score >= cn_score + 20:
                market_value = "美股更有参考价值" if lang == "zh" else "US is more informative"
                market_copy = (
                    "当前历史验证更多来自美股，先按美股的短周期表现来读这套模型。"
                    if lang == "zh"
                    else "Historical validation is currently stronger in US, so read this model through the US short-horizon results first."
                )
            else:
                market_value = "A股和美股目前接近" if lang == "zh" else "CN and US are currently close"
                market_copy = (
                    "两个市场当前都可观察，但还要结合样本数判断哪边更值得信。"
                    if lang == "zh"
                    else "Both markets are worth watching, but sample depth still matters before assigning stronger confidence."
                )
        else:
            market_value = f"当前范围：{_market_label(market)}" if lang == "zh" else f"Current scope: {_market_label(market)}"
            market_copy = (
                "当前页面已经只看这个市场，先在该市场里判断次日胜率和动作偏向。"
                if lang == "zh"
                else "This page is already scoped to one market, so judge next-day hit rate and action bias inside this market first."
            )
        ranked = sorted(
            [
                (int(breakout_1d.get("count") or 0), float(breakout_1d.get("hit_rate") or 0.0), "Breakout"),
                (int(pullback_1d.get("count") or 0), float(pullback_1d.get("hit_rate") or 0.0), "Pullback"),
                (int(watch_1d.get("count") or 0), float(watch_1d.get("hit_rate") or 0.0), "Watch"),
            ],
            key=lambda item: (-item[0], -item[1], item[2]),
        )
        lead_count, lead_hit, lead_label = ranked[0]
        if sample_count <= 0 or lead_count <= 0:
            verdict_value = "先观察，不急着下结论" if lang == "zh" else "Observe first, do not force a verdict"
            verdict_copy = (
                "当前历史样本还不够，先把这套模型当作观察面板，而不是直接依赖它做第二天交易。"
                if lang == "zh"
                else "Historical sample depth is still too thin, so treat this as an observation panel rather than a next-day execution engine."
            )
        elif sample_count < 80:
            verdict_value = "可以初步参考" if lang == "zh" else "Good for an early read"
            verdict_copy = (
                f"当前 1D 更偏 {lead_label}，命中率 {_fmt_number(lead_hit, suffix='%', digits=1)}，已经可以开始作为次日操作参考。"
                if lang == "zh"
                else f"1D currently leans {lead_label} with a {_fmt_number(lead_hit, suffix='%', digits=1)} hit rate, which is useful as an early next-day read."
            )
        else:
            verdict_value = "次日统计已可参考" if lang == "zh" else "1D stats are now usable"
            verdict_copy = (
                f"当前 1D 更偏 {lead_label}，命中率 {_fmt_number(lead_hit, suffix='%', digits=1)}，已经可以更认真地纳入第二天操作决策。"
                if lang == "zh"
                else f"1D currently leans {lead_label} with a {_fmt_number(lead_hit, suffix='%', digits=1)} hit rate, which is strong enough to weigh more seriously in next-session decisions."
            )
    else:
        return ""
    return (
        "<article class='card' style='background:#f7faf8;border-color:#dce8e1;'>"
        f"<div class='eyebrow'>{'模型评测摘要' if lang == 'zh' else 'Evaluation Brief'}</div>"
        f"<div class='muted' style='margin-bottom:12px;'>{html.escape(summary)}</div>"
        "<div style='display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));'>"
        + "".join(
            (
                "<div style='border:1px solid #d9e5df;border-radius:18px;padding:14px;background:rgba(255,255,255,0.68);'>"
                f"<div style='font-size:11px;font-weight:800;letter-spacing:0.06em;text-transform:uppercase;color:#64748b;margin-bottom:6px;'>{title}</div>"
                f"<div style='font-size:22px;font-weight:800;color:#0f172a;line-height:1.25;margin-bottom:8px;'>{html.escape(value)}</div>"
                f"<div class='muted'>{html.escape(copy)}</div>"
                "</div>"
            )
            for title, value, copy in (
                ("当前更该怎么看" if lang == "zh" else "How to read it now", focus_value, focus_copy),
                ("市场参考度" if lang == "zh" else "Market usefulness", market_value, market_copy),
                ("一句话判断" if lang == "zh" else "Bottom line", verdict_value, verdict_copy),
            )
        )
        + "</div></article>"
    )


def _technical_momentum_evaluation_card(*, market: str, lang: str) -> str:
    evaluation = build_technical_momentum_evaluation(market=market, lookback_snapshots=15, top_n=40)
    maturity = technical_momentum_maturity(evaluation, lang=lang)
    per_market = evaluation.get("per_market") or {}
    windows = evaluation.get("windows") or {}
    sector_windows = evaluation.get("sector_windows") or {}
    sector_counts = evaluation.get("sector_counts") or {}
    labeled_snapshot_total = int(evaluation.get("labeled_snapshot_total") or 0)
    snapshot_total = int(evaluation.get("snapshot_total") or 0)

    def _metric_row(action_key: str, label: str) -> str:
        payload = windows.get(action_key) or {}
        return (
            "<tr>"
            f"<td>{label}</td>"
            f"<td>{int((payload.get(3) or {}).get('count') or 0)}</td>"
            f"<td>{_fmt_number((payload.get(3) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_number((payload.get(3) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{int((payload.get(5) or {}).get('count') or 0)}</td>"
            f"<td>{_fmt_number((payload.get(5) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_number((payload.get(5) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{int((payload.get(10) or {}).get('count') or 0)}</td>"
            f"<td>{_fmt_number((payload.get(10) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_number((payload.get(10) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            "</tr>"
        )

    def _market_split_html() -> str:
        market_codes = [code for code in ("CN", "US") if code in per_market]
        if len(market_codes) <= 1:
            return ""
        return (
            "<div style='display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));margin-bottom:12px;'>"
            + "".join(
                (
                    "<div style='border:1px solid #d9e5df;border-radius:18px;padding:14px;background:rgba(255,255,255,0.68);'>"
                    f"<div style='font-size:16px;font-weight:800;color:#0f172a;margin-bottom:6px;'>{'A股' if code == 'CN' and lang == 'zh' else '美股' if code == 'US' and lang == 'zh' else code}</div>"
                    f"<div class='muted'>{html.escape(str(technical_momentum_maturity(per_market.get(code) or {}, lang=lang).get('level') or '-'))}</div>"
                    f"<div class='muted' style='margin-top:6px;'>{'当前偏向' if lang == 'zh' else 'Current bias'}: {html.escape(technical_momentum_bias(per_market.get(code) or {}, lang=lang))}</div>"
                    f"<div class='muted' style='margin-top:6px;'>{'快照' if lang == 'zh' else 'Snapshots'} {int((per_market.get(code) or {}).get('snapshot_total') or 0)} · {'带标签样本' if lang == 'zh' else 'Labeled samples'} {int((per_market.get(code) or {}).get('labeled_snapshot_total') or 0)}</div>"
                    "</div>"
                )
                for code in market_codes
            )
            + "</div>"
        )

    def _sector_summary(action_key: str) -> str:
        groups = sector_windows.get(action_key) or {}
        counts = sector_counts.get(action_key) or {}
        ordered = sorted(
            counts.items(),
            key=lambda item: (
                -int((((groups.get(item[0]) or {}).get(5) or {}).get("count") or 0)),
                -int(item[1] or 0),
                str(item[0] or ""),
            ),
        )[:3]
        if not ordered:
            return f"<div class='muted'>{'当前还没有足够的板块样本。' if lang == 'zh' else 'No sector concentration yet.'}</div>"
        rows: list[str] = []
        for sector_label, seen_count in ordered:
            stats_5 = ((groups.get(sector_label) or {}).get(5) or {})
            rows.append(
                "<div style='padding:8px 0;border-bottom:1px solid #e2e8f0;'>"
                f"<div style='font-weight:700;color:#0f172a;'>{html.escape(str(sector_label or '-'))}</div>"
                f"<div class='muted'>{'出现' if lang == 'zh' else 'Seen'} {int(seen_count)} {'次' if lang == 'zh' else 'times'}"
                + (
                    f" · 5D {_fmt_number(stats_5.get('avg_return'), suffix='%', digits=2)} / {_fmt_number(stats_5.get('hit_rate'), suffix='%', digits=1)}"
                    if int(stats_5.get("count") or 0) > 0
                    else ""
                )
                + "</div></div>"
            )
        return "".join(rows)

    maturity_style = (
        "background:#dcfce7;color:#166534;"
        if str(maturity.get("tone")) == "good"
        else "background:#fef3c7;color:#92400e;"
        if str(maturity.get("tone")) == "mid"
        else "background:#e5eef7;color:#37516b;"
    )


def _technical_pattern_evaluation_card(*, model_template: str, market: str, lang: str) -> str:
    evaluation = build_pattern_template_evaluation(
        template_key=model_template,
        market=market,
        lookback_snapshots=15,
        top_n=40,
    )
    maturity = pattern_template_maturity(evaluation, lang=lang)
    windows = evaluation.get("windows") or {}
    execution = evaluation.get("execution") or {}
    sector_windows = evaluation.get("sector_windows") or {}
    sector_counts = evaluation.get("sector_counts") or {}
    snapshot_total = int(evaluation.get("snapshot_total") or 0)
    labeled_snapshot_total = int(evaluation.get("labeled_snapshot_total") or 0)
    template_name = _template_label(model_template, MODEL_TEMPLATES[model_template]["label"], lang)

    def _metric_row(action_key: str, label: str) -> str:
        payload = windows.get(action_key) or {}
        return (
            "<tr>"
            f"<td>{label}</td>"
            f"<td>{int((payload.get(1) or {}).get('count') or 0)}</td>"
            f"<td>{_fmt_number((payload.get(1) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_number((payload.get(1) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{int((payload.get(3) or {}).get('count') or 0)}</td>"
            f"<td>{_fmt_number((payload.get(3) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_number((payload.get(3) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{int((payload.get(5) or {}).get('count') or 0)}</td>"
            f"<td>{_fmt_number((payload.get(5) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_number((payload.get(5) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            "</tr>"
        )

    def _sector_summary(action_key: str) -> str:
        groups = sector_windows.get(action_key) or {}
        counts = sector_counts.get(action_key) or {}
        ordered = sorted(
            set(groups.keys()) | set(counts.keys()),
            key=lambda sector: (
                -int((counts.get(sector) or 0)),
                -int((((groups.get(sector) or {}).get(5) or {}).get("count") or 0)),
                str(sector or ""),
            ),
        )[:3]
        if not ordered:
            return f"<div class='muted'>{'当前还没有足够的板块样本。' if lang == 'zh' else 'No sector concentration yet.'}</div>"
        rows: list[str] = []
        for sector_label in ordered:
            stats_5 = ((groups.get(sector_label) or {}).get(5) or {})
            rows.append(
                "<div style='padding:8px 0;border-bottom:1px solid #e2e8f0;'>"
                f"<div style='font-weight:700;color:#0f172a;'>{html.escape(str(sector_label or '-'))}</div>"
                f"<div class='muted'>{'出现' if lang == 'zh' else 'Seen'} {int(counts.get(sector_label, 0))} {'次' if lang == 'zh' else 'times'}"
                + (
                    f" · 5D {_fmt_number(stats_5.get('avg_return'), suffix='%', digits=2)} / {_fmt_number(stats_5.get('hit_rate'), suffix='%', digits=1)}"
                    if int(stats_5.get("count") or 0) > 0
                    else ""
                )
                + "</div></div>"
            )
        return "".join(rows)

    def _execution_row(action_key: str, label: str) -> str:
        stats = execution.get(action_key) or {}
        return (
            "<tr>"
            f"<td>{label}</td>"
            f"<td>{int(stats.get('count') or 0)}</td>"
            f"<td>{_fmt_number(stats.get('execution_hit_rate'), suffix='%', digits=1)}</td>"
            f"<td>{_fmt_number(stats.get('avg_next_open_gap'), suffix='%', digits=2)}</td>"
            f"<td>{_fmt_number(stats.get('avg_next_open_to_high'), suffix='%', digits=2)}</td>"
            f"<td>{_fmt_number(stats.get('avg_next_low_drawdown'), suffix='%', digits=2)}</td>"
            f"<td>{_fmt_number(stats.get('gap_blocked_rate'), suffix='%', digits=1)}</td>"
            f"<td>{_fmt_number(stats.get('limit_unbuyable_rate'), suffix='%', digits=1)}</td>"
            "</tr>"
        )

    maturity_style = (
        "background:#dcfce7;color:#166534;"
        if str(maturity.get("tone")) == "good"
        else "background:#fef3c7;color:#92400e;"
        if str(maturity.get("tone")) == "mid"
        else "background:#e5eef7;color:#37516b;"
    )
    note = (
        f"最近回看 {snapshot_total} 个快照，其中 {labeled_snapshot_total} 个带动作标签，可用于 {template_name} 的历史验证。"
        if lang == "zh"
        else f"Reviewing the latest {snapshot_total} snapshots, with {labeled_snapshot_total} carrying usable action labels for {template_name}."
    )
    takeaway = pattern_template_bias(evaluation, lang=lang)
    if labeled_snapshot_total <= 0:
        tactical_note = (
            "当前还没有足够成熟的样本，先把这套模板当作观察面板。"
            if lang == "zh"
            else "There are not enough mature samples yet, so treat this template as an observation panel first."
        )
    else:
        tactical_note = (
            "先用 1D / 3D / 5D 看它更偏回踩、突破，还是只适合观察，再决定第二天是否处理。"
            if lang == "zh"
            else "Use the 1D / 3D / 5D windows to judge whether this setup currently behaves more like a pullback, a breakout, or a watch-only candidate."
        )
    return (
        "<article class='card' style='background:#f7faf8;border-color:#dce8e1;'>"
        f"<div class='eyebrow'>{'模型评测' if lang == 'zh' else 'Template Evaluation'}</div>"
        f"<div class='muted' style='margin-bottom:10px;'>{'这块直接看历史 screener 快照的 1D / 3D / 5D 结果，更适合判断模板是否适合次日交易。' if lang == 'zh' else 'This block reads historical screener snapshots over 1D / 3D / 5D windows to judge whether the template is suitable for next-session trading.'}</div>"
        f"<div style='display:inline-flex;align-items:center;padding:8px 12px;border-radius:999px;margin-bottom:12px;{maturity_style}font-weight:800;font-size:12px;'>{html.escape(str(maturity.get('level') or '-'))}</div>"
        + "<div style='overflow-x:auto;border:1px solid #e2e8f0;border-radius:12px;background:white;'>"
        + "<table style='width:100%;min-width:760px;border-collapse:collapse;font-size:13px;'>"
        + f"<thead><tr><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{'动作' if lang == 'zh' else 'Action'}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>1D {'样本' if lang == 'zh' else 'Samples'}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>1D</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>3D {'样本' if lang == 'zh' else 'Samples'}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>3D</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>5D {'样本' if lang == 'zh' else 'Samples'}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>5D</th></tr></thead>"
        + f"<tbody>{_metric_row('buy_the_dip', 'Buy The Dip')}{_metric_row('wait_for_breakout', 'Wait For Breakout')}{_metric_row('hold_and_watch', 'Hold And Watch')}</tbody>"
        + "</table></div>"
        + "<div style='overflow-x:auto;border:1px solid #e2e8f0;border-radius:12px;background:white;margin-top:12px;'>"
        + "<table style='width:100%;min-width:860px;border-collapse:collapse;font-size:13px;'>"
        + f"<thead><tr><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{'执行动作' if lang == 'zh' else 'Execution'}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{'样本' if lang == 'zh' else 'Samples'}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{'次日可交易命中' if lang == 'zh' else 'Tradable Hit'}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{'次日开盘缺口' if lang == 'zh' else 'Next Open Gap'}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{'开盘后最大冲高' if lang == 'zh' else 'Open To High'}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{'盘中最大回撤' if lang == 'zh' else 'Intraday Drawdown'}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{'高开受阻' if lang == 'zh' else 'Gap Blocked'}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{'一字/买不到' if lang == 'zh' else 'Limit Unbuyable'}</th></tr></thead>"
        + f"<tbody>{_execution_row('buy_the_dip', 'Buy The Dip')}{_execution_row('wait_for_breakout', 'Wait For Breakout')}{_execution_row('hold_and_watch', 'Hold And Watch')}</tbody>"
        + "</table></div>"
        + f"<div class='muted' style='margin-top:10px;'>{note}</div>"
        + f"<div class='muted' style='margin-top:8px;'>{html.escape(str(maturity.get('summary') or ''))}</div>"
        + "<div style='display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));margin-top:12px;'>"
        + "<div style='border:1px solid #d9e5df;border-radius:18px;padding:14px;background:rgba(255,255,255,0.68);'>"
        + f"<div class='eyebrow'>{'突破确认主导板块' if lang == 'zh' else 'Breakout sectors'}</div>"
        + _sector_summary("wait_for_breakout")
        + "</div>"
        + "<div style='border:1px solid #d9e5df;border-radius:18px;padding:14px;background:rgba(255,255,255,0.68);'>"
        + f"<div class='eyebrow'>{'观察等待主导板块' if lang == 'zh' else 'Watch sectors'}</div>"
        + _sector_summary("hold_and_watch")
        + "</div>"
        + "</div>"
        + f"<div class='muted' style='margin-top:12px;'>{html.escape(tactical_note)}</div>"
        + f"<div class='muted' style='margin-top:12px;font-weight:700;'>{'结论' if lang == 'zh' else 'Takeaway'}: {html.escape(takeaway)}</div>"
        + "</article>"
    )
    note = (
        f"最近回看 {snapshot_total} 个快照，其中 {labeled_snapshot_total} 个带 BUY / WATCH / HOLD 标签。"
        if lang == "zh"
        else f"Reviewing the latest {snapshot_total} snapshots, with {labeled_snapshot_total} carrying BUY / WATCH / HOLD labels."
    )
    takeaway = technical_momentum_bias(evaluation, lang=lang)
    if int(((windows.get("buy") or {}).get(5) or {}).get("count") or 0) <= 0 and int(((windows.get("watch") or {}).get(5) or {}).get("count") or 0) <= 0:
        tactical_note = (
            "当前还没有成熟 5 日窗口，因此更适合把这块当作观察看板，而不是直接下结论。"
            if lang == "zh"
            else "There are no mature 5-day windows yet, so treat this as an observation panel rather than a verdict."
        )
    else:
        buy_hit = float((((windows.get("buy") or {}).get(5) or {}).get("hit_rate") or 0.0))
        watch_hit = float((((windows.get("watch") or {}).get(5) or {}).get("hit_rate") or 0.0))
        if buy_hit >= watch_hit + 5:
            tactical_note = (
                "近期直接 BUY 的后续命中率更高，说明动量确认后的直接跟随更顺。"
                if lang == "zh"
                else "Direct BUY currently carries the higher 5-day hit rate, suggesting cleaner momentum follow-through."
            )
        elif watch_hit >= buy_hit + 5:
            tactical_note = (
                "近期先 WATCH 再等确认更稳，说明动量信号更适合二次确认。"
                if lang == "zh"
                else "WATCH-first names currently carry the higher 5-day hit rate, suggesting a cleaner confirmation-first approach."
            )
        else:
            tactical_note = (
                "BUY 和 WATCH 目前差距不大，更适合把它们当成两套执行节奏。"
                if lang == "zh"
                else "BUY and WATCH are currently close enough to be treated as two execution tempos rather than one dominant edge."
            )
    return (
        "<article class='card' style='background:#f7faf8;border-color:#dce8e1;'>"
        f"<div class='eyebrow'>{'模型评测' if lang == 'zh' else 'Template Evaluation'}</div>"
        f"<div class='muted' style='margin-bottom:10px;'>{'先用最简单的执行问题来评：动量模板里，直接 BUY 和先 WATCH 哪类后续更稳。' if lang == 'zh' else 'Start with the simplest execution question: inside the momentum template, is direct BUY follow-through steadier than WATCH-first candidates?'}</div>"
        f"<div style='display:inline-flex;align-items:center;padding:8px 12px;border-radius:999px;margin-bottom:12px;{maturity_style}font-weight:800;font-size:12px;'>{html.escape(str(maturity.get('level') or '-'))}</div>"
        + _market_split_html()
        + "<div style='overflow-x:auto;border:1px solid #e2e8f0;border-radius:12px;background:white;'>"
        + "<table style='width:100%;min-width:760px;border-collapse:collapse;font-size:13px;'>"
        + f"<thead><tr><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{'动作' if lang == 'zh' else 'Action'}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>3D {'样本' if lang == 'zh' else 'Samples'}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>3D</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>5D {'样本' if lang == 'zh' else 'Samples'}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>5D</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>10D {'样本' if lang == 'zh' else 'Samples'}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>10D</th></tr></thead>"
        + f"<tbody>{_metric_row('buy', 'BUY')}{_metric_row('watch', 'WATCH')}</tbody>"
        + "</table></div>"
        + f"<div class='muted' style='margin-top:10px;'>{note}</div>"
        + f"<div class='muted' style='margin-top:8px;'>{html.escape(str(maturity.get('summary') or ''))}</div>"
        + "<div style='display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));margin-top:12px;'>"
        + "<div style='border:1px solid #d9e5df;border-radius:18px;padding:14px;background:rgba(255,255,255,0.68);'>"
        + f"<div class='eyebrow'>BUY {'主导板块' if lang == 'zh' else 'Dominant sectors'}</div>"
        + _sector_summary("buy")
        + "</div>"
        + "<div style='border:1px solid #d9e5df;border-radius:18px;padding:14px;background:rgba(255,255,255,0.68);'>"
        + f"<div class='eyebrow'>WATCH {'主导板块' if lang == 'zh' else 'Dominant sectors'}</div>"
        + _sector_summary("watch")
        + "</div>"
        + "</div>"
        + f"<div class='muted' style='margin-top:12px;'>{html.escape(tactical_note)}</div>"
        + f"<div class='muted' style='margin-top:12px;font-weight:700;'>{'结论' if lang == 'zh' else 'Takeaway'}: {html.escape(takeaway)}</div>"
        + "</article>"
    )


def _lightgbm_history_bias(payload: dict, *, lang: str) -> str:
    windows = (payload or {}).get("windows") or {}
    pullback_1d = (windows.get("pullback") or {}).get(1) or {}
    breakout_1d = (windows.get("breakout") or {}).get(1) or {}
    watch_1d = (windows.get("watch") or {}).get(1) or {}
    ranked = sorted(
        [
            (int(pullback_1d.get("count") or 0), float(pullback_1d.get("hit_rate") or 0.0), "回踩布局" if lang == "zh" else "Pullback"),
            (int(breakout_1d.get("count") or 0), float(breakout_1d.get("hit_rate") or 0.0), "突破确认" if lang == "zh" else "Breakout"),
            (int(watch_1d.get("count") or 0), float(watch_1d.get("hit_rate") or 0.0), "观察等待" if lang == "zh" else "Watch"),
        ],
        key=lambda item: (-item[0], -item[1], item[2]),
    )
    count, hit_rate, label = ranked[0]
    if count <= 0:
        return "历史样本观察中" if lang == "zh" else "Historical samples are still observational"
    if lang == "zh":
        return f"当前次日更偏 {label}，命中率 {_fmt_number(hit_rate, suffix='%', digits=1)}。"
    return f"1D currently leans {label} with a {_fmt_number(hit_rate, suffix='%', digits=1)} hit rate."


def _lightgbm_tactical_guidance(
    *,
    action_key: str,
    market_payload: dict,
    fallback_payload: dict,
    lang: str,
) -> tuple[str, str]:
    payload = market_payload if int((market_payload or {}).get("sample_count") or 0) > 0 else fallback_payload
    sample_count = int((payload or {}).get("sample_count") or 0)
    windows = (payload or {}).get("windows") or {}
    one_day = ((windows.get(action_key) or {}).get(1) or {})
    hit_rate = float(one_day.get("hit_rate") or 0.0)
    bias_text = _lightgbm_history_bias(payload or fallback_payload, lang=lang)
    if sample_count <= 0:
        return (
            ("先观察" if lang == "zh" else "Observe First"),
            ("历史样本仍不足，先把这只票当作观察对象。" if lang == "zh" else "Historical samples are still too thin, so treat this name as watch-only for now."),
        )
    if lang == "zh":
        if action_key == "pullback":
            if "突破确认" in bias_text:
                return ("回踩不抢", f"当前整体更偏突破确认，回踩只做缩量确认；同类 1D 命中率 {hit_rate:.1f}%。")
            return ("回踩确认", f"当前更适合等回踩企稳再处理；同类 1D 命中率 {hit_rate:.1f}%。")
        if action_key == "breakout":
            if "回踩布局" in bias_text:
                return ("突破慎追", f"当前整体更偏回踩布局，突破单需要等放量确认；同类 1D 命中率 {hit_rate:.1f}%。")
            return ("突破跟随", f"当前更适合等放量突破确认；同类 1D 命中率 {hit_rate:.1f}%。")
        return ("先观察", f"当前这类信号更适合作为观察名单；同类 1D 命中率 {hit_rate:.1f}%。")
    if action_key == "pullback":
        if "Breakout" in bias_text:
            return ("Avoid Early Pullback", f"The tape currently leans breakout confirmation, so only buy pullbacks after a cleaner reset. Peer 1D hit rate {hit_rate:.1f}%.")
        return ("Wait For Pullback", f"Lean toward pullback confirmation before acting. Peer 1D hit rate {hit_rate:.1f}%.")
    if action_key == "breakout":
        if "Pullback" in bias_text:
            return ("Breakout Needs Proof", f"The tape leans pullbacks, so only chase breakouts after real volume confirmation. Peer 1D hit rate {hit_rate:.1f}%.")
        return ("Follow Breakout", f"Lean toward confirmed breakouts with volume. Peer 1D hit rate {hit_rate:.1f}%.")
    return ("Observe First", f"This setup still behaves best as a watchlist candidate. Peer 1D hit rate {hit_rate:.1f}%.")


def _lightgbm_execution_bias_bar(*, market: str, lang: str) -> str:
    evaluation = build_lightgbm_prediction_evaluation(market=market, recent_runs=8, top_n=40)
    windows = evaluation.get("windows") or {}
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
    if lead_count <= 0:
        title = "今日执行偏向：先观察" if lang == "zh" else "Today’s execution bias: Observe"
        body = (
            "当前还没有足够成熟的 1D 样本，先把 LightGBM 当作观察面板。"
            if lang == "zh"
            else "There are not enough mature 1D samples yet, so use LightGBM as an observation panel first."
        )
        tone = "background:#f8fafc;border-color:#dbe4ee;color:#334155;"
    elif lead_key == "breakout":
        title = "今日执行偏向：突破确认" if lang == "zh" else "Today’s execution bias: Breakout Confirmation"
        body = (
            f"当前次日更偏突破确认，优先处理放量突破的名字；同类 1D 命中率 {lead_hit:.1f}%。"
            if lang == "zh"
            else f"1D currently leans breakout confirmation, so prioritize names with cleaner volume breakouts. Peer 1D hit rate {lead_hit:.1f}%."
        )
        tone = "background:#eff6ff;border-color:#bfdbfe;color:#1d4ed8;"
    elif lead_key == "pullback":
        title = "今日执行偏向：回踩布局" if lang == "zh" else "Today’s execution bias: Pullback Entries"
        body = (
            f"当前次日更偏回踩布局，优先处理回踩企稳的名字；同类 1D 命中率 {lead_hit:.1f}%。"
            if lang == "zh"
            else f"1D currently leans pullback entries, so prioritize names resetting into support. Peer 1D hit rate {lead_hit:.1f}%."
        )
        tone = "background:#ecfdf5;border-color:#a7f3d0;color:#047857;"
    else:
        title = "今日执行偏向：先观察" if lang == "zh" else "Today’s execution bias: Observe"
        body = (
            f"当前 Watch 信号更占优，适合把 LightGBM 当成观察名单；同类 1D 命中率 {lead_hit:.1f}%。"
            if lang == "zh"
            else f"Watch signals currently lead, so treat LightGBM as a monitored watchlist first. Peer 1D hit rate {lead_hit:.1f}%."
        )
        tone = "background:#fff7ed;border-color:#fed7aa;color:#c2410c;"
    return (
        f"<article class='card' style='{tone}'>"
        f"<div style='font-size:12px;font-weight:800;letter-spacing:0.06em;text-transform:uppercase;margin-bottom:6px;'>{'今日执行偏向' if lang == 'zh' else 'Today Execution Bias'}</div>"
        f"<div style='font-size:20px;font-weight:800;line-height:1.3;margin-bottom:6px;'>{html.escape(title)}</div>"
        f"<div style='font-size:14px;line-height:1.6;opacity:0.92;'>{html.escape(body)}</div>"
        "</article>"
    )


def _annotate_lightgbm_results(items: list[dict], *, selected_market: str, lang: str, force_apply: bool = False) -> None:
    if not items:
        return
    history_eval = build_lightgbm_prediction_evaluation(market=selected_market, recent_runs=8, top_n=40)
    per_market = history_eval.get("per_market") or {}
    for item in items:
        matched_templates = [str(value).strip() for value in (item.get("matched_model_templates") or []) if str(value).strip()]
        if not force_apply and "lightgbm_top_picks" not in matched_templates:
            continue
        action_key = normalize_lightgbm_prediction_action(
            entry_style=item.get("model_entry_style"),
            signal_label=item.get("model_signal_label"),
        )
        if not action_key:
            action_key = normalize_lightgbm_action(item.get("action_label"))
        if action_key not in {"pullback", "breakout", "watch"}:
            continue
        market_code = str(item.get("market") or selected_market or "CN").upper()
        market_payload = per_market.get(market_code) or {}
        tactical_tag, tactical_note = _lightgbm_tactical_guidance(
            action_key=action_key,
            market_payload=market_payload,
            fallback_payload=history_eval,
            lang=lang,
        )
        item["lightgbm_tactical_action"] = action_key
        item["lightgbm_tactical_tag"] = tactical_tag
        item["lightgbm_tactical_note"] = tactical_note
        highlights = [str(value).strip() for value in (item.get("model_highlights") or []) if str(value).strip()]
        if tactical_note and tactical_note not in highlights:
            item["model_highlights"] = [tactical_note] + highlights


def _kronos_sort_value(item: dict) -> float:
    validation = item.get("kronos_validation") if isinstance(item.get("kronos_validation"), dict) else {}
    try:
        score = float((validation or {}).get("kronos_score"))
    except (TypeError, ValueError):
        score = -1.0
    decision = str((validation or {}).get("kronos_decision") or "").lower()
    support_bonus = 1000.0 if ("支持" in decision or "support" in decision) and "不支持" not in decision else 0.0
    ready_bonus = 100.0 if str((validation or {}).get("kronos_status") or "").upper() == "READY" else 0.0
    return support_bonus + ready_bonus + score


def _kronos_compact_chip(item: dict, lang: str) -> str:
    validation = item.get("kronos_validation") if isinstance(item.get("kronos_validation"), dict) else {}
    if not validation:
        return ""
    decision = str(validation.get("kronos_decision") or "-")
    status = str(validation.get("kronos_status") or "").upper()
    score = validation.get("kronos_score")
    is_support = ("支持" in decision or "support" in decision.lower()) and "不支持" not in decision
    is_reject = "不支持" in decision or "avoid" in decision.lower()
    bg = "#dcfce7" if is_support else "#fee2e2" if is_reject else "#fef9c3"
    fg = "#166534" if is_support else "#991b1b" if is_reject else "#854d0e"
    try:
        score_text = f"{float(score):.1f}"
    except (TypeError, ValueError):
        score_text = "-"
    label = "Kronos" if lang == "zh" else "Kronos"
    return (
        f"<span style='display:inline-flex;align-items:center;padding:5px 10px;border-radius:999px;"
        f"background:{bg};color:{fg};font-weight:900;font-size:12px;'>"
        f"{label} {html.escape(score_text)} · {html.escape(decision if status == 'READY' else status or decision)}</span>"
    )


def _lightgbm_confluence_fit_score(item: dict, *, confluence_action_filter: str) -> int:
    action_key = str(item.get("lightgbm_tactical_action") or "").strip().lower()
    if not action_key:
        return 0
    normalized_filter = _normalize_action_filter(confluence_action_filter)
    if normalized_filter == "buy_the_dip":
        return 3 if action_key == "pullback" else 1 if action_key == "breakout" else 0
    if normalized_filter == "breakout_confirmation":
        return 3 if action_key == "breakout" else 1 if action_key == "pullback" else 0
    if normalized_filter == "watchlist":
        return 3 if action_key == "watch" else 0
    if normalized_filter == "bullish_entry":
        return 3 if action_key in {"pullback", "breakout"} else 1 if action_key == "watch" else 0
    if action_key in {"breakout", "pullback"}:
        return 2
    if action_key == "watch":
        return 1
    return 0


def _rerank_with_lightgbm_tactical_signal(results: list[dict], *, confluence_action_filter: str) -> list[dict]:
    return sorted(
        results,
        key=lambda row: (
            int(_lightgbm_confluence_fit_score(row, confluence_action_filter=confluence_action_filter)),
            int(row.get("model_hit_count") or 0),
            int(row.get("confluence_alignment_count") or 0),
            float(row.get("snapshot_score") or 0.0),
            float(row.get("trend_score") or 0.0),
            str(row.get("ticker") or ""),
        ),
        reverse=True,
    )


def _apply_quality_confluence_profile(results: list[dict], *, profile: str) -> list[dict]:
    if profile != "quality_confluence_v1":
        return results
    blocked_tags = {"chase-risk", "weak-market", "weak-breadth", "do-not-chase", "missing-latest-price"}
    approved: list[dict] = []
    for row in results:
        tags = {str(item).strip().lower() for item in (row.get("risk_flags") or row.get("model_execution_tags") or []) if str(item).strip()}
        ready = str(row.get("tradability_status") or "").upper() == "READY"
        confluence = int(row.get("model_hit_count") or 0) >= 2
        readiness = float(row.get("trade_readiness_score") or 0.0)
        if ready and confluence and readiness >= 72.0 and not tags.intersection(blocked_tags):
            row["strategy_tier"] = "A"
            row["strategy_gate_reason"] = "双模型共振、市场状态与可成交性均通过"
            approved.append(row)
    return sorted(
        approved,
        key=lambda row: (float(row.get("trade_readiness_score") or 0.0), int(row.get("model_hit_count") or 0)),
        reverse=True,
    )


def _lightgbm_evaluation_card(*, market: str, lang: str) -> str:
    snapshot_eval = build_lightgbm_evaluation(market=market, lookback_snapshots=15, top_n=40)
    history_eval = build_lightgbm_prediction_evaluation(market=market, recent_runs=8, top_n=40)
    maturity = lightgbm_maturity(snapshot_eval, lang=lang)
    per_market = history_eval.get("per_market") or {}
    windows = history_eval.get("windows") or {}
    sample_count = int(history_eval.get("sample_count") or 0)
    run_count = int(history_eval.get("run_count") or 0)
    latest_trade_date = str(history_eval.get("latest_trade_date") or "")

    def _metric_row(action_key: str, label: str) -> str:
        payload = windows.get(action_key) or {}
        return (
            "<tr>"
            f"<td>{label}</td>"
            f"<td>{int((payload.get(1) or {}).get('count') or 0)}</td>"
            f"<td>{_fmt_number((payload.get(1) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_number((payload.get(1) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{int((payload.get(3) or {}).get('count') or 0)}</td>"
            f"<td>{_fmt_number((payload.get(3) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_number((payload.get(3) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            f"<td>{int((payload.get(5) or {}).get('count') or 0)}</td>"
            f"<td>{_fmt_number((payload.get(5) or {}).get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_number((payload.get(5) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
            "</tr>"
        )

    def _market_split_html() -> str:
        market_codes = [code for code in ("CN", "US") if code in per_market]
        if len(market_codes) <= 1:
            return ""
        return (
            "<div style='display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));margin-bottom:12px;'>"
            + "".join(
                (
                    "<div style='border:1px solid #d9e5df;border-radius:18px;padding:14px;background:rgba(255,255,255,0.68);'>"
                    f"<div style='font-size:16px;font-weight:800;color:#0f172a;margin-bottom:6px;'>{'A股' if code == 'CN' and lang == 'zh' else '美股' if code == 'US' and lang == 'zh' else code}</div>"
                    f"<div class='muted'>{'历史样本' if lang == 'zh' else 'Historical samples'} {int((per_market.get(code) or {}).get('sample_count') or 0)}</div>"
                    f"<div class='muted' style='margin-top:6px;'>{'当前偏向' if lang == 'zh' else 'Current bias'}: {html.escape(_lightgbm_history_bias(per_market.get(code) or {}, lang=lang))}</div>"
                    "</div>"
                )
                for code in market_codes
            )
            + "</div>"
        )

    def _short_card(window: int, title: str) -> str:
        ranked = []
        for action_key, action_label in (("pullback", "Pullback"), ("breakout", "Breakout"), ("watch", "Watch")):
            stats = (windows.get(action_key) or {}).get(window) or {}
            ranked.append((int(stats.get("count") or 0), float(stats.get("hit_rate") or 0.0), float(stats.get("avg_return") or 0.0), action_label))
        ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
        count, hit_rate, avg_return, action_label = ranked[0]
        if count <= 0:
            summary = "暂无成熟样本" if lang == "zh" else "No mature samples"
            detail = "继续累积历史预测。" if lang == "zh" else "Keep accumulating historical predictions."
        else:
            summary = f"{action_label} 当前更占优" if lang == "zh" else f"{action_label} currently leads"
            detail = (
                f"命中率 {_fmt_number(hit_rate, suffix='%', digits=1)} · 平均收益 {_fmt_number(avg_return, suffix='%', digits=2)}"
                if lang == "zh"
                else f"Hit rate {_fmt_number(hit_rate, suffix='%', digits=1)} · Avg {_fmt_number(avg_return, suffix='%', digits=2)}"
            )
        return (
            "<div style='border:1px solid #d9e5df;border-radius:18px;padding:14px;background:rgba(255,255,255,0.68);'>"
            f"<div class='eyebrow'>{title}</div>"
            f"<div style='font-size:18px;font-weight:800;color:#0f172a;margin-bottom:8px;'>{html.escape(summary)}</div>"
            f"<div class='muted'>{html.escape(detail)}</div>"
            f"<div class='muted' style='margin-top:8px;'>{'样本' if lang == 'zh' else 'Samples'} {count}</div>"
            "</div>"
        )

    maturity_style = (
        "background:#dcfce7;color:#166534;"
        if str(maturity.get("tone")) == "good"
        else "background:#fef3c7;color:#92400e;"
        if str(maturity.get("tone")) == "mid"
        else "background:#e5eef7;color:#37516b;"
    )
    note = (
        f"直接回看 {run_count} 个成功 LightGBM run，累计历史样本 {sample_count} 条；最新交易日 {latest_trade_date or '-'}。"
        if lang == "zh"
        else f"Directly reviewing {run_count} successful LightGBM runs with {sample_count} historical samples; latest trade date {latest_trade_date or '-'}."
    )
    return (
        "<article class='card' style='background:#f7faf8;border-color:#dce8e1;'>"
        f"<div class='eyebrow'>{'模型评测' if lang == 'zh' else 'Template Evaluation'}</div>"
        f"<div class='muted' style='margin-bottom:10px;'>{'这块直接看历史 LightGBM predictions/model_runs 的 1D / 3D / 5D 验证，更贴近第二天操作。' if lang == 'zh' else 'This block validates historical LightGBM predictions/model_runs over 1D / 3D / 5D horizons for a closer next-session read.'}</div>"
        f"<div style='display:inline-flex;align-items:center;padding:8px 12px;border-radius:999px;margin-bottom:12px;{maturity_style}font-weight:800;font-size:12px;'>{html.escape(str(maturity.get('level') or '-'))}</div>"
        + _market_split_html()
        + "<div style='display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));margin-bottom:12px;'>"
        + _short_card(1, "次日 / 1D" if lang == "zh" else "Next Day / 1D")
        + _short_card(3, "3日 / 3D" if lang == "zh" else "3 Day / 3D")
        + _short_card(5, "5日 / 5D" if lang == "zh" else "5 Day / 5D")
        + "</div>"
        + "<div style='overflow-x:auto;border:1px solid #e2e8f0;border-radius:12px;background:white;'>"
        + "<table style='width:100%;min-width:760px;border-collapse:collapse;font-size:13px;'>"
        + f"<thead><tr><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{'动作' if lang == 'zh' else 'Action'}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>1D {'样本' if lang == 'zh' else 'Samples'}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>1D</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>3D {'样本' if lang == 'zh' else 'Samples'}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>3D</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>5D {'样本' if lang == 'zh' else 'Samples'}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>5D</th></tr></thead>"
        + f"<tbody>{_metric_row('pullback', 'Pullback')}{_metric_row('breakout', 'Breakout')}{_metric_row('watch', 'Watch')}</tbody>"
        + "</table></div>"
        + f"<div class='muted' style='margin-top:10px;'>{note}</div>"
        + f"<div class='muted' style='margin-top:12px;font-weight:700;'>{'结论' if lang == 'zh' else 'Takeaway'}: {html.escape(_lightgbm_history_bias(history_eval, lang=lang))}</div>"
        + "</article>"
    )


def _tradingview_rating_cell(ratings: dict | None, lang: str) -> str:
    if not ratings:
        return "-"
    labels = {"1d": "1D", "1w": "1W", "1M": "1M"}
    chips: list[str] = []
    for interval in ("1d", "1w", "1M"):
        payload = ratings.get(interval) or {}
        recommendation = str(payload.get("recommendation") or "-").upper()
        bg = "#f3f4f6"
        fg = "#374151"
        if recommendation in {"BUY", "STRONG_BUY"}:
            bg, fg = "#dcfce7", "#166534"
        elif recommendation in {"SELL", "STRONG_SELL"}:
            bg, fg = "#fee2e2", "#991b1b"
        elif recommendation == "NEUTRAL":
            bg, fg = "#fef3c7", "#92400e"
        chips.append(
            "<span style='display:inline-flex;align-items:center;gap:6px;padding:6px 10px;"
            f"border-radius:999px;background:{bg};color:{fg};font-weight:700;font-size:12px;'>"
            f"{labels[interval]} {recommendation}"
            "</span>"
        )
    return "<div class='detail-chip-row'>" + "".join(chips) + "</div>"


def _pattern_hits_inline(patterns: list[str] | None) -> str:
    values = [str(item).strip() for item in (patterns or []) if str(item).strip()]
    if not values:
        return "-"
    return " / ".join(values[:3])


def _snapshot_score_badge(value: float | int | None) -> str:
    if value is None:
        return "-"
    try:
        numeric = int(round(float(value)))
    except (TypeError, ValueError):
        return str(value)
    bg = "#eef2ff"
    fg = "#3730a3"
    if numeric >= 85:
        bg, fg = "#dcfce7", "#166534"
    elif numeric >= 70:
        bg, fg = "#dbeafe", "#1d4ed8"
    elif numeric >= 55:
        bg, fg = "#fef3c7", "#92400e"
    return (
        "<span style='display:inline-flex;align-items:center;padding:5px 10px;border-radius:999px;"
        f"background:{bg};color:{fg};font-weight:800;font-size:12px;'>{numeric}</span>"
    )


def _signal_chip(label: str, value: str) -> str:
    normalized = str(value or "-").replace("_", " ").upper()
    bg = "#f3f4f6"
    fg = "#374151"
    if "RISK ON" in normalized or "BUY" in normalized or "BULLISH" in normalized:
        bg, fg = "#dcfce7", "#166534"
    elif "RISK OFF" in normalized or "SELL" in normalized or "BEARISH" in normalized:
        bg, fg = "#fee2e2", "#991b1b"
    elif "NEUTRAL" in normalized or "MIXED" in normalized:
        bg, fg = "#fef3c7", "#92400e"
    return (
        "<span style='display:inline-flex;align-items:center;gap:6px;padding:6px 10px;border-radius:999px;"
        f"background:{bg};color:{fg};font-weight:700;font-size:12px;'>{label} {normalized}</span>"
    )


def _score_breakdown_inline(parts: list[str] | None) -> str:
    values = [str(item).strip() for item in (parts or []) if str(item).strip()]
    if not values:
        return "-"
    return "<div class='detail-chip-row' style='margin-top:0;'>" + "".join(_highlight_chip(item) for item in values[:4]) + "</div>"


def _mode_switch_html(base_path: str, current_mode: str, lang: str, extra_params: dict | None = None) -> str:
    options = [
        ("premarket", _lang_text(lang, "mode_premarket")),
        ("monitor", _lang_text(lang, "mode_monitor")),
        ("postmarket", _lang_text(lang, "mode_postmarket")),
    ]
    base_params = dict(extra_params or {})
    chips: list[str] = []
    for value, label in options:
        active = value == current_mode
        style = (
            "background:#0f766e;color:#fff;border-color:#0f766e;"
            if active
            else "background:#fffdf7;color:#0f766e;border-color:#cde9e4;"
        )
        params = {**base_params, "lang": lang, "mode": value}
        chips.append(
            f"<a href='{base_path}?{urlencode(params)}' "
            "style='display:inline-flex;align-items:center;padding:8px 12px;border-radius:999px;"
            f"border:1px solid;{style}text-decoration:none;font-weight:800;font-size:12px;'>{label}</a>"
        )
    return "<div style='display:flex;gap:8px;flex-wrap:wrap;'>" + "".join(chips) + "</div>"


def _market_snapshot_history(
    db: Session,
    *,
    snapshot_type: str,
    limit: int = 6,
) -> dict[str, list[int]]:
    snapshots = WorkspaceSnapshotRepository(db).list_snapshots(snapshot_type, limit=max(limit * 3, limit))
    points: list[dict[str, int]] = []
    for snapshot in reversed(snapshots):
        payload = snapshot.get("payload") or {}
        boards = payload.get("boards") or []
        if not isinstance(boards, list) or not boards:
            continue
        point: dict[str, int] = {}
        for board in boards:
            market = str(board.get("market") or "").upper()
            if not market:
                continue
            point[market] = point.get(market, 0) + len(board.get("rows") or [])
        if point:
            points.append(point)
    points = points[-limit:]
    return {
        market: [int(point.get(market, 0)) for point in points]
        for market in ("CN", "US")
    }


def _mini_trend_bars(values: list[int], *, lang: str) -> str:
    normalized = [max(0, int(value)) for value in values]
    if not normalized:
        label = "暂无趋势" if lang == "zh" else "No trend"
        return f"<div class='mini-trend empty'><span>{label}</span></div>"
    top = max(normalized) or 1
    bars = "".join(
        f"<span style='height:{max(16, int((value / top) * 100))}%;'></span>"
        for value in normalized
    )
    return f"<div class='mini-trend'>{bars}</div>"


def _market_snapshot_table(rows: list[dict], watchlist_map: dict[str, dict], lang: str, current_params: dict | None = None) -> str:
    if not rows:
        return f"<div class='muted'>{_lang_text(lang, 'snapshot_empty')}</div>"
    params = dict(current_params or {})
    body_rows: list[str] = []
    for item in rows:
        ticker = str(item.get("ticker") or "").upper()
        existing = watchlist_map.get(ticker)
        body_rows.append(
            "<tr>"
            f"<td><a class='main-open-link' href='/insights/{ticker}?lang={lang}'>{ticker}</a></td>"
            f"<td>{item.get('name') or ticker}</td>"
            f"<td>{_snapshot_score_badge(item.get('snapshot_score'))}</td>"
            f"<td>{_trend_badge(item.get('trend_score'))}</td>"
            f"<td>{_change_chip(item.get('momentum_5'))}</td>"
            f"<td>{_number_badge(item.get('volume_ratio'))}</td>"
            f"<td>{_pattern_hits_inline(item.get('matched_patterns'))}</td>"
            f"<td>{_score_breakdown_inline(item.get('snapshot_score_breakdown'))}</td>"
            f"<td>{_tradingview_rating_cell(item.get('tradingview_ratings'), lang)}</td>"
            f"<td>{_watchlist_summary(existing, lang) if existing else '-'}</td>"
            "<td>"
            "<form method='post' action='/screeners/market-snapshot/add-to-focus' style='margin:0;'>"
            f"<input type='hidden' name='lang' value='{lang}' />"
            f"<input type='hidden' name='mode' value='{html.escape(str(params.get('mode') or 'monitor'), quote=True)}' />"
            f"<input type='hidden' name='market_filter' value='{html.escape(str(params.get('market_filter') or 'CN'), quote=True)}' />"
            f"<input type='hidden' name='ticker' value='{ticker}' />"
            f"<input type='hidden' name='name' value='{item.get('name') or ticker}' />"
            f"<input type='hidden' name='market' value='{item.get('market') or 'CN'}' />"
            f"<input type='hidden' name='selection_reason' value='{item.get('selection_reason') or ''}' />"
            f"<input type='hidden' name='matched_patterns' value='{_pattern_hits_inline(item.get('matched_patterns'))}' />"
            f"<button type='submit'>{_lang_text(lang, 'add_to_today_focus')}</button>"
            "</form>"
            "</td>"
            "</tr>"
        )
    return (
        "<div class='table-wrap'>"
        "<table>"
        "<thead>"
        "<tr>"
        f"<th>{_lang_text(lang, 'ticker')}</th>"
        f"<th>{_lang_text(lang, 'name')}</th>"
        f"<th>{_lang_text(lang, 'snapshot_score')}</th>"
        f"<th>{_lang_text(lang, 'trend')}</th>"
        "<th>5D %</th>"
        "<th>Volume</th>"
        f"<th>{_lang_text(lang, 'pattern_hits')}</th>"
        f"<th>{_lang_text(lang, 'score_breakdown')}</th>"
        f"<th>{_lang_text(lang, 'technical_rating')}</th>"
        f"<th>{_lang_text(lang, 'watchlist')}</th>"
        f"<th>{_lang_text(lang, 'today_focus_pool')}</th>"
        "</tr>"
        "</thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table>"
        "</div>"
    )


def _detail_panel(item: dict, watchlist_map: dict[str, dict], current_params: dict, lang: str) -> str:
    details_label = "Details" if lang == "en" else "展开"
    collapse_label = "Collapse" if lang == "en" else "收起"
    model_highlights = item.get("model_highlights") or []
    model_execution_tags = list(item.get("model_execution_tags") or [])
    lightgbm_tactical_note = str(item.get("lightgbm_tactical_note") or "").strip()
    action_badge = _action_badge(item.get("action_label"), lang)
    trend_badge = _trend_badge(item.get("trend_score"))
    existing = watchlist_map.get(item["ticker"])
    sync_badge = _sync_status_badge(existing, lang)
    model_highlights_html = (
        "<div class='detail-chip-row'>"
        + "".join(
            _highlight_chip(highlight)
            for highlight in model_highlights
        )
        + "</div>"
        if model_highlights
        else f"<div style='margin-top:8px;color:#6b7280;'>-</div>"
    )
    execution_tags_html = (
        "<div class='detail-chip-row' style='margin-top:10px;'>"
        + "".join(_execution_tag_chip(tag) for tag in model_execution_tags)
        + "</div>"
        if model_execution_tags
        else ""
    )
    pseudo_strength_html = _pseudo_strong_signal_html(item, lang)
    tactical_note_html = (
        f"<div style='margin-top:10px;font-size:13px;color:#334155;line-height:1.55;'><strong>{'模型下一步' if lang == 'zh' else 'Model next step'}:</strong> {html.escape(lightgbm_tactical_note)}</div>"
        if lightgbm_tactical_note
        else ""
    )
    why_selected_html = _why_selected_cell(item.get("selection_reason"), lang)
    tradingview_html = _tradingview_rating_cell(item.get("tradingview_ratings"), lang)
    watchlist_html = _watchlist_action_cell(item, watchlist_map, current_params, lang)
    watchlist_summary_html = _watchlist_summary(existing, lang)
    last_sync = existing.get("last_synced_date") if existing else None
    return (
        "<details class='row-detail-toggle'>"
        "<summary>"
        f"<span>{details_label}</span>"
        "<span class='detail-summary-meta'>"
        f"<span>{trend_badge}</span>"
        f"<span>{action_badge}</span>"
        f"<span>{sync_badge}</span>"
        f"<span class='detail-summary-price'>{item.get('latest_close') or '-'}</span>"
        "</span>"
        "</summary>"
        "<div class='detail-grid'>"
        "<div class='detail-card'>"
        f"<div class='detail-label'>{_lang_text(lang, 'why_selected')}</div>"
        f"{why_selected_html}"
        f"{pseudo_strength_html}"
        "</div>"
        "<div class='detail-card'>"
        f"<div class='detail-label'>{_lang_text(lang, 'technical_rating')}</div>"
        f"{tradingview_html}"
        "</div>"
        "<div class='detail-card'>"
        f"<div class='detail-label'>{_lang_text(lang, 'watchlist')}</div>"
        f"{watchlist_summary_html}"
        "</div>"
        "<div class='detail-card'>"
        f"<div class='detail-label'>{_lang_text(lang, 'last_sync')}</div>"
        f"<div class='detail-value'>{last_sync or '-'}</div>"
        f"<div style='margin-top:8px;'>{sync_badge}</div>"
        "</div>"
        "<div class='detail-card detail-card-action'>"
        f"<div class='detail-label'>{_lang_text(lang, 'insight')}</div>"
        "<div class='detail-action-stack'>"
        f"<div class='detail-value'><a class='detail-link' href='/insights/{item['ticker']}?lang={lang}'>{_lang_text(lang, 'open_insight')}</a></div>"
        f"{watchlist_html}"
        "</div>"
        "</div>"
        "<div class='detail-card detail-card-wide'>"
        f"<div class='detail-label'>{_lang_text(lang, 'model')}</div>"
        f"<div class='detail-value'>{item.get('model_summary') or '-'}</div>"
        f"{tactical_note_html}"
        f"{model_highlights_html}"
        f"{execution_tags_html}"
        "</div>"
        "</div>"
        f"<div class='detail-collapse-note'>{collapse_label}</div>"
        "</details>"
    )


def _model_cell(item: dict, lang: str) -> str:
    summary = item.get("model_summary")
    highlights = item.get("model_highlights") or []
    kronos_validation = item.get("kronos_validation") if isinstance(item.get("kronos_validation"), dict) else {}
    lightgbm_tactical_tag = str(item.get("lightgbm_tactical_tag") or "").strip()
    raw_state = item.get("model_state")
    if isinstance(raw_state, dict):
        state = raw_state
    elif isinstance(raw_state, str) and raw_state.strip():
        state = {
            "key": raw_state.strip().lower().replace(" ", "_"),
            "label": raw_state.strip().replace("_", " ").title(),
        }
    else:
        state = {}
    confidence = item.get("model_confidence")
    signal_label = item.get("model_signal_label")
    signal_strength = item.get("model_signal_strength")
    model_percentile = item.get("model_percentile")
    model_horizon_days = item.get("model_horizon_days")
    model_reward_risk_ratio = item.get("model_reward_risk_ratio")
    model_expected_drawdown_20d = item.get("model_expected_drawdown_20d")
    readiness_score = item.get("trade_readiness_score")
    readiness_bucket = str(item.get("readiness_bucket") or "").upper()
    readiness_reason = item.get("readiness_reason")
    block_reason = item.get("block_reason")
    tradability_status = item.get("tradability_status")
    model_conviction_bucket = item.get("model_conviction_bucket")
    model_position_size_hint = item.get("model_position_size_hint")
    model_entry_style = item.get("model_entry_style")
    model_execution_tags = list(item.get("model_execution_tags") or [])
    model_hit_count = item.get("model_hit_count")
    confluence_alignment_count = item.get("confluence_alignment_count")
    matched_action_buckets = list(item.get("matched_action_buckets") or [])
    if not summary and not highlights and not state and not lightgbm_tactical_tag and readiness_score is None and not kronos_validation:
        return "-"
    display_summary = summary
    if lightgbm_tactical_tag:
        display_summary = f"{summary} · {lightgbm_tactical_tag}" if summary else lightgbm_tactical_tag
    bg = state.get("bg", "#f3f4f6")
    fg = state.get("fg", "#374151")
    badge_text = state.get("label", ("Neutral" if lang == "en" else "中性"))
    compact = highlights[0] if highlights else (_lang_text(lang, "drag_hint") if False else "")
    details_label = "Details" if lang == "en" else "展开"
    detail_rows = "".join(
        f"<li style='margin:4px 0;color:#4b5563;line-height:1.45;white-space:normal;'>{highlight}</li>"
        for highlight in highlights
    )
    confidence_html = (
        f"<div style='margin-top:6px;font-size:12px;color:#6b7280;'>{'Confidence' if lang == 'en' else '置信度'}: {confidence}%</div>"
        if confidence is not None
        else ""
    )
    meta_bits = []
    if model_hit_count is not None:
        meta_bits.append(f"{int(model_hit_count)} {'模型共振' if lang == 'zh' else 'model hits'}")
    if confluence_alignment_count is not None and int(confluence_alignment_count or 0) > 0:
        meta_bits.append(f"{int(confluence_alignment_count)} {'动作一致' if lang == 'zh' else 'aligned'}")
    if model_percentile is not None:
        meta_bits.append(f"{'Pct' if lang == 'en' else '分位'} {float(model_percentile):.1f}%")
    if model_horizon_days is not None:
        meta_bits.append(f"{'Horizon' if lang == 'en' else '周期'} {int(model_horizon_days)}d")
    if model_reward_risk_ratio is not None:
        meta_bits.append(f"{'R/R' if lang == 'en' else '盈亏比'} {float(model_reward_risk_ratio):.2f}")
    if model_expected_drawdown_20d is not None:
        meta_bits.append(f"{'DD20' if lang == 'en' else '20日回撤'} {float(model_expected_drawdown_20d):.1f}%")
    if model_conviction_bucket:
        meta_bits.append(model_conviction_bucket)
    if model_position_size_hint:
        meta_bits.append(model_position_size_hint)
    if model_entry_style:
        meta_bits.append(model_entry_style)
    if lightgbm_tactical_tag:
        meta_bits.append(lightgbm_tactical_tag)
    if model_execution_tags:
        meta_bits.extend(model_execution_tags[:2])
    friendly_explain = build_trade_explain_text(item, lang=lang)
    if friendly_explain and friendly_explain != "-":
        meta_bits.append(friendly_explain)
    if matched_action_buckets:
        meta_bits.append(("动作桶 " if lang == "zh" else "Buckets ") + "/".join(matched_action_buckets[:2]))
    meta_html = (
        f"<div style='margin-top:6px;font-size:12px;color:#6b7280;'>{' · '.join(meta_bits)}</div>"
        if meta_bits
        else ""
    )
    signal_html = (
        f"<div style='margin-top:6px;font-size:12px;color:#6b7280;'>{signal_label or ('Hold' if lang == 'en' else '持有')}"
        f"{' · ' + str(int(signal_strength)) if signal_strength is not None else ''}</div>"
    )
    readiness_palette = {
        "HIGH": ("#dcfce7", "#166534", "高" if lang == "zh" else "High"),
        "MEDIUM": ("#fef9c3", "#854d0e", "中" if lang == "zh" else "Medium"),
        "LOW": ("#ffedd5", "#9a3412", "低" if lang == "zh" else "Low"),
        "BLOCKED": ("#fee2e2", "#991b1b", "阻断" if lang == "zh" else "Blocked"),
    }
    readiness_bg, readiness_fg, readiness_label = readiness_palette.get(
        readiness_bucket,
        ("#e5e7eb", "#374151", readiness_bucket or ("待确认" if lang == "zh" else "Review")),
    )
    readiness_html = (
        f"<div style='margin-top:6px;display:inline-flex;align-items:center;gap:6px;padding:4px 8px;border-radius:999px;background:{readiness_bg};color:{readiness_fg};font-size:12px;font-weight:800;'>"
        f"{'就绪度' if lang == 'zh' else 'Readiness'} {float(readiness_score):.1f} · {html.escape(readiness_label)}</div>"
        if readiness_score is not None
        else ""
    )
    kronos_html = ""
    if kronos_validation:
        kronos_status = str(kronos_validation.get("kronos_status") or "-").upper()
        kronos_decision = str(kronos_validation.get("kronos_decision") or "-")
        kronos_reason = str(kronos_validation.get("kronos_reason") or "-")
        kronos_score = kronos_validation.get("kronos_score")
        expected_3d = kronos_validation.get("kronos_expected_return_3d_pct")
        drawdown = kronos_validation.get("kronos_max_drawdown_pct")
        is_support = ("支持" in kronos_decision or "support" in kronos_decision.lower()) and "不支持" not in kronos_decision
        is_reject = "不支持" in kronos_decision or "avoid" in kronos_decision.lower()
        kronos_bg = "#dcfce7" if is_support else "#fee2e2" if is_reject else "#fef9c3"
        kronos_fg = "#166534" if is_support else "#991b1b" if is_reject else "#854d0e"
        try:
            score_text = f"{float(kronos_score):.1f}"
        except (TypeError, ValueError):
            score_text = "-"
        extra_bits = []
        try:
            extra_bits.append(f"3D {float(expected_3d):.2f}%")
        except (TypeError, ValueError):
            pass
        try:
            extra_bits.append(f"DD {float(drawdown):.2f}%")
        except (TypeError, ValueError):
            pass
        kronos_html = (
            f"<div style='margin-top:6px;display:flex;flex-wrap:wrap;gap:6px;align-items:center;'>"
            f"<span style='display:inline-flex;align-items:center;gap:5px;padding:4px 8px;border-radius:999px;background:{kronos_bg};color:{kronos_fg};font-size:12px;font-weight:900;'>"
            f"Kronos {html.escape(score_text)} · {html.escape(kronos_decision)}</span>"
            f"</div>"
            f"<div style='margin-top:4px;font-size:12px;color:#6b7280;white-space:normal;'>"
            f"{html.escape(kronos_status)}"
            f"{' · ' + html.escape(' · '.join(extra_bits)) if extra_bits else ''}"
            f" · {html.escape(kronos_reason)}</div>"
        )
    block_html = ""
    if block_reason or str(tradability_status or "").upper() in {"BLOCKED", "DO_NOT_CHASE"}:
        block_tone_bg = "#fee2e2" if str(tradability_status or "").upper() != "DO_NOT_CHASE" else "#ffedd5"
        block_tone_fg = "#991b1b" if str(tradability_status or "").upper() != "DO_NOT_CHASE" else "#9a3412"
        block_html = (
            f"<div style='margin-top:6px;display:inline-flex;align-items:center;gap:6px;padding:4px 8px;border-radius:999px;background:{block_tone_bg};color:{block_tone_fg};font-size:12px;font-weight:800;'>"
            f"{html.escape(format_trade_status(tradability_status, lang=lang))} · {html.escape(format_trade_gate_reason(block_reason or str(tradability_status or '').lower(), lang=lang))}</div>"
        )
    detail_block = (
        "<details style='margin-top:4px;'>"
        f"<summary style='cursor:pointer;color:#6b7280;font-size:12px;font-weight:700;list-style:none;'>{compact or details_label}</summary>"
        f"<ul style='margin:8px 0 0 18px;padding:0;'>{detail_rows}</ul>"
        f"{signal_html}"
        f"{kronos_html}"
        f"{readiness_html}"
        f"{block_html}"
        f"{meta_html}"
        f"{confidence_html}"
        f"<div style='margin-top:6px;font-size:12px;color:#6b7280;'>{details_label}</div>"
        "</details>"
        if highlights
        else signal_html + readiness_html + block_html + meta_html + confidence_html
    )
    if kronos_html and not highlights:
        detail_block = signal_html + kronos_html + readiness_html + block_html + meta_html + confidence_html
    return (
        f"<div style='min-width:180px;white-space:normal;'>"
        f"<div style='display:flex;align-items:center;gap:8px;flex-wrap:wrap;'>"
        f"<span style='display:inline-flex;align-items:center;padding:4px 8px;border-radius:999px;background:{bg};color:{fg};font-weight:800;font-size:12px;'>{badge_text}</span>"
        f"<span style='font-weight:700;color:#0f172a;'>{display_summary or '-'}</span>"
        f"</div>"
        f"{detail_block}"
        "</div>"
    )


def _load_saved_presets(db: Session) -> list[dict]:
    raw = AppSettingRepository(db).get(SCREENERS_PRESETS_KEY)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _save_saved_presets(db: Session, presets: list[dict]) -> None:
    AppSettingRepository(db).set(SCREENERS_PRESETS_KEY, json.dumps(presets, ensure_ascii=False))


def _build_screen_query(params: dict) -> str:
    compact: list[tuple[str, object]] = []
    for key, value in params.items():
        if value in (None, "", "ALL"):
            continue
        if isinstance(value, list):
            compact.extend((key, item) for item in value if item not in (None, "", "ALL"))
            continue
        compact.append((key, value))
    return f"/screeners?{urlencode(compact, doseq=True)}"


def _hidden_fields_html(params: dict) -> str:
    parts: list[str] = []
    for key, value in params.items():
        if value in (None, ""):
            continue
        if isinstance(value, list):
            for item in value:
                if item in (None, ""):
                    continue
                parts.append(f"<input type='hidden' name='{key}' value='{html.escape(str(item))}' />")
            continue
        parts.append(f"<input type='hidden' name='{key}' value='{html.escape(str(value))}' />")
    return "".join(parts)


def _redirect_with_message(message: str, lang: str = "en") -> RedirectResponse:
    return RedirectResponse(url=f"/screeners?{urlencode({'message': message, 'lang': lang})}", status_code=303)


def _banner_html(message: str | None, lang: str) -> str:
    if not message:
        return ""
    actions = ""
    if "watchlist" in message.lower():
        actions = (
            "<div style='display:flex;gap:10px;flex-wrap:wrap;margin-top:10px;'>"
            f"<a href='/dashboard?lang={lang}' style='display:inline-flex;align-items:center;padding:8px 12px;border-radius:999px;"
            "background:#0f172a;color:#dff5ef;border:1px solid #223246;font-weight:700;text-decoration:none;'>"
            f"{'打开 Dashboard' if lang == 'zh' else 'Open Dashboard'}</a>"
            "<a href='/watchlist' style='display:inline-flex;align-items:center;padding:8px 12px;border-radius:999px;"
            f"background:#eef8f5;color:#0f766e;font-weight:700;text-decoration:none;'>{_lang_text(lang, 'open_watchlist')}</a>"
            "<a href='/watchlist' style='display:inline-flex;align-items:center;padding:8px 12px;border-radius:999px;"
            f"background:#fff;color:#0f766e;border:1px solid #bfe6dd;font-weight:700;text-decoration:none;'>{_lang_text(lang, 'review_sync_settings')}</a>"
            "</div>"
        )
    return (
        "<div class='card' style='margin-bottom:16px;color:var(--accent);font-weight:700;'>"
        f"{message}"
        f"{actions}"
        "</div>"
    )


def _preset_display_label(params: dict, lang: str) -> str:
    multi_templates = _normalize_multi_model_templates(params.get("multi_model_templates"))
    if len(multi_templates) >= 2:
        return (
            f"多模型共振 ({len(multi_templates)})"
            if lang == "zh"
            else f"Multi-model Confluence ({len(multi_templates)})"
        )
    template_key = str(params.get("model_template") or "")
    template_config = MODEL_TEMPLATES.get(template_key) or {"label": template_key or "-"}
    return _template_label(template_key, template_config["label"], lang)


def _preset_hidden_fields_html(params: dict) -> str:
    normalized = _normalize_screen_params({**params, "lang": params.get("lang", "en")})
    fields: list[str] = []
    for key, value in normalized.items():
        if isinstance(value, list):
            for item in value:
                fields.append(
                    f"<input type='hidden' name='{html.escape(str(key))}' value='{html.escape(str(item))}' />"
                )
        else:
            fields.append(
                f"<input type='hidden' name='{html.escape(str(key))}' value='{html.escape(str(value))}' />"
            )
    return "".join(fields)


def _preset_summary(params: dict, lang: str = "en") -> str:
    multi_templates = _normalize_multi_model_templates(params.get("multi_model_templates"))
    if len(multi_templates) >= 2:
        selected_labels = [
            _template_label(key, MODEL_TEMPLATES[key]["label"], lang)
            for key in multi_templates
            if key in MODEL_TEMPLATES
        ]
        bucket = _normalize_action_filter(params.get("confluence_action_filter"))
        bucket_label = (
            _confluence_bucket_label(bucket, lang)
            if bucket not in {"", "all"}
            else ("任意动作" if lang == "zh" else "Any action")
        )
        min_hits = max(1, int(float(params.get("min_multi_model_hits", 2) or 2)))
        models_text = " / ".join(selected_labels[:3])
        if len(selected_labels) > 3:
            models_text += f" +{len(selected_labels) - 3}"
        if lang == "zh":
            return f"{bucket_label} · 至少 {min_hits} 模型 · {models_text}"
        return f"{bucket_label} · min {min_hits} models · {models_text}"
    template_key = params.get("model_template", "")
    if template_key == "cn_growth_value":
        return (
            f"PE {params.get('pe_min', 0)}-{params.get('pe_max', 30)}, "
            f"ROE>{params.get('min_roe_avg_3y', 12)}%, "
            f"Profit>{params.get('min_net_profit_yoy', 20)}%"
        )
    if template_key == "cn_high_roe_steady_growth":
        return (
            f"ROE>{params.get('min_roe_avg_3y', 15)}%, "
            f"Revenue>{params.get('min_revenue_yoy', 10)}%, "
            f"Debt<{params.get('max_debt_to_assets', 65)}%"
        )
    if template_key == "cn_low_valuation_high_dividend":
        return (
            f"PE<{params.get('pe_max', 20)}, "
            f"Dividend>{params.get('min_dividend_yield', 3)}%, "
            f"ROE>{params.get('min_roe_avg_3y', 10)}%"
        )
    return (
        f"Trend>{params.get('min_trend_score', 60)}, "
        f"Volume>{params.get('min_volume_ratio', 0)}"
    )


def _template_audience_group(template_key: str, config: dict) -> str:
    market = str(config.get("market") or "ALL").upper()
    mode = str(config.get("mode") or "").strip().lower()
    if market == "CN" or template_key.startswith("cn_"):
        return "cn"
    if mode == "fundamental" and template_key.startswith("global_"):
        return "us_global"
    return "core"


def _template_group_meta(group_key: str, lang: str) -> dict[str, str]:
    if group_key == "cn":
        return {
            "title": "A股专用模型" if lang == "zh" else "A-Share Models",
            "hint": "只用于 A 股筛选，适合涨停、均线、布林带、A股基本面等口径。"
            if lang == "zh"
            else "Use these only for A-shares: limit-up, MA structure, Bollinger, and A-share-specific fundamentals.",
            "badge": "A股专用" if lang == "zh" else "A-Share Only",
        }
    if group_key == "us_global":
        return {
            "title": "美股与全球质量模型" if lang == "zh" else "U.S. & Global Quality Models",
            "hint": "优先用于美股，也可用于港股等全球质量/估值筛选。"
            if lang == "zh"
            else "Primarily for U.S. screening, and also suitable for global quality/value filters.",
            "badge": "美股/全球" if lang == "zh" else "U.S./Global",
        }
    return {
        "title": "跨市场核心模型" if lang == "zh" else "Cross-market Core Models",
        "hint": "A股和美股都可先从这组开始，再叠加各自专用模型做二次过滤。"
        if lang == "zh"
        else "Start here for both A-shares and U.S. names, then add market-specific models for a second pass.",
        "badge": "跨市场核心" if lang == "zh" else "Cross-market Core",
    }


def _ordered_template_groups(selected_market: str) -> list[str]:
    market = str(selected_market or "ALL").upper()
    if market == "US":
        return ["core", "us_global", "cn"]
    if market == "CN":
        return ["core", "cn", "us_global"]
    return ["core", "cn", "us_global"]


def _template_groups_for_display(selected_market: str, lang: str) -> list[tuple[str, dict[str, str], list[tuple[str, dict]]]]:
    grouped: dict[str, list[tuple[str, dict]]] = {"core": [], "cn": [], "us_global": []}
    for key, config in MODEL_TEMPLATES.items():
        grouped[_template_audience_group(key, config)].append((key, config))
    sections: list[tuple[str, dict[str, str], list[tuple[str, dict]]]] = []
    for group_key in _ordered_template_groups(selected_market):
        items = grouped.get(group_key) or []
        if not items:
            continue
        sections.append((group_key, _template_group_meta(group_key, lang), items))
    return sections


def _watchlist_action_cell(item: dict, watchlist_map: dict[str, dict], current_params: dict, lang: str) -> str:
    existing = watchlist_map.get(item["ticker"])
    if existing:
        sync_badge = ""
        if existing.get("sync_enabled"):
            sync_badge = (
                "<span style='display:inline-flex;align-items:center;padding:6px 10px;"
                "border-radius:999px;background:#eef8f5;color:#0f766e;font-weight:700;"
                f"font-size:12px;'>{_lang_text(lang, 'sync_on')}</span>"
            )
        sync_state = _lang_text(lang, "off")
        if existing.get("sync_enabled") and existing.get("sync_status") == "success":
            sync_state = _lang_text(lang, "ready")
        elif existing.get("sync_enabled"):
            sync_state = _lang_text(lang, "waiting")
        hidden_fields = _hidden_fields_html(current_params)
        sync_now = ""
        if existing.get("sync_status") != "success":
            sync_now = (
                "<form method='post' action='/screeners/sync-symbol' style='margin:0;'>"
                f"<input type='hidden' name='ticker' value='{item['ticker']}' />"
                f"<input type='hidden' name='item_id' value='{existing['item_id']}' />"
                f"{hidden_fields}"
                f"<button type='submit'>{_lang_text(lang, 'sync_now')}</button>"
                "</form>"
            )
        return (
            "<div style='display:flex;flex-wrap:wrap;gap:6px;'>"
            "<span style='display:inline-flex;align-items:center;padding:6px 10px;"
            "border-radius:999px;background:#dff5ef;color:#0f766e;font-weight:700;"
            f"font-size:12px;'>{_lang_text(lang, 'in_watchlist')}</span>"
            f"{sync_badge}"
            "<span style='display:inline-flex;align-items:center;padding:6px 10px;"
            "border-radius:999px;background:#f8f5ee;color:#6b7280;font-weight:700;"
            "font-size:12px;'>"
            f"{sync_state}"
            "</span>"
            f"{sync_now}"
            "</div>"
        )
    hidden_fields = _hidden_fields_html(current_params)
    market = item.get("market") or ""
    name = item.get("name") or ""
    return (
        "<form method='post' action='/screeners/add-to-watchlist' style='margin:0;'>"
        f"<input type='hidden' name='ticker' value='{item['ticker']}' />"
        f"<input type='hidden' name='name' value='{name}' />"
        f"<input type='hidden' name='symbol_market' value='{market}' />"
        f"{hidden_fields}"
        f"<button type='submit'>{_lang_text(lang, 'add_to_watchlist')}</button>"
        "</form>"
    )


def _current_params(
    *,
    model_template: str,
    universe: str,
    market: str,
    min_trend_score: int,
    action_filter: str,
    min_volume_ratio: float,
    min_listing_days: int,
    pe_min: float,
    pe_max: float,
    min_roe_avg_3y: float,
    min_net_profit_yoy: float,
    min_revenue_yoy: float,
    max_debt_to_assets: float,
    min_dividend_yield: float,
    exclude_bottom_market_cap_pct: float,
    recent_snapshot_runs: int,
    min_snapshot_hits: int,
    model_signal_filter: str,
    min_model_signal_strength: float,
    execution_tag_filter: str,
    exclude_execution_tag_filter: str,
    sort_by: str,
    sort_order: str,
    lang: str,
    multi_model_templates: list[str] | None = None,
    min_multi_model_hits: int = 2,
    confluence_action_filter: str = "ALL",
    strategy_profile: str = "",
) -> dict:
    params = {
        "model_template": model_template,
        "multi_model_templates": _normalize_multi_model_templates(multi_model_templates),
        "min_multi_model_hits": min_multi_model_hits,
        "confluence_action_filter": str(confluence_action_filter or "ALL"),
        "universe": universe,
        "market": market,
        "min_trend_score": min_trend_score,
        "action_filter": action_filter,
        "min_volume_ratio": min_volume_ratio,
        "min_listing_days": min_listing_days,
        "pe_min": pe_min,
        "pe_max": pe_max,
        "min_roe_avg_3y": min_roe_avg_3y,
        "min_net_profit_yoy": min_net_profit_yoy,
        "min_revenue_yoy": min_revenue_yoy,
        "max_debt_to_assets": max_debt_to_assets,
        "min_dividend_yield": min_dividend_yield,
        "exclude_bottom_market_cap_pct": exclude_bottom_market_cap_pct,
        "recent_snapshot_runs": recent_snapshot_runs,
        "min_snapshot_hits": min_snapshot_hits,
        "model_signal_filter": model_signal_filter,
        "min_model_signal_strength": min_model_signal_strength,
        "execution_tag_filter": execution_tag_filter,
        "exclude_execution_tag_filter": exclude_execution_tag_filter,
        "sort_by": sort_by,
        "sort_order": sort_order,
        "lang": lang,
    }
    if str(strategy_profile).strip() == "quality_confluence_v1":
        params["strategy_profile"] = "quality_confluence_v1"
    return params


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


def _normalize_screen_params(params: dict) -> dict:
    model_template = str(params.get("model_template", "technical_momentum"))
    template = MODEL_TEMPLATES.get(model_template, MODEL_TEMPLATES["technical_momentum"])
    requested_market = str(params.get("market", "ALL"))
    effective_market = str(template.get("market") or requested_market)
    if effective_market != "ALL":
        requested_market = effective_market
    normalized = {
        "model_template": model_template,
        "multi_model_templates": _normalize_multi_model_templates(params.get("multi_model_templates")),
        "min_multi_model_hits": max(1, int(float(params.get("min_multi_model_hits", 2)))),
        "confluence_action_filter": str(params.get("confluence_action_filter", "ALL")),
        "lang": str(params.get("lang", "en")),
        "universe": str(params.get("universe", "watchlist")),
        "market": requested_market,
        "min_trend_score": int(float(params.get("min_trend_score", 60))),
        "action_filter": str(params.get("action_filter", "ALL")),
        "min_volume_ratio": float(params.get("min_volume_ratio", 0.0)),
        "min_listing_days": int(float(params.get("min_listing_days", 365))),
        "pe_min": float(params.get("pe_min", 0.0)),
        "pe_max": float(params.get("pe_max", 30.0)),
        "min_roe_avg_3y": float(params.get("min_roe_avg_3y", 12.0)),
        "min_net_profit_yoy": float(params.get("min_net_profit_yoy", 20.0)),
        "min_revenue_yoy": float(params.get("min_revenue_yoy", 0.0)),
        "max_debt_to_assets": float(params.get("max_debt_to_assets", 100.0)),
        "min_dividend_yield": float(params.get("min_dividend_yield", 0.0)),
        "exclude_bottom_market_cap_pct": float(params.get("exclude_bottom_market_cap_pct", 10.0)),
        "recent_snapshot_runs": int(float(params.get("recent_snapshot_runs", 0))),
        "min_snapshot_hits": int(float(params.get("min_snapshot_hits", 0))),
        "model_signal_filter": str(params.get("model_signal_filter", "ALL")),
        "min_model_signal_strength": float(params.get("min_model_signal_strength", 0.0)),
        "execution_tag_filter": str(params.get("execution_tag_filter", "ALL")),
        "exclude_execution_tag_filter": str(params.get("exclude_execution_tag_filter", "ALL")),
        "sort_by": str(params.get("sort_by", "default")),
        "sort_order": str(params.get("sort_order", "desc")),
        "limit": 500,
    }
    if str(params.get("strategy_profile") or "").strip() == "quality_confluence_v1":
        normalized["strategy_profile"] = "quality_confluence_v1"
    return normalized


def _load_screen_rows_from_snapshot(service: ScreenerService, normalized: dict) -> tuple[list[dict] | None, bool]:
    snapshot_rows = _load_precomputed_screener_rows(service, normalized)
    if snapshot_rows is not None:
        return snapshot_rows, True
    snapshot_rows = _load_screener_snapshot(normalized)
    if snapshot_rows is not None:
        return snapshot_rows, True
    return None, False


def _run_screen(service: ScreenerService, params: dict) -> list[dict]:
    normalized = _normalize_screen_params(params)
    multi_templates = normalized.get("multi_model_templates") or []
    if len(multi_templates) >= 2:
        rows, _ready, _meta = _run_multi_screen(service, normalized)
        return rows
    rows, ready = _load_screen_rows_from_snapshot(service, normalized)
    if ready:
        return rows or []
    return []


def _screen_snapshot_ready(service: ScreenerService, params: dict) -> bool:
    normalized = _normalize_screen_params(params)
    multi_templates = normalized.get("multi_model_templates") or []
    if len(multi_templates) >= 2:
        _rows, ready, _meta = _run_multi_screen(service, normalized)
        return ready
    _, ready = _load_screen_rows_from_snapshot(service, normalized)
    return ready


def _run_multi_screen(service: ScreenerService, params: dict) -> tuple[list[dict], bool, dict]:
    combo_snapshot_rows = _load_screener_snapshot(params)
    if combo_snapshot_rows is not None:
        return combo_snapshot_rows, True, {"available_templates": _normalize_multi_model_templates(params.get("multi_model_templates")), "missing_templates": [], "precomputed_combo": True}
    template_keys = _normalize_multi_model_templates(params.get("multi_model_templates"))
    if len(template_keys) < 2:
        return [], False, {"available_templates": [], "missing_templates": []}
    template_rows: dict[str, list[dict]] = {}
    missing_templates: list[str] = []
    available_templates: list[str] = []
    for template_key in template_keys:
        local_params = dict(params)
        local_params["model_template"] = template_key
        local_params["multi_model_templates"] = []
        local_params["min_multi_model_hits"] = 1
        rows, ready = _load_screen_rows_from_snapshot(service, _normalize_screen_params(local_params))
        if not ready or rows is None:
            missing_templates.append(template_key)
            continue
        template_rows[template_key] = rows
        available_templates.append(template_key)
    if not available_templates:
        return [], False, {"available_templates": [], "missing_templates": missing_templates}

    aggregated: dict[str, dict] = {}
    for template_key in available_templates:
        label = _template_label(template_key, MODEL_TEMPLATES[template_key]["label"], "zh")
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
                base["_source_template"] = template_key
                base["_source_label"] = label
                base["_template_keys"] = []
                base["_template_labels"] = []
                base["_action_labels"] = []
                base["_confluence_bucket_hits"] = {}
                base["_selection_reasons"] = []
                base["_execution_tags"] = []
                base["_scores"] = []
                aggregated[ticker] = base
                existing = base
            existing["_template_keys"].append(template_key)
            existing["_template_labels"].append(label)
            existing["_action_labels"].append(str(row.get("action_label") or "").strip())
            for bucket in _template_action_semantic_buckets(template_key, row.get("action_label")):
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
    confluence_action_filter = _normalize_action_filter(params.get("confluence_action_filter"))
    results: list[dict] = []
    for ticker, item in aggregated.items():
        template_keys_hit = list(dict.fromkeys(item.pop("_template_keys", [])))
        if len(template_keys_hit) < min_hits:
            continue
        template_labels_hit = list(dict.fromkeys(item.pop("_template_labels", [])))
        action_labels_hit = [label for label in dict.fromkeys(item.pop("_action_labels", [])) if label]
        confluence_bucket_hits = item.pop("_confluence_bucket_hits", {})
        selection_reasons = list(dict.fromkeys(item.pop("_selection_reasons", [])))
        execution_tags = list(dict.fromkeys(item.pop("_execution_tags", [])))
        scores = item.pop("_scores", [])
        item.pop("_best_score", None)
        item.pop("_source_template", None)
        item.pop("_source_label", None)
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
            f"{'命中模板' if params.get('lang') == 'zh' else 'Matched templates'}: " + " / ".join(template_labels_hit[:5]),
            f"{'动作形态' if params.get('lang') == 'zh' else 'Action mix'}: " + " / ".join(action_labels_hit[:4])
            if action_labels_hit
            else "",
        ] + selection_reasons[:2]
        item["model_highlights"] = [text for text in item["model_highlights"] if text]
        item["snapshot_score"] = round(sum(float(score or 0.0) for score in scores) / max(1, len(scores)), 2) if scores else item.get("snapshot_score")
        item["trend_score"] = round(sum(float(score or 0.0) for score in scores) / max(1, len(scores))) if scores else item.get("trend_score")
        if len(action_labels_hit) == 1:
            item["action_label"] = action_labels_hit[0]
        else:
            item["action_label"] = "多模型共振" if params.get("lang") == "zh" else "Multi-signal"
        results.append(item)

    sort_by = str(params.get("sort_by", "default"))
    sort_order = str(params.get("sort_order", "desc"))
    if sort_by in {"default", "confluence_rank"}:
        reverse = sort_order != "asc"
        results.sort(
            key=lambda row: (
                int(row.get("model_hit_count") or 0),
                int(row.get("confluence_alignment_count") or 0),
                float(row.get("snapshot_score") or 0.0),
                float(row.get("trend_score") or 0.0),
                str(row.get("ticker") or ""),
            ),
            reverse=reverse,
        )
    elif sort_by in {"model_hit_count", "confluence_alignment_count"}:
        reverse = sort_order != "asc"
        primary_key = "model_hit_count" if sort_by == "model_hit_count" else "confluence_alignment_count"
        results.sort(
            key=lambda row: (
                int(row.get(primary_key) or 0),
                int(row.get("model_hit_count") or 0),
                int(row.get("confluence_alignment_count") or 0),
                float(row.get("snapshot_score") or 0.0),
                float(row.get("trend_score") or 0.0),
                str(row.get("ticker") or ""),
            ),
            reverse=reverse,
        )
    else:
        results = service._sort_results(
            results,
            sort_by=sort_by,
            sort_order=sort_order,
        )
    limit = int(params.get("limit", 500))
    return results[:limit], len(missing_templates) < len(template_keys), {
        "available_templates": available_templates,
        "missing_templates": missing_templates,
    }


def _snapshot_pending_message(lang: str) -> str:
    return _lang_text(lang, "snapshot_pending")


def _normalize_action_filter(value: str | None) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def _action_semantic_buckets(value: str | None) -> list[str]:
    normalized = _normalize_action_filter(value)
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


def _load_precomputed_screener_rows(service: ScreenerService, params: dict) -> list[dict] | None:
    base_params = build_base_precompute_params(
        model_template=str(params.get("model_template") or "technical_momentum"),
        universe=str(params.get("universe") or "watchlist"),
        market=str(params.get("market") or "ALL"),
    )
    snapshot_rows = _load_screener_snapshot(base_params)
    if snapshot_rows is None:
        return None
    return _filter_precomputed_rows(service, snapshot_rows, params)


def _filter_precomputed_rows(service: ScreenerService, rows: list[dict], params: dict) -> list[dict]:
    min_trend_score = int(params.get("min_trend_score", 60))
    action_filter = str(params.get("action_filter", "ALL"))
    min_volume_ratio = float(params.get("min_volume_ratio", 0.0))
    min_listing_days = int(params.get("min_listing_days", 365))
    pe_min = float(params.get("pe_min", 0.0))
    pe_max = float(params.get("pe_max", 30.0))
    min_roe_avg_3y = float(params.get("min_roe_avg_3y", 12.0))
    min_net_profit_yoy = float(params.get("min_net_profit_yoy", 20.0))
    min_revenue_yoy = float(params.get("min_revenue_yoy", 0.0))
    max_debt_to_assets = float(params.get("max_debt_to_assets", 100.0))
    min_dividend_yield = float(params.get("min_dividend_yield", 0.0))
    recent_snapshot_runs = int(params.get("recent_snapshot_runs", 0))
    min_snapshot_hits = int(params.get("min_snapshot_hits", 0))
    filtered: list[dict] = []
    normalized_action_filter = _normalize_action_filter(action_filter)

    for row in rows:
        trend_score = row.get("trend_score")
        if trend_score is not None and float(trend_score or 0.0) < min_trend_score:
            continue
        if normalized_action_filter not in {"", "all"}:
            if _normalize_action_filter(row.get("action_label")) != normalized_action_filter:
                continue
        if float(row.get("volume_ratio") or 0.0) < min_volume_ratio:
            continue
        listing_days = row.get("listing_days")
        if listing_days is not None and int(listing_days or 0) < min_listing_days:
            continue
        pe_ttm = row.get("pe_ttm")
        if pe_ttm is not None and not (pe_min <= float(pe_ttm) <= pe_max):
            continue
        roe_avg_3y = row.get("roe_avg_3y")
        if roe_avg_3y is not None and float(roe_avg_3y) < min_roe_avg_3y:
            continue
        net_profit_yoy = row.get("net_profit_yoy")
        if net_profit_yoy is not None and float(net_profit_yoy) < min_net_profit_yoy:
            continue
        revenue_yoy = row.get("revenue_yoy")
        if revenue_yoy is not None and float(revenue_yoy) < min_revenue_yoy:
            continue
        debt_to_assets = row.get("debt_to_assets")
        if debt_to_assets is not None and float(debt_to_assets) > max_debt_to_assets:
            continue
        dividend_yield = row.get("dividend_yield")
        if dividend_yield is not None and float(dividend_yield) < min_dividend_yield:
            continue
        filtered.append(dict(row))

    filtered = service._apply_snapshot_persistence_filter(
        filtered,
        recent_snapshot_runs=recent_snapshot_runs,
        min_snapshot_hits=min_snapshot_hits,
    )
    filtered = service._apply_model_signal_filter(
        filtered,
        model_signal_filter=str(params.get("model_signal_filter", "ALL")),
        min_model_signal_strength=float(params.get("min_model_signal_strength", 0.0)),
    )
    filtered = service._apply_execution_tag_filter(
        filtered,
        execution_tag_filter=str(params.get("execution_tag_filter", "ALL")),
        exclude_execution_tag_filter=str(params.get("exclude_execution_tag_filter", "ALL")),
    )
    filtered = service._sort_results(
        filtered,
        sort_by=str(params.get("sort_by", "default")),
        sort_order=str(params.get("sort_order", "desc")),
    )
    limit = int(params.get("limit", 500))
    return filtered[:limit]


def _should_execute_screen(request: Request) -> bool:
    if str(request.query_params.get("run") or "").strip() == "1":
        return True
    meaningful_keys = {
        "model_template",
        "universe",
        "market",
        "min_trend_score",
        "action_filter",
        "min_volume_ratio",
        "min_listing_days",
        "pe_min",
        "pe_max",
        "min_roe_avg_3y",
        "min_net_profit_yoy",
        "min_revenue_yoy",
        "max_debt_to_assets",
        "min_dividend_yield",
        "exclude_bottom_market_cap_pct",
        "recent_snapshot_runs",
        "min_snapshot_hits",
        "model_signal_filter",
        "min_model_signal_strength",
        "execution_tag_filter",
        "exclude_execution_tag_filter",
        "sort_by",
        "sort_order",
    }
    return any(key in meaningful_keys for key in request.query_params.keys())


def _load_screener_snapshot(params: dict) -> list[dict] | None:
    with SessionLocal() as db:
        snapshot = WorkspaceSnapshotRepository(db).get_latest_snapshot(screener_snapshot_type(params))
    if not snapshot:
        return None
    payload = snapshot.get("payload") or {}
    if payload.get("key") != screener_snapshot_key(params):
        return None
    created_at = str(snapshot.get("created_at") or "")
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return None
    if datetime.fromisoformat(app_now_iso()) - created > SCREENER_SNAPSHOT_TTL:
        return None
    rows = payload.get("rows")
    return rows if isinstance(rows, list) else None


def _load_screener_snapshot_record(params: dict) -> dict | None:
    with SessionLocal() as db:
        snapshot = WorkspaceSnapshotRepository(db).get_latest_snapshot(screener_snapshot_type(params))
    if not snapshot:
        return None
    payload = snapshot.get("payload") or {}
    if payload.get("key") != screener_snapshot_key(params):
        return None
    return snapshot


def _persist_screener_snapshot(params: dict, rows: list[dict]) -> None:
    payload = {
        "key": screener_snapshot_key(params),
        "rows": rows,
        "updated_at": app_now_iso(),
    }
    with SessionLocal() as db:
        WorkspaceSnapshotRepository(db).create_snapshot(
            snapshot_type=screener_snapshot_type(params),
            snapshot_date=app_now_iso(),
            payload=payload,
        )


def _screen_run_receipt_html(
    *,
    service: ScreenerService,
    params: dict,
    results: list[dict],
    snapshot_ready: bool,
    multi_templates_active: list[str],
    multi_screen_meta: dict,
    lang: str,
) -> str:
    normalized = _normalize_screen_params(params)
    source_params = normalized
    source_kind = "multi" if len(multi_templates_active) >= 2 else "exact"
    snapshot = _load_screener_snapshot_record(source_params)
    if snapshot is None and len(multi_templates_active) < 2:
        base_params = build_base_precompute_params(
            model_template=str(normalized.get("model_template") or "technical_momentum"),
            universe=str(normalized.get("universe") or "watchlist"),
            market=str(normalized.get("market") or "ALL"),
        )
        snapshot = _load_screener_snapshot_record(base_params)
        if snapshot is not None:
            source_params = base_params
            source_kind = "base_precompute"
    payload = (snapshot or {}).get("payload") if isinstance(snapshot, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    param_digest = screener_snapshot_type(source_params).split(":", 1)[-1]
    source_label = {
        "multi": "多模型组合快照" if lang == "zh" else "Multi-model snapshot",
        "base_precompute": "基础预计算快照" if lang == "zh" else "Base precompute snapshot",
        "exact": "精确参数快照" if lang == "zh" else "Exact parameter snapshot",
    }.get(source_kind, source_kind)
    if snapshot is None:
        source_label = "页面实时结果 / 待预计算" if lang == "zh" else "Page result / pending precompute"
    template_label = _preset_display_label(normalized, lang)
    param_summary = _preset_summary(normalized, lang)
    available_labels = [
        _template_label(key, MODEL_TEMPLATES[key]["label"], lang)
        for key in (multi_screen_meta.get("available_templates") or [])
        if key in MODEL_TEMPLATES
    ]
    missing_labels = [
        _template_label(key, MODEL_TEMPLATES[key]["label"], lang)
        for key in (multi_screen_meta.get("missing_templates") or [])
        if key in MODEL_TEMPLATES
    ]
    lineage_rows = [
        ("策略/模型" if lang == "zh" else "Strategy/Model", template_label),
        ("参数摘要" if lang == "zh" else "Parameter Summary", param_summary),
        ("结果数量" if lang == "zh" else "Result Count", str(len(results))),
        ("快照状态" if lang == "zh" else "Snapshot Status", "已就绪" if snapshot_ready else ("待生成" if lang == "zh" else "Pending")),
        ("来源类型" if lang == "zh" else "Source Type", source_label),
        ("快照 ID" if lang == "zh" else "Snapshot ID", str((snapshot or {}).get("id") or "-")),
        ("来源 Job" if lang == "zh" else "Source Job", str((snapshot or {}).get("source_job_id") or "-")),
        ("生成时间" if lang == "zh" else "Created At", _compact_text(str((snapshot or {}).get("created_at") or payload.get("updated_at") or "-"), 32)),
        ("参数指纹" if lang == "zh" else "Param Fingerprint", param_digest),
    ]
    candidate_stats = payload.get("candidate_stats") if isinstance(payload.get("candidate_stats"), dict) else {}
    if candidate_stats:
        returned_count = candidate_stats.get("returned_count")
        persisted_count = candidate_stats.get("persisted_count")
        source_limit = candidate_stats.get("limit")
        stats_text = (
            f"筛选返回 {returned_count} / 快照落库 {persisted_count} / 上限 {source_limit}"
            if lang == "zh"
            else f"Returned {returned_count} / persisted {persisted_count} / limit {source_limit}"
        )
        lineage_rows.append(("候选口径" if lang == "zh" else "Candidate Scope", stats_text))
    if len(multi_templates_active) >= 2:
        lineage_rows.append(("参与模型" if lang == "zh" else "Available Models", " / ".join(available_labels) or "-"))
        lineage_rows.append(("缺失模型" if lang == "zh" else "Missing Models", " / ".join(missing_labels) or ("无" if lang == "zh" else "None")))
    rows_html = "".join(
        "<div class='receipt-row'>"
        f"<span>{html.escape(label)}</span>"
        f"<strong title='{html.escape(value)}'>{html.escape(_compact_text(value, 72))}</strong>"
        "</div>"
        for label, value in lineage_rows
    )
    return (
        "<article class='card run-receipt-card'>"
        f"<div class='eyebrow'>{_lang_text(lang, 'run_receipt')}</div>"
        f"<p class='lead'>{'这张收据用于回看：本次结果来自哪组参数、哪个快照、哪个后台 job。后续日报和历史命中评测可以继续沿这个指纹关联。' if lang == 'zh' else 'This receipt records which parameters, snapshot, and background job produced the current result set.'}</p>"
        f"<div class='receipt-grid'>{rows_html}</div>"
        "</article>"
    )


def _load_today_focus_items() -> list[dict]:
    return get_or_set(
        "today_focus_items",
        "latest",
        ttl_seconds=30.0,
        loader=lambda: enrich_focus_pool_with_symbols(load_today_focus_pool()),
    )


def _add_screen_results_to_watchlist(
    *,
    db: Session,
    params: dict,
    top_n: int = 0,
    auto_enable_sync: bool = False,
) -> tuple[int, int, int]:
    service = ScreenerService()
    symbol_repo = SymbolRepository(db)
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)
    results = _run_screen(service, params)
    if top_n > 0:
        results = results[:top_n]
    added = 0
    already_in_watchlist = 0
    sync_enabled_count = 0

    for item in results:
        ticker = item["ticker"]
        existing = watchlist_map.get(ticker)
        if existing:
            already_in_watchlist += 1
            if auto_enable_sync and not existing.get("sync_enabled"):
                updated = watchlist_repo.set_sync_enabled(existing["item_id"], True)
                if updated is not None:
                    sync_enabled_count += 1
                    existing["sync_enabled"] = 1
            continue
        symbol = symbol_repo.get_or_create_symbol(
            SymbolCreate(
                ticker=ticker,
                name=item.get("name"),
                market=item.get("market"),
            )
        )
        watchlist_item = watchlist_repo.add_symbol(watchlist.id, symbol.id)
        if auto_enable_sync:
            watchlist_repo.set_sync_enabled(watchlist_item.id, True)
            sync_enabled_count += 1
        watchlist_map[ticker] = {
            "item_id": watchlist_item.id,
            "symbol_id": symbol.id,
            "ticker": ticker,
            "name": item.get("name"),
            "market": item.get("market"),
            "sync_enabled": 1 if auto_enable_sync else 0,
        }
        added += 1

    return added, already_in_watchlist, sync_enabled_count


def _focus_pool_trade_candidates(results: list[dict]) -> tuple[list[dict], int]:
    candidates: list[dict] = []
    skipped = 0
    for item in results:
        status = str(item.get("tradability_status") or "").strip().upper()
        bucket = str(item.get("readiness_bucket") or "").strip().upper()
        try:
            readiness = float(item.get("trade_readiness_score") or 0.0)
        except (TypeError, ValueError):
            readiness = 0.0
        hard_blocked = status in {"BLOCKED", "DO_NOT_CHASE"}
        low_readiness = bucket in {"LOW", "BLOCKED"} or (readiness > 0 and readiness < 60.0)
        if hard_blocked or low_readiness:
            skipped += 1
            continue
        candidates.append(item)
    return candidates, skipped


def _factor_lab_url(*, lang: str, message: str | None = None, strategy_id: str | None = None) -> str:
    params = {"lang": lang}
    if strategy_id:
        params["strategy_id"] = strategy_id
    if message:
        params["message"] = message
    return f"/screeners/factor-lab?{urlencode(params)}"


def _factor_label(factor: dict, lang: str) -> str:
    return str(factor.get("label_zh" if lang == "zh" else "label_en") or factor.get("key") or "")


def _factor_strategy_summary(strategy: dict, lang: str) -> str:
    filters = strategy.get("filters") or []
    weights = strategy.get("weights") or {}
    if lang == "zh":
        return f"{len(filters)} 个过滤条件 · {len(weights)} 个权重因子"
    return f"{len(filters)} filters · {len(weights)} weighted factors"


@router.get("/factor-lab", response_class=HTMLResponse)
def factor_lab_page(
    request: Request,
    lang: str = Query("en"),
    strategy_id: str | None = Query(None),
    message: str | None = Query(None),
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/screeners/factor-lab")
    lang = resolve_request_lang(request)
    strategies = list_factor_strategies(db)
    selected_strategy = get_factor_strategy(db, strategy_id) or (strategies[0] if strategies else {})
    factor_defs = list_factor_definitions()
    factor_map = {item["key"]: item for item in factor_defs}
    runs = list_factor_experiment_runs(db, limit=12)

    categories = [
        ("technical", "技术条件", "Technical"),
        ("fundamental", "基本面条件", "Fundamental"),
        ("risk", "风险标签", "Risk"),
        ("model", "模型信号", "Model"),
        ("profit_gap", "利润断层", "Profit Gap"),
    ]
    factor_cards = []
    for category_key, title_zh, title_en in categories:
        items = [item for item in factor_defs if item.get("category") == category_key]
        if not items:
            continue
        rows = "".join(
            f"""
            <tr>
              <td><strong>{html.escape(_factor_label(item, lang))}</strong><div class="muted mini">{html.escape(str(item.get('key') or ''))}</div></td>
              <td>{html.escape(str(item.get('description_zh') if lang == 'zh' else item.get('description_zh') or ''))}</td>
              <td>{html.escape(str(item.get('usage_zh') or ''))}</td>
              <td>{html.escape(str(item.get('risk_zh') or ''))}</td>
              <td>{html.escape(str(item.get('market_fit_zh') or ''))}</td>
            </tr>
            """
            for item in items
        )
        factor_cards.append(
            f"""
            <section class="card">
              <div class="section-head">
                <div>
                  <div class="eyebrow">{html.escape(title_zh if lang == 'zh' else title_en)}</div>
                  <h2>{html.escape(title_zh if lang == 'zh' else title_en)}</h2>
                </div>
                <span class="pill">{len(items)} factors</span>
              </div>
              <div class="table-wrap compact-table">
                <table>
                  <thead>
                    <tr>
                      <th>{'因子' if lang == 'zh' else 'Factor'}</th>
                      <th>{'含义' if lang == 'zh' else 'Meaning'}</th>
                      <th>{'用途' if lang == 'zh' else 'Usage'}</th>
                      <th>{'风险' if lang == 'zh' else 'Risk'}</th>
                      <th>{'适用环境' if lang == 'zh' else 'Best Regime'}</th>
                    </tr>
                  </thead>
                  <tbody>{rows}</tbody>
                </table>
              </div>
            </section>
            """
        )

    strategy_options = "".join(
        f"<option value='{html.escape(str(item.get('id') or ''))}' {'selected' if str(item.get('id') or '') == str(selected_strategy.get('id') or '') else ''}>{html.escape(str(item.get('name') or item.get('id') or ''))}</option>"
        for item in strategies
    )
    selected_filters = selected_strategy.get("filters") or []
    selected_weights = selected_strategy.get("weights") or {}
    filter_rows = "".join(
        f"""
        <tr>
          <td>{html.escape(_factor_label(factor_map.get(str(item.get('factor_key') or ''), {'key': item.get('factor_key')}), lang))}</td>
          <td>{html.escape(str(item.get('op') or ''))}</td>
          <td>{html.escape(str(item.get('value') if item.get('value') is not None else f"{item.get('min', '')} - {item.get('max', '')}"))}</td>
          <td>{'硬条件' if item.get('required', True) and lang == 'zh' else ('Hard' if item.get('required', True) else ('可缺失' if lang == 'zh' else 'Optional'))}</td>
        </tr>
        """
        for item in selected_filters
    ) or f"<tr><td colspan='4' class='muted'>{'暂无过滤条件' if lang == 'zh' else 'No filters'}</td></tr>"
    weight_chips = "".join(
        f"<span class='chip'>{html.escape(_factor_label(factor_map.get(key, {'key': key}), lang))} · {float(value or 0):.0f}</span>"
        for key, value in sorted(selected_weights.items(), key=lambda pair: -float(pair[1] or 0))[:16]
    )

    run_rows = []
    for snapshot in runs:
        payload = snapshot.get("payload") or {}
        strategy = payload.get("strategy") or {}
        metrics = payload.get("metrics") or {}
        run_rows.append(
            f"""
            <tr>
              <td><strong>{html.escape(str(strategy.get('name') or '-'))}</strong><div class="muted mini">#{snapshot.get('id')} · {html.escape(str(snapshot.get('created_at') or ''))}</div></td>
              <td>{html.escape(str(payload.get('source_count') or 0))} / {html.escape(str(payload.get('matched_count') or 0))}</td>
              <td>{_fmt_metric(metrics.get('hit_rate_1d_pct'), suffix='%')}</td>
              <td>{_fmt_metric(metrics.get('hit_rate_3d_pct'), suffix='%')}</td>
              <td>{_fmt_metric(metrics.get('hit_rate_5d_pct'), suffix='%')}</td>
              <td>{_fmt_metric(metrics.get('avg_max_drawdown_5d_pct'), suffix='%')}</td>
              <td>{_fmt_metric(metrics.get('gap_unbuyable_rate_pct'), suffix='%')}</td>
              <td><a class="button secondary mini-button" href="/screeners/factor-lab/runs/{snapshot.get('id')}?lang={lang}">{'详情' if lang == 'zh' else 'Detail'}</a></td>
            </tr>
            """
        )
    runs_table = "".join(run_rows) or f"<tr><td colspan='8' class='muted'>{'还没有实验 run。先点击运行策略。' if lang == 'zh' else 'No runs yet. Run a strategy first.'}</td></tr>"

    source_params = selected_strategy.get("source_params") or {}
    snapshot_ready = exact_screener_snapshot_exists(source_params, db=db) if source_params else False
    source_hint = (
        "已找到预计算快照，可直接运行。"
        if snapshot_ready and lang == "zh"
        else "Snapshot ready."
        if snapshot_ready
        else "没有找到对应预计算快照，请先等待/触发模型预计算 job。"
        if lang == "zh"
        else "No matching precomputed snapshot yet. Run or wait for screener precompute first."
    )
    msg_html = f"<div class='notice'>{html.escape(message)}</div>" if message else ""
    nav_html = render_workspace_nav_html(lang=lang, active_key="screeners")
    return f"""
    <!doctype html>
    <html lang="{lang}">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>{'因子实验室' if lang == 'zh' else 'Factor Lab'}</title>
      <style>
        {WORKSPACE_SIDEBAR_STYLE}
        {WORKSPACE_COMPACT_STYLE}
        :root {{ --bg:#07111b; --panel:#0d1824; --panel2:#111f2d; --line:#213447; --ink:#edf5f2; --muted:#8da1ad; --accent:#35e0b4; --warn:#f8c95d; --bad:#fb7185; }}
        body {{ margin:0; background:radial-gradient(circle at 20% 0%, rgba(53,224,180,.14), transparent 28%), linear-gradient(135deg,#07111b,#0a1724 58%,#07111b); color:var(--ink); font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
        .workspace-shell {{ gap:12px; }}
        .workspace-main {{ max-width:1480px; }}
        .hero {{ display:grid; gap:16px; grid-template-columns:minmax(0,1.5fr) minmax(320px,.8fr); align-items:stretch; margin-bottom:14px; }}
        .card {{ background:linear-gradient(180deg, rgba(17,31,45,.96), rgba(10,20,31,.96)); border:1px solid var(--line); border-radius:22px; padding:18px; box-shadow:0 18px 40px rgba(0,0,0,.22); }}
        .section-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:12px; }}
        .eyebrow {{ color:var(--accent); font-size:12px; font-weight:900; letter-spacing:.12em; text-transform:uppercase; }}
        h1,h2,h3 {{ margin:0; }}
        h1 {{ font-size:34px; letter-spacing:-.04em; }}
        h2 {{ font-size:20px; letter-spacing:-.02em; }}
        p {{ color:var(--muted); line-height:1.6; margin:8px 0 0; }}
        .grid {{ display:grid; gap:14px; grid-template-columns:repeat(2, minmax(0, 1fr)); }}
        .muted {{ color:var(--muted); }}
        .mini {{ font-size:12px; margin-top:4px; }}
        .mini-button {{ padding:6px 9px; font-size:12px; white-space:nowrap; }}
        .pill,.chip {{ display:inline-flex; align-items:center; gap:6px; border-radius:999px; padding:6px 10px; background:rgba(53,224,180,.10); color:var(--accent); font-weight:800; font-size:12px; border:1px solid rgba(53,224,180,.20); }}
        .chip {{ margin:4px 6px 4px 0; color:#dffdf5; background:rgba(255,255,255,.06); border-color:rgba(255,255,255,.09); }}
        .toolbar {{ display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-top:14px; }}
        a.button, button {{ border:0; border-radius:14px; padding:11px 14px; background:var(--accent); color:#04231b; font-weight:900; text-decoration:none; cursor:pointer; }}
        a.secondary, button.secondary {{ background:#18293a; color:var(--ink); border:1px solid var(--line); }}
        select,input {{ width:100%; box-sizing:border-box; border:1px solid var(--line); background:#081522; color:var(--ink); border-radius:12px; padding:10px 12px; }}
        label {{ display:block; color:var(--muted); font-size:12px; font-weight:800; margin-bottom:6px; }}
        .form-grid {{ display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:12px; }}
        table {{ width:100%; border-collapse:collapse; min-width:920px; }}
        th,td {{ text-align:left; padding:10px 9px; border-bottom:1px solid var(--line); vertical-align:top; }}
        th {{ color:var(--muted); font-size:12px; white-space:nowrap; }}
        td {{ font-size:13px; }}
        .table-wrap {{ overflow:auto; border:1px solid var(--line); border-radius:16px; background:rgba(8,18,29,.62); }}
        .compact-table table {{ min-width:1050px; }}
        .notice {{ border:1px solid rgba(53,224,180,.25); background:rgba(53,224,180,.08); color:#c9fff2; border-radius:16px; padding:12px 14px; margin-bottom:14px; }}
        .status-ready {{ color:var(--accent); font-weight:900; }}
        .status-wait {{ color:var(--warn); font-weight:900; }}
        @media (max-width: 980px) {{ .hero,.grid,.form-grid {{ grid-template-columns:1fr; }} .workspace-shell {{ display:block; }} }}
      </style>
    </head>
    <body>
      <div class="workspace-shell">
        <nav class="side-nav">{nav_html}</nav>
        <main class="workspace-main">
          {msg_html}
          <section class="hero">
            <div class="card">
              <div class="eyebrow">{'Profit-Oriented Factor Experiments' if lang != 'zh' else '盈利导向因子实验'}</div>
              <h1>{'因子实验室' if lang == 'zh' else 'Factor Lab'}</h1>
              <p>{'这里不是替换现有模型，而是在模型结果上增加一层可解释、可回测、可保存的因子配置。你可以把技术条件、基本面、风险标签、LightGBM、多模型命中、Kronos 和利润断层条件组合成自己的选股策略。' if lang == 'zh' else 'This layer does not replace the existing models. It adds explainable, backtestable, saved factor strategies on top of model results.'}</p>
              <div class="toolbar">
                <a class="button secondary" href="/screeners?lang={lang}">{'返回模型选股' if lang == 'zh' else 'Back to Screeners'}</a>
                <a class="button secondary" href="/dashboard/model-performance?lang={lang}">{'模型评测总览' if lang == 'zh' else 'Model Evaluation'}</a>
              </div>
            </div>
            <div class="card">
              <div class="eyebrow">{'当前策略' if lang == 'zh' else 'Current Strategy'}</div>
              <h2>{html.escape(str(selected_strategy.get('name') or '-'))}</h2>
              <p>{html.escape(str(selected_strategy.get('description') or ''))}</p>
              <p class="{'status-ready' if snapshot_ready else 'status-wait'}">{html.escape(source_hint)}</p>
              <div class="toolbar">
                <form method="post" action="/screeners/factor-lab/run">
                  <input type="hidden" name="lang" value="{lang}" />
                  <input type="hidden" name="strategy_id" value="{html.escape(str(selected_strategy.get('id') or ''))}" />
                  <button type="submit">{'运行策略实验' if lang == 'zh' else 'Run Experiment'}</button>
                </form>
              </div>
            </div>
          </section>

          <section class="grid">
            <div class="card">
              <div class="section-head">
                <div>
                  <div class="eyebrow">{'策略项目' if lang == 'zh' else 'Strategy Project'}</div>
                  <h2>{'保存过滤 + 权重' if lang == 'zh' else 'Save Filters + Weights'}</h2>
                </div>
              </div>
              <form method="post" action="/screeners/factor-lab/save">
                <input type="hidden" name="lang" value="{lang}" />
                <div class="form-grid">
                  <div>
                    <label>{'基于模板' if lang == 'zh' else 'Base strategy'}</label>
                    <select name="base_strategy_id">{strategy_options}</select>
                  </div>
                  <div>
                    <label>{'新策略名称' if lang == 'zh' else 'New strategy name'}</label>
                    <input name="name" value="{html.escape(str(selected_strategy.get('name') or ''))}" />
                  </div>
                  <div>
                    <label>{'最低交易就绪度' if lang == 'zh' else 'Min readiness'}</label>
                    <input name="min_readiness" type="number" step="1" value="58" />
                  </div>
                  <div>
                    <label>{'最低趋势分' if lang == 'zh' else 'Min trend score'}</label>
                    <input name="min_trend" type="number" step="1" value="58" />
                  </div>
                  <div>
                    <label>{'最低利润同比' if lang == 'zh' else 'Min profit YoY'}</label>
                    <input name="min_profit_yoy" type="number" step="1" value="15" />
                  </div>
                  <div>
                    <label>{'交易就绪度权重' if lang == 'zh' else 'Readiness weight'}</label>
                    <input name="readiness_weight" type="number" step="1" value="22" />
                  </div>
                </div>
                <div class="toolbar"><button type="submit">{'保存为我的策略' if lang == 'zh' else 'Save Strategy'}</button></div>
              </form>
            </div>
            <div class="card">
              <div class="section-head">
                <div>
                  <div class="eyebrow">{'已选策略结构' if lang == 'zh' else 'Selected Strategy'}</div>
                  <h2>{html.escape(_factor_strategy_summary(selected_strategy, lang))}</h2>
                </div>
              </div>
              <div>{weight_chips or f"<span class='muted'>{'暂无权重' if lang == 'zh' else 'No weights'}</span>"}</div>
              <div class="table-wrap" style="margin-top:12px;">
                <table>
                  <thead><tr><th>{'因子' if lang == 'zh' else 'Factor'}</th><th>Op</th><th>{'阈值' if lang == 'zh' else 'Value'}</th><th>{'类型' if lang == 'zh' else 'Type'}</th></tr></thead>
                  <tbody>{filter_rows}</tbody>
                </table>
              </div>
            </div>
          </section>

          <section class="card" style="margin-top:14px;">
            <div class="section-head">
              <div>
                <div class="eyebrow">{'实验 Run 历史' if lang == 'zh' else 'Experiment Runs'}</div>
                <h2>{'自动统计 1D/3D/5D 命中率、回撤和高开买不到' if lang == 'zh' else 'Tracks 1D/3D/5D hit rate, drawdown, and gap-unbuyable rate'}</h2>
              </div>
            </div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr><th>{'策略' if lang == 'zh' else 'Strategy'}</th><th>{'源样本/命中' if lang == 'zh' else 'Source/Matched'}</th><th>1D Hit</th><th>3D Hit</th><th>5D Hit</th><th>5D DD</th><th>{'高开买不到' if lang == 'zh' else 'Gap blocked'}</th><th>{'操作' if lang == 'zh' else 'Actions'}</th></tr>
                </thead>
                <tbody>{runs_table}</tbody>
              </table>
            </div>
          </section>
          {''.join(factor_cards)}
        </main>
      </div>
    </body>
    </html>
    """


def _fmt_metric(value: object, *, suffix: str = "") -> str:
    if value in (None, ""):
        return "<span class='muted'>-</span>"
    try:
        return f"{float(value):.2f}{suffix}"
    except (TypeError, ValueError):
        return html.escape(str(value))


@router.get("/selection-quality", response_class=HTMLResponse)
def selection_quality_page(
    request: Request,
    lang: str = Query("en"),
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/screeners/selection-quality")
    lang = resolve_request_lang(request)
    try:
        payload = load_or_build_selection_quality(db=db)
    except Exception as exc:
        payload = {
            "sample_count": 0,
            "summary": {"all": {}, "by_source": []},
            "guidance": {
                "headline_zh": f"命中率闭环暂时不可用：{exc}",
                "headline_en": f"Selection quality is temporarily unavailable: {exc}",
                "rules_zh": [],
            },
            "recent_records": [],
        }
    summary = payload.get("summary") or {}
    all_metrics = summary.get("all") or {}
    by_source = summary.get("by_source") or []
    guidance = payload.get("guidance") or {}
    recent_records = payload.get("recent_records") or []
    source_counts = payload.get("source_counts") or {}
    meta = payload.get("snapshot_meta") or {}

    source_rows = "".join(
        f"""
        <tr>
          <td><strong>{html.escape(str(item.get('source_name') or '-'))}</strong><div class="muted mini">{html.escape(str(item.get('source_type') or '-'))}</div></td>
          <td>{html.escape(str((item.get('metrics') or {}).get('available_1d') or 0))} / {html.escape(str((item.get('metrics') or {}).get('count') or 0))}</td>
          <td>{_fmt_metric((item.get('metrics') or {}).get('hit_rate_1d_pct'), suffix='%')}</td>
          <td>{_fmt_metric((item.get('metrics') or {}).get('execution_hit_rate_pct'), suffix='%')}</td>
          <td>{_fmt_metric((item.get('metrics') or {}).get('avg_return_1d_pct'), suffix='%')}</td>
          <td>{_fmt_metric((item.get('metrics') or {}).get('avg_open_to_high_pct'), suffix='%')}</td>
          <td>{_fmt_metric((item.get('metrics') or {}).get('avg_open_to_low_pct'), suffix='%')}</td>
          <td>{_fmt_metric((item.get('metrics') or {}).get('gap_blocked_rate_pct'), suffix='%')}</td>
        </tr>
        """
        for item in by_source[:24]
    ) or f"<tr><td colspan='8' class='muted'>{'还没有可评估样本。' if lang == 'zh' else 'No evaluable samples yet.'}</td></tr>"

    record_rows = []
    for record in recent_records[:60]:
        ticker = html.escape(str(record.get("ticker") or "-"))
        name = html.escape(str(record.get("name") or ""))
        risk_flags = "".join(
            f"<span class='risk-chip'>{html.escape(str(flag))}</span>"
            for flag in list(record.get("risk_flags") or [])[:3]
        )
        link = f"/insights/{ticker}?lang={lang}" if ticker != "-" else "#"
        record_rows.append(
            f"""
            <tr>
              <td><strong><a href="{link}">{ticker}</a></strong><div class="muted mini">{name}</div></td>
              <td>{html.escape(str(record.get('market') or '-'))}</td>
              <td>{html.escape(str(record.get('signal_date') or '-'))}<div class="muted mini">{html.escape(str(record.get('next_date') or 'pending'))}</div></td>
              <td>{html.escape(str(record.get('source_name') or '-'))}<div class="muted mini">{html.escape(str(record.get('source_type') or '-'))}</div></td>
              <td>{_fmt_metric(record.get('score'))}</td>
              <td>{_fmt_metric(record.get('return_1d_pct'), suffix='%')}</td>
              <td>{_fmt_metric(record.get('open_to_high_pct'), suffix='%')}</td>
              <td>{_fmt_metric(record.get('open_to_low_pct'), suffix='%')}</td>
              <td>{risk_flags or '<span class="muted">-</span>'}</td>
            </tr>
            """
        )
    records_table = "".join(record_rows) or f"<tr><td colspan='9' class='muted'>{'暂无最近样本。' if lang == 'zh' else 'No recent samples.'}</td></tr>"
    rules_html = "".join(
        f"<li>{html.escape(str(rule))}</li>"
        for rule in list(guidance.get("rules_zh") or [])[:5]
    )
    nav_html = render_workspace_nav_html(lang=lang, active_key="screeners")
    return f"""
    <!doctype html>
    <html lang="{lang}">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>{'命中率闭环' if lang == 'zh' else 'Selection Quality'}</title>
      <style>
        {WORKSPACE_SIDEBAR_STYLE}
        {WORKSPACE_COMPACT_STYLE}
        :root {{ --bg:#07111b; --panel:#0d1824; --panel2:#111f2d; --line:#213447; --ink:#edf5f2; --muted:#8da1ad; --accent:#35e0b4; --warn:#f8c95d; --bad:#fb7185; }}
        body {{ margin:0; background:radial-gradient(circle at 18% 0%, rgba(53,224,180,.13), transparent 28%), linear-gradient(135deg,#07111b,#0a1724 58%,#07111b); color:var(--ink); font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
        .workspace-shell {{ gap:12px; }}
        .workspace-main {{ max-width:1480px; }}
        .hero {{ display:grid; gap:14px; grid-template-columns:minmax(0,1.35fr) minmax(320px,.75fr); margin-bottom:14px; }}
        .card {{ background:linear-gradient(180deg, rgba(17,31,45,.96), rgba(10,20,31,.96)); border:1px solid var(--line); border-radius:22px; padding:18px; box-shadow:0 18px 40px rgba(0,0,0,.22); }}
        .section-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:12px; }}
        .eyebrow {{ color:var(--accent); font-size:12px; font-weight:900; letter-spacing:.12em; text-transform:uppercase; }}
        h1,h2 {{ margin:0; letter-spacing:-.03em; }}
        h1 {{ font-size:34px; }}
        h2 {{ font-size:20px; }}
        p,li {{ color:var(--muted); line-height:1.58; }}
        .metric-grid {{ display:grid; gap:10px; grid-template-columns:repeat(4,minmax(0,1fr)); margin-top:14px; }}
        .metric {{ border:1px solid var(--line); border-radius:16px; padding:12px; background:rgba(255,255,255,.04); }}
        .metric b {{ display:block; font-size:22px; color:var(--ink); }}
        .metric span,.muted {{ color:var(--muted); }}
        .mini {{ font-size:12px; margin-top:4px; }}
        .toolbar {{ display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-top:14px; }}
        a.button, button {{ border:0; border-radius:14px; padding:11px 14px; background:var(--accent); color:#04231b; font-weight:900; text-decoration:none; cursor:pointer; }}
        a.secondary, button.secondary {{ background:#18293a; color:var(--ink); border:1px solid var(--line); }}
        table {{ width:100%; border-collapse:collapse; min-width:980px; }}
        th,td {{ text-align:left; padding:10px 9px; border-bottom:1px solid var(--line); vertical-align:top; font-size:13px; }}
        th {{ color:var(--muted); font-size:12px; white-space:nowrap; }}
        .table-wrap {{ overflow:auto; border:1px solid var(--line); border-radius:16px; background:rgba(8,18,29,.62); }}
        .risk-chip {{ display:inline-flex; margin:2px 4px 2px 0; border-radius:999px; padding:4px 8px; background:rgba(248,201,93,.12); color:#ffe4a3; border:1px solid rgba(248,201,93,.24); font-size:12px; font-weight:800; }}
        a {{ color:#a7f3df; text-decoration:none; }}
        @media (max-width: 980px) {{ .hero,.metric-grid {{ grid-template-columns:1fr; }} .workspace-shell {{ display:block; }} }}
      </style>
    </head>
    <body>
      <div class="workspace-shell">
        <nav class="side-nav">{nav_html}</nav>
        <main class="workspace-main">
          <section class="hero">
            <div class="card">
              <div class="eyebrow">{'Selection Quality Loop' if lang != 'zh' else '选股质量闭环'}</div>
              <h1>{'命中率闭环' if lang == 'zh' else 'Selection Quality'}</h1>
              <p>{html.escape(str(guidance.get('headline_zh' if lang == 'zh' else 'headline_en') or ''))}</p>
              <div class="metric-grid">
                <div class="metric"><span>{'总样本' if lang == 'zh' else 'Samples'}</span><b>{html.escape(str(payload.get('sample_count') or 0))}</b></div>
                <div class="metric"><span>1D Hit</span><b>{_fmt_metric(all_metrics.get('hit_rate_1d_pct'), suffix='%')}</b></div>
                <div class="metric"><span>{'执行命中' if lang == 'zh' else 'Execution Hit'}</span><b>{_fmt_metric(all_metrics.get('execution_hit_rate_pct'), suffix='%')}</b></div>
                <div class="metric"><span>{'平均1D' if lang == 'zh' else 'Avg 1D'}</span><b>{_fmt_metric(all_metrics.get('avg_return_1d_pct'), suffix='%')}</b></div>
              </div>
              <div class="toolbar">
                <a class="button secondary" href="/screeners?lang={lang}">{'返回模型选股' if lang == 'zh' else 'Back to Screeners'}</a>
                <a class="button secondary" href="/screeners/factor-lab?lang={lang}">{'因子实验室' if lang == 'zh' else 'Factor Lab'}</a>
                <form method="post" action="/screeners/selection-quality/refresh">
                  <input type="hidden" name="lang" value="{lang}" />
                  <button type="submit">{'刷新质量快照' if lang == 'zh' else 'Refresh Snapshot'}</button>
                </form>
              </div>
            </div>
            <div class="card">
              <div class="eyebrow">{'使用建议' if lang == 'zh' else 'Playbook'}</div>
              <h2>{'今天怎么用' if lang == 'zh' else 'How to use today'}</h2>
              <ul>{rules_html or '<li>继续累计样本，优先使用多模型共振候选。</li>'}</ul>
              <p class="mini">{'来源' if lang == 'zh' else 'Source'}: {html.escape(str(meta.get('source') or 'live'))} · AI {html.escape(str(source_counts.get('ai_daily_report') or 0))} · Factor {html.escape(str(source_counts.get('factor_experiment') or 0))}</p>
            </div>
          </section>
          <section class="card">
            <div class="section-head">
              <div>
                <div class="eyebrow">{'按来源评估' if lang == 'zh' else 'By Source'}</div>
                <h2>{'谁真的有兑现率' if lang == 'zh' else 'What is really converting'}</h2>
              </div>
            </div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr><th>{'来源' if lang == 'zh' else 'Source'}</th><th>{'有效/总数' if lang == 'zh' else 'Available/Total'}</th><th>1D Hit</th><th>{'执行命中' if lang == 'zh' else 'Execution'}</th><th>{'平均1D' if lang == 'zh' else 'Avg 1D'}</th><th>{'开盘冲高' if lang == 'zh' else 'Open-High'}</th><th>{'开盘回撤' if lang == 'zh' else 'Open-Low'}</th><th>{'高开拦截' if lang == 'zh' else 'Gap Block'}</th></tr>
                </thead>
                <tbody>{source_rows}</tbody>
              </table>
            </div>
          </section>
          <section class="card" style="margin-top:14px;">
            <div class="section-head">
              <div>
                <div class="eyebrow">{'最近样本' if lang == 'zh' else 'Recent Samples'}</div>
                <h2>{'逐票验证记录' if lang == 'zh' else 'Per-name validation records'}</h2>
              </div>
            </div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr><th>{'股票' if lang == 'zh' else 'Ticker'}</th><th>{'市场' if lang == 'zh' else 'Market'}</th><th>{'信号/验证日' if lang == 'zh' else 'Signal/Next'}</th><th>{'来源' if lang == 'zh' else 'Source'}</th><th>{'分数' if lang == 'zh' else 'Score'}</th><th>1D</th><th>{'冲高' if lang == 'zh' else 'High'}</th><th>{'回撤' if lang == 'zh' else 'Low'}</th><th>{'风险' if lang == 'zh' else 'Risk'}</th></tr>
                </thead>
                <tbody>{records_table}</tbody>
              </table>
            </div>
          </section>
        </main>
      </div>
    </body>
    </html>
    """


@router.post("/selection-quality/refresh")
def refresh_selection_quality_snapshot(
    request: Request,
    lang: str = Form("en"),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners/selection-quality")
    lang = resolve_request_lang(request)
    save_selection_quality_snapshot(db=db)
    return RedirectResponse(f"/screeners/selection-quality?lang={lang}", status_code=303)


@router.post("/factor-lab/run")
def run_factor_lab_strategy(
    request: Request,
    lang: str = Form("en"),
    strategy_id: str = Form(...),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners/factor-lab")
    lang = resolve_request_lang(request)
    strategy = get_factor_strategy(db, strategy_id)
    if not strategy:
        return RedirectResponse(_factor_lab_url(lang=lang, message="策略不存在。"), status_code=303)
    source_params = strategy.get("source_params") or {}
    source_snapshot = load_exact_screener_snapshot(source_params, db=db)
    if source_snapshot is None:
        message = "没有找到对应的预计算快照，请先让 screener_precompute job 跑完。" if lang == "zh" else "No matching precomputed snapshot. Please run screener_precompute first."
        return RedirectResponse(_factor_lab_url(lang=lang, message=message, strategy_id=strategy_id), status_code=303)
    source_payload = source_snapshot.get("payload") or {}
    rows = [dict(row) for row in (source_payload.get("rows") or []) if isinstance(row, dict)]
    source_signal_date = str(source_snapshot.get("snapshot_date") or "")[:10]
    if source_signal_date:
        for row in rows:
            row.setdefault("factor_signal_trade_date", source_signal_date)
    try:
        result = run_factor_experiment(
            db,
            strategy_id=strategy_id,
            source_rows=rows,
            source_params=source_params,
            limit=80,
        )
        payload = result.get("payload") or {}
        metrics = payload.get("metrics") or {}
        message = (
            f"实验完成：源样本 {payload.get('source_count', 0)}，命中 {payload.get('matched_count', 0)}，1D 命中率 {_fmt_plain_metric(metrics.get('hit_rate_1d_pct'))}。"
            if lang == "zh"
            else f"Run complete: source {payload.get('source_count', 0)}, matched {payload.get('matched_count', 0)}, 1D hit {_fmt_plain_metric(metrics.get('hit_rate_1d_pct'))}."
        )
    except Exception as exc:
        message = f"实验运行失败：{exc}" if lang == "zh" else f"Experiment failed: {exc}"
    return RedirectResponse(_factor_lab_url(lang=lang, message=message, strategy_id=strategy_id), status_code=303)


def _fmt_plain_metric(value: object) -> str:
    if value in (None, ""):
        return "-"
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return str(value)


@router.get("/factor-lab/runs/{snapshot_id}", response_class=HTMLResponse)
def factor_lab_run_detail_page(
    snapshot_id: int,
    request: Request,
    lang: str = Query("en"),
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect(f"/screeners/factor-lab/runs/{snapshot_id}")
    lang = resolve_request_lang(request)
    snapshot = get_factor_experiment_run(db, snapshot_id)
    if snapshot is None:
        return HTMLResponse(
            f"<h1>{'实验 run 不存在' if lang == 'zh' else 'Run not found'}</h1>",
            status_code=404,
        )
    payload = snapshot.get("payload") or {}
    strategy = payload.get("strategy") or {}
    metrics = payload.get("metrics") or {}
    rows = [row for row in payload.get("rows") or [] if isinstance(row, dict)]
    factor_defs = {item["key"]: item for item in list_factor_definitions()}

    def _factor_bits(row: dict) -> str:
        scores = row.get("factor_scores") if isinstance(row.get("factor_scores"), dict) else {}
        values = row.get("factor_values") if isinstance(row.get("factor_values"), dict) else {}
        bits: list[str] = []
        for key, score in sorted(scores.items(), key=lambda pair: -float(pair[1] or -1))[:4]:
            label = _factor_label(factor_defs.get(key, {"key": key}), lang)
            value = values.get(key)
            rendered_value = "-" if value in (None, "") else str(value)
            bits.append(
                f"<span class='chip'>{html.escape(label)} {_fmt_metric(score)} · {html.escape(rendered_value[:22])}</span>"
            )
        return "".join(bits) or "<span class='muted'>-</span>"

    def _outcome_cell(outcome: dict, key: str) -> str:
        value = outcome.get(key) if isinstance(outcome, dict) else None
        if value in (None, ""):
            return "<span class='muted'>待行情</span>" if lang == "zh" else "<span class='muted'>pending</span>"
        try:
            numeric = float(value)
            cls = "pos" if numeric > 0 else "neg" if numeric < 0 else "flat"
            return f"<span class='{cls}'>{numeric:.2f}%</span>"
        except (TypeError, ValueError):
            return html.escape(str(value))

    row_html = "".join(
        f"""
        <tr>
          <td class="sticky-col"><a class="ticker-link" href="/insights/{html.escape(str(row.get('ticker') or ''), quote=True)}?lang={lang}">{html.escape(str(row.get('ticker') or '-'))}</a><div class="muted mini">{html.escape(str(row.get('name') or '-'))}</div></td>
          <td>{html.escape(str(row.get('market') or '-'))}</td>
          <td><strong>{_fmt_metric(row.get('factor_score'))}</strong><div class="muted mini">Trend {_fmt_plain_number(row.get('trend_score'))}</div></td>
          <td>{_factor_bits(row)}</td>
          <td>{html.escape(str((row.get('forward_outcome') or {}).get('trade_date') or row.get('factor_signal_trade_date') or '-'))}</td>
          <td>{_outcome_cell(row.get('forward_outcome') or {}, 'next_open_gap_pct')}</td>
          <td>{_outcome_cell(row.get('forward_outcome') or {}, 'return_1d_pct')}</td>
          <td>{_outcome_cell(row.get('forward_outcome') or {}, 'return_3d_pct')}</td>
          <td>{_outcome_cell(row.get('forward_outcome') or {}, 'return_5d_pct')}</td>
          <td>{_outcome_cell(row.get('forward_outcome') or {}, 'max_drawdown_5d_pct')}</td>
          <td>{'是' if (row.get('forward_outcome') or {}).get('gap_unbuyable') and lang == 'zh' else ('Yes' if (row.get('forward_outcome') or {}).get('gap_unbuyable') else ('否' if lang == 'zh' else 'No'))}</td>
        </tr>
        """
        for row in rows
    ) or f"<tr><td colspan='11' class='muted'>{'这个 run 没有候选明细。' if lang == 'zh' else 'This run has no row details.'}</td></tr>"

    nav_html = render_workspace_nav_html(lang=lang, active_key="screeners")
    refreshed_from = payload.get("refreshed_from_snapshot_id")
    refreshed_note = (
        f"<span class='pill'>{'刷新自' if lang == 'zh' else 'Refreshed from'} #{html.escape(str(refreshed_from))}</span>"
        if refreshed_from
        else ""
    )
    return f"""
    <!doctype html>
    <html lang="{lang}">
    <head>
      <meta charset="utf-8" />
      <meta name="viewport" content="width=device-width, initial-scale=1" />
      <title>{'因子实验详情' if lang == 'zh' else 'Factor Run Detail'}</title>
      <style>
        {WORKSPACE_SIDEBAR_STYLE}
        {WORKSPACE_COMPACT_STYLE}
        :root {{ --bg:#07111b; --panel:#0d1824; --line:#213447; --ink:#edf5f2; --muted:#8da1ad; --accent:#35e0b4; --warn:#f8c95d; --bad:#fb7185; --good:#6ee7b7; }}
        body {{ margin:0; color:var(--ink); background:radial-gradient(circle at 16% 0%, rgba(53,224,180,.13), transparent 30%), #07111b; font-family:ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
        .workspace-shell {{ gap:12px; }}
        .workspace-main {{ max-width:1500px; }}
        .card {{ background:linear-gradient(180deg, rgba(17,31,45,.96), rgba(10,20,31,.96)); border:1px solid var(--line); border-radius:22px; padding:18px; box-shadow:0 18px 40px rgba(0,0,0,.22); margin-bottom:14px; }}
        .section-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:12px; }}
        .eyebrow {{ color:var(--accent); font-size:12px; font-weight:900; letter-spacing:.12em; text-transform:uppercase; }}
        h1,h2 {{ margin:0; }}
        h1 {{ font-size:32px; letter-spacing:-.04em; }}
        p {{ color:var(--muted); line-height:1.6; margin:8px 0 0; }}
        .toolbar {{ display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-top:14px; }}
        a.button, button {{ border:0; border-radius:14px; padding:10px 13px; background:var(--accent); color:#04231b; font-weight:900; text-decoration:none; cursor:pointer; }}
        a.secondary, button.secondary {{ background:#18293a; color:var(--ink); border:1px solid var(--line); }}
        .metric-grid {{ display:grid; gap:10px; grid-template-columns:repeat(auto-fit, minmax(160px, 1fr)); margin-top:14px; }}
        .metric {{ border:1px solid rgba(255,255,255,.07); background:rgba(8,18,29,.62); border-radius:16px; padding:12px; }}
        .metric span {{ color:var(--muted); font-size:12px; }}
        .metric strong {{ display:block; font-size:22px; margin-top:5px; }}
        .muted {{ color:var(--muted); }}
        .mini {{ font-size:12px; margin-top:4px; }}
        .pill,.chip {{ display:inline-flex; align-items:center; gap:6px; border-radius:999px; padding:6px 10px; background:rgba(53,224,180,.10); color:var(--accent); font-weight:800; font-size:12px; border:1px solid rgba(53,224,180,.20); margin:3px 4px 3px 0; }}
        .chip {{ color:#dffdf5; background:rgba(255,255,255,.06); border-color:rgba(255,255,255,.09); }}
        .table-wrap {{ overflow:auto; border:1px solid var(--line); border-radius:16px; background:rgba(8,18,29,.62); }}
        table {{ width:100%; border-collapse:collapse; min-width:1260px; }}
        th,td {{ text-align:left; padding:10px 9px; border-bottom:1px solid var(--line); vertical-align:top; font-size:13px; }}
        th {{ color:var(--muted); font-size:12px; white-space:nowrap; }}
        .sticky-col {{ position:sticky; left:0; z-index:2; background:#0d1824; min-width:150px; box-shadow:10px 0 16px rgba(0,0,0,.12); }}
        th.sticky-col {{ z-index:4; }}
        .ticker-link {{ color:var(--accent); font-weight:900; text-decoration:none; }}
        .pos {{ color:var(--good); font-weight:900; }}
        .neg {{ color:var(--bad); font-weight:900; }}
        .flat {{ color:var(--muted); font-weight:900; }}
      </style>
    </head>
    <body>
      <div class="workspace-shell">
        <nav class="side-nav">{nav_html}</nav>
        <main class="workspace-main">
          <section class="card">
            <div class="section-head">
              <div>
                <div class="eyebrow">{'因子实验 Run' if lang == 'zh' else 'Factor Experiment Run'}</div>
                <h1>{html.escape(str(strategy.get('name') or '-'))}</h1>
                <p>{html.escape(str(strategy.get('description') or ''))}</p>
                <div class="toolbar">
                  <a class="button secondary" href="/screeners/factor-lab?lang={lang}&strategy_id={html.escape(str(strategy.get('id') or ''), quote=True)}">{'返回因子实验室' if lang == 'zh' else 'Back to Factor Lab'}</a>
                  <form method="post" action="/screeners/factor-lab/runs/{snapshot_id}/refresh">
                    <input type="hidden" name="lang" value="{lang}" />
                    <button type="submit">{'用最新行情刷新表现' if lang == 'zh' else 'Refresh Outcomes'}</button>
                  </form>
                  {refreshed_note}
                </div>
              </div>
              <span class="pill">#{snapshot_id}</span>
            </div>
            <div class="metric-grid">
              <div class="metric"><span>{'源样本' if lang == 'zh' else 'Source'}</span><strong>{html.escape(str(payload.get('source_count') or 0))}</strong></div>
              <div class="metric"><span>{'命中' if lang == 'zh' else 'Matched'}</span><strong>{html.escape(str(payload.get('matched_count') or 0))}</strong></div>
              <div class="metric"><span>1D Hit</span><strong>{_fmt_metric(metrics.get('hit_rate_1d_pct'), suffix='%')}</strong></div>
              <div class="metric"><span>3D Hit</span><strong>{_fmt_metric(metrics.get('hit_rate_3d_pct'), suffix='%')}</strong></div>
              <div class="metric"><span>5D Hit</span><strong>{_fmt_metric(metrics.get('hit_rate_5d_pct'), suffix='%')}</strong></div>
              <div class="metric"><span>{'高开买不到' if lang == 'zh' else 'Gap blocked'}</span><strong>{_fmt_metric(metrics.get('gap_unbuyable_rate_pct'), suffix='%')}</strong></div>
            </div>
          </section>
          <section class="card">
            <div class="section-head">
              <div>
                <div class="eyebrow">{'候选明细' if lang == 'zh' else 'Candidate Details'}</div>
                <h2>{'看每只股票为什么入选，以及后续真实表现' if lang == 'zh' else 'Why each name passed and how it performed afterwards'}</h2>
              </div>
            </div>
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th class="sticky-col">{'股票' if lang == 'zh' else 'Ticker'}</th>
                    <th>{'市场' if lang == 'zh' else 'Market'}</th>
                    <th>{'因子分' if lang == 'zh' else 'Factor Score'}</th>
                    <th>{'主要因子' if lang == 'zh' else 'Top Factors'}</th>
                    <th>{'信号日' if lang == 'zh' else 'Signal Date'}</th>
                    <th>{'次日高开' if lang == 'zh' else 'Next Gap'}</th>
                    <th>1D</th>
                    <th>3D</th>
                    <th>5D</th>
                    <th>5D DD</th>
                    <th>{'买不到' if lang == 'zh' else 'Blocked'}</th>
                  </tr>
                </thead>
                <tbody>{row_html}</tbody>
              </table>
            </div>
          </section>
        </main>
      </div>
    </body>
    </html>
    """


def _fmt_plain_number(value: object) -> str:
    if value in (None, ""):
        return "-"
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return str(value)


@router.post("/factor-lab/runs/{snapshot_id}/refresh")
def refresh_factor_lab_run(
    snapshot_id: int,
    request: Request,
    lang: str = Form("en"),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect(f"/screeners/factor-lab/runs/{snapshot_id}")
    lang = resolve_request_lang(request)
    try:
        result = refresh_factor_experiment_run(db, snapshot_id)
        refreshed_id = result["snapshot"].id
        return RedirectResponse(
            f"/screeners/factor-lab/runs/{refreshed_id}?lang={lang}",
            status_code=303,
        )
    except Exception as exc:
        message = f"刷新失败：{exc}" if lang == "zh" else f"Refresh failed: {exc}"
        return RedirectResponse(_factor_lab_url(lang=lang, message=message), status_code=303)


@router.post("/factor-lab/save")
def save_factor_lab_strategy(
    request: Request,
    lang: str = Form("en"),
    base_strategy_id: str = Form(...),
    name: str = Form(...),
    min_readiness: float = Form(58.0),
    min_trend: float = Form(58.0),
    min_profit_yoy: float = Form(15.0),
    readiness_weight: float = Form(22.0),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners/factor-lab")
    lang = resolve_request_lang(request)
    base = get_factor_strategy(db, base_strategy_id)
    if not base:
        return RedirectResponse(_factor_lab_url(lang=lang, message="基础策略不存在。"), status_code=303)
    strategy = json.loads(json.dumps(base, ensure_ascii=False))
    strategy["id"] = ""
    strategy["name"] = name.strip() or str(base.get("name") or "Factor strategy")
    strategy["filters"] = [
        item for item in (strategy.get("filters") or [])
        if str(item.get("factor_key") or "") not in {"trend_score", "trade_readiness_score", "net_profit_yoy"}
    ]
    strategy["filters"].extend(
        [
            {"factor_key": "trend_score", "op": "gte", "value": float(min_trend), "required": True},
            {"factor_key": "trade_readiness_score", "op": "gte", "value": float(min_readiness), "required": True},
            {"factor_key": "net_profit_yoy", "op": "gte", "value": float(min_profit_yoy), "required": False},
        ]
    )
    weights = dict(strategy.get("weights") or {})
    weights["trade_readiness_score"] = float(readiness_weight)
    strategy["weights"] = weights
    saved = save_factor_strategy(db, strategy)
    message = "策略已保存，可以直接运行实验。" if lang == "zh" else "Strategy saved. You can run it now."
    return RedirectResponse(_factor_lab_url(lang=lang, message=message, strategy_id=str(saved.get("id") or "")), status_code=303)


@router.get("", response_class=HTMLResponse)
def screener_page(
    request: Request,
    message: str | None = Query(None),
    lang: str = Query("en"),
    model_template: str = Query("technical_momentum"),
    multi_model_templates: list[str] = Query([]),
    min_multi_model_hits: int = Query(2),
    confluence_action_filter: str = Query("ALL"),
    strategy_profile: str = Query(""),
    universe: str = Query("watchlist"),
    market: str = Query("ALL"),
    min_trend_score: int = Query(60),
    action_filter: str = Query("ALL"),
    min_volume_ratio: float = Query(0.0),
    min_listing_days: int = Query(365),
    pe_min: float = Query(0.0),
    pe_max: float = Query(30.0),
    min_roe_avg_3y: float = Query(12.0),
    min_net_profit_yoy: float = Query(20.0),
    min_revenue_yoy: float = Query(0.0),
    max_debt_to_assets: float = Query(100.0),
    min_dividend_yield: float = Query(0.0),
    exclude_bottom_market_cap_pct: float = Query(10.0),
    recent_snapshot_runs: int = Query(0),
    min_snapshot_hits: int = Query(0),
    model_signal_filter: str = Query("ALL"),
    min_model_signal_strength: float = Query(0.0),
    execution_tag_filter: str = Query("ALL"),
    exclude_execution_tag_filter: str = Query("ALL"),
    sort_by: str = Query("default"),
    sort_order: str = Query("desc"),
    show_evaluation: int = Query(0),
    show_details: int = Query(0),
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/screeners")
    lang = resolve_request_lang(request)

    service = ScreenerService()
    saved_presets = _load_saved_presets(db)
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)
    current_params = _current_params(
        lang=lang,
        model_template=model_template,
        multi_model_templates=multi_model_templates,
        min_multi_model_hits=min_multi_model_hits,
        confluence_action_filter=confluence_action_filter,
        strategy_profile=strategy_profile,
        universe=universe,
        market=market,
        min_trend_score=min_trend_score,
        action_filter=action_filter,
        min_volume_ratio=min_volume_ratio,
        min_listing_days=min_listing_days,
        pe_min=pe_min,
        pe_max=pe_max,
        min_roe_avg_3y=min_roe_avg_3y,
        min_net_profit_yoy=min_net_profit_yoy,
        min_revenue_yoy=min_revenue_yoy,
        max_debt_to_assets=max_debt_to_assets,
        min_dividend_yield=min_dividend_yield,
        exclude_bottom_market_cap_pct=exclude_bottom_market_cap_pct,
        recent_snapshot_runs=recent_snapshot_runs,
        min_snapshot_hits=min_snapshot_hits,
        model_signal_filter=model_signal_filter,
        min_model_signal_strength=min_model_signal_strength,
        execution_tag_filter=execution_tag_filter,
        exclude_execution_tag_filter=exclude_execution_tag_filter,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    should_execute = _should_execute_screen(request)
    normalized_current_params = _normalize_screen_params(current_params)
    multi_templates_active = normalized_current_params.get("multi_model_templates") or []
    multi_screen_meta = {"available_templates": [], "missing_templates": []}
    if should_execute and len(multi_templates_active) >= 2:
        results, snapshot_ready, multi_screen_meta = _run_multi_screen(service, normalized_current_params)
    else:
        snapshot_ready = _screen_snapshot_ready(service, current_params) if should_execute else True
        results = _run_screen(service, current_params) if should_execute else []
    results = _apply_quality_confluence_profile(
        results,
        profile=str(normalized_current_params.get("strategy_profile") or ""),
    )
    if results and (model_template == "lightgbm_top_picks" or "lightgbm_top_picks" in multi_templates_active):
        _annotate_lightgbm_results(
            results,
            selected_market=market,
            lang=lang,
            force_apply=(model_template == "lightgbm_top_picks"),
        )
        if len(multi_templates_active) >= 2 and sort_by in {"default", "confluence_rank"}:
            results = _rerank_with_lightgbm_tactical_signal(
                results,
                confluence_action_filter=confluence_action_filter,
            )
    if results:
        annotate_rows_with_kronos(results, db=db)
        if sort_by == "kronos_score":
            results = sorted(
                results,
                key=lambda item: _kronos_sort_value(item),
                reverse=(sort_order != "asc"),
            )
    total_results = len(results)
    detail_rows_enabled = bool(int(show_details or 0))
    visible_results = results[:60]
    risk_counts: dict[str, int] = {}
    risk_examples: list[dict[str, object]] = []
    tagged_names = 0
    for item in visible_results:
        tags = [str(tag).strip() for tag in (item.get("model_execution_tags") or []) if str(tag).strip()]
        if not tags:
            continue
        tagged_names += 1
        for tag in tags:
            risk_counts[tag] = risk_counts.get(tag, 0) + 1
        risk_examples.append({"ticker": item.get("ticker"), "tags": tags[:2]})
    risk_examples = risk_examples[:3]
    risk_top_tags = sorted(risk_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:3]
    try:
        recommendation_regression = load_or_build_recommendation_regression(db=db)
    except Exception:
        recommendation_regression = {}
    regression_guidance = summarize_recommendation_regression(recommendation_regression, lang=lang)
    regression_policy = (recommendation_regression or {}).get("policy") or {}
    status_counts = {"ready": 0, "do_not_chase": 0, "blocked": 0, "review": 0, "low_readiness": 0}
    for item in visible_results:
        status = str(item.get("tradability_status") or "").strip().upper()
        bucket = str(item.get("readiness_bucket") or "").strip().upper()
        try:
            readiness = float(item.get("trade_readiness_score") or 0.0)
        except (TypeError, ValueError):
            readiness = 0.0
        if status == "READY":
            status_counts["ready"] += 1
        elif status == "DO_NOT_CHASE":
            status_counts["do_not_chase"] += 1
        elif status == "BLOCKED":
            status_counts["blocked"] += 1
        else:
            status_counts["review"] += 1
        if bucket in {"LOW", "BLOCKED"} or (readiness and readiness < 60.0):
            status_counts["low_readiness"] += 1

    def _screener_regression_discipline_html() -> str:
        sample_count = int((recommendation_regression or {}).get("sample_count") or 0)
        if sample_count <= 0 and not visible_results:
            return ""
        min_quality = regression_policy.get("min_actionable_quality_score")
        max_actionable = regression_policy.get("max_actionable_count")
        metrics = regression_guidance.get("metrics") or []
        rules = [str(item).strip() for item in (regression_guidance.get("rules") or []) if str(item).strip()]
        warnings = [str(item).strip() for item in (regression_guidance.get("warnings") or []) if str(item).strip()]
        if lang == "zh":
            title = "手动筛选纪律"
            subtitle = "这里不强行删掉筛选结果，而是提醒你哪些票只能观察，哪些才值得进入明日盯盘。"
            current_stats = [
                ("当前展示", f"{len(visible_results)} 只"),
                ("READY", f"{status_counts['ready']} 只"),
                ("不要追高", f"{status_counts['do_not_chase']} 只"),
                ("阻断", f"{status_counts['blocked']} 只"),
                ("低就绪", f"{status_counts['low_readiness']} 只"),
                ("质量门槛", str(min_quality) if min_quality is not None else "常规"),
                ("最多可执行", f"{max_actionable} 只" if max_actionable is not None else "不额外限制"),
            ]
            workflow = "使用建议：先按模型/共振筛出候选，再只把 READY + 接近买点 + 无硬风险标签的股票加入重点池；其余放观察池等二次确认。"
        else:
            title = "Manual Screening Discipline"
            subtitle = "The screener keeps results visible, but tells you which names should stay on watch versus move into the next-session focus list."
            current_stats = [
                ("Visible", f"{len(visible_results)}"),
                ("READY", f"{status_counts['ready']}"),
                ("Do not chase", f"{status_counts['do_not_chase']}"),
                ("Blocked", f"{status_counts['blocked']}"),
                ("Low readiness", f"{status_counts['low_readiness']}"),
                ("Quality gate", str(min_quality) if min_quality is not None else "normal"),
                ("Max actionable", str(max_actionable) if max_actionable is not None else "no extra cap"),
            ]
            workflow = "Workflow: screen for model/confluence candidates first, then only move READY names near buy zones with no hard risk tags into the focus pool."
        stat_html = "".join(
            f"<span class='default-chip'>{html.escape(label)}: {html.escape(str(value))}</span>"
            for label, value in current_stats
        )
        metric_html = "".join(
            f"<span class='default-chip'>{html.escape(str(item.get('label') or '-'))}: {html.escape(str(item.get('value') or '-'))}</span>"
            for item in metrics[:4]
            if isinstance(item, dict)
        )
        rules_html = "".join(f"<div class='muted'>• {html.escape(item)}</div>" for item in (warnings[:3] or rules[:3])) or "<div class='muted'>-</div>"
        return (
            "<article class='card' style='background:linear-gradient(180deg, rgba(17,28,40,0.98), rgba(12,21,32,0.96));border-color:rgba(246,200,95,0.28);'>"
            f"<div class='eyebrow'>{html.escape(title)}</div>"
            f"<div style='font-size:18px;font-weight:900;color:var(--ink);margin-bottom:6px;'>{html.escape(str(regression_guidance.get('headline') or title))}</div>"
            f"<div class='muted'>{html.escape(subtitle)}</div>"
            f"<div style='display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;'>{stat_html}</div>"
            f"<div style='display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;'>{metric_html}</div>"
            f"<div class='summary-note' style='margin-top:12px;'>{html.escape(workflow)}</div>"
            f"<div class='detail-card' style='margin-top:12px;background:rgba(11,19,29,0.82);border-color:rgba(255,255,255,0.06);'>{rules_html}</div>"
            "</article>"
        )

    regression_discipline_html = _screener_regression_discipline_html()
    quality_profile_status_html = ""
    if str(normalized_current_params.get("strategy_profile") or "") == "quality_confluence_v1":
        market_context = load_market_context_snapshot(db, market="CN")
        regime = str(market_context.get("regime") or "unknown")
        breadth = market_context.get("breadth_pct")
        paused = not visible_results
        title = "A 级质量策略已暂停开仓" if paused else "A 级质量策略候选"
        detail = (
            "当前市场处于防御/低广度状态，所有共振票只保留观察，不强行凑单。"
            if paused else "以下候选已通过双模型、市场状态及可成交性硬门槛。"
        )
        if lang != "zh":
            title = "A-tier quality strategy: no new entries" if paused else "A-tier quality candidates"
            detail = (
                "The market gate is defensive or breadth is weak; confluence names remain watch-only."
                if paused else "These names passed the confluence, market-state, and tradability gates."
            )
        quality_profile_status_html = (
            "<article class='card' style='border-color:rgba(246,200,95,0.32);'>"
            f"<div class='eyebrow'>{'Quality Confluence · 30 snapshots'}</div>"
            f"<div style='font-size:18px;font-weight:900;color:var(--ink);margin-bottom:6px;'>{html.escape(title)}</div>"
            f"<div class='muted'>{html.escape(detail)}</div>"
            "<div style='display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;'>"
            f"<span class='default-chip'>Market: {html.escape(regime)}</span>"
            f"<span class='default-chip'>Breadth: {html.escape(str(breadth if breadth is not None else '-'))}%</span>"
            f"<span class='default-chip'>A-tier: {len(visible_results)}</span>"
            "</div></article>"
        )
    if sort_by == "watchlist_state":
        reverse = sort_order != "asc"
        visible_results = sorted(
            visible_results,
            key=lambda item: (
                _sync_state_rank(watchlist_map.get(item["ticker"])),
                item.get("ticker", ""),
            ),
            reverse=reverse,
        )
    active_template = MODEL_TEMPLATES.get(model_template, MODEL_TEMPLATES["technical_momentum"])
    active_multi_labels = [
        _template_label(key, MODEL_TEMPLATES[key]["label"], lang)
        for key in multi_templates_active
        if key in MODEL_TEMPLATES
    ]
    confluence_option_html = "".join(
        f"<option value='{value}' {'selected' if confluence_action_filter == value else ''}>{labels[lang]}</option>"
        for value, labels in CONFLUENCE_ACTION_OPTIONS
    )
    confluence_filter_label = next(
        (labels[lang] for value, labels in CONFLUENCE_ACTION_OPTIONS if value == confluence_action_filter),
        confluence_action_filter,
    )
    def header_link(label: str, field: str) -> str:
        next_order = "asc" if sort_by == field and sort_order == "desc" else "desc"
        params = dict(current_params)
        params["sort_by"] = field
        params["sort_order"] = next_order
        indicator = ""
        if sort_by == field:
            indicator = " ↑" if sort_order == "asc" else " ↓"
        return f"<a href='{_build_screen_query(params)}'>{label}{indicator}</a>"

    universe_options = [
        ("watchlist", "My Watchlist"),
        ("synced", "Synced Stocks"),
        ("full_market", "Full Market"),
    ]
    market_options = [
        ("ALL", "All Markets"),
        ("US", "US"),
        ("CN", "A-Shares"),
        ("HK", "Hong Kong"),
    ]
    template_option_html = "".join(
        f"<option value='{value}' {'selected' if model_template == value else ''}>{_template_label(value, config['label'], lang)}</option>"
        for value, config in MODEL_TEMPLATES.items()
    )

    action_option_html = "".join(
        f"<option value='{value}' {'selected' if action_filter == value else ''}>{label}</option>"
        for value, label in ACTION_OPTIONS
    )
    signal_option_html = "".join(
        f"<option value='{value}' {'selected' if model_signal_filter == value else ''}>{label_map[lang]}</option>"
        for value, label_map in MODEL_SIGNAL_OPTIONS
    )
    universe_option_html = "".join(
        f"<option value='{value}' {'selected' if universe == value else ''}>{label}</option>"
        for value, label in universe_options
    )
    market_option_html = "".join(
        f"<option value='{value}' {'selected' if market == value else ''}>{label}</option>"
        for value, label in market_options
    )
    template_groups = _template_groups_for_display(market, lang)
    sort_by_option_html = "".join(
        f"<option value='{value}' {'selected' if sort_by == value else ''}>{labels[lang]}</option>"
        for value, labels in SORT_BY_OPTIONS
    )
    sort_order_option_html = "".join(
        f"<option value='{value}' {'selected' if sort_order == value else ''}>{labels[lang]}</option>"
        for value, labels in SORT_ORDER_OPTIONS
    )
    template_option_html = "".join(
        (
            f"<optgroup label='{html.escape(meta['title'])}'>"
            + "".join(
                f"<option value='{value}' {'selected' if model_template == value else ''}>"
                f"{html.escape(_template_label(value, config['label'], lang))}</option>"
                for value, config in items
            )
            + "</optgroup>"
        )
        for _, meta, items in template_groups
    )
    template_cards_html = "".join(
        (
            "<div style='margin-bottom:18px;'>"
            f"<div style='display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:10px;'>"
            f"<div style='font-size:15px;font-weight:900;color:var(--ink);'>{html.escape(meta['title'])}</div>"
            f"<div class='muted' style='font-size:12px;'>{html.escape(meta['hint'])}</div>"
            "</div>"
            "<div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;'>"
            + "".join(
                f"<a class='template-card{' active' if value == model_template else ''}' href='{_build_screen_query({**current_params, 'model_template': value, 'market': config.get('market') or market})}'>"
                f"<div class='template-top'><span class='template-mode'>{config.get('mode', 'mixed')}</span><span class='template-market'>{html.escape(meta['badge'])}</span></div>"
                f"<div class='template-title'>{_template_label(value, config['label'], lang)}</div>"
                f"<div class='template-desc'>{config.get('description') or ''}</div>"
                "</a>"
                for value, config in items
            )
            + "</div></div>"
        )
        for _, meta, items in template_groups
    )
    multi_template_picker_html = "".join(
        (
            "<div style='margin-bottom:12px;'>"
            f"<div style='font-size:13px;font-weight:800;color:var(--ink);margin-bottom:6px;'>{html.escape(meta['title'])}</div>"
            f"<div class='muted' style='margin-bottom:8px;font-size:12px;'>{html.escape(meta['hint'])}</div>"
            "<div style='display:flex;flex-wrap:wrap;gap:8px;'>"
            + "".join(
                "<label class='multi-template-chip'>"
                f"<input type='checkbox' name='multi_model_templates' value='{value}' {'checked' if value in multi_templates_active else ''} />"
                f"<span>{_template_label(value, config['label'], lang)}</span>"
                "</label>"
                for value, config in items
            )
            + "</div></div>"
        )
        for _, meta, items in template_groups
    )
    active_defaults = active_template.get("defaults") or {}
    active_defaults_html = "".join(
        f"<span class='default-chip'>{key}: {value}</span>"
        for key, value in active_defaults.items()
    ) or f"<span class='default-chip'>{'No template defaults' if lang == 'en' else '无模板默认值'}</span>"
    evaluation_href = _build_screen_query({**current_params, "show_evaluation": 1})
    evaluation_overview_href = f"/dashboard/model-performance?lang={lang}&market={market if market in {'CN', 'US', 'ALL'} else 'ALL'}"
    detail_rows_href = _build_screen_query({**current_params, "show_details": 1})
    evaluation_cta_html = (
        "<article class='card' style='background:#f7faf8;border-color:#dce8e1;'>"
        f"<div class='eyebrow'>{'模型评测' if lang == 'zh' else 'Model Evaluation'}</div>"
        f"<div class='muted'>{'为保证全市场筛选秒开，默认不在本页加载完整评测。需要时再展开当前模板评测，或打开模型评测总览。' if lang == 'zh' else 'To keep full-market screening fast, the full evaluation panel is lazy-loaded. Open this template evaluation only when needed, or use the model overview.'}</div>"
        "<div style='display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;'>"
        f"<a class='default-chip' href='{evaluation_href}'>{'展开本模型评测' if lang == 'zh' else 'Open this template evaluation'}</a>"
        f"<a class='default-chip' href='{evaluation_overview_href}'>{'模型评测总览' if lang == 'zh' else 'Model Evaluation Overview'}</a>"
        "</div>"
        "</article>"
    )
    template_read_html = _template_interpretation_card(
        model_template=model_template,
        results=results if should_execute else [],
        lang=lang,
    )
    if show_evaluation:
        template_overview_brief_html = _template_overview_brief_html(
            model_template=model_template,
            market=market,
            lang=lang,
        )
    else:
        template_overview_brief_html = evaluation_cta_html
    lightgbm_bias_bar_html = _lightgbm_execution_bias_bar(
        market=market,
        lang=lang,
    ) if model_template == "lightgbm_top_picks" else ""
    template_evaluation_html = (
        _next_tesla_evaluation_card(
            market=market,
            lang=lang,
        ) if model_template == "next_tesla_swing" else _technical_momentum_evaluation_card(
            market=market,
            lang=lang,
        ) if model_template == "technical_momentum" else _lightgbm_evaluation_card(
            market=market,
            lang=lang,
        ) if model_template == "lightgbm_top_picks" else _technical_pattern_evaluation_card(
            model_template=model_template,
            market=market,
            lang=lang,
        ) if model_template in PATTERN_EVALUATION_TEMPLATES else ""
    ) if show_evaluation else ""
    multi_template_summary_html = ""
    if len(multi_templates_active) >= 2:
        available_multi_labels = [
            _template_label(key, MODEL_TEMPLATES[key]["label"], lang)
            for key in (multi_screen_meta.get("available_templates") or [])
            if key in MODEL_TEMPLATES
        ]
        missing_multi_labels = [
            _template_label(key, MODEL_TEMPLATES[key]["label"], lang)
            for key in (multi_screen_meta.get("missing_templates") or [])
            if key in MODEL_TEMPLATES
        ]
        if lang == "zh":
            summary_title = "多模型共振筛选"
            summary_text = (
                f"当前勾选 {len(active_multi_labels)} 个模型，要求至少 {min_multi_model_hits} 个模型同时命中。"
            )
            if confluence_action_filter not in {"", "ALL", "all"}:
                summary_text += f" 当前只保留“{confluence_filter_label}”这一类共振动作。"
            availability_text = (
                f"已参与聚合：{' / '.join(available_multi_labels) if available_multi_labels else '暂无'}；"
                f"待快照：{' / '.join(missing_multi_labels) if missing_multi_labels else '无'}。"
            )
        else:
            summary_title = "Multi-model Confluence"
            summary_text = (
                f"{len(active_multi_labels)} templates selected, requiring at least {min_multi_model_hits} hits on the same ticker."
            )
            if confluence_action_filter not in {"", "ALL", "all"}:
                summary_text += f" Filtered to the “{confluence_filter_label}” confluence bucket."
            availability_text = (
                f"Used now: {' / '.join(available_multi_labels) if available_multi_labels else 'none'}; "
                f"pending snapshots: {' / '.join(missing_multi_labels) if missing_multi_labels else 'none'}."
            )
        multi_template_summary_html = (
            "<article class='card' style='background:#f6f8f7;border-color:#d9e5df;'>"
            f"<div class='eyebrow'>{summary_title}</div>"
            f"<div style='display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px;'>"
            + "".join(f"<span class='default-chip'>{html.escape(label)}</span>" for label in active_multi_labels)
            + "</div>"
            f"<div style='color:#334155;line-height:1.6;'>{summary_text}</div>"
            f"<div class='muted' style='margin-top:8px;'>{availability_text}</div>"
            "</article>"
        )
    confluence_strength_counts: dict[int, int] = {}
    for item in results:
        try:
            hit_count = int(item.get("model_hit_count") or 0)
        except (TypeError, ValueError):
            hit_count = 0
        if hit_count > 0:
            confluence_strength_counts[hit_count] = confluence_strength_counts.get(hit_count, 0) + 1
    confluence_strength_html = ""
    if len(multi_templates_active) >= 2 and confluence_strength_counts:
        chips = "".join(
            f"<span class='default-chip'>{count} {'只' if lang == 'zh' else 'names'} · {hit} {'模型共振' if lang == 'zh' else 'model hits'}</span>"
            for hit, count in sorted(confluence_strength_counts.items(), reverse=True)
        )
        confluence_strength_html = (
            "<article class='card' style='background:#f8fbff;border-color:#d7e7f8;'>"
            f"<div class='eyebrow'>{'共振强度' if lang == 'zh' else 'Confluence Strength'}</div>"
            f"<div style='display:flex;flex-wrap:wrap;gap:8px;'>{chips}</div>"
            "</article>"
        )
    confluence_leaderboard_html = ""
    if len(multi_templates_active) >= 2 and results:
        leaderboard_rows = []
        for index, item in enumerate(results[:8], start=1):
            ticker = str(item.get("ticker") or "-")
            display_name = str(item.get("name") or ticker)
            model_hits = int(item.get("model_hit_count") or 0)
            alignment_hits = int(item.get("confluence_alignment_count") or 0)
            tactical_tag = str(item.get("lightgbm_tactical_tag") or "").strip()
            matched_templates = [
                _template_label(key, MODEL_TEMPLATES[key]["label"], lang)
                for key in (item.get("matched_model_templates") or [])
                if key in MODEL_TEMPLATES
            ]
            matched_template_text = " / ".join(matched_templates[:3]) or ("未识别模型" if lang == "zh" else "No mapped templates")
            bucket_text = " / ".join(
                _confluence_bucket_label(bucket, lang) for bucket in (item.get("matched_action_buckets") or [])[:3]
            ) or ("未归类" if lang == "zh" else "Unbucketed")
            tactical_html = (
                "<div style='margin-top:8px;'>"
                "<span style='display:inline-flex;align-items:center;padding:5px 10px;border-radius:999px;"
                "background:#e6f4f1;color:#0f766e;font-weight:800;font-size:12px;'>"
                f"{html.escape(tactical_tag)}</span>"
                "</div>"
                if tactical_tag
                else ""
            )
            leaderboard_rows.append(
                "<div class='detail-card' style='background:#f8fbff;'>"
                f"<div style='display:flex;justify-content:space-between;gap:12px;align-items:flex-start;'>"
                f"<div><div class='detail-label'>#{index} · {ticker}</div>"
                f"<div style='font-size:18px;font-weight:800;color:#0f172a;margin-top:4px;'>{html.escape(display_name)}</div>"
                f"<div class='muted' style='margin-top:6px;'>{html.escape(matched_template_text)}</div>"
                f"<div class='muted' style='margin-top:4px;'>{html.escape(bucket_text)}</div>"
                f"{tactical_html}</div>"
                f"<div style='text-align:right;min-width:112px;'>"
                f"<div style='font-size:24px;font-weight:800;color:#0f172a;'>{model_hits}</div>"
                f"<div class='muted'>{'模型命中' if lang == 'zh' else 'model hits'}</div>"
                f"<div style='margin-top:8px;font-size:15px;font-weight:700;color:#0f172a;'>{alignment_hits}</div>"
                f"<div class='muted'>{'动作一致' if lang == 'zh' else 'aligned'}</div>"
                "</div></div>"
                "</div>"
            )
        confluence_leaderboard_html = (
            "<article class='card' style='background:#f8fbff;border-color:#d7e7f8;'>"
            f"<div class='eyebrow'>{'共振排行榜' if lang == 'zh' else 'Confluence Leaderboard'}</div>"
            f"<div class='muted' style='margin-bottom:12px;'>{'优先展示同时被更多模型命中、且动作更一致的股票。' if lang == 'zh' else 'Prioritizes names with more model overlap and tighter action alignment.'}</div>"
            f"<div class='detail-grid'>{''.join(leaderboard_rows)}</div>"
            "</article>"
        )
    confluence_bucket_groups_html = ""
    if len(multi_templates_active) >= 2 and results:
        bucket_groups: dict[str, list[dict]] = {}
        for item in results:
            for bucket in item.get("matched_action_buckets") or []:
                bucket_groups.setdefault(bucket, []).append(item)
        ordered_buckets = [bucket for bucket in ("buy_the_dip", "breakout_confirmation", "bullish_entry", "watchlist") if bucket in bucket_groups]
        cards = []
        for bucket in ordered_buckets:
            bucket_items = bucket_groups.get(bucket) or []
            top_names = " · ".join(
                f"{row.get('ticker')} ({int(row.get('model_hit_count') or 0)})"
                for row in bucket_items[:4]
            ) or "-"
            cards.append(
                "<article class='detail-card' style='background:#f8fbff;'>"
                f"<div class='detail-label'>{_confluence_bucket_label(bucket, lang)}</div>"
                f"<div style='font-size:26px;font-weight:800;color:#0f172a;margin:4px 0 8px;'>{len(bucket_items)}</div>"
                f"<div class='muted'>{top_names}</div>"
                "</article>"
            )
        confluence_bucket_groups_html = (
            "<article class='card' style='background:#f8fbff;border-color:#d7e7f8;'>"
            f"<div class='eyebrow'>{'按动作桶看结果' if lang == 'zh' else 'Results by Confluence Bucket'}</div>"
            f"<div class='detail-grid'>{''.join(cards)}</div>"
            "</article>"
        )
    quick_confluence_presets = [
        {
            "label": {"zh": "回踩共振", "en": "Dip Confluence"},
            "save_name": {"zh": "回踩共振", "en": "Dip Confluence"},
            "description": {
                "zh": "用 LightGBM、强趋势、动量、锤子线和水下金叉共同确认回踩机会，适合找不追高的低吸候选。",
                "en": "Combines LightGBM, trend, momentum, hammer reversal, and underwater MACD cross to find non-chasing dip candidates.",
            },
            "best_for": {"zh": "低吸 / Buy the dip", "en": "Dip entries"},
            "params": {
                "model_template": "next_tesla_swing",
                "multi_model_templates": ["lightgbm_top_picks", "next_tesla_swing", "technical_momentum", "cn_hammer_reversal", "cn_macd_underwater_cross"],
                "min_multi_model_hits": 2,
                "confluence_action_filter": "buy_the_dip",
                "market": "CN",
                "universe": "full_market",
                "min_trend_score": 10,
                "sort_by": "confluence_rank",
                "sort_order": "desc",
                "run": 1,
                "lang": lang,
            },
        },
        {
            "label": {"zh": "突破共振", "en": "Breakout Confluence"},
            "save_name": {"zh": "突破共振", "en": "Breakout Confluence"},
            "description": {
                "zh": "把趋势、动量、放量突破和均线多头排列叠加，用于识别临近突破或已经确认突破的强势股。",
                "en": "Stacks trend, momentum, volume breakout, and bullish MA alignment to find confirmed or near-confirmed breakouts.",
            },
            "best_for": {"zh": "突破确认", "en": "Breakout confirmation"},
            "params": {
                "model_template": "next_tesla_swing",
                "multi_model_templates": ["lightgbm_top_picks", "next_tesla_swing", "technical_momentum", "cn_volume_breakout", "cn_bullish_ma_stack"],
                "min_multi_model_hits": 2,
                "confluence_action_filter": "breakout_confirmation",
                "market": "CN",
                "universe": "full_market",
                "min_trend_score": 10,
                "sort_by": "confluence_rank",
                "sort_order": "desc",
                "run": 1,
                "lang": lang,
            },
        },
        {
            "label": {"zh": "偏多入场共振", "en": "Bullish Entry Confluence"},
            "save_name": {"zh": "偏多入场共振", "en": "Bullish Entry Confluence"},
            "description": {
                "zh": "把多因子、技术动量、放量突破、成长价值和高 ROE 稳增组合在一起，偏向基本面质量更强的入场候选。",
                "en": "Blends factors, technical momentum, breakout, growth/value, and high-ROE steadiness for higher-quality bullish entries.",
            },
            "best_for": {"zh": "质量 + 入场", "en": "Quality entries"},
            "params": {
                "model_template": "lightgbm_top_picks",
                "multi_model_templates": ["lightgbm_top_picks", "technical_momentum", "cn_volume_breakout", "cn_growth_value", "cn_high_roe_steady_growth"],
                "min_multi_model_hits": 2,
                "confluence_action_filter": "bullish_entry",
                "market": "CN",
                "universe": "full_market",
                "min_trend_score": 10,
                "sort_by": "confluence_rank",
                "sort_order": "desc",
                "run": 1,
                "lang": lang,
            },
        },
        {
            "label": {"zh": "强趋势+动量+LightGBM", "en": "Trend + Momentum + LightGBM"},
            "save_name": {"zh": "强趋势+动量+LightGBM", "en": "Trend + Momentum + LightGBM"},
            "description": {
                "zh": "仅保留双模型共振、READY、就绪度至少 72 且无追高/弱市硬风险的 A 级候选。",
                "en": "Uses the core models as a broad confluence pass, ranking by model-hit count for fast full-market discovery.",
            },
            "best_for": {"zh": "强势池初筛", "en": "Momentum pool"},
            "params": {
                "model_template": "lightgbm_top_picks",
                "multi_model_templates": ["lightgbm_top_picks", "next_tesla_swing", "technical_momentum"],
                "min_multi_model_hits": 2,
                "confluence_action_filter": "ALL",
                "strategy_profile": "quality_confluence_v1",
                "market": "CN",
                "universe": "full_market",
                "min_trend_score": 10,
                "sort_by": "model_hit_count",
                "sort_order": "desc",
                "run": 1,
                "lang": lang,
            },
        },
        {
            "label": {"zh": "LightGBM+突破共振", "en": "LightGBM + Breakout"},
            "save_name": {"zh": "LightGBM+突破共振", "en": "LightGBM + Breakout"},
            "description": {
                "zh": "保留 LightGBM 排名，同时叠加动量、放量突破和均线结构，适合从模型高分里筛真正可交易的突破票。",
                "en": "Keeps LightGBM ranking but requires momentum, volume breakout, and MA structure to narrow high-score names into tradable breakouts.",
            },
            "best_for": {"zh": "模型高分 + 突破", "en": "High-score breakouts"},
            "params": {
                "model_template": "lightgbm_top_picks",
                "multi_model_templates": ["lightgbm_top_picks", "technical_momentum", "cn_volume_breakout", "cn_bullish_ma_stack"],
                "min_multi_model_hits": 2,
                "confluence_action_filter": "breakout_confirmation",
                "market": "CN",
                "universe": "full_market",
                "min_trend_score": 10,
                "sort_by": "confluence_rank",
                "sort_order": "desc",
                "run": 1,
                "lang": lang,
            },
        },
        {
            "label": {"zh": "美股强趋势+动量+LightGBM", "en": "US Trend + Momentum + LightGBM"},
            "save_name": {"zh": "美股强趋势+动量+LightGBM", "en": "US Trend + Momentum + LightGBM"},
            "description": {
                "zh": "把美股 LightGBM、Next Tesla Swing 和技术动量叠加，先用模型命中数找出真正有共振的强势候选。",
                "en": "Stacks U.S. LightGBM, Next Tesla Swing, and technical momentum to prioritize names with real model overlap.",
            },
            "best_for": {"zh": "美股强势池", "en": "U.S. momentum pool"},
            "params": {
                "model_template": "lightgbm_top_picks",
                "multi_model_templates": ["lightgbm_top_picks", "next_tesla_swing", "technical_momentum"],
                "min_multi_model_hits": 2,
                "confluence_action_filter": "ALL",
                "market": "US",
                "universe": "full_market",
                "min_trend_score": 10,
                "sort_by": "model_hit_count",
                "sort_order": "desc",
                "run": 1,
                "lang": lang,
            },
        },
        {
            "label": {"zh": "美股突破共振", "en": "US Breakout Confluence"},
            "save_name": {"zh": "美股突破共振", "en": "US Breakout Confluence"},
            "description": {
                "zh": "保留美股核心三模型，但只看突破确认动作，适合在强势股里继续缩小候选范围。",
                "en": "Keeps the three core U.S. models but filters for breakout-confirmation setups to narrow the field.",
            },
            "best_for": {"zh": "美股突破确认", "en": "U.S. breakouts"},
            "params": {
                "model_template": "lightgbm_top_picks",
                "multi_model_templates": ["lightgbm_top_picks", "next_tesla_swing", "technical_momentum"],
                "min_multi_model_hits": 2,
                "confluence_action_filter": "breakout_confirmation",
                "market": "US",
                "universe": "full_market",
                "min_trend_score": 10,
                "sort_by": "confluence_rank",
                "sort_order": "desc",
                "run": 1,
                "lang": lang,
            },
        },
        {
            "label": {"zh": "美股质量成长共振", "en": "US Quality Growth Confluence"},
            "save_name": {"zh": "美股质量成长共振", "en": "US Quality Growth Confluence"},
            "description": {
                "zh": "用 LightGBM、技术动量、成长价值和分红质量交叉验证，优先找质量更高的美股入场候选。",
                "en": "Cross-checks LightGBM, momentum, growth/value, and income quality to surface higher-quality U.S. entries.",
            },
            "best_for": {"zh": "美股质量入场", "en": "U.S. quality entries"},
            "params": {
                "model_template": "lightgbm_top_picks",
                "multi_model_templates": ["lightgbm_top_picks", "technical_momentum", "global_growth_value", "global_income_quality"],
                "min_multi_model_hits": 2,
                "confluence_action_filter": "bullish_entry",
                "market": "US",
                "universe": "full_market",
                "min_trend_score": 10,
                "sort_by": "confluence_rank",
                "sort_order": "desc",
                "run": 1,
                "lang": lang,
            },
        },
    ]
    quick_confluence_presets = [
        preset for preset in quick_confluence_presets
        if not set((preset.get("params") or {}).get("multi_model_templates") or []).intersection(
            {"cn_volume_breakout", "cn_macd_underwater_cross"}
        )
    ]
    quick_confluence_presets_html = "".join(
        (
            "<div style='display:flex;gap:8px;align-items:center;flex-wrap:wrap;'>"
            f"<a class='detail-link' href='{_build_screen_query(preset['params'])}'>{preset['label'][lang]}</a>"
            f"<form method='post' action='/screeners/save' style='margin:0;display:flex;align-items:center;'>"
            f"<input type='hidden' name='preset_name' value='{html.escape(preset['save_name'][lang])}' />"
            f"{_preset_hidden_fields_html(preset['params'])}"
            f"<button type='submit' style='width:auto;min-width:0;padding:10px 12px;'>{'保存' if lang == 'zh' else 'Save'}</button>"
            "</form>"
            "</div>"
        )
        for preset in quick_confluence_presets
    )
    guidance_payload = load_model_selection_guidance_snapshot(
        db,
        market=market if market in {"CN", "US", "ALL"} else "CN",
        allow_fallback=True,
    )
    guidance_summary = summarize_model_selection_guidance(guidance_payload, lang=lang)
    guidance_top_model = guidance_summary.get("top_model") or {}
    guidance_top_combo = guidance_summary.get("top_combo") or {}
    top_model_href = html.escape(
        str(guidance_summary.get("top_model_href") or f"/screeners?lang={lang}&market=CN&universe=full_market&run=1"),
        quote=True,
    )
    top_combo_href = html.escape(
        str(guidance_summary.get("top_combo_href") or f"/screeners?lang={lang}&market=CN&universe=full_market&run=1"),
        quote=True,
    )
    guidance_source_meta = guidance_summary.get("snapshot_meta") or {}
    guidance_source_text = (
        f"{'样本来源' if lang == 'zh' else 'Source'}："
        f"{'后台评测快照' if str(guidance_source_meta.get('source') or '') == 'snapshot' else '实时回退'}"
        f" · {guidance_source_meta.get('snapshot_date') or guidance_source_meta.get('generated_at') or '-'}"
    )
    guidance_top_model_metrics = (
        f"1D {_fmt_number(((guidance_top_model.get('stats_1d') or {}).get('avg_return')), suffix='%', digits=2)}"
        f" / {_fmt_number(((guidance_top_model.get('stats_1d') or {}).get('hit_rate')), suffix='%', digits=1)}"
        f" · 3D {_fmt_number(((guidance_top_model.get('stats_3d') or {}).get('avg_return')), suffix='%', digits=2)}"
        f" / {_fmt_number(((guidance_top_model.get('stats_3d') or {}).get('hit_rate')), suffix='%', digits=1)}"
    )
    guidance_top_combo_metrics = (
        f"1D {_fmt_number(((guidance_top_combo.get('stats_1d') or {}).get('avg_return')), suffix='%', digits=2)}"
        f" / {_fmt_number(((guidance_top_combo.get('stats_1d') or {}).get('hit_rate')), suffix='%', digits=1)}"
        f" · 5D {_fmt_number(((guidance_top_combo.get('stats_5d') or {}).get('avg_return')), suffix='%', digits=2)}"
        f" / {_fmt_number(((guidance_top_combo.get('stats_5d') or {}).get('hit_rate')), suffix='%', digits=1)}"
    )
    guidance_bridge_html = (
        "<section class='card' style='background:#f8fbff;border-color:#d7e7f8;'>"
        f"<div class='eyebrow'>{'今日模型导航' if lang == 'zh' else 'Today Model Guide'}</div>"
        f"<h2 style='margin:0 0 6px;'>{'先跑最有效的，再补充共振' if lang == 'zh' else 'Start with the best edge, then add confluence'}</h2>"
        f"<p class='lead'>{html.escape(str(guidance_source_text))}</p>"
        "<div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px;margin-top:14px;'>"
        "<article class='detail-card' style='background:#ffffff;'>"
        f"<div class='detail-label'>{'优先模型' if lang == 'zh' else 'Priority Model'}</div>"
        f"<div style='font-size:22px;font-weight:900;color:#0f172a;margin-top:6px;'>{html.escape(str(guidance_summary.get('top_model_title') or '-'))}</div>"
        f"<div class='muted' style='margin-top:8px;'>{html.escape(str(guidance_summary.get('top_model_summary') or '-'))}</div>"
        f"<div class='muted' style='margin-top:8px;'>{html.escape(guidance_top_model_metrics)}</div>"
        f"<div style='display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;'><a class='detail-link' href='{top_model_href}'>{'按这个模型筛' if lang == 'zh' else 'Run this model'}</a><a class='detail-link' href='{evaluation_overview_href}'>{'看评测' if lang == 'zh' else 'View evaluation'}</a></div>"
        "</article>"
        "<article class='detail-card' style='background:#ffffff;'>"
        f"<div class='detail-label'>{'优先组合' if lang == 'zh' else 'Priority Confluence'}</div>"
        f"<div style='font-size:22px;font-weight:900;color:#0f172a;margin-top:6px;'>{html.escape(str(guidance_summary.get('top_combo_title') or '-'))}</div>"
        f"<div class='muted' style='margin-top:8px;'>{html.escape(str(guidance_summary.get('top_combo_summary') or '-'))}</div>"
        f"<div class='muted' style='margin-top:8px;'>{html.escape(guidance_top_combo_metrics)}</div>"
        f"<div style='display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;'><a class='detail-link' href='{top_combo_href}'>{'直接跑组合' if lang == 'zh' else 'Run confluence'}</a><a class='detail-link' href='/dashboard/model-performance/winner-traceback?lang={lang}&market={market if market in {'CN','US','ALL'} else 'CN'}'>{'看强票归因' if lang == 'zh' else 'Winner traceback'}</a></div>"
        "</article>"
        "<article class='detail-card' style='background:#ffffff;'>"
        f"<div class='detail-label'>{'今日使用建议' if lang == 'zh' else 'Today Workflow'}</div>"
        f"<div class='muted' style='margin-top:8px;'>{'1. 先跑优先模型看当日基准候选。 2. 再跑优先组合做降噪。 3. 最后只处理 READY 且接近买点的票。' if lang == 'zh' else '1. Start with the priority model. 2. Use the priority confluence to reduce noise. 3. Only act on READY names near their planned entry zones.'}</div>"
        f"<div style='display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;'><a class='detail-link' href='{top_model_href}'>{'先跑基准模型' if lang == 'zh' else 'Run baseline'}</a><a class='detail-link' href='{top_combo_href}'>{'再跑共振' if lang == 'zh' else 'Then confluence'}</a></div>"
        "</article>"
        "</div>"
        "</section>"
    )
    strategy_template_cards_html = "".join(
        (
            "<article class='strategy-template-card'>"
            "<div class='template-top'>"
            f"<span class='template-mode'>{'策略模板' if lang == 'zh' else 'Strategy'}</span>"
            f"<span class='template-market'>{preset['params'].get('market', 'CN')}</span>"
            "</div>"
            f"<div class='strategy-template-title'>{preset['label'][lang]}</div>"
            f"<div class='template-desc'>{preset.get('description', {}).get(lang, '')}</div>"
            "<div class='strategy-meta-row'>"
            f"<span class='default-chip'>{preset.get('best_for', {}).get(lang, '')}</span>"
            f"<span class='default-chip'>{'至少命中' if lang == 'zh' else 'Min hits'} {preset['params'].get('min_multi_model_hits', 2)}</span>"
            f"<span class='default-chip'>{_confluence_bucket_label(str(preset['params'].get('confluence_action_filter') or 'ALL'), lang)}</span>"
            "</div>"
            "<div class='strategy-template-actions'>"
            f"<a class='detail-link' href='{_build_screen_query(preset['params'])}'>{'运行策略' if lang == 'zh' else 'Run Strategy'}</a>"
            f"<a class='detail-link' href='{evaluation_overview_href}'>{'查看评测' if lang == 'zh' else 'View Evaluation'}</a>"
            f"<form method='post' action='/screeners/save' style='margin:0;display:flex;align-items:center;'>"
            f"<input type='hidden' name='preset_name' value='{html.escape(preset['save_name'][lang])}' />"
            f"{_preset_hidden_fields_html(preset['params'])}"
            f"<button type='submit' style='width:auto;min-width:0;padding:10px 12px;'>{'保存为我的策略' if lang == 'zh' else 'Save Strategy'}</button>"
            "</form>"
            "</div>"
            "</article>"
        )
        for preset in quick_confluence_presets
    )
    single_model_href = _build_screen_query(
        {
            **current_params,
            "multi_model_templates": [],
            "min_multi_model_hits": 1,
            "confluence_action_filter": "ALL",
            "run": 1,
        }
    )
    workbench_entries = [
        {
            "code": "MODEL",
            "title": {"zh": "模型筛选", "en": "Model Screening"},
            "body": {"zh": "单独运行一个模型模板，适合验证某个打法今天是否有票。", "en": "Run one model template to validate whether a single playbook has candidates today."},
            "href": single_model_href,
        },
        {
            "code": "TPL",
            "title": {"zh": "策略模板", "en": "Strategy Templates"},
            "body": {"zh": "直接使用预设组合：回踩、突破、质量入场、强趋势共振。", "en": "Use pre-built strategy combinations for dips, breakouts, quality entries, and trend confluence."},
            "href": "#strategy-templates",
        },
        {
            "code": "COMBO",
            "title": {"zh": "多模型组合", "en": "Multi-Model Confluence"},
            "body": {"zh": "勾选多个模型，按命中数量和动作一致性筛掉噪音。", "en": "Select multiple models and rank by overlap plus action alignment to reduce noise."},
            "href": "#multi-model-combos",
        },
        {
            "code": "EVAL",
            "title": {"zh": "模型评测总览", "en": "Model Evaluation Overview"},
            "body": {"zh": "先看近期哪个模型或组合更有效，再决定今天用哪套筛选。", "en": "Review recent model and combo effectiveness before choosing today’s screening path."},
            "href": evaluation_overview_href,
        },
        {
            "code": "FACTOR",
            "title": {"zh": "因子实验室", "en": "Factor Lab"},
            "body": {"zh": "把过滤条件和权重保存成策略项目，跟踪 1D/3D/5D 命中率和高开买不到。", "en": "Save filters and weights as factor strategies, then track 1D/3D/5D hit rates and gap risk."},
            "href": f"/screeners/factor-lab?lang={lang}",
        },
        {
            "code": "QUALITY",
            "title": {"zh": "命中率闭环", "en": "Selection Quality"},
            "body": {"zh": "统一查看 AI 日报和因子实验的真实兑现率，决定今天该信哪套策略。", "en": "Compare realized AI report and factor-experiment outcomes before choosing today’s strategy."},
            "href": f"/screeners/selection-quality?lang={lang}",
        },
    ]
    strategy_workbench_html = "".join(
        (
            f"<a class='workbench-card' href='{entry['href']}'>"
            f"<span class='workbench-code'>{entry['code']}</span>"
            f"<strong>{entry['title'][lang]}</strong>"
            f"<span>{entry['body'][lang]}</span>"
            "</a>"
        )
        for entry in workbench_entries
    )

    row_chunks: list[str] = []
    previous_market = None
    for item in visible_results:
        current_market = (item.get("market") or "").upper()
        sync_badge = _sync_status_badge(watchlist_map.get(item["ticker"]), lang)
        watchlist_action_html = _watchlist_action_cell(item, watchlist_map, current_params, lang)
        snapshot_badge = _number_badge(
            item.get("snapshot_hits"),
            suffix=f"/{item.get('snapshot_runs') or 0}",
            higher_is_good=True,
        )
        if current_market != previous_market:
            row_chunks.append(
                "<tr class='market-section-row'>"
                f"<td colspan='18'>{_market_section_label(current_market, lang)}</td>"
                "</tr>"
            )
            previous_market = current_market
        row_chunks.append(
            "<tr>"
            f"<td class='sticky-col sticky-col-1'><a href='/insights/{item['ticker']}?lang={lang}'>{item['ticker']}</a></td>"
            f"<td class='sticky-col sticky-col-2'><div>{item.get('name') or '-'}</div>{_pseudo_strong_signal_html(item, lang)}</td>"
            f"<td class='sticky-col sticky-col-3'>{_action_badge(item.get('action_label'), lang)}</td>"
            f"<td>{_trend_badge(item.get('trend_score'))}</td>"
            f"<td>{item.get('market') or '-'}</td>"
            f"<td>{_price_badge(item.get('latest_close') if item.get('latest_close') is not None else item.get('close'))}</td>"
            f"<td>{_model_cell(item, lang)}</td>"
            f"<td>{sync_badge}</td>"
            f"<td>{snapshot_badge}</td>"
            f"<td>{_change_chip(item.get('momentum_5'))}</td>"
            f"<td>{_change_chip(item.get('momentum_20'))}</td>"
            f"<td>{_number_badge(item.get('volume_ratio'), suffix='x', higher_is_good=True)}</td>"
            f"<td>{_number_badge(item.get('pe_ttm'), higher_is_good=False)}</td>"
            f"<td>{_number_badge(item.get('roe_avg_3y'), suffix='%', higher_is_good=True)}</td>"
            f"<td>{_number_badge(item.get('net_profit_yoy'), suffix='%', higher_is_good=True)}</td>"
            f"<td>{_number_badge(item.get('dividend_yield'), suffix='%', higher_is_good=True)}</td>"
            f"<td>{_number_badge(item.get('distance_to_breakout_pct'), suffix='%', higher_is_good=False)}</td>"
            f"<td><div class='row-action-stack'><a class='main-open-link' href='/insights/{item['ticker']}?lang={lang}'>{_lang_text(lang, 'open_insight')}</a>{watchlist_action_html}</div></td>"
            "</tr>"
        )
        if detail_rows_enabled:
            row_chunks.append(
            "<tr class='detail-row'>"
            f"<td colspan='18'>{_detail_panel(item, watchlist_map, current_params, lang)}</td>"
            "</tr>"
            )
    empty_state = (
        "先选择一个模板或调整参数后再执行筛选，首屏默认不自动跑重计算。"
        if lang == "zh"
        else "Choose a template or adjust rules, then run the screen. The first load stays lightweight by default."
    )
    if should_execute and not snapshot_ready:
        empty_state = _snapshot_pending_message(lang)
    rows = "".join(row_chunks) or (
        f"<tr><td colspan='18'>{_lang_text(lang, 'no_match') if should_execute else empty_state}</td></tr>"
    )
    mobile_result_cards = "".join(
        (
            "<article class='mobile-result-card'>"
            f"<div class='mobile-result-head'>"
            f"<div><div class='mobile-result-ticker'><a href='/insights/{item['ticker']}?lang={lang}'>{item['ticker']}</a></div><div class='muted'>{_compact_text(item.get('name') or '-', 28)} · {item.get('market') or '-'}</div>{_pseudo_strong_signal_html(item, lang)}</div>"
            f"<div class='mobile-result-price'>{_price_badge(item.get('latest_close') if item.get('latest_close') is not None else item.get('close'))}</div>"
            "</div>"
            f"<div class='mobile-result-chip-row'>{_trend_badge(item.get('trend_score'))}{_action_badge(item.get('action_label'), lang)}{_sync_status_badge(watchlist_map.get(item['ticker']), lang)}{_kronos_compact_chip(item, lang)}</div>"
            f"<div class='mobile-result-chip-row'>{_number_badge(item.get('model_hit_count'), suffix=('模' if lang == 'zh' else ' hits'), higher_is_good=True)}{_number_badge(item.get('confluence_alignment_count'), suffix=('齐' if lang == 'zh' else ' aligned'), higher_is_good=True)}</div>"
            f"<div class='mobile-result-grid'>"
            f"<div><span class='muted'>5D</span><div>{_change_chip(item.get('momentum_5'))}</div></div>"
            f"<div><span class='muted'>20D</span><div>{_change_chip(item.get('momentum_20'))}</div></div>"
            f"<div><span class='muted'>Hits</span><div>{_number_badge(item.get('snapshot_hits'), suffix='/' + str(item.get('snapshot_runs') or 0), higher_is_good=True)}</div></div>"
            f"<div><span class='muted'>Volume</span><div>{_number_badge(item.get('volume_ratio'), suffix='x', higher_is_good=True)}</div></div>"
            f"<div><span class='muted'>PE</span><div>{_number_badge(item.get('pe_ttm'), higher_is_good=False)}</div></div>"
            f"<div><span class='muted'>ROE</span><div>{_number_badge(item.get('roe_avg_3y'), suffix='%', higher_is_good=True)}</div></div>"
            "</div>"
            + (
                "<div class='mobile-result-chip-row'>"
                "<span style='display:inline-flex;align-items:center;padding:5px 10px;border-radius:999px;background:#e6f4f1;color:#0f766e;font-weight:800;font-size:12px;'>"
                + html.escape(str(item.get('lightgbm_tactical_tag') or ''))
                + "</span></div>"
                if item.get("lightgbm_tactical_tag")
                else ""
            )
            + f"<div class='mobile-result-summary'>{_compact_text(item.get('model_summary') or '-', 140)}</div>"
            f"<div class='mobile-result-meta'>{_pattern_hits_inline(item.get('matched_patterns'))}</div>"
            f"<div class='mobile-result-actions'><a class='detail-link' href='/insights/{item['ticker']}?lang={lang}'>{_lang_text(lang, 'open_insight')}</a>{_watchlist_action_cell(item, watchlist_map, current_params, lang)}</div>"
            "</article>"
        )
        for item in visible_results
    ) or f"<div class='empty'>{_lang_text(lang, 'no_match') if should_execute else empty_state}</div>"
    preset_rows_parts: list[str] = []
    mobile_preset_parts: list[str] = []
    for preset in saved_presets:
        preset_name = str(preset.get("name") or "").strip()
        if not preset_name:
            continue
        preset_params = preset.get("params") if isinstance(preset.get("params"), dict) else {}
        run_href = _build_screen_query(preset_params)
        display_label = _preset_display_label(preset_params, lang)
        preset_summary = _preset_summary(preset_params, lang)
        market_label = str(preset_params.get("market") or "ALL")
        preset_name_html = html.escape(preset_name)
        preset_rows_parts.append(
            "<tr>"
            f"<td title='{preset_name_html}'><strong>{html.escape(_compact_text(preset_name, 26))}</strong><div class='muted'>{market_label}</div></td>"
            f"<td title='{html.escape(display_label)}'>{html.escape(_compact_text(display_label, 24))}</td>"
            f"<td title='{html.escape(preset_summary)}'>{html.escape(_compact_text(preset_summary, 48))}</td>"
            f"<td>{'点击运行后查看' if lang == 'zh' else 'Run to view'}</td>"
            f"<td><a class='detail-link' href='{run_href}'>{_lang_text(lang, 'run_strategy')}</a><div style='margin-top:6px;'><a class='detail-link' href='{evaluation_overview_href}'>{_lang_text(lang, 'view_evaluation')}</a></div></td>"
            "<td>"
            "<form method='post' action='/screeners/rename' style='margin:0;display:grid;gap:6px;min-width:180px;'>"
            f"<input type='hidden' name='preset_name' value='{preset_name_html}' />"
            f"<input type='hidden' name='lang' value='{lang}' />"
            f"<input type='text' name='new_name' placeholder='{_lang_text(lang, 'new_name')}' value='{preset_name_html}' />"
            f"<button type='submit' style='padding:8px 10px;'>{_lang_text(lang, 'rename')}</button>"
            "</form>"
            "</td>"
            f"<td><form method='post' action='/screeners/delete' style='margin:0;'><input type='hidden' name='preset_name' value='{preset_name_html}' /><input type='hidden' name='lang' value='{lang}' /><button type='submit'>{_lang_text(lang, 'delete')}</button></form></td>"
            "</tr>"
        )
        mobile_preset_parts.append(
            "<article class='mobile-preset-card'>"
            f"<div style='font-weight:800;'>{html.escape(_compact_text(preset_name, 30))}</div>"
            f"<div class='muted' style='margin-top:6px;'>{html.escape(_compact_text(display_label, 28))}</div>"
            f"<div class='mobile-result-summary'>{html.escape(_compact_text(preset_summary, 140))}</div>"
            "<div class='mobile-result-actions'>"
            f"<a class='detail-link' href='{run_href}'>{_lang_text(lang, 'run_strategy')}</a>"
            f"<a class='detail-link' href='{evaluation_overview_href}'>{_lang_text(lang, 'view_evaluation')}</a>"
            "</div>"
            "<form method='post' action='/screeners/rename' style='margin:10px 0 0;display:grid;gap:8px;'>"
            f"<input type='hidden' name='preset_name' value='{preset_name_html}' />"
            f"<input type='hidden' name='lang' value='{lang}' />"
            f"<input type='text' name='new_name' placeholder='{_lang_text(lang, 'new_name')}' value='{preset_name_html}' />"
            f"<button type='submit'>{_lang_text(lang, 'rename')}</button>"
            "</form>"
            "<div class='mobile-result-actions'>"
            f"<form method='post' action='/screeners/delete' style='margin:0;'><input type='hidden' name='preset_name' value='{preset_name_html}' /><input type='hidden' name='lang' value='{lang}' /><button type='submit'>{_lang_text(lang, 'delete')}</button></form>"
            "</div>"
            "</article>"
        )
    preset_rows = "".join(preset_rows_parts) or f"<tr><td colspan='7'>{_lang_text(lang, 'no_saved')}</td></tr>"
    mobile_preset_cards = "".join(mobile_preset_parts) or f"<div class='empty'>{_lang_text(lang, 'no_saved')}</div>"
    banner_html = _banner_html(message, lang)
    risk_top_tags_html = "".join(
        f"<span class='linkbtn'>{tag} · {count}</span>" for tag, count in risk_top_tags
    ) or f"<span class='muted'>{_lang_text(lang, 'no_execution_risks')}</span>"
    risk_examples_html = " · ".join(
        f"{item['ticker']} ({' / '.join(item['tags'])})" for item in risk_examples
    ) or "-"
    hidden_fields = _hidden_fields_html(current_params)
    actions_available = snapshot_ready and total_results > 0
    bulk_add_disabled = "disabled" if not actions_available else ""
    bulk_add_label = _lang_text(lang, "add_current_results") if total_results else _lang_text(lang, "no_results_to_add")
    watchlist_overlap_count = sum(1 for item in visible_results if watchlist_map.get(item["ticker"]))
    pending_snapshot_count = len(multi_screen_meta.get("missing_templates") or [])
    active_model_count = len(multi_templates_active) if len(multi_templates_active) >= 2 else 1
    market_scope_label = {
        "CN": "A股" if lang == "zh" else "A-Shares",
        "US": "美股" if lang == "zh" else "U.S.",
        "HK": "港股" if lang == "zh" else "Hong Kong",
        "ALL": "全市场" if lang == "zh" else "All Markets",
    }.get(str(market or "ALL").upper(), str(market or "ALL").upper())
    universe_scope_label = {
        "watchlist": "自选股" if lang == "zh" else "Watchlist",
        "synced": "已同步股票" if lang == "zh" else "Synced",
        "full_market": "全市场" if lang == "zh" else "Full Market",
    }.get(str(universe or "full_market"), str(universe or "full_market"))
    screen_overview_html = (
        f"""
          <section class="card" style="margin-bottom:16px;padding:18px 18px 14px;">
            <div style="display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap;">
              <div>
                <div class="eyebrow">{'筛选总览' if lang == 'zh' else 'Screener Overview'}</div>
                <div style="font-size:28px;font-weight:900;letter-spacing:-0.03em;">{total_results if should_execute else active_model_count}</div>
                <div class="muted">{market_scope_label} · {universe_scope_label}</div>
              </div>
              <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;flex:1;min-width:min(100%,760px);">
                <article style="border:1px solid var(--line);border-radius:16px;padding:14px 15px;background:rgba(15,24,35,0.58);">
                  <div class="eyebrow">{'当前模式' if lang == 'zh' else 'Current Mode'}</div>
                  <div style="margin-top:6px;font-size:22px;font-weight:900;">{active_model_count}</div>
                  <div class="muted">{(f'{active_model_count} 个模型参与 · 已运行' if should_execute else f'{active_model_count} 个模型参与 · 待运行') if lang == 'zh' else (f'{active_model_count} models active · Executed' if should_execute else f'{active_model_count} models active · Ready')}</div>
                </article>
                <article style="border:1px solid var(--line);border-radius:16px;padding:14px 15px;background:rgba(15,24,35,0.58);">
                  <div class="eyebrow">{'命中结果' if lang == 'zh' else 'Matched Results'}</div>
                  <div style="margin-top:6px;font-size:22px;font-weight:900;">{total_results}</div>
                  <div class="muted">{f'页面显示前 {len(visible_results)} 只' if lang == 'zh' else f'Top {len(visible_results)} shown first'}</div>
                </article>
                <article style="border:1px solid var(--line);border-radius:16px;padding:14px 15px;background:rgba(15,24,35,0.58);">
                  <div class="eyebrow">{'风险标记' if lang == 'zh' else 'Risk Tags'}</div>
                  <div style="margin-top:6px;font-size:22px;font-weight:900;">{tagged_names}</div>
                  <div class="muted">{'带执行提醒的候选股' if lang == 'zh' else 'Candidates carrying execution warnings'}</div>
                </article>
                <article style="border:1px solid var(--line);border-radius:16px;padding:14px 15px;background:rgba(15,24,35,0.58);">
                  <div class="eyebrow">{'自选重叠' if lang == 'zh' else 'Watchlist Overlap'}</div>
                  <div style="margin-top:6px;font-size:22px;font-weight:900;">{watchlist_overlap_count}</div>
                  <div class="muted">{('缺前置快照 ' + str(pending_snapshot_count)) if (should_execute and not snapshot_ready and pending_snapshot_count > 0 and lang == 'zh') else ('Pending snapshots ' + str(pending_snapshot_count)) if (should_execute and not snapshot_ready and pending_snapshot_count > 0) else ('已在自选里的候选股' if lang == 'zh' else 'Candidates already in watchlist')}</div>
                </article>
              </div>
            </div>
          </section>
        """
    )
    visible_note = (
        (
            f"当前共命中 {total_results} 只，页面先展示前 {len(visible_results)} 只；导出 CSV 仍包含全部结果。"
            if lang == "zh"
            else f"{total_results} names matched; the page shows the top {len(visible_results)} first, while CSV export still includes the full result set."
        )
        if should_execute
        else (
            "当前页面默认只展示模板和规则，避免首屏直接跑重计算。选择模板后点击“运行筛选”即可。"
            if lang == "zh"
            else "The first load only shows templates and rules to keep the page fast. Click Run Screen when you're ready."
        )
    )
    if should_execute and not snapshot_ready:
        visible_note = _snapshot_pending_message(lang)
    if should_execute and snapshot_ready and total_results > 0 and not detail_rows_enabled:
        visible_note += (
            f" <a href='{detail_rows_href}' style='color:var(--accent);font-weight:800;text-decoration:none;'>{'需要逐行解释时再展开行内详情' if lang == 'zh' else 'Open row details only when needed'}</a>."
        )
    run_receipt_html = _screen_run_receipt_html(
        service=service,
        params=current_params,
        results=results,
        snapshot_ready=snapshot_ready,
        multi_templates_active=multi_templates_active,
        multi_screen_meta=multi_screen_meta,
        lang=lang,
    ) if should_execute else ""
    lang_switch_html = "".join(
        f"<a href='{_build_screen_query({**current_params, 'lang': code})}' style='padding:8px 12px;border-radius:999px;border:1px solid var(--line);background:{'#eef8f5' if lang == code else '#fff'};text-decoration:none;color:var(--ink);font-weight:700;'>{label}</a>"
        for code, label in LANG_OPTIONS
    )

    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{_lang_text(lang, 'title')}</title>
        <style>
          :root {{
            --bg: #071018;
            --panel: #111c28;
            --panel-2: #152231;
            --ink: #e6edf3;
            --muted: #90a3b8;
            --line: #223246;
            --accent: #3dd9b6;
            --accent-soft: rgba(61,217,182,0.12);
          }}
          * {{ box-sizing: border-box; }}
          body {{
            margin: 0;
            font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            color: var(--ink);
            background:
              radial-gradient(circle at top left, rgba(82,168,255,0.14) 0, transparent 28%),
              radial-gradient(circle at top right, rgba(61,217,182,0.10) 0, transparent 26%),
              var(--bg);
          }}
          {WORKSPACE_COMPACT_STYLE}
          {WORKSPACE_SIDEBAR_STYLE}
          .content {{ padding:16px 14px 24px; }}
          .wrap {{ max-width:none; margin:0; padding: 0 0 36px; }}
          .toolbar {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:12px; }}
          .toolbar a {{ color: var(--accent); text-decoration:none; font-weight:700; }}
          .nav-grid {{ display:grid; gap:8px; grid-template-columns:repeat(auto-fit, minmax(210px, 1fr)); margin-bottom:10px; }}
          .nav-card {{
            display:block;
            text-decoration:none;
            color:inherit;
            background:linear-gradient(180deg, rgba(17,28,40,0.98) 0%, rgba(21,34,49,0.98) 100%);
            border:1px solid var(--line);
            border-radius:10px;
            padding:10px;
            box-shadow:0 8px 18px rgba(0,0,0,0.10);
          }}
          .nav-card:hover {{ border-color:var(--accent); box-shadow:0 12px 28px rgba(61,217,182,0.08); }}
          .nav-head {{ display:flex; align-items:center; gap:8px; margin-bottom:5px; }}
          .nav-icon {{
            width:30px; height:30px; border-radius:9px; display:inline-flex; align-items:center; justify-content:center;
            background:rgba(61,217,182,0.10); color:var(--accent); font-size:11px; font-weight:900; letter-spacing:0.04em; border:1px solid rgba(61,217,182,0.18); flex:0 0 auto;
          }}
          .nav-title {{ font-size:14px; font-weight:800; color:var(--ink); }}
          .nav-kicker {{ color:var(--muted); font-size:10px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; }}
          h1 {{ margin:0 0 6px; font-size:32px; }}
          .lead {{ margin:0; color:var(--muted); max-width:760px; }}
          .section-stack {{ display:grid; gap:12px; }}
          .workbench-grid {{
            display:grid;
            gap:12px;
            grid-template-columns:repeat(auto-fit, minmax(220px, 1fr));
            margin:12px 0;
          }}
          .workbench-card {{
            display:grid;
            gap:10px;
            min-height:112px;
            padding:12px;
            border-radius:12px;
            border:1px solid rgba(61,217,182,0.18);
            background:
              linear-gradient(135deg, rgba(61,217,182,0.12), rgba(82,168,255,0.07)),
              rgba(11,19,29,0.88);
            color:inherit;
            text-decoration:none;
            box-shadow:0 14px 34px rgba(0,0,0,0.18);
          }}
          .workbench-card:hover {{ border-color:var(--accent); transform:translateY(-1px); }}
          .workbench-code {{
            width:max-content;
            display:inline-flex;
            align-items:center;
            padding:6px 10px;
            border-radius:999px;
            background:rgba(61,217,182,0.12);
            color:var(--accent);
            font-size:11px;
            font-weight:900;
            letter-spacing:0.06em;
          }}
          .workbench-card strong {{ font-size:15px; color:var(--ink); }}
          .workbench-card span:last-child {{ color:var(--muted); font-size:12px; line-height:1.38; }}
          .strategy-template-section {{ display:grid; gap:12px; margin-bottom:12px; }}
          .strategy-template-grid {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit, minmax(260px, 1fr)); }}
          .strategy-template-card {{
            display:grid;
            gap:9px;
            align-content:start;
            min-height:170px;
            padding:12px;
            border-radius:12px;
            border:1px solid var(--line);
            background:linear-gradient(180deg, rgba(17,28,40,0.98), rgba(12,21,32,0.96));
            box-shadow:0 12px 28px rgba(0,0,0,0.14);
          }}
          .strategy-template-title {{ font-size:15px; font-weight:900; color:var(--ink); }}
          .strategy-meta-row {{ display:flex; flex-wrap:wrap; gap:8px; }}
          .strategy-template-actions {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-top:auto; }}
          .run-receipt-card {{ background:linear-gradient(180deg, rgba(17,28,40,0.98), rgba(9,17,27,0.96)); border-color:rgba(82,168,255,0.20); }}
          .receipt-grid {{ display:grid; gap:10px; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); margin-top:12px; }}
          .receipt-row {{
            display:grid;
            gap:6px;
            min-width:0;
            padding:12px 13px;
            border-radius:14px;
            border:1px solid rgba(255,255,255,0.05);
            background:rgba(11,19,29,0.82);
          }}
          .receipt-row span {{ color:var(--muted); font-size:11px; font-weight:900; letter-spacing:0.04em; text-transform:uppercase; }}
          .receipt-row strong {{ color:var(--ink); font-size:13px; line-height:1.45; word-break:break-word; overflow-wrap:anywhere; }}
          .template-group-stack {{ display:grid; gap:18px; margin-top:12px; }}
          .template-grid {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); margin-top:12px; }}
          .multi-template-stack {{ display:grid; gap:14px; margin-top:12px; }}
          .multi-template-grid {{ display:grid; gap:10px; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); margin-top:12px; }}
          .multi-template-chip {{
            display:flex;
            align-items:center;
            gap:10px;
            padding:12px 14px;
            border-radius:14px;
            border:1px solid var(--line);
            background:rgba(11,19,29,0.82);
            color:var(--ink);
          }}
          .multi-template-chip input {{
            width:18px;
            min-width:18px;
            height:18px;
            margin:0;
            padding:0;
          }}
          .multi-template-chip span {{
            font-size:13px;
            font-weight:700;
            line-height:1.35;
          }}
          .template-card {{
            display:grid;
            gap:10px;
            padding:14px;
            border-radius:15px;
            border:1px solid var(--line);
            background:rgba(11,19,29,0.82);
            color:inherit;
            text-decoration:none;
          }}
          .template-card.active {{ border-color:rgba(61,217,182,0.34); background:linear-gradient(180deg, rgba(61,217,182,0.14), rgba(82,168,255,0.08)); }}
          .template-top {{ display:flex; align-items:center; justify-content:space-between; gap:8px; }}
          .template-mode, .template-market, .default-chip {{
            display:inline-flex; align-items:center; padding:6px 10px; border-radius:999px; font-size:11px; font-weight:800;
          }}
          .template-mode {{ background:rgba(61,217,182,0.10); color:var(--accent); text-transform:uppercase; }}
          .template-market {{ background:rgba(82,168,255,0.10); color:#9acbff; }}
          .template-title {{ font-size:16px; font-weight:800; color:var(--ink); }}
          .template-desc {{ color:var(--muted); font-size:13px; line-height:1.5; }}
          .default-chip {{ background:rgba(246,200,95,0.10); color:#ffd982; }}
          details.advanced-panel {{ border-top:1px solid var(--line); padding-top:12px; margin-top:12px; }}
          details.advanced-panel > summary {{ cursor:pointer; list-style:none; font-weight:800; color:var(--ink); }}
          details.advanced-panel > summary::-webkit-details-marker {{ display:none; }}
          .summary-note {{ color:var(--muted); font-size:13px; margin-top:8px; }}
          .rules-grid {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); align-items:end; }}
          .action-grid {{ display:grid; gap:12px; grid-template-columns:repeat(2, minmax(0, 1fr)); }}
          .action-form {{
            display:grid;
            gap:10px;
            align-content:start;
            height:100%;
            padding:14px;
            border:1px solid var(--line);
            border-radius:16px;
            background:rgba(11,19,29,0.82);
          }}
          .action-head {{
            display:flex;
            align-items:flex-start;
            justify-content:space-between;
            gap:12px;
          }}
          .action-kicker {{
            color:var(--muted);
            font-size:11px;
            font-weight:800;
            letter-spacing:0.06em;
            text-transform:uppercase;
            margin-bottom:4px;
          }}
          .action-title {{
            color:var(--ink);
            font-size:16px;
            font-weight:800;
            line-height:1.2;
          }}
          .action-icon {{
            width:36px;
            height:36px;
            border-radius:12px;
            background:#eef8f5;
            border:1px solid #cde9e4;
            color:#0f766e;
            display:inline-flex;
            align-items:center;
            justify-content:center;
            font-size:11px;
            font-weight:900;
            letter-spacing:0.05em;
            flex:0 0 auto;
          }}
          .action-label {{
            color:var(--muted);
            font-size:12px;
            font-weight:800;
            letter-spacing:0.04em;
            text-transform:uppercase;
            min-height:28px;
            display:flex;
            align-items:flex-end;
          }}
          .action-row {{
            display:grid;
            grid-template-columns:minmax(0, 1fr);
            gap:10px;
          }}
          .action-input-wrap {{
            min-height:42px;
            display:flex;
            align-items:center;
          }}
          .action-checkbox {{
            display:flex;
            align-items:center;
            gap:8px;
            min-height:42px;
            color:var(--muted);
            font-size:14px;
            font-weight:600;
          }}
          .action-checkbox input {{
            width:18px;
            min-width:18px;
            height:18px;
            margin:0;
            padding:0;
            border-radius:6px;
          }}
          .action-submit {{
            min-height:42px;
            display:flex;
            align-items:flex-end;
          }}
          .action-submit button {{
            width:100%;
            min-width:0;
          }}
          .results-toolbar {{
            display:flex;
            align-items:center;
            justify-content:space-between;
            gap:12px;
            flex-wrap:wrap;
            margin-bottom:12px;
          }}
          .results-toolbar form {{
            margin:0;
          }}
          .results-toolbar button {{
            width:auto;
            min-width:140px;
          }}
          input, select, button {{
            border-radius:12px;
            border:1px solid var(--line);
            padding:10px 12px;
            font:inherit;
            background:rgba(11,19,29,0.82);
            color:var(--ink);
            width:100%;
            max-width:100%;
          }}
          button {{ background:var(--accent); color:#fff; border-color:var(--accent); font-weight:700; }}
          .muted {{ color:var(--muted); font-size:14px; }}
          .table-wrap {{ width:100%; max-width:100%; overflow-x:auto; overflow-y:hidden; border-radius:14px; border:1px solid var(--line); background:rgba(11,19,29,0.82); padding-bottom:8px; scrollbar-gutter:stable both-edges; }}
          .table-wrap::-webkit-scrollbar {{ height:12px; }}
          .table-wrap::-webkit-scrollbar-track {{ background:#0f1823; border-radius:999px; }}
          .table-wrap::-webkit-scrollbar-thumb {{ background:#32465d; border-radius:999px; border:2px solid #0f1823; }}
          .table-wrap::-webkit-scrollbar-thumb:hover {{ background:#47627f; }}
          table {{ width:100%; border-collapse:collapse; min-width:1660px; font-size:14px; table-layout:auto; }}
          th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; white-space:nowrap; }}
          th {{ color:var(--muted); font-weight:600; white-space:nowrap; }}
          td {{ white-space:nowrap; }}
          .table-wrap th:nth-child(7), .table-wrap td:nth-child(7) {{ min-width:220px; width:220px; }}
          .table-wrap th:nth-child(8), .table-wrap td:nth-child(8) {{ min-width:120px; width:120px; }}
          .table-wrap th:nth-child(9), .table-wrap td:nth-child(9) {{ min-width:100px; width:100px; }}
          .table-wrap th:nth-child(18), .table-wrap td:nth-child(18) {{ min-width:110px; width:110px; }}
          .sticky-col {{ position:sticky; background:var(--panel); z-index:2; }}
          th.sticky-col {{ z-index:4; }}
          .sticky-col-1 {{ left:0; min-width:124px; box-shadow: 10px 0 14px rgba(31,41,55,0.05); }}
          .sticky-col-2 {{ left:124px; min-width:180px; box-shadow: 10px 0 14px rgba(31,41,55,0.05); }}
          .sticky-col-3 {{ left:304px; min-width:120px; box-shadow: 10px 0 14px rgba(31,41,55,0.04); }}
          .market-section-row td {{ background:#132031; color:var(--accent); font-weight:800; letter-spacing:0.03em; border-top:1px solid var(--line); }}
          .detail-row td {{ white-space:normal; background:#0d1722; padding:12px 10px 14px; }}
          .row-detail-toggle > summary {{
            cursor:pointer;
            color:var(--accent);
            font-weight:800;
            list-style:none;
            margin-bottom:10px;
            display:flex;
            align-items:center;
            justify-content:space-between;
            gap:12px;
            padding:10px 12px;
            border:1px dashed #294256;
            border-radius:14px;
            background:#0f1823;
          }}
          .row-detail-toggle > summary::-webkit-details-marker {{ display:none; }}
          .detail-summary-meta {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-left:auto; }}
          .detail-summary-price {{
            display:inline-flex;
            align-items:center;
            padding:5px 10px;
            border-radius:999px;
            background:#f3f4f6;
            color:#374151;
            font-weight:800;
            font-size:12px;
          }}
          .detail-grid {{ display:grid; gap:10px; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); }}
          .detail-card {{
            border:1px solid var(--line);
            border-radius:14px;
            background:#111c28;
            padding:12px;
            min-width:0;
            overflow:hidden;
          }}
          .detail-card-wide {{ grid-column:span 2; }}
          .detail-card-action {{ display:flex; flex-direction:column; justify-content:space-between; }}
          .detail-action-stack {{ display:grid; gap:10px; align-items:start; }}
          .detail-chip-row {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }}
          .detail-label {{
            color:var(--muted);
            font-size:12px;
            font-weight:800;
            letter-spacing:0.04em;
            text-transform:uppercase;
            margin-bottom:8px;
          }}
          .detail-value {{ color:var(--ink); line-height:1.5; white-space:normal; }}
          .detail-link {{
            display:inline-flex;
            align-items:center;
            justify-content:center;
            padding:10px 14px;
            border-radius:12px;
            background:rgba(61,217,182,0.10);
            color:var(--accent);
            font-weight:800;
            text-decoration:none;
          }}
          .main-open-link {{
            display:inline-flex;
            align-items:center;
            justify-content:center;
            padding:6px 9px;
            border-radius:999px;
            background:rgba(61,217,182,0.10);
            color:var(--accent);
            text-decoration:none;
            font-weight:800;
            font-size:12px;
            white-space:nowrap;
          }}
          .row-action-stack {{
            display:flex;
            align-items:center;
            gap:8px;
            flex-wrap:wrap;
            min-width:220px;
          }}
          .row-action-stack form {{ margin:0; }}
          .row-action-stack button {{
            width:auto;
            min-width:0;
            padding:6px 9px;
            border-radius:999px;
            font-size:12px;
            white-space:nowrap;
          }}
          .mobile-result-list, .mobile-preset-list {{ display:none; gap:10px; }}
          .mobile-result-card, .mobile-preset-card {{
            border:1px solid var(--line);
            border-radius:14px;
            background:rgba(11,19,29,0.82);
            padding:12px;
          }}
          .mobile-result-head {{
            display:flex;
            justify-content:space-between;
            gap:10px;
            align-items:flex-start;
          }}
          .mobile-result-ticker a {{ color:var(--accent); font-size:15px; font-weight:800; text-decoration:none; }}
          .mobile-result-price {{ display:flex; align-items:flex-start; justify-content:flex-end; }}
          .mobile-result-chip-row {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }}
          .mobile-result-grid {{
            display:grid;
            gap:8px;
            grid-template-columns:repeat(2, minmax(0, 1fr));
            margin-top:10px;
          }}
          .mobile-result-grid > div {{
            border:1px solid rgba(255,255,255,0.04);
            border-radius:12px;
            background:rgba(21,34,49,0.9);
            padding:10px 12px;
            min-width:0;
          }}
          .mobile-result-summary {{
            margin-top:10px;
            color:var(--muted);
            font-size:13px;
            line-height:1.5;
            white-space:normal;
          }}
          .mobile-result-meta {{
            margin-top:8px;
            color:var(--muted);
            font-size:12px;
            line-height:1.5;
            white-space:normal;
          }}
          .mobile-result-actions {{
            display:grid;
            gap:8px;
            margin-top:10px;
          }}
          .detail-collapse-note {{
            margin-top:10px;
            color:var(--muted);
            font-size:12px;
            font-weight:700;
          }}
          .scroll-hint {{ margin-top:10px; font-size:12px; color:var(--muted); }}
          .lang-switch {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-left:auto; }}
          @media (max-width: 920px) {{
            .rules-grid {{ grid-template-columns:1fr; }}
            .action-grid {{ grid-template-columns:1fr; }}
            h1 {{ font-size:30px; }}
            .sticky-col, .sticky-col-1, .sticky-col-2 {{ position:static; box-shadow:none; min-width:auto; }}
            .table-wrap th:nth-child(7), .table-wrap td:nth-child(7),
            .table-wrap th:nth-child(8), .table-wrap td:nth-child(8),
            .table-wrap th:nth-child(9), .table-wrap td:nth-child(9),
            .table-wrap th:nth-child(18), .table-wrap td:nth-child(18) {{ width:auto; min-width:unset; }}
            .detail-card-wide {{ grid-column:span 1; }}
            .row-detail-toggle > summary {{ align-items:flex-start; flex-direction:column; }}
            .detail-summary-meta {{ margin-left:0; }}
          }}
          @media (max-width: 720px) {{
            .results-table-wrap, .saved-strategies-wrap {{ display:none; }}
            .mobile-result-list, .mobile-preset-list {{ display:grid; }}
            .results-toolbar {{ align-items:flex-start; }}
          }}
          @media (max-width: 1120px) {{
            .app {{ grid-template-columns:1fr; }}
            .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }}
            .content {{ padding:20px 10px 40px; }}
          }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{'策略工作台' if lang == 'zh' else 'Strategy Workbench'}</h1>
              <p>{'模型、模板、组合和评测放在同一条选股路径里。' if lang == 'zh' else 'Models, templates, confluence, and evaluation in one screening path.'}</p>
            </div>
            <nav class="side-nav">{render_workspace_nav_html(lang=lang, active_key='screeners')}</nav>
          </aside>
          <main class="content">
        <div class="wrap">
          <div class="toolbar">
            <a href="/dashboard">← {_lang_text(lang, 'back_to_dashboard')}</a>
            <a href="/watchlist">{_lang_text(lang, 'open_watchlist')}</a>
            <a href="/screeners/focus/today?lang={lang}">{_lang_text(lang, 'open_focus_pool')}</a>
            <a href="/screeners/market-snapshot?lang={lang}">{_lang_text(lang, 'open_market_snapshot')}</a>
            <a href="/dashboard/model-performance?lang={lang}&market={market if market in {'CN','US','ALL'} else 'ALL'}">{'模型评测总览' if lang == 'zh' else 'Model Evaluation Overview'}</a>
            <a href="/screeners/kronos-validation?lang={lang}">{'Kronos 二次验证池' if lang == 'zh' else 'Kronos Validation Pool'}</a>
            <a href="/dashboard#cn-fundamental-tickers">{_lang_text(lang, 'sync_cn_fundamentals')}</a>
            <div class="lang-switch">
              <span class="muted">{_lang_text(lang, 'language')}:</span>
              {lang_switch_html}
            </div>
          </div>
          {banner_html}
          <div class="card">
            <div class="eyebrow">{'Strategy Workbench' if lang == 'en' else '策略工作台'}</div>
            <h1>{'先选打法，再看结果' if lang == 'zh' else 'Choose the playbook before the picks'}</h1>
            <p class="lead">{'把单模型、策略模板、多模型共振和模型评测放在同一页，先确定今天用哪套打法，再决定是否加入自选或盯盘池。' if lang == 'zh' else 'Start with a single model, a strategy template, multi-model confluence, or the evaluation overview before moving names into watchlist or focus.'}</p>
            <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:14px;">
              <span class="default-chip">{'Active template' if lang == 'en' else '当前模板'}: {_template_label(model_template, active_template['label'], lang)}</span>
              {active_defaults_html}
            </div>
          </div>
          {screen_overview_html}
          {run_receipt_html}
          {template_read_html}
          {template_overview_brief_html}
          {lightgbm_bias_bar_html}
          {template_evaluation_html}
          {multi_template_summary_html}
          {confluence_strength_html}
          {confluence_leaderboard_html}
          {confluence_bucket_groups_html}
          {quality_profile_status_html}
	          <section class="section-stack">
	            <article id="single-model-rules" class="card">
              <div class="eyebrow">{_lang_text(lang, 'rules')}</div>
              <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px;">{quick_confluence_presets_html}</div>
              <div class="template-group-stack">{template_cards_html}</div>
              <form class="stack" method="get" action="/screeners">
                <input type="hidden" name="lang" value="{lang}" />
                <input type="hidden" name="run" value="1" />
                <div class="summary-note">{'先选模板，再决定是否展开高级规则。' if lang == 'zh' else 'Start with a template, then open advanced rules only if needed.'}</div>
                <div class="rules-grid">
                  <div>
                    <label class="muted">{_lang_text(lang, 'model_template')}</label>
                    <select name="model_template">{template_option_html}</select>
                  </div>
                  <div>
                    <label class="muted">{'共振最少命中模型数' if lang == 'zh' else 'Minimum multi-model hits'}</label>
                    <input type="number" name="min_multi_model_hits" min="1" max="{max(2, len(MODEL_TEMPLATES))}" value="{min_multi_model_hits}" />
                  </div>
                  <div>
                    <label class="muted">{'共振动作桶' if lang == 'zh' else 'Confluence action bucket'}</label>
                    <select name="confluence_action_filter">{confluence_option_html}</select>
                  </div>
                  <div>
                    <label class="muted">{_lang_text(lang, 'universe')}</label>
                    <select name="universe">{universe_option_html}</select>
                  </div>
                  <div>
                    <label class="muted">{_lang_text(lang, 'market')}</label>
                    <select name="market">{market_option_html}</select>
                  </div>
                  <div>
                    <label class="muted">{'结果排序' if lang == 'zh' else 'Result sort'}</label>
                    <select name="sort_by">{sort_by_option_html}</select>
                  </div>
                  <div>
                    <label class="muted">{'排序方向' if lang == 'zh' else 'Sort order'}</label>
                    <select name="sort_order">{sort_order_option_html}</select>
                  </div>
                  <div>
                    <label class="muted">{_lang_text(lang, 'min_trend_score')}</label>
                    <input type="number" name="min_trend_score" min="1" max="99" value="{min_trend_score}" />
                  </div>
                  <div>
                    <label class="muted">{_lang_text(lang, 'action_filter')}</label>
                    <select name="action_filter">{action_option_html}</select>
                  </div>
                  <div>
                    <label class="muted">{_lang_text(lang, 'min_volume_strength')}</label>
                    <input type="number" name="min_volume_ratio" min="0" step="0.1" value="{min_volume_ratio}" />
                  </div>
                </div>
                <div id="multi-model-combos" class="summary-note">{'如果想做多模型共振，勾选两个或以上模板。系统会按同一只股票被多少个模型同时命中来排序。' if lang == 'zh' else 'For confluence screening, tick two or more templates. The screener will rank names by how many models hit the same ticker.'}</div>
                <div class="multi-template-stack">{multi_template_picker_html}</div>
                <details class="advanced-panel">
                  <summary>{_lang_text(lang, 'cn_rules')}</summary>
                  <div class="summary-note">{'这些参数保留给需要做精细筛选的时候。' if lang == 'zh' else 'Use these only when you need a more precise filter pass.'}</div>
                  <div style="height:12px;"></div>
                  <div class="rules-grid">
                    <div>
                      <label class="muted">{_lang_text(lang, 'min_listing_days')}</label>
                      <input type="number" name="min_listing_days" min="1" value="{min_listing_days}" />
                    </div>
                    <div>
                      <label class="muted">{_lang_text(lang, 'pe_range')}</label>
                      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">
                        <input type="number" name="pe_min" step="0.1" value="{pe_min}" />
                        <input type="number" name="pe_max" step="0.1" value="{pe_max}" />
                      </div>
                    </div>
                    <div>
                      <label class="muted">{_lang_text(lang, 'min_roe_3y')}</label>
                      <input type="number" name="min_roe_avg_3y" step="0.1" value="{min_roe_avg_3y}" />
                    </div>
                    <div>
                      <label class="muted">{_lang_text(lang, 'min_profit_yoy')}</label>
                      <input type="number" name="min_net_profit_yoy" step="0.1" value="{min_net_profit_yoy}" />
                    </div>
                    <div>
                      <label class="muted">{_lang_text(lang, 'min_revenue_yoy')}</label>
                      <input type="number" name="min_revenue_yoy" step="0.1" value="{min_revenue_yoy}" />
                    </div>
                    <div>
                      <label class="muted">{_lang_text(lang, 'max_debt')}</label>
                      <input type="number" name="max_debt_to_assets" step="0.1" value="{max_debt_to_assets}" />
                    </div>
                    <div>
                      <label class="muted">{_lang_text(lang, 'min_dividend')}</label>
                      <input type="number" name="min_dividend_yield" step="0.1" value="{min_dividend_yield}" />
                    </div>
                    <div>
                      <label class="muted">{_lang_text(lang, 'exclude_bottom_cap')}</label>
                      <input type="number" name="exclude_bottom_market_cap_pct" min="0" max="49" step="1" value="{exclude_bottom_market_cap_pct}" />
                    </div>
                    <div>
                      <label class="muted">{_lang_text(lang, 'recent_snapshot_runs')}</label>
                      <input type="number" name="recent_snapshot_runs" min="0" max="10" step="1" value="{recent_snapshot_runs}" />
                    </div>
                    <div>
                      <label class="muted">{_lang_text(lang, 'min_snapshot_hits')}</label>
                      <input type="number" name="min_snapshot_hits" min="0" max="10" step="1" value="{min_snapshot_hits}" />
                    </div>
                    <div>
                      <label class="muted">{_lang_text(lang, 'model_signal_filter')}</label>
                      <select name="model_signal_filter">{signal_option_html}</select>
                    </div>
                    <div>
                      <label class="muted">{_lang_text(lang, 'min_model_signal_strength')}</label>
                      <input type="number" name="min_model_signal_strength" min="0" max="100" step="1" value="{min_model_signal_strength}" />
                    </div>
                    <div>
                      <label class="muted">{_lang_text(lang, 'execution_tag_filter')}</label>
                      <input type="text" name="execution_tag_filter" list="execution-tag-options" value="{execution_tag_filter if str(execution_tag_filter).upper() != 'ALL' else ''}" placeholder="gap-risk, earnings-soon" />
                    </div>
                    <div>
                      <label class="muted">{_lang_text(lang, 'exclude_execution_tag_filter')}</label>
                      <input type="text" name="exclude_execution_tag_filter" list="execution-tag-options" value="{exclude_execution_tag_filter if str(exclude_execution_tag_filter).upper() != 'ALL' else ''}" placeholder="gap-risk, earnings-soon" />
                    </div>
                  </div>
                  <div style="margin-top:12px;">
                    <div class="muted" style="margin-bottom:8px;font-weight:700;">{'Quick Tags' if lang == 'en' else '快捷标签'}</div>
                    <div style="display:flex;flex-wrap:wrap;gap:8px;">
                      <button type="button" onclick="appendExecutionTag('execution_tag_filter', 'gap-risk')">gap-risk</button>
                      <button type="button" onclick="appendExecutionTag('execution_tag_filter', 'earnings-soon')">earnings-soon</button>
                      <button type="button" onclick="appendExecutionTag('execution_tag_filter', 'thin-liquidity')">thin-liquidity</button>
                      <button type="button" onclick="appendExecutionTag('exclude_execution_tag_filter', 'gap-risk')">{'exclude gap-risk' if lang == 'en' else '排除 gap-risk'}</button>
                      <button type="button" onclick="clearExecutionTags()">{'Clear Tags' if lang == 'en' else '清空标签'}</button>
                    </div>
                  </div>
                  <datalist id="execution-tag-options">
                    <option value="gap-risk"></option>
                    <option value="earnings-soon"></option>
                    <option value="thin-liquidity"></option>
                  </datalist>
                </details>
	                <button type="submit">{_lang_text(lang, 'run_screener')}</button>
	              </form>
	            </article>
	            {regression_discipline_html}
	            <article class="card">
	              <div class="eyebrow">{_lang_text(lang, 'risk_overview')}</div>
              <div class="rules-grid">
                <div class="detail-card" style="background:#f9f7f0;">
                  <div class="detail-label">{_lang_text(lang, 'tagged_names')}</div>
                  <div style="font-size:28px;font-weight:800;margin:6px 0;">{tagged_names}</div>
                  <div class="muted">{_lang_text(lang, 'risk_examples')}</div>
                </div>
                <div class="detail-card detail-card-wide" style="background:#f9f7f0;">
                  <div class="detail-label">{_lang_text(lang, 'common_risks')}</div>
                  <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px;">
                    {risk_top_tags_html}
                  </div>
                  <div class="muted">{_lang_text(lang, 'risk_examples')}: {risk_examples_html}</div>
                </div>
              </div>
            </article>
            <article class="card">
              <div class="action-grid" style="margin-bottom:14px;">
                <form class="action-form" method="post" action="/screeners/save">
                  <div class="action-head">
                    <div>
                      <div class="action-kicker">{'Strategy' if lang == 'en' else '策略'}</div>
                      <div class="action-title">{_lang_text(lang, 'save_strategy')}</div>
                    </div>
                    <span class="action-icon">SAVE</span>
                  </div>
                  <div class="action-row">
                    <label class="action-label">{_lang_text(lang, 'strategy_name')}</label>
                    <div class="action-input-wrap">
                      <input type="text" name="preset_name" placeholder="{_lang_text(lang, 'strategy_name')}" required />
                    </div>
                  </div>
                  {hidden_fields}
                  <div class="action-submit">
                    <button type="submit">{_lang_text(lang, 'save_as_strategy')}</button>
                  </div>
                </form>
                <form class="action-form" method="post" action="/screeners/add-all-to-watchlist">
                  <div class="action-head">
                    <div>
                      <div class="action-kicker">{'Watchlist' if lang == 'en' else '自选股'}</div>
                      <div class="action-title">{_lang_text(lang, 'add_current_results')}</div>
                    </div>
                    <span class="action-icon">LIST</span>
                  </div>
                  {hidden_fields}
                  <div class="action-row">
                    <label class="action-label">{_lang_text(lang, 'only_add_top_n')}</label>
                    <div class="action-input-wrap">
                      <input type="number" name="bulk_top_n" min="0" value="0" />
                    </div>
                  </div>
                  <label class="action-checkbox">
                    <input type="checkbox" name="auto_enable_sync" value="1" />
                    {_lang_text(lang, 'auto_enable_sync')}
                  </label>
                  <div class="action-submit">
                  <button type="submit" {bulk_add_disabled}>{bulk_add_label if snapshot_ready else _lang_text(lang, 'snapshot_pending_short')}</button>
                  </div>
                </form>
                <form class="action-form" method="post" action="/screeners/add-to-focus">
                  <div class="action-head">
                    <div>
                      <div class="action-kicker">{'Focus' if lang == 'en' else '盯盘池'}</div>
                      <div class="action-title">{_lang_text(lang, 'add_current_results_to_focus')}</div>
                    </div>
                    <span class="action-icon">FOCUS</span>
                  </div>
                  {hidden_fields}
                  <div class="action-row">
                    <label class="action-label">{_lang_text(lang, 'focus_top_n')}</label>
                    <div class="action-input-wrap">
                      <input type="number" name="focus_top_n" min="0" value="10" />
                    </div>
                  </div>
                  <div class="action-submit">
                    <button type="submit" {bulk_add_disabled}>{_lang_text(lang, 'add_current_results_to_focus') if snapshot_ready else _lang_text(lang, 'snapshot_pending_short')}</button>
                  </div>
                </form>
                <form class="action-form" method="post" action="/screeners/sync-top-results">
                  <div class="action-head">
                    <div>
                      <div class="action-kicker">{'Sync' if lang == 'en' else '同步'}</div>
                      <div class="action-title">{_lang_text(lang, 'sync_top_n_now')}</div>
                    </div>
                    <span class="action-icon">SYNC</span>
                  </div>
                  {hidden_fields}
                  <div class="action-row">
                    <label class="action-label">{_lang_text(lang, 'sync_top_n_help')}</label>
                    <div class="action-input-wrap">
                      <input type="number" name="sync_top_n" min="0" value="5" />
                    </div>
                  </div>
                  <div class="action-submit">
                    <button type="submit" {'disabled' if not actions_available else ''}>{_lang_text(lang, 'sync_top_n_now') if snapshot_ready else _lang_text(lang, 'snapshot_pending_short')}</button>
                  </div>
                </form>
              </div>
              <div class="eyebrow">{_lang_text(lang, 'results')}</div>
              <div class="results-toolbar">
                <div class="muted">{total_results} {_lang_text(lang, 'stocks_matched')}</div>
                <form method="get" action="/screeners/export">
                  {hidden_fields}
                  <button type="submit" {'disabled' if not actions_available else ''}>{_lang_text(lang, 'export_csv') if snapshot_ready else _lang_text(lang, 'snapshot_pending_short')}</button>
                </form>
              </div>
              <div class="muted" style="margin-bottom:12px;">{visible_note}</div>
              <div class="table-wrap results-table-wrap">
                <table>
                  <thead>
                    <tr><th class='sticky-col sticky-col-1'>{header_link(_lang_text(lang, 'ticker'), 'ticker')}</th><th class='sticky-col sticky-col-2'>{_lang_text(lang, 'name')}</th><th class='sticky-col sticky-col-3'>{_lang_text(lang, 'action')}</th><th>{header_link(_lang_text(lang, 'trend'), 'trend_score')}</th><th>{_lang_text(lang, 'market')}</th><th>{header_link(_lang_text(lang, 'close'), 'latest_close')}</th><th>{header_link(_lang_text(lang, 'model'), 'model_signal_strength')}</th><th>{header_link(_lang_text(lang, 'watchlist'), 'watchlist_state')}</th><th>{header_link('Hits', 'snapshot_hits')}</th><th>{header_link('5D %', 'momentum_5')}</th><th>{header_link('20D %', 'momentum_20')}</th><th>{header_link('Volume', 'volume_ratio')}</th><th>{header_link('PE', 'pe_ttm')}</th><th>{header_link('ROE 3Y', 'roe_avg_3y')}</th><th>{header_link('Profit YoY', 'net_profit_yoy')}</th><th>{header_link('Dividend %', 'dividend_yield')}</th><th>{header_link('Breakout %', 'distance_to_breakout_pct')}</th><th>{_lang_text(lang, 'insight')}</th></tr>
                  </thead>
                  <tbody>{rows}</tbody>
                </table>
              </div>
              <div class="mobile-result-list">{mobile_result_cards}</div>
              <div class="scroll-hint">{_lang_text(lang, 'drag_hint')}</div>
            </article>
          </section>
          {guidance_bridge_html}
          <section id="strategy-workbench" class="workbench-grid">{strategy_workbench_html}</section>
          <section id="strategy-templates" class="strategy-template-section">
            <article class="card">
              <div class="eyebrow">{'策略模板' if lang == 'zh' else 'Strategy Templates'}</div>
              <h2 style="margin:0 0 6px;">{'一键运行常用组合' if lang == 'zh' else 'Run the common playbooks in one click'}</h2>
              <p class="lead">{'这些模板会复用完整筛选参数，适合每天盘后先跑一遍，再把结果加入自选或今日盯盘池。' if lang == 'zh' else 'These templates reuse the full parameter set, making them a fast after-hours first pass before watchlist or focus-pool review.'}</p>
              <div class="strategy-template-actions" style="margin-top:12px;"><a class="detail-link" href="#my-strategies">{'管理我的策略' if lang == 'zh' else 'Manage My Strategies'}</a></div>
            </article>
            <div class="strategy-template-grid">{strategy_template_cards_html}</div>
          </section>
          <section id="my-strategies" class="card">
            <div class="eyebrow">{_lang_text(lang, 'saved_strategies')}</div>
            <h2 style="margin:0 0 6px;">{'我的策略管理' if lang == 'zh' else 'My Strategy Manager'}</h2>
            <p class="lead" style="margin-bottom:14px;">{'这里保存的是你自己的筛选模板。可以重新运行、改名、删除，也可以跳到模型评测先看近期表现。' if lang == 'zh' else 'Saved strategies keep your own screening templates. You can rerun, rename, delete, or jump into model evaluation before using them.'}</p>
            <div class="table-wrap saved-strategies-wrap">
            <table>
              <thead>
                <tr><th>{_lang_text(lang, 'name')}</th><th>{_lang_text(lang, 'model_template')}</th><th>{_lang_text(lang, 'summary')}</th><th>{_lang_text(lang, 'hits')}</th><th>{_lang_text(lang, 'run_strategy')}</th><th>{_lang_text(lang, 'rename')}</th><th>{_lang_text(lang, 'delete')}</th></tr>
              </thead>
              <tbody>{preset_rows}</tbody>
            </table>
            </div>
            <div class="mobile-preset-list">{mobile_preset_cards}</div>
          </section>
        </div>
          </main>
        </div>
        <script>
          function appendExecutionTag(inputName, tag) {{
            const form = document.querySelector('form[action="/screeners"]');
            if (!form) return;
            const input = form.querySelector(`input[name="${{inputName}}"]`);
            if (!input) return;
            const values = input.value.split(",").map((item) => item.trim()).filter(Boolean);
            if (!values.includes(tag)) {{
              values.push(tag);
            }}
            input.value = values.join(", ");
            input.focus();
          }}

          function clearExecutionTags() {{
            const form = document.querySelector('form[action="/screeners"]');
            if (!form) return;
            const includeInput = form.querySelector('input[name="execution_tag_filter"]');
            const excludeInput = form.querySelector('input[name="exclude_execution_tag_filter"]');
            if (includeInput) includeInput.value = "";
            if (excludeInput) excludeInput.value = "";
            if (includeInput) includeInput.focus();
          }}
        </script>
      </body>
    </html>
    """


@router.post("/save")
def save_screener_preset(
    request: Request,
    preset_name: str = Form(...),
    lang: str = Form("en"),
    model_template: str = Form("technical_momentum"),
    multi_model_templates: list[str] = Form([]),
    min_multi_model_hits: int = Form(2),
    confluence_action_filter: str = Form("ALL"),
    universe: str = Form("watchlist"),
    market: str = Form("ALL"),
    min_trend_score: int = Form(60),
    action_filter: str = Form("ALL"),
    min_volume_ratio: float = Form(0.0),
    min_listing_days: int = Form(365),
    pe_min: float = Form(0.0),
    pe_max: float = Form(30.0),
    min_roe_avg_3y: float = Form(12.0),
    min_net_profit_yoy: float = Form(20.0),
    min_revenue_yoy: float = Form(0.0),
    max_debt_to_assets: float = Form(100.0),
    min_dividend_yield: float = Form(0.0),
    exclude_bottom_market_cap_pct: float = Form(10.0),
    recent_snapshot_runs: int = Form(0),
    min_snapshot_hits: int = Form(0),
    model_signal_filter: str = Form("ALL"),
    min_model_signal_strength: float = Form(0.0),
    execution_tag_filter: str = Form("ALL"),
    exclude_execution_tag_filter: str = Form("ALL"),
    sort_by: str = Form("default"),
    sort_order: str = Form("desc"),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners")
    params = _current_params(
        lang=lang,
        model_template=model_template,
        multi_model_templates=multi_model_templates,
        min_multi_model_hits=min_multi_model_hits,
        confluence_action_filter=confluence_action_filter,
        universe=universe,
        market=market,
        min_trend_score=min_trend_score,
        action_filter=action_filter,
        min_volume_ratio=min_volume_ratio,
        min_listing_days=min_listing_days,
        pe_min=pe_min,
        pe_max=pe_max,
        min_roe_avg_3y=min_roe_avg_3y,
        min_net_profit_yoy=min_net_profit_yoy,
        min_revenue_yoy=min_revenue_yoy,
        max_debt_to_assets=max_debt_to_assets,
        min_dividend_yield=min_dividend_yield,
        exclude_bottom_market_cap_pct=exclude_bottom_market_cap_pct,
        recent_snapshot_runs=recent_snapshot_runs,
        min_snapshot_hits=min_snapshot_hits,
        model_signal_filter=model_signal_filter,
        min_model_signal_strength=min_model_signal_strength,
        execution_tag_filter=execution_tag_filter,
        exclude_execution_tag_filter=exclude_execution_tag_filter,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    presets = _load_saved_presets(db)
    clean_name = preset_name.strip()
    filtered = [preset for preset in presets if preset.get("name") != clean_name]
    filtered.insert(0, {"name": clean_name, "params": params, "updated_at": app_now_iso()})
    _save_saved_presets(db, filtered[:20])
    return RedirectResponse(
        url=f"{_build_screen_query(params)}&message={urlencode({'m': f'Saved strategy: {clean_name}'})[2:]}",
        status_code=303,
    )


@router.post("/rename")
def rename_screener_preset(
    request: Request,
    preset_name: str = Form(...),
    new_name: str = Form(...),
    lang: str = Form("en"),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners")
    old_name = preset_name.strip()
    clean_name = new_name.strip()
    if not old_name or not clean_name:
        return _redirect_with_message("Strategy name cannot be empty.", lang=lang)
    presets = _load_saved_presets(db)
    renamed: list[dict] = []
    found = False
    for preset in presets:
        current_name = str(preset.get("name") or "").strip()
        if current_name == old_name:
            renamed.append({**preset, "name": clean_name, "updated_at": app_now_iso()})
            found = True
            continue
        if current_name == clean_name:
            continue
        renamed.append(preset)
    if found:
        _save_saved_presets(db, renamed[:20])
        return _redirect_with_message(f"Renamed strategy: {clean_name}", lang=lang)
    return _redirect_with_message(f"Strategy not found: {old_name}", lang=lang)


@router.post("/delete")
def delete_screener_preset(
    request: Request,
    preset_name: str = Form(...),
    lang: str = Form("en"),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners")
    clean_name = preset_name.strip()
    presets = _load_saved_presets(db)
    filtered = [preset for preset in presets if preset.get("name") != clean_name]
    _save_saved_presets(db, filtered)
    return _redirect_with_message(f"Deleted strategy: {clean_name}", lang=lang)


@router.get("/export")
def export_screener_csv(
    request: Request,
    lang: str = Query("en"),
    model_template: str = Query("technical_momentum"),
    multi_model_templates: list[str] = Query([]),
    min_multi_model_hits: int = Query(2),
    confluence_action_filter: str = Query("ALL"),
    universe: str = Query("watchlist"),
    market: str = Query("ALL"),
    min_trend_score: int = Query(60),
    action_filter: str = Query("ALL"),
    min_volume_ratio: float = Query(0.0),
    min_listing_days: int = Query(365),
    pe_min: float = Query(0.0),
    pe_max: float = Query(30.0),
    min_roe_avg_3y: float = Query(12.0),
    min_net_profit_yoy: float = Query(20.0),
    min_revenue_yoy: float = Query(0.0),
    max_debt_to_assets: float = Query(100.0),
    min_dividend_yield: float = Query(0.0),
    exclude_bottom_market_cap_pct: float = Query(10.0),
    recent_snapshot_runs: int = Query(0),
    min_snapshot_hits: int = Query(0),
    model_signal_filter: str = Query("ALL"),
    min_model_signal_strength: float = Query(0.0),
    execution_tag_filter: str = Query("ALL"),
    exclude_execution_tag_filter: str = Query("ALL"),
    sort_by: str = Query("default"),
    sort_order: str = Query("desc"),
) -> Response:
    if not is_authenticated(request):
        return login_redirect("/screeners")
    params = _current_params(
        lang=lang,
        model_template=model_template,
        multi_model_templates=multi_model_templates,
        min_multi_model_hits=min_multi_model_hits,
        confluence_action_filter=confluence_action_filter,
        universe=universe,
        market=market,
        min_trend_score=min_trend_score,
        action_filter=action_filter,
        min_volume_ratio=min_volume_ratio,
        min_listing_days=min_listing_days,
        pe_min=pe_min,
        pe_max=pe_max,
        min_roe_avg_3y=min_roe_avg_3y,
        min_net_profit_yoy=min_net_profit_yoy,
        min_revenue_yoy=min_revenue_yoy,
        max_debt_to_assets=max_debt_to_assets,
        min_dividend_yield=min_dividend_yield,
        exclude_bottom_market_cap_pct=exclude_bottom_market_cap_pct,
        recent_snapshot_runs=recent_snapshot_runs,
        min_snapshot_hits=min_snapshot_hits,
        model_signal_filter=model_signal_filter,
        min_model_signal_strength=min_model_signal_strength,
        execution_tag_filter=execution_tag_filter,
        exclude_execution_tag_filter=exclude_execution_tag_filter,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    if not _screen_snapshot_ready(ScreenerService(), params):
        return RedirectResponse(
            url=f"{_build_screen_query(params)}&message={urlencode({'m': _lang_text(lang, 'snapshot_pending_export')})[2:]}",
            status_code=303,
        )
    results = _run_screen(ScreenerService(), params)
    buffer = StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=[
            "ticker",
            "name",
            "market",
            "model_hit_count",
            "matched_model_templates",
            "trend_score",
            "action_label",
            "latest_close",
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
            "selection_reason",
        ],
    )
    writer.writeheader()
    for item in results:
        row = {key: item.get(key) for key in writer.fieldnames}
        row["model_execution_tags"] = ";".join(item.get("model_execution_tags") or [])
        row["matched_model_templates"] = ";".join(item.get("matched_model_templates") or [])
        writer.writerow(row)
    filename = (
        f"multi_model_{len(_normalize_multi_model_templates(multi_model_templates))}_screener.csv"
        if len(_normalize_multi_model_templates(multi_model_templates)) >= 2
        else f"{model_template}_screener.csv"
    )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.post("/add-to-watchlist")
def add_screener_result_to_watchlist(
    request: Request,
    ticker: str = Form(...),
    name: str | None = Form(None),
    symbol_market: str | None = Form(None),
    lang: str = Form("en"),
    model_template: str = Form("technical_momentum"),
    multi_model_templates: list[str] = Form([]),
    min_multi_model_hits: int = Form(2),
    confluence_action_filter: str = Form("ALL"),
    universe: str = Form("watchlist"),
    market: str = Form("ALL"),
    min_trend_score: int = Form(60),
    action_filter: str = Form("ALL"),
    min_volume_ratio: float = Form(0.0),
    min_listing_days: int = Form(365),
    pe_min: float = Form(0.0),
    pe_max: float = Form(30.0),
    min_roe_avg_3y: float = Form(12.0),
    min_net_profit_yoy: float = Form(20.0),
    min_revenue_yoy: float = Form(0.0),
    max_debt_to_assets: float = Form(100.0),
    min_dividend_yield: float = Form(0.0),
    exclude_bottom_market_cap_pct: float = Form(10.0),
    recent_snapshot_runs: int = Form(0),
    min_snapshot_hits: int = Form(0),
    model_signal_filter: str = Form("ALL"),
    min_model_signal_strength: float = Form(0.0),
    execution_tag_filter: str = Form("ALL"),
    exclude_execution_tag_filter: str = Form("ALL"),
    sort_by: str = Form("default"),
    sort_order: str = Form("desc"),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners")
    symbol_repo = SymbolRepository(db)
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    symbol = symbol_repo.get_or_create_symbol(
        SymbolCreate(
            ticker=ticker,
            name=name,
            market=symbol_market,
        )
    )
    watchlist_repo.add_symbol(watchlist.id, symbol.id)
    refresh_workspace_snapshots(db)
    params = _current_params(
        lang=lang,
        model_template=model_template,
        multi_model_templates=multi_model_templates,
        min_multi_model_hits=min_multi_model_hits,
        confluence_action_filter=confluence_action_filter,
        universe=universe,
        market=market,
        min_trend_score=min_trend_score,
        action_filter=action_filter,
        min_volume_ratio=min_volume_ratio,
        min_listing_days=min_listing_days,
        pe_min=pe_min,
        pe_max=pe_max,
        min_roe_avg_3y=min_roe_avg_3y,
        min_net_profit_yoy=min_net_profit_yoy,
        min_revenue_yoy=min_revenue_yoy,
        max_debt_to_assets=max_debt_to_assets,
        min_dividend_yield=min_dividend_yield,
        exclude_bottom_market_cap_pct=exclude_bottom_market_cap_pct,
        recent_snapshot_runs=recent_snapshot_runs,
        min_snapshot_hits=min_snapshot_hits,
        model_signal_filter=model_signal_filter,
        min_model_signal_strength=min_model_signal_strength,
        execution_tag_filter=execution_tag_filter,
        exclude_execution_tag_filter=exclude_execution_tag_filter,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return RedirectResponse(
        url=f"{_build_screen_query(params)}&message={urlencode({'m': f'Added {ticker} to watchlist · dashboard refreshed'})[2:]}",
        status_code=303,
    )


@router.post("/add-all-to-watchlist")
def add_all_screener_results_to_watchlist(
    request: Request,
    lang: str = Form("en"),
    model_template: str = Form("technical_momentum"),
    multi_model_templates: list[str] = Form([]),
    min_multi_model_hits: int = Form(2),
    confluence_action_filter: str = Form("ALL"),
    universe: str = Form("watchlist"),
    market: str = Form("ALL"),
    min_trend_score: int = Form(60),
    action_filter: str = Form("ALL"),
    min_volume_ratio: float = Form(0.0),
    min_listing_days: int = Form(365),
    pe_min: float = Form(0.0),
    pe_max: float = Form(30.0),
    min_roe_avg_3y: float = Form(12.0),
    min_net_profit_yoy: float = Form(20.0),
    min_revenue_yoy: float = Form(0.0),
    max_debt_to_assets: float = Form(100.0),
    min_dividend_yield: float = Form(0.0),
    exclude_bottom_market_cap_pct: float = Form(10.0),
    recent_snapshot_runs: int = Form(0),
    min_snapshot_hits: int = Form(0),
    model_signal_filter: str = Form("ALL"),
    min_model_signal_strength: float = Form(0.0),
    execution_tag_filter: str = Form("ALL"),
    exclude_execution_tag_filter: str = Form("ALL"),
    sort_by: str = Form("default"),
    sort_order: str = Form("desc"),
    bulk_top_n: int = Form(0),
    auto_enable_sync: str | None = Form(None),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners")
    params = _current_params(
        lang=lang,
        model_template=model_template,
        multi_model_templates=multi_model_templates,
        min_multi_model_hits=min_multi_model_hits,
        confluence_action_filter=confluence_action_filter,
        universe=universe,
        market=market,
        min_trend_score=min_trend_score,
        action_filter=action_filter,
        min_volume_ratio=min_volume_ratio,
        min_listing_days=min_listing_days,
        pe_min=pe_min,
        pe_max=pe_max,
        min_roe_avg_3y=min_roe_avg_3y,
        min_net_profit_yoy=min_net_profit_yoy,
        min_revenue_yoy=min_revenue_yoy,
        max_debt_to_assets=max_debt_to_assets,
        min_dividend_yield=min_dividend_yield,
        exclude_bottom_market_cap_pct=exclude_bottom_market_cap_pct,
        recent_snapshot_runs=recent_snapshot_runs,
        min_snapshot_hits=min_snapshot_hits,
        model_signal_filter=model_signal_filter,
        min_model_signal_strength=min_model_signal_strength,
        execution_tag_filter=execution_tag_filter,
        exclude_execution_tag_filter=exclude_execution_tag_filter,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    if not _screen_snapshot_ready(ScreenerService(), params):
        return RedirectResponse(
            url=f"{_build_screen_query(params)}&message={urlencode({'m': _snapshot_pending_message(lang)})[2:]}",
            status_code=303,
        )
    added, already_in_watchlist, sync_enabled_count = _add_screen_results_to_watchlist(
        db=db,
        params=params,
        top_n=bulk_top_n,
        auto_enable_sync=auto_enable_sync == "1",
    )
    refresh_workspace_snapshots(db)
    if added:
        message = f"Added {added} screener results to watchlist · dashboard refreshed"
    elif already_in_watchlist:
        message = "All matching stocks are already in your watchlist · dashboard refreshed"
    else:
        message = "No matching stocks to add"
    if sync_enabled_count:
        message += f" · Sync enabled for {sync_enabled_count}"
    return RedirectResponse(
        url=f"{_build_screen_query(params)}&message={urlencode({'m': message})[2:]}",
        status_code=303,
    )


@router.post("/sync-symbol")
def sync_screener_symbol(
    request: Request,
    ticker: str = Form(...),
    item_id: int | None = Form(None),
    lang: str = Form("en"),
    model_template: str = Form("technical_momentum"),
    multi_model_templates: list[str] = Form([]),
    min_multi_model_hits: int = Form(2),
    confluence_action_filter: str = Form("ALL"),
    universe: str = Form("watchlist"),
    market: str = Form("ALL"),
    min_trend_score: int = Form(60),
    action_filter: str = Form("ALL"),
    min_volume_ratio: float = Form(0.0),
    min_listing_days: int = Form(365),
    pe_min: float = Form(0.0),
    pe_max: float = Form(30.0),
    min_roe_avg_3y: float = Form(12.0),
    min_net_profit_yoy: float = Form(20.0),
    min_revenue_yoy: float = Form(0.0),
    max_debt_to_assets: float = Form(100.0),
    min_dividend_yield: float = Form(0.0),
    exclude_bottom_market_cap_pct: float = Form(10.0),
    recent_snapshot_runs: int = Form(0),
    min_snapshot_hits: int = Form(0),
    model_signal_filter: str = Form("ALL"),
    min_model_signal_strength: float = Form(0.0),
    execution_tag_filter: str = Form("ALL"),
    exclude_execution_tag_filter: str = Form("ALL"),
    sort_by: str = Form("default"),
    sort_order: str = Form("desc"),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners")
    watchlist_repo = WatchlistRepository(db)
    if item_id is not None:
        watchlist_repo.set_sync_enabled(item_id, True)
    results = sync_market_data(tickers=[ticker], start_date="2025-01-01", provider="auto")
    result = results[0] if results else None
    if result and result["status"] == "success":
        message = f"Synced {ticker} with {result['rows']} rows"
    elif result:
        message = f"Sync failed for {ticker}: {result.get('message', 'Unknown error')}"
    else:
        message = f"Sync did not return a result for {ticker}"
    params = _current_params(
        lang=lang,
        model_template=model_template,
        multi_model_templates=multi_model_templates,
        min_multi_model_hits=min_multi_model_hits,
        confluence_action_filter=confluence_action_filter,
        universe=universe,
        market=market,
        min_trend_score=min_trend_score,
        action_filter=action_filter,
        min_volume_ratio=min_volume_ratio,
        min_listing_days=min_listing_days,
        pe_min=pe_min,
        pe_max=pe_max,
        min_roe_avg_3y=min_roe_avg_3y,
        min_net_profit_yoy=min_net_profit_yoy,
        min_revenue_yoy=min_revenue_yoy,
        max_debt_to_assets=max_debt_to_assets,
        min_dividend_yield=min_dividend_yield,
        exclude_bottom_market_cap_pct=exclude_bottom_market_cap_pct,
        recent_snapshot_runs=recent_snapshot_runs,
        min_snapshot_hits=min_snapshot_hits,
        model_signal_filter=model_signal_filter,
        min_model_signal_strength=min_model_signal_strength,
        execution_tag_filter=execution_tag_filter,
        exclude_execution_tag_filter=exclude_execution_tag_filter,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return RedirectResponse(
        url=f"{_build_screen_query(params)}&message={urlencode({'m': message})[2:]}",
        status_code=303,
    )


@router.post("/sync-top-results")
def sync_top_screener_results(
    request: Request,
    lang: str = Form("en"),
    model_template: str = Form("technical_momentum"),
    multi_model_templates: list[str] = Form([]),
    min_multi_model_hits: int = Form(2),
    confluence_action_filter: str = Form("ALL"),
    universe: str = Form("watchlist"),
    market: str = Form("ALL"),
    min_trend_score: int = Form(60),
    action_filter: str = Form("ALL"),
    min_volume_ratio: float = Form(0.0),
    min_listing_days: int = Form(365),
    pe_min: float = Form(0.0),
    pe_max: float = Form(30.0),
    min_roe_avg_3y: float = Form(12.0),
    min_net_profit_yoy: float = Form(20.0),
    min_revenue_yoy: float = Form(0.0),
    max_debt_to_assets: float = Form(100.0),
    min_dividend_yield: float = Form(0.0),
    exclude_bottom_market_cap_pct: float = Form(10.0),
    recent_snapshot_runs: int = Form(0),
    min_snapshot_hits: int = Form(0),
    model_signal_filter: str = Form("ALL"),
    min_model_signal_strength: float = Form(0.0),
    execution_tag_filter: str = Form("ALL"),
    exclude_execution_tag_filter: str = Form("ALL"),
    sort_by: str = Form("default"),
    sort_order: str = Form("desc"),
    sync_top_n: int = Form(5),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners")
    params = _current_params(
        lang=lang,
        model_template=model_template,
        multi_model_templates=multi_model_templates,
        min_multi_model_hits=min_multi_model_hits,
        confluence_action_filter=confluence_action_filter,
        universe=universe,
        market=market,
        min_trend_score=min_trend_score,
        action_filter=action_filter,
        min_volume_ratio=min_volume_ratio,
        min_listing_days=min_listing_days,
        pe_min=pe_min,
        pe_max=pe_max,
        min_roe_avg_3y=min_roe_avg_3y,
        min_net_profit_yoy=min_net_profit_yoy,
        min_revenue_yoy=min_revenue_yoy,
        max_debt_to_assets=max_debt_to_assets,
        min_dividend_yield=min_dividend_yield,
        exclude_bottom_market_cap_pct=exclude_bottom_market_cap_pct,
        recent_snapshot_runs=recent_snapshot_runs,
        min_snapshot_hits=min_snapshot_hits,
        model_signal_filter=model_signal_filter,
        min_model_signal_strength=min_model_signal_strength,
        execution_tag_filter=execution_tag_filter,
        exclude_execution_tag_filter=exclude_execution_tag_filter,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    if not _screen_snapshot_ready(ScreenerService(), params):
        return RedirectResponse(
            url=f"{_build_screen_query(params)}&message={urlencode({'m': _snapshot_pending_message(lang)})[2:]}",
            status_code=303,
        )
    results = _run_screen(ScreenerService(), params)
    if universe == "watchlist":
        watchlist_map = WatchlistRepository(db).list_ticker_map(WatchlistRepository(db).get_or_create_default().id)
        results = [item for item in results if item["ticker"] in watchlist_map]
    if sync_top_n > 0:
        results = results[:sync_top_n]
    tickers = [item["ticker"] for item in results]
    if not tickers:
        return _redirect_with_message("No screener results available to sync.", lang=lang)
    sync_results = sync_market_data(tickers=tickers, start_date="2025-01-01", provider="auto")
    success_count = sum(1 for item in sync_results if item["status"] == "success")
    return RedirectResponse(
        url=f"{_build_screen_query(params)}&message={urlencode({'m': f'Synced {success_count}/{len(sync_results)} screener results'})[2:]}",
        status_code=303,
    )


@router.post("/add-to-focus")
def add_all_screener_results_to_focus(
    request: Request,
    lang: str = Form("en"),
    model_template: str = Form("technical_momentum"),
    multi_model_templates: list[str] = Form([]),
    min_multi_model_hits: int = Form(2),
    confluence_action_filter: str = Form("ALL"),
    universe: str = Form("watchlist"),
    market: str = Form("ALL"),
    min_trend_score: int = Form(60),
    action_filter: str = Form("ALL"),
    min_volume_ratio: float = Form(0.0),
    min_listing_days: int = Form(365),
    pe_min: float = Form(0.0),
    pe_max: float = Form(30.0),
    min_roe_avg_3y: float = Form(12.0),
    min_net_profit_yoy: float = Form(20.0),
    min_revenue_yoy: float = Form(0.0),
    max_debt_to_assets: float = Form(100.0),
    min_dividend_yield: float = Form(0.0),
    exclude_bottom_market_cap_pct: float = Form(10.0),
    recent_snapshot_runs: int = Form(0),
    min_snapshot_hits: int = Form(0),
    model_signal_filter: str = Form("ALL"),
    min_model_signal_strength: float = Form(0.0),
    execution_tag_filter: str = Form("ALL"),
    exclude_execution_tag_filter: str = Form("ALL"),
    sort_by: str = Form("default"),
    sort_order: str = Form("desc"),
    focus_top_n: int = Form(10),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners")
    params = _current_params(
        lang=lang,
        model_template=model_template,
        multi_model_templates=multi_model_templates,
        min_multi_model_hits=min_multi_model_hits,
        confluence_action_filter=confluence_action_filter,
        universe=universe,
        market=market,
        min_trend_score=min_trend_score,
        action_filter=action_filter,
        min_volume_ratio=min_volume_ratio,
        min_listing_days=min_listing_days,
        pe_min=pe_min,
        pe_max=pe_max,
        min_roe_avg_3y=min_roe_avg_3y,
        min_net_profit_yoy=min_net_profit_yoy,
        min_revenue_yoy=min_revenue_yoy,
        max_debt_to_assets=max_debt_to_assets,
        min_dividend_yield=min_dividend_yield,
        exclude_bottom_market_cap_pct=exclude_bottom_market_cap_pct,
        recent_snapshot_runs=recent_snapshot_runs,
        min_snapshot_hits=min_snapshot_hits,
        model_signal_filter=model_signal_filter,
        min_model_signal_strength=min_model_signal_strength,
        execution_tag_filter=execution_tag_filter,
        exclude_execution_tag_filter=exclude_execution_tag_filter,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    if not _screen_snapshot_ready(ScreenerService(), params):
        return RedirectResponse(
            url=f"{_build_screen_query(params)}&message={urlencode({'m': _snapshot_pending_message(lang)})[2:]}",
            status_code=303,
        )
    raw_results = _run_screen(ScreenerService(), params)
    focus_candidates, skipped_count = _focus_pool_trade_candidates(raw_results)
    result = add_to_today_focus_pool(focus_candidates, top_n=focus_top_n)
    message = (
        f"Added {result['added']} stock(s) to today focus pool; skipped {skipped_count} blocked/low-readiness name(s)."
        if lang == "en"
        else f"已将 {result['added']} 只股票加入今日重点盯盘池；已跳过 {skipped_count} 只阻断/低就绪度候选"
    )
    return RedirectResponse(
        url=f"{_build_screen_query(params)}&message={urlencode({'m': message})[2:]}",
        status_code=303,
    )


@router.get("/kronos-validation", response_class=HTMLResponse)
def kronos_validation_pool_page(
    request: Request,
    lang: str = Query("zh"),
    market: str = Query("ALL"),
    status: str = Query("ALL"),
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/screeners/kronos-validation")
    lang = resolve_request_lang(request)
    selected_market = str(market or "ALL").strip().upper()
    selected_status = str(status or "ALL").strip().upper()
    snapshot = load_latest_kronos_validation(db) or {}
    payload = snapshot.get("payload") if isinstance(snapshot, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    rows = [dict(item) for item in (payload.get("rows") or []) if isinstance(item, dict)]
    if selected_market in {"CN", "US"}:
        rows = [row for row in rows if str(row.get("market") or "").strip().upper() == selected_market]
    if selected_status != "ALL":
        rows = [row for row in rows if str(row.get("kronos_status") or "").strip().upper() == selected_status]

    def _safe_num(value, default: float = -999999.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    rows.sort(
        key=lambda item: (
            2 if "支持" in str(item.get("kronos_decision") or "") or "support" in str(item.get("kronos_decision") or "").lower() else 1 if str(item.get("kronos_status") or "").upper() == "READY" else 0,
            _safe_num(item.get("kronos_score")),
            _safe_num((item.get("path_precheck") or {}).get("score")),
            _safe_num(item.get("trade_readiness_score")),
        ),
        reverse=True,
    )
    all_rows = payload.get("rows") or []
    status_counts: dict[str, int] = {}
    market_counts: dict[str, int] = {}
    for item in all_rows:
        row_status = str((item or {}).get("kronos_status") or "UNKNOWN").strip().upper() or "UNKNOWN"
        row_market = str((item or {}).get("market") or "UNKNOWN").strip().upper() or "UNKNOWN"
        status_counts[row_status] = status_counts.get(row_status, 0) + 1
        market_counts[row_market] = market_counts.get(row_market, 0) + 1

    def _status_badge(row_status: object) -> str:
        value = str(row_status or "-").strip().upper()
        cls = "ready" if value == "READY" else "pending" if value in {"NOT_CONFIGURED", "PENDING"} else "skipped" if value == "SKIPPED" else "failed" if value == "FAILED" else "idle"
        label = {
            "READY": "已验证" if lang == "zh" else "Validated",
            "NOT_CONFIGURED": "待配置" if lang == "zh" else "Needs setup",
            "PENDING": "待运行" if lang == "zh" else "Pending",
            "SKIPPED": "已跳过" if lang == "zh" else "Skipped",
            "FAILED": "失败" if lang == "zh" else "Failed",
        }.get(value, value)
        return f"<span class='status-badge {cls}'>{html.escape(label)}</span>"

    def _fmt(value: object, suffix: str = "", digits: int = 2) -> str:
        try:
            return f"{float(value):.{digits}f}{suffix}"
        except (TypeError, ValueError):
            return "-"

    def _decision_chip(decision: object) -> str:
        text = str(decision or "-").strip()
        tone = "support" if "支持" in text or "support" in text.lower() else "avoid" if "不支持" in text or "avoid" in text.lower() else "neutral"
        return f"<span class='decision-chip {tone}'>{html.escape(text)}</span>"

    def _filter_href(next_market: str | None = None, next_status: str | None = None) -> str:
        return f"/screeners/kronos-validation?{urlencode({'lang': lang, 'market': next_market or selected_market, 'status': next_status or selected_status})}"

    market_chips = "".join(
        f"<a class='filter-chip{' active' if selected_market == value else ''}' href='{_filter_href(next_market=value)}'>{label}</a>"
        for value, label in (
            ("ALL", "全部市场" if lang == "zh" else "All"),
            ("CN", f"A股 {market_counts.get('CN', 0)}" if lang == "zh" else f"CN {market_counts.get('CN', 0)}"),
            ("US", f"美股 {market_counts.get('US', 0)}" if lang == "zh" else f"US {market_counts.get('US', 0)}"),
        )
    )
    status_chips = "".join(
        f"<a class='filter-chip{' active' if selected_status == value else ''}' href='{_filter_href(next_status=value)}'>{label}</a>"
        for value, label in (
            ("ALL", "全部状态" if lang == "zh" else "All status"),
            ("READY", f"已验证 {status_counts.get('READY', 0)}" if lang == "zh" else f"Validated {status_counts.get('READY', 0)}"),
            ("NOT_CONFIGURED", f"待配置 {status_counts.get('NOT_CONFIGURED', 0)}" if lang == "zh" else f"Needs setup {status_counts.get('NOT_CONFIGURED', 0)}"),
            ("SKIPPED", f"跳过 {status_counts.get('SKIPPED', 0)}" if lang == "zh" else f"Skipped {status_counts.get('SKIPPED', 0)}"),
            ("FAILED", f"失败 {status_counts.get('FAILED', 0)}" if lang == "zh" else f"Failed {status_counts.get('FAILED', 0)}"),
        )
    )
    row_html = "".join(
        "<tr>"
        f"<td class='sticky-col sticky-col-1'><a class='ticker-link' href='/insights/{html.escape(str(item.get('ticker') or ''), quote=True)}?lang={lang}'>{html.escape(str(item.get('ticker') or '-'))}</a><div class='muted'>{html.escape(str(item.get('name') or '-'))}</div></td>"
        f"<td>{html.escape(str(item.get('market') or '-'))}</td>"
        f"<td>{_status_badge(item.get('kronos_status'))}</td>"
        f"<td>{_decision_chip(item.get('kronos_decision'))}<div class='muted'>{html.escape(str(item.get('kronos_reason') or '-'))}</div></td>"
        f"<td>{_fmt(item.get('kronos_score'), digits=1)}<div class='muted'>预检 {_fmt((item.get('path_precheck') or {}).get('score'), digits=1)}</div></td>"
        f"<td>{_fmt(item.get('kronos_expected_return_1d_pct'), '%')}<div class='muted'>3日 {_fmt(item.get('kronos_expected_return_3d_pct'), '%')}</div></td>"
        f"<td>{_fmt(item.get('kronos_max_drawdown_pct'), '%')}</td>"
        f"<td>{_fmt(item.get('latest_close'))}<div class='muted'>{html.escape(str(item.get('latest_date') or '-'))}</div></td>"
        f"<td>{html.escape(str(item.get('source') or '-'))}<div class='muted'>{html.escape(' / '.join(item.get('source_templates') or []) or '-')}</div></td>"
        f"<td>{html.escape(str(item.get('model_signal_label') or '-'))}<div class='muted'>强度 {_fmt(item.get('model_signal_strength'), digits=1)}</div></td>"
        f"<td>{html.escape(str(item.get('history_count') or '-'))}</td>"
        "</tr>"
        for item in rows
    ) or (
        f"<tr><td colspan='11'>{'当前筛选条件下没有 Kronos 验证候选。可以先在任务中心刷新 Kronos 验证快照，或等待收盘预计算自动生成。' if lang == 'zh' else 'No Kronos validation candidates under current filters. Refresh the snapshot or wait for post-close precompute.'}</td></tr>"
    )
    snapshot_time = html.escape(str(payload.get("updated_at") or snapshot.get("created_at") or "-"))
    status_text = html.escape(str(payload.get("status") or "-"))
    message_text = html.escape(str(payload.get("message") or "-"))
    model_name = html.escape(str(payload.get("model_name") or "-"))
    candidate_count = int(payload.get("candidate_count") or 0)
    validated_count = int(payload.get("validated_count") or 0)
    nav_html = render_workspace_nav_html(lang=lang, active_key="screeners")
    refresh_markets = selected_market if selected_market in {"CN", "US"} else "CN,US"
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{'Kronos 二次验证池' if lang == 'zh' else 'Kronos Validation Pool'}</title>
        <style>
          :root {{ --bg:#071018; --panel:#111c28; --panel-2:#152231; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; }}
          * {{ box-sizing:border-box; }}
          body {{ margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.15), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.10), transparent 26%),var(--bg); }}
          a {{ color:inherit; text-decoration:none; }}
          {WORKSPACE_COMPACT_STYLE}
          {WORKSPACE_SIDEBAR_STYLE}
          .content {{ padding:16px 14px 28px; }}
          .wrap {{ max-width:none; margin:0; }}
          .toolbar,.chip-row {{ display:flex; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom:12px; }}
          .pill,.filter-chip {{ display:inline-flex; align-items:center; justify-content:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.72); color:var(--muted); font-size:13px; font-weight:800; }}
          .filter-chip.active,.pill.primary {{ color:#06131b; border-color:var(--accent); background:var(--accent); }}
          .card {{ border:1px solid var(--line); border-radius:18px; background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); padding:16px; margin-bottom:12px; box-shadow:0 14px 34px rgba(0,0,0,0.18); }}
          .hero {{ display:grid; grid-template-columns:minmax(0,1.2fr) minmax(280px,0.8fr); gap:12px; }}
          .eyebrow {{ display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:900; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:10px; }}
          h1 {{ margin:0 0 8px; font-size:32px; letter-spacing:-0.03em; }}
          .lead,.muted {{ color:var(--muted); line-height:1.5; }}
          .metric-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }}
          .metric {{ border:1px solid rgba(144,163,184,0.12); border-radius:14px; background:rgba(11,19,29,0.72); padding:12px; }}
          .metric strong {{ display:block; font-size:24px; margin-top:4px; }}
          .table-wrap {{ width:100%; overflow:auto; border-radius:16px; border:1px solid var(--line); background:rgba(11,19,29,0.8); }}
          table {{ width:100%; min-width:1280px; border-collapse:collapse; font-size:13px; }}
          th,td {{ padding:10px 9px; text-align:left; vertical-align:top; border-bottom:1px solid var(--line); white-space:nowrap; }}
          th {{ color:var(--muted); font-weight:800; }}
          td .muted {{ font-size:12px; white-space:normal; max-width:360px; }}
          .sticky-col {{ position:sticky; left:0; z-index:2; background:var(--panel); box-shadow:10px 0 16px rgba(0,0,0,0.14); }}
          th.sticky-col {{ z-index:4; }}
          .sticky-col-1 {{ min-width:170px; }}
          .ticker-link {{ color:var(--accent); font-weight:900; }}
          .status-badge,.decision-chip {{ display:inline-flex; align-items:center; padding:5px 9px; border-radius:999px; font-size:12px; font-weight:900; border:1px solid rgba(144,163,184,0.2); }}
          .status-badge.ready,.decision-chip.support {{ color:#9ff3d5; border-color:rgba(61,217,182,0.32); background:rgba(61,217,182,0.08); }}
          .status-badge.pending,.decision-chip.neutral {{ color:#ffd08a; border-color:rgba(255,190,92,0.32); background:rgba(255,190,92,0.08); }}
          .status-badge.skipped {{ color:#b9c7d7; background:rgba(144,163,184,0.08); }}
          .status-badge.failed,.decision-chip.avoid {{ color:#ffaaa5; border-color:rgba(239,68,68,0.32); background:rgba(239,68,68,0.08); }}
          form.inline {{ display:inline-flex; align-items:center; gap:8px; margin:0; }}
          input,select,button {{ border-radius:999px; border:1px solid var(--line); padding:8px 12px; background:rgba(11,19,29,0.82); color:var(--ink); font:inherit; font-weight:800; }}
          button {{ background:var(--accent); color:#06131b; border-color:var(--accent); cursor:pointer; }}
          @media (max-width:1120px) {{ .app {{ grid-template-columns:1fr; }} .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }} .hero {{ grid-template-columns:1fr; }} }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{'Kronos 验证池' if lang == 'zh' else 'Kronos Pool'}</h1>
              <p>{'这里展示模型候选经过 K 线路径模型二次验证后的结果。' if lang == 'zh' else 'Review model candidates after K-line path validation.'}</p>
            </div>
            <nav class="side-nav">{nav_html}</nav>
          </aside>
          <main class="content">
            <div class="wrap">
              <div class="toolbar">
                <a class="pill" href="/screeners?lang={lang}">← {'返回模型选股' if lang == 'zh' else 'Back to Screeners'}</a>
                <a class="pill" href="/settings/kronos?lang={lang}">{'Kronos 配置' if lang == 'zh' else 'Kronos Settings'}</a>
                <a class="pill" href="/dashboard/ai-daily-report?lang={lang}">{'AI 日报' if lang == 'zh' else 'AI Report'}</a>
                <form class="inline" action="/jobs/kronos-validation" method="post">
                  <input type="hidden" name="redirect_to" value="/screeners/kronos-validation?lang={lang}&market={selected_market}&status={selected_status}" />
                  <input type="hidden" name="markets" value="{refresh_markets}" />
                  <input type="hidden" name="candidate_limit" value="80" />
                  <button type="submit">{'刷新验证池' if lang == 'zh' else 'Refresh Pool'}</button>
                </form>
              </div>
              <section class="hero">
                <article class="card">
                  <div class="eyebrow">{'二次验证，不是单独选股' if lang == 'zh' else 'Secondary validation, not standalone screening'}</div>
                  <h1>{'Kronos 二次验证池' if lang == 'zh' else 'Kronos Validation Pool'}</h1>
                  <p class="lead">{'这里的股票来自 LightGBM、多模型共振和技术模板的 Top 候选。Kronos 负责验证未来 1-3 日 K 线路径是否支持，而不是重新扫描全市场。' if lang == 'zh' else 'These names come from LightGBM, multi-model confluence, and technical-template top candidates. Kronos validates the 1-3 day path instead of scanning the whole market.'}</p>
                  <div class="chip-row" style="margin-top:12px;">{market_chips}</div>
                  <div class="chip-row">{status_chips}</div>
                </article>
                <article class="card">
                  <div class="eyebrow">{'最新快照' if lang == 'zh' else 'Latest Snapshot'}</div>
                  <div class="metric-grid">
                    <div class="metric"><span class="muted">{'状态' if lang == 'zh' else 'Status'}</span><strong>{status_text}</strong></div>
                    <div class="metric"><span class="muted">{'模型' if lang == 'zh' else 'Model'}</span><strong style="font-size:17px;">{model_name}</strong></div>
                    <div class="metric"><span class="muted">{'候选' if lang == 'zh' else 'Candidates'}</span><strong>{candidate_count}</strong></div>
                    <div class="metric"><span class="muted">{'已验证' if lang == 'zh' else 'Validated'}</span><strong>{validated_count}</strong></div>
                  </div>
                  <div class="muted" style="margin-top:10px;">{message_text}</div>
                  <div class="muted" style="margin-top:6px;">{'更新时间' if lang == 'zh' else 'Updated'}: {snapshot_time}</div>
                </article>
              </section>
              <section class="card">
                <div class="eyebrow">{'验证结果' if lang == 'zh' else 'Validation Results'}</div>
                <div class="muted">{'优先看“已验证 + Kronos 支持 + 预期收益为正 + 回撤可控”的股票；待配置状态说明主流程已接上，但独立 PyTorch/Kronos runner 还没有启用。' if lang == 'zh' else 'Focus on validated names with Kronos support, positive expected return, and controlled drawdown. Needs-setup means the pipeline is wired, but the isolated PyTorch/Kronos runner is not enabled yet.'}</div>
                <div class="table-wrap" style="margin-top:12px;">
                  <table>
                    <thead>
                      <tr>
                        <th class="sticky-col sticky-col-1">{'股票' if lang == 'zh' else 'Ticker'}</th>
                        <th>{'市场' if lang == 'zh' else 'Market'}</th>
                        <th>{'状态' if lang == 'zh' else 'Status'}</th>
                        <th>{'结论 / 原因' if lang == 'zh' else 'Decision / Reason'}</th>
                        <th>{'Kronos 分' if lang == 'zh' else 'Kronos Score'}</th>
                        <th>{'预期收益' if lang == 'zh' else 'Expected Return'}</th>
                        <th>{'最大回撤' if lang == 'zh' else 'Max Drawdown'}</th>
                        <th>{'最新价' if lang == 'zh' else 'Latest Close'}</th>
                        <th>{'候选来源' if lang == 'zh' else 'Source'}</th>
                        <th>{'原模型信号' if lang == 'zh' else 'Original Signal'}</th>
                        <th>{'历史条数' if lang == 'zh' else 'Bars'}</th>
                      </tr>
                    </thead>
                    <tbody>{row_html}</tbody>
                  </table>
                </div>
              </section>
            </div>
          </main>
        </div>
      </body>
    </html>
    """


@router.get("/focus/today", response_class=HTMLResponse)
def today_focus_pool_page(request: Request, lang: str = Query("en"), db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/screeners/focus/today")

    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)
    items = _load_today_focus_items()
    item_rows = []
    for item in items:
        ticker = str(item.get("ticker") or "").upper()
        existing = watchlist_map.get(ticker)
        patterns = " / ".join(item.get("matched_patterns") or []) or "-"
        item_rows.append(
            "<tr>"
            f"<td><a href='/insights/{ticker}?lang={lang}'>{ticker}</a></td>"
            f"<td>{item.get('name') or ticker}</td>"
            f"<td>{item.get('market') or '-'}</td>"
            f"<td>{patterns}</td>"
            f"<td>{item.get('model_signal_label') or '-'}</td>"
            f"<td>{item.get('model_signal_strength') or '-'}</td>"
            f"<td>{_watchlist_summary(existing, lang) if existing else '-'}</td>"
            f"<td>{_sync_status_badge(existing, lang)}</td>"
            f"<td><a class='main-open-link' href='/insights/{ticker}?lang={lang}'>{_lang_text(lang, 'open_insight')}</a></td>"
            "</tr>"
        )
    item_rows_html = "".join(item_rows) or f"<tr><td colspan='9'>{_lang_text(lang, 'no_match')}</td></tr>"
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{_lang_text(lang, 'today_focus_pool')}</title>
        <style>
          :root {{ --bg:#071018; --panel:#111c28; --panel-2:#152231; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; --accent-soft:rgba(61,217,182,0.12); }}
          body {{ margin:0; font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.14) 0, transparent 28%),radial-gradient(circle at top right, rgba(61,217,182,0.10) 0, transparent 26%),var(--bg); }}
          .app {{ display:grid; grid-template-columns:260px minmax(0, 1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .content {{ padding:28px; }}
          .wrap {{ max-width:1108px; margin:0 auto; padding:0 0 40px; }}
          .toolbar {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:16px; }}
          .toolbar a {{ color:var(--accent); text-decoration:none; font-weight:700; }}
          .card {{ background:linear-gradient(180deg, rgba(21,34,49,0.98), rgba(17,28,40,0.98)); border:1px solid var(--line); border-radius:22px; padding:18px; box-shadow:0 24px 48px rgba(0,0,0,0.18); margin-bottom:16px; }}
          .eyebrow {{ display:inline-block; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:700; text-transform:uppercase; margin-bottom:12px; }}
          .market-scope-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; margin-bottom:16px; }}
          .market-scope-card {{ display:block; min-height:188px; padding:16px; border-radius:18px; background:rgba(11,19,29,0.74); border:1px solid rgba(34,50,70,0.92); transition:transform .16s ease, border-color .16s ease, background .16s ease; color:var(--ink); text-decoration:none; }}
          .market-scope-card:hover {{ transform:translateY(-1px); border-color:rgba(61,217,182,0.36); background:rgba(15,25,37,0.92); }}
          .market-scope-card.active {{ border-color:rgba(61,217,182,0.5); box-shadow:0 12px 28px rgba(0,0,0,0.16); }}
          .market-scope-head {{ display:flex; justify-content:space-between; gap:12px; align-items:baseline; }}
          .market-scope-head strong {{ font-size:16px; }}
          .market-scope-head span {{ color:var(--muted); font-size:11px; }}
          .market-scope-headline {{ display:flex; justify-content:flex-start; margin-top:10px; }}
          .market-scope-chip {{ display:inline-flex; align-items:center; padding:5px 9px; border-radius:999px; font-size:11px; font-weight:800; letter-spacing:0.02em; }}
          .market-scope-chip.up {{ background:rgba(74,222,128,0.14); color:#8af0a6; }}
          .market-scope-chip.down {{ background:rgba(255,107,129,0.14); color:#ff93a4; }}
          .market-scope-chip.flat {{ background:rgba(82,168,255,0.14); color:#89c2ff; }}
          .market-scope-stats {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; margin-top:14px; }}
          .market-scope-stats b {{ display:block; font-size:22px; line-height:1; }}
          .market-scope-stats span {{ display:block; margin-top:6px; color:var(--muted); font-size:11px; }}
          .market-scope-meta {{ margin-top:10px; color:var(--muted); font-size:12px; }}
          .market-scope-trend-wrap {{ margin-top:12px; }}
          .market-scope-trend-wrap span {{ display:block; color:var(--muted); font-size:11px; margin-bottom:8px; }}
          .mini-trend {{ height:34px; display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:5px; align-items:end; }}
          .mini-trend span {{ display:block; border-radius:999px 999px 3px 3px; background:linear-gradient(180deg, rgba(61,217,182,0.96), rgba(61,217,182,0.28)); box-shadow:0 6px 12px rgba(0,0,0,0.14); }}
          .mini-trend.empty {{ grid-template-columns:1fr; align-items:center; }}
          .mini-trend.empty span {{ height:auto; border-radius:0; background:none; box-shadow:none; color:var(--muted); font-size:11px; }}
          .table-wrap {{ overflow-x:auto; border-radius:14px; border:1px solid var(--line); background:rgba(11,19,29,0.82); }}
          table {{ width:100%; border-collapse:collapse; min-width:980px; }}
          th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }}
          th {{ color:var(--muted); font-weight:700; }}
          .main-open-link {{ display:inline-flex; align-items:center; justify-content:center; padding:6px 9px; border-radius:999px; background:rgba(61,217,182,0.10); color:var(--accent); text-decoration:none; font-weight:800; font-size:12px; }}
          h1 {{ margin:0 0 8px; font-size:36px; }}
          p {{ color:var(--muted); }}
          @media (max-width: 1120px) {{
            .app {{ grid-template-columns:1fr; }}
            .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }}
            .content {{ padding:20px 10px 40px; }}
          }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{_lang_text(lang, 'today_focus_pool')}</h1>
              <p>{'把今天最值得先看的股票先放进一个临时研究池。' if lang == 'zh' else 'Collect the names you want to review first into a temporary focus pool.'}</p>
            </div>
            <nav class="side-nav">{render_workspace_nav_html(lang=lang, active_key='screeners')}</nav>
            <div class="sidebar-foot">{'这页更像盘前/盘后优先级列表，决定谁先看，而不是最终持有清单。' if lang == 'zh' else 'This page acts like a premarket/postmarket priority list rather than a final holdings list.'}</div>
          </aside>
          <main class="content">
        <div class="wrap">
          <div class="toolbar">
            <a href="/screeners?lang={lang}">← {_lang_text(lang, 'quant_screener')}</a>
            <a href="/watchlist?lang={lang}">{_lang_text(lang, 'open_watchlist')}</a>
          </div>
          <div class="card">
            <div class="eyebrow">{_lang_text(lang, 'today_focus_pool')}</div>
            <h1 style="margin:0 0 8px;">{_lang_text(lang, 'today_focus_pool')}</h1>
            <p style="margin:0;color:#6b7280;">{'A holding area for the names you want to study first today.' if lang == 'en' else '把最值得优先盯盘和复盘的股票，先放进今天的重点池。'}</p>
          </div>
          <div class="card">
            <div class="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>{_lang_text(lang, 'ticker')}</th>
                    <th>{_lang_text(lang, 'name')}</th>
                    <th>{_lang_text(lang, 'market')}</th>
                    <th>{_lang_text(lang, 'pattern_hits')}</th>
                    <th>{_lang_text(lang, 'model_signal_filter')}</th>
                    <th>{_lang_text(lang, 'min_model_signal_strength')}</th>
                    <th>{_lang_text(lang, 'watchlist')}</th>
                    <th>{_lang_text(lang, 'last_sync')}</th>
                    <th>{_lang_text(lang, 'insight')}</th>
                  </tr>
                </thead>
                <tbody>{item_rows_html}</tbody>
              </table>
            </div>
          </div>
        </div>
          </main>
        </div>
      </body>
    </html>
    """


@router.get("/market-snapshot", response_class=HTMLResponse)
def market_snapshot_page(
    request: Request,
    lang: str = Query("en"),
    mode: str = Query("monitor"),
    market_filter: str = Query("CN"),
    message: str | None = Query(None),
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/screeners/market-snapshot")

    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)
    market_filter = str(market_filter or "CN").strip().upper()
    if market_filter not in {"ALL", "CN", "US"}:
        market_filter = "CN"
    view_mode = (mode or "monitor").strip().lower()
    if view_mode not in {"premarket", "monitor", "postmarket"}:
        view_mode = "monitor"
    snapshot_type = {
        "premarket": SNAPSHOT_MARKET_WORKSPACE_PREMARKET,
        "monitor": SNAPSHOT_MARKET_WORKSPACE_MONITOR,
        "postmarket": SNAPSHOT_MARKET_WORKSPACE_POSTMARKET,
    }[view_mode]
    market_snapshot = load_latest_workspace_snapshot(db, snapshot_type)
    payload = (market_snapshot or {}).get("payload") if isinstance(market_snapshot, dict) else None
    boards = (payload or {}).get("boards") if isinstance(payload, dict) else None
    snapshot_ready = isinstance(boards, list) and bool(boards)
    if not snapshot_ready:
        boards = []
    boards = [
        board
        for board in boards
        if market_filter == "ALL" or str(board.get("market") or "").upper() == market_filter
    ]
    sentiment = get_or_set(
        "screener_market_sentiment",
        json.dumps({"mode": view_mode, "market_filter": market_filter}, sort_keys=True, ensure_ascii=False),
        ttl_seconds=45.0,
        loader=lambda: build_market_sentiment_snapshot(boards=boards),
    )
    sentiment_chip = _signal_chip("Market", sentiment.get("sentiment") or "neutral")
    sentiment_summary = (
        f"Avg score {sentiment.get('average_snapshot_score', '-')} | "
        f"Bullish boards {sentiment.get('bullish_boards', '-')} | "
        f"Candidates {sentiment.get('total_candidates', '-')}"
    )
    snapshot_history = _market_snapshot_history(db, snapshot_type=snapshot_type, limit=6)
    market_scope_help = (
        "口径说明：这里的热度统一表示 0-100 的平均强度分。行动榜单页面使用当前模式下候选股 `trend_score` 的均值。"
        if lang == "zh"
        else "Methodology: heat is a unified 0-100 average strength score. On action boards it is the mean `trend_score` of candidates under the current mode."
    )
    market_scope_label = {
        "ALL": "全部市场" if lang == "zh" else "All Markets",
        "CN": "A股" if lang == "zh" else "A-Shares",
        "US": "美股" if lang == "zh" else "U.S. Stocks",
    }[market_filter]
    market_pills = "".join(
        f"<a href='/screeners/market-snapshot?{urlencode({'lang': lang, 'mode': view_mode, 'market_filter': market})}' "
        f"style='display:inline-flex;align-items:center;padding:8px 12px;border-radius:999px;border:1px solid;"
        f"{'background:#0f766e;color:#fff;border-color:#0f766e;' if market_filter == market else 'background:#fffdf7;color:#0f766e;border-color:#cde9e4;'}"
        f"text-decoration:none;font-weight:800;font-size:12px;'>{label}</a>"
        for market, label in (
            ("ALL", "All Markets" if lang == "en" else "全部市场"),
            ("CN", "A-Shares" if lang == "en" else "A股"),
            ("US", "U.S." if lang == "en" else "美股"),
        )
    )
    all_boards = (payload or {}).get("boards") if isinstance(payload, dict) else []
    market_summary_cards_html = ""
    for scope_market, scope_label in (("CN", "A股" if lang == "zh" else "A-Shares"), ("US", "美股" if lang == "zh" else "U.S. Stocks")):
        scope_boards = [
            board for board in (all_boards or [])
            if str(board.get("market") or "").upper() == scope_market
        ]
        scope_rows = [row for board in scope_boards for row in (board.get("rows") or [])]
        scope_heat = round(
            sum(float(row.get("trend_score") or 0.0) for row in scope_rows) / max(len(scope_rows), 1),
            1,
        ) if scope_rows else 0.0
        scope_high_trend = max((float(row.get("trend_score") or 0.0) for row in scope_rows), default=0.0)
        scope_history_values = snapshot_history.get(scope_market) or []
        scope_delta = (scope_history_values[-1] - scope_history_values[0]) if len(scope_history_values) >= 2 else 0
        scope_delta_tone = "up" if scope_delta > 0 else "down" if scope_delta < 0 else "flat"
        scope_delta_label = (
            (f"+{scope_delta} {'升温' if lang == 'zh' else 'warming'}" if scope_delta > 0 else f"{scope_delta} {'降温' if lang == 'zh' else 'cooling'}")
            if scope_delta != 0
            else ("持平" if lang == "zh" else "Flat")
        )
        scope_href = f"/screeners/market-snapshot?{urlencode({'lang': lang, 'mode': view_mode, 'market_filter': scope_market})}"
        market_summary_cards_html += (
            f"<a class='market-scope-card{' active' if market_filter == scope_market else ''}' href='{scope_href}'>"
            f"<div class='market-scope-head'><strong>{scope_label}</strong><span>{'榜单' if lang == 'zh' else 'Boards'} {len(scope_boards)}</span></div>"
            f"<div class='market-scope-headline'><span class='market-scope-chip {scope_delta_tone}'>{scope_delta_label}</span></div>"
            f"<div class='market-scope-stats'>"
            f"<div><b>{len(scope_rows)}</b><span>{'候选' if lang == 'zh' else 'Names'}</span></div>"
            f"<div><b>{scope_heat:.1f}</b><span>{'热度' if lang == 'zh' else 'Heat'}</span></div>"
            f"<div><b>{scope_high_trend:.0f}</b><span>{'最高趋势' if lang == 'zh' else 'Top Trend'}</span></div>"
            f"</div>"
            f"<div class='market-scope-trend-wrap'><span>{'近6次同模式快照' if lang == 'zh' else 'Last 6 same-mode snapshots'}</span>{_mini_trend_bars(scope_history_values, lang=lang)}</div>"
            f"<div class='market-scope-meta'>{'当前模式' if lang == 'zh' else 'Mode'} {view_mode}</div>"
            "</a>"
        )
    board_html = []
    for board in boards:
        title = board["title_zh"] if lang == "zh" else board["title_en"]
        description = board["description_zh"] if lang == "zh" else board["description_en"]
        board_html.append(
            "<section class='card'>"
            f"<div class='eyebrow'>{_lang_text(lang, 'market_snapshot')}</div>"
            f"<h2 style='margin:0 0 8px;'>{title}</h2>"
            f"<p style='margin:0 0 14px;color:#6b7280;'>{description}</p>"
            f"{_market_snapshot_table(board.get('rows') or [], watchlist_map, lang, {'mode': view_mode, 'market_filter': market_filter})}"
            "</section>"
        )
    empty_hint = (
        "<div class='card'>"
        f"<div class='eyebrow'>{_lang_text(lang, 'market_snapshot')}</div>"
        f"<p style='margin:0;color:#6b7280;'>{'当前市场范围下还没有可展示的快照榜单。' if lang == 'zh' else 'There are no snapshot boards to show under the current market scope.'}</p>"
        "</div>"
        if snapshot_ready and not board_html
        else ""
    )
    loading_hint = (
        "<div class='card'>"
        f"<div class='eyebrow'>{'后台预计算' if lang == 'zh' else 'Background Precompute'}</div>"
        f"<p style='margin:0;color:#6b7280;'>{'市场快照仍在后台生成，稍后刷新即可。' if lang == 'zh' else 'Market snapshot boards are still being generated in the background. Please refresh shortly.'}</p>"
        "</div>"
        if not snapshot_ready
        else ""
    )
    return f"""
    <!DOCTYPE html>
    <html lang="{lang}">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>{_lang_text(lang, 'market_snapshot')}</title>
        <style>
          :root {{ --bg:#071018; --panel:#111c28; --panel-2:#152231; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; --accent-soft:rgba(61,217,182,0.12); }}
          body {{ margin:0; font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.14) 0, transparent 28%),radial-gradient(circle at top right, rgba(61,217,182,0.10) 0, transparent 26%),var(--bg); }}
          .app {{ display:grid; grid-template-columns:260px minmax(0, 1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .content {{ padding:28px; }}
          .wrap {{ max-width:1200px; margin:0 auto; padding:0 0 56px; }}
          .toolbar {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:16px; }}
          .toolbar a {{ color:var(--accent); text-decoration:none; font-weight:700; }}
          .card {{ background:linear-gradient(180deg, rgba(21,34,49,0.98), rgba(17,28,40,0.98)); border:1px solid var(--line); border-radius:22px; padding:18px; box-shadow:0 24px 48px rgba(0,0,0,0.18); margin-bottom:16px; }}
          .eyebrow {{ display:inline-block; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:700; text-transform:uppercase; margin-bottom:12px; }}
          .market-scope-help {{ margin:12px 0 16px; padding:11px 12px; border-radius:14px; border:1px dashed rgba(61,217,182,0.24); background:rgba(61,217,182,0.06); color:var(--muted); font-size:12px; line-height:1.55; }}
          .table-wrap {{ overflow-x:auto; border-radius:14px; border:1px solid var(--line); background:rgba(11,19,29,0.82); }}
          table {{ width:100%; border-collapse:collapse; min-width:1080px; }}
          th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }}
          th {{ color:var(--muted); font-weight:700; white-space:nowrap; }}
          .main-open-link {{ display:inline-flex; align-items:center; justify-content:center; padding:6px 9px; border-radius:999px; background:rgba(61,217,182,0.10); color:var(--accent); text-decoration:none; font-weight:800; font-size:12px; white-space:nowrap; }}
          .muted {{ color:var(--muted); }}
          h1 {{ margin:0 0 8px; font-size:36px; }}
          p {{ color:var(--muted); }}
          @media (max-width: 1120px) {{
            .app {{ grid-template-columns:1fr; }}
            .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }}
            .content {{ padding:20px 10px 40px; }}
          }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{_lang_text(lang, 'market_snapshot')}</h1>
              <p>{'把强势、收口、连阳、放量候选放进一个盘面快照板。' if lang == 'zh' else 'Collect momentum, squeeze, candle, and volume candidates into one market snapshot board.'}</p>
            </div>
            <nav class="side-nav">{render_workspace_nav_html(lang=lang, active_key='screeners')}</nav>
            <div class="sidebar-foot">{'这个页面适合盘前盘后扫榜，不适合做深度研究；看中某只票再进入洞察页。' if lang == 'zh' else 'Use this page for a fast premarket/postmarket scan, then open insight for deep work.'}</div>
          </aside>
          <main class="content">
        <div class="wrap">
          <div class="toolbar">
            <a href="/screeners?lang={lang}">← {_lang_text(lang, 'quant_screener')}</a>
            <a href="/screeners/focus/today?lang={lang}">{_lang_text(lang, 'today_focus_pool')}</a>
            <a href="/watchlist?lang={lang}">{_lang_text(lang, 'open_watchlist')}</a>
          </div>
          <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px;">{market_pills}</div>
          <div class="market-scope-grid">{market_summary_cards_html}</div>
          <div class="market-scope-help">{market_scope_help}</div>
          <div class="card">
            <div class="eyebrow">{_lang_text(lang, 'market_snapshot')}</div>
            <h1 style="margin:0 0 8px;">{_lang_text(lang, 'market_snapshot')}</h1>
            <p style="margin:0;color:#6b7280;">{('A compact trading board for today’s strongest local setups.' if lang == 'en' else '把今天最值得先看的强势、收口、连阳、放量候选股集中成一个快照页。')} {'Current scope: ' + market_scope_label if lang == 'en' else '当前范围：' + market_scope_label}</p>
            <div style="margin-top:14px;">
              <div class="muted" style="margin-bottom:8px;">{_lang_text(lang, 'view_mode')}</div>
              {_mode_switch_html('/screeners/market-snapshot', view_mode, lang, {'market_filter': market_filter})}
            </div>
          </div>
          <div class="card">
            <div class="eyebrow">{_lang_text(lang, 'market_sentiment')}</div>
            <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;">{sentiment_chip}</div>
            <p style="margin:12px 0 0;color:#6b7280;">{sentiment_summary}</p>
          </div>
          {_banner_html(message, lang)}
          {loading_hint}
          {empty_hint}
          {''.join(board_html)}
        </div>
          </main>
        </div>
      </body>
    </html>
    """


@router.post("/market-snapshot/add-to-focus")
def add_market_snapshot_ticker_to_focus(
    request: Request,
    lang: str = Form("en"),
    mode: str = Form("monitor"),
    market_filter: str = Form("CN"),
    ticker: str = Form(...),
    name: str = Form(""),
    market: str = Form("CN"),
    selection_reason: str = Form(""),
    matched_patterns: str = Form(""),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners/market-snapshot")

    patterns = [item.strip() for item in str(matched_patterns or "").split("/") if item.strip()]
    add_to_today_focus_pool(
        [
            {
                "ticker": str(ticker).strip().upper(),
                "name": name or str(ticker).strip().upper(),
                "market": market or "CN",
                "selection_reason": selection_reason or "",
                "matched_patterns": patterns,
            }
        ]
    )
    message = _lang_text(lang, "added_to_focus_message").format(ticker=str(ticker).strip().upper())
    return RedirectResponse(
        url=f"/screeners/market-snapshot?{urlencode({'lang': lang, 'mode': mode, 'market_filter': market_filter, 'message': message})}",
        status_code=303,
    )
