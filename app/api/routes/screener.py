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
from app.models.schema import SymbolCreate
from app.services.market_sync import sync_market_data
from app.services.repository import AppSettingRepository, SymbolRepository, WatchlistRepository, WorkspaceSnapshotRepository
from app.services.runtime_cache import get_or_set
from app.services.screener import MODEL_TEMPLATES, ScreenerService
from app.services.screener_snapshots import (
    build_base_precompute_params,
    screener_snapshot_key,
    screener_snapshot_type,
)
from app.services.focus_pool import add_to_today_focus_pool, enrich_focus_pool_with_symbols, load_today_focus_pool
from app.services.ui_lang import resolve_request_lang
from app.services.workspace_nav import WORKSPACE_SIDEBAR_STYLE, render_workspace_nav_html
from app.services.workspace_snapshots import (
    SNAPSHOT_MARKET_WORKSPACE_MONITOR,
    SNAPSHOT_MARKET_WORKSPACE_POSTMARKET,
    SNAPSHOT_MARKET_WORKSPACE_PREMARKET,
    load_latest_workspace_snapshot,
)
from app.services.workspace_snapshots import refresh_workspace_snapshots
from app.services.time_utils import app_now_iso


router = APIRouter(prefix="/screeners", tags=["screeners"])


SCREENER_SNAPSHOT_TTL = timedelta(hours=18)


ACTION_OPTIONS = [
    ("ALL", "All setups"),
    ("buy_the_dip", "Buy The Dip"),
    ("wait_for_breakout", "Wait For Breakout"),
    ("hold_and_watch", "Hold And Watch"),
    ("wait", "Wait"),
]

MODEL_SIGNAL_OPTIONS = [
    ("ALL", {"en": "All signals", "zh": "全部信号"}),
    ("BUY", {"en": "Buy", "zh": "买点"}),
    ("WATCH", {"en": "Watch", "zh": "观察"}),
    ("SELL", {"en": "Sell", "zh": "卖点"}),
    ("HOLD", {"en": "Hold", "zh": "持有"}),
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
        "delete": "Delete",
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
        "delete": "删除",
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
    },
}

TEMPLATE_LABELS = {
    "next_tesla_swing": {"en": "Next Tesla Swing", "zh": "强趋势二次启动"},
    "technical_momentum": {"en": "Technical Momentum", "zh": "技术动量"},
    "cn_limit_up_watch": {"en": "Yesterday Limit-Up Watch", "zh": "昨日涨停观察"},
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


def _mode_switch_html(base_path: str, current_mode: str, lang: str) -> str:
    options = [
        ("premarket", _lang_text(lang, "mode_premarket")),
        ("monitor", _lang_text(lang, "mode_monitor")),
        ("postmarket", _lang_text(lang, "mode_postmarket")),
    ]
    chips: list[str] = []
    for value, label in options:
        active = value == current_mode
        style = (
            "background:#0f766e;color:#fff;border-color:#0f766e;"
            if active
            else "background:#fffdf7;color:#0f766e;border-color:#cde9e4;"
        )
        chips.append(
            f"<a href='{base_path}?{urlencode({'lang': lang, 'mode': value})}' "
            "style='display:inline-flex;align-items:center;padding:8px 12px;border-radius:999px;"
            f"border:1px solid;{style}text-decoration:none;font-weight:800;font-size:12px;'>{label}</a>"
        )
    return "<div style='display:flex;gap:8px;flex-wrap:wrap;'>" + "".join(chips) + "</div>"


def _market_snapshot_table(rows: list[dict], watchlist_map: dict[str, dict], lang: str) -> str:
    if not rows:
        return f"<div class='muted'>{_lang_text(lang, 'snapshot_empty')}</div>"
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
    state = item.get("model_state") or {}
    confidence = item.get("model_confidence")
    signal_label = item.get("model_signal_label")
    signal_strength = item.get("model_signal_strength")
    model_percentile = item.get("model_percentile")
    model_horizon_days = item.get("model_horizon_days")
    model_reward_risk_ratio = item.get("model_reward_risk_ratio")
    model_expected_drawdown_20d = item.get("model_expected_drawdown_20d")
    model_conviction_bucket = item.get("model_conviction_bucket")
    model_position_size_hint = item.get("model_position_size_hint")
    model_entry_style = item.get("model_entry_style")
    model_execution_tags = list(item.get("model_execution_tags") or [])
    if not summary and not highlights and not state:
        return "-"
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
    if model_execution_tags:
        meta_bits.extend(model_execution_tags[:2])
    meta_html = (
        f"<div style='margin-top:6px;font-size:12px;color:#6b7280;'>{' · '.join(meta_bits)}</div>"
        if meta_bits
        else ""
    )
    signal_html = (
        f"<div style='margin-top:6px;font-size:12px;color:#6b7280;'>{signal_label or ('Hold' if lang == 'en' else '持有')}"
        f"{' · ' + str(int(signal_strength)) if signal_strength is not None else ''}</div>"
    )
    detail_block = (
        "<details style='margin-top:4px;'>"
        f"<summary style='cursor:pointer;color:#6b7280;font-size:12px;font-weight:700;list-style:none;'>{compact or details_label}</summary>"
        f"<ul style='margin:8px 0 0 18px;padding:0;'>{detail_rows}</ul>"
        f"{signal_html}"
        f"{meta_html}"
        f"{confidence_html}"
        f"<div style='margin-top:6px;font-size:12px;color:#6b7280;'>{details_label}</div>"
        "</details>"
        if highlights
        else signal_html + meta_html + confidence_html
    )
    return (
        f"<div style='min-width:180px;white-space:normal;'>"
        f"<div style='display:flex;align-items:center;gap:8px;flex-wrap:wrap;'>"
        f"<span style='display:inline-flex;align-items:center;padding:4px 8px;border-radius:999px;background:{bg};color:{fg};font-weight:800;font-size:12px;'>{badge_text}</span>"
        f"<span style='font-weight:700;color:#0f172a;'>{summary or '-'}</span>"
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
    compact = {key: value for key, value in params.items() if value not in (None, "", "ALL")}
    return f"/screeners?{urlencode(compact)}"


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


def _preset_summary(params: dict) -> str:
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
        hidden_fields = "".join(
            f"<input type='hidden' name='{key}' value='{value}' />"
            for key, value in current_params.items()
        )
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
    hidden_fields = "".join(
        f"<input type='hidden' name='{key}' value='{value}' />"
        for key, value in current_params.items()
    )
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
) -> dict:
    return {
        "model_template": model_template,
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


def _run_screen(service: ScreenerService, params: dict) -> list[dict]:
    normalized = {
        "model_template": str(params.get("model_template", "technical_momentum")),
        "universe": str(params.get("universe", "watchlist")),
        "market": str(params.get("market", "ALL")),
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
    snapshot_rows = _load_precomputed_screener_rows(service, normalized)
    if snapshot_rows is not None:
        return snapshot_rows
    snapshot_rows = _load_screener_snapshot(normalized)
    if snapshot_rows is not None:
        return snapshot_rows
    cache_key = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
    rows = get_or_set(
        "screener_results",
        cache_key,
        ttl_seconds=90.0,
        loader=lambda: service.screen(**normalized),
    )
    _persist_screener_snapshot(normalized, rows)
    return rows


def _normalize_action_filter(value: str | None) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


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


@router.get("", response_class=HTMLResponse)
def screener_page(
    request: Request,
    message: str | None = Query(None),
    lang: str = Query("en"),
    model_template: str = Query("technical_momentum"),
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
    results = _run_screen(service, current_params) if should_execute else []
    total_results = len(results)
    visible_results = results[:120]
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
    template_cards_html = "".join(
        (
            f"<a class='template-card{' active' if value == model_template else ''}' href='{_build_screen_query({**current_params, 'model_template': value, 'market': config.get('market') or market})}'>"
            f"<div class='template-top'><span class='template-mode'>{config.get('mode', 'mixed')}</span><span class='template-market'>{config.get('market', 'ALL')}</span></div>"
            f"<div class='template-title'>{_template_label(value, config['label'], lang)}</div>"
            f"<div class='template-desc'>{config.get('description') or ''}</div>"
            "</a>"
        )
        for value, config in MODEL_TEMPLATES.items()
    )
    active_defaults = active_template.get("defaults") or {}
    active_defaults_html = "".join(
        f"<span class='default-chip'>{key}: {value}</span>"
        for key, value in active_defaults.items()
    ) or f"<span class='default-chip'>{'No template defaults' if lang == 'en' else '无模板默认值'}</span>"

    row_chunks: list[str] = []
    previous_market = None
    for item in visible_results:
        current_market = (item.get("market") or "").upper()
        sync_badge = _sync_status_badge(watchlist_map.get(item["ticker"]), lang)
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
            f"<td class='sticky-col sticky-col-2'>{item.get('name') or '-'}</td>"
            f"<td>{item.get('market') or '-'}</td>"
            f"<td>{_trend_badge(item.get('trend_score'))}</td>"
            f"<td>{_action_badge(item.get('action_label'), lang)}</td>"
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
            f"<td><a class='main-open-link' href='/insights/{item['ticker']}?lang={lang}'>{_lang_text(lang, 'open_insight')}</a></td>"
            "</tr>"
            "<tr class='detail-row'>"
            f"<td colspan='18'>{_detail_panel(item, watchlist_map, current_params, lang)}</td>"
            "</tr>"
        )
    empty_state = (
        "先选择一个模板或调整参数后再执行筛选，首屏默认不自动跑重计算。"
        if lang == "zh"
        else "Choose a template or adjust rules, then run the screen. The first load stays lightweight by default."
    )
    rows = "".join(row_chunks) or (
        f"<tr><td colspan='18'>{_lang_text(lang, 'no_match') if should_execute else empty_state}</td></tr>"
    )
    preset_rows = "".join(
        "<tr>"
        f"<td title='{preset['name']}'>{_compact_text(preset['name'], 26)}</td>"
        f"<td title='{_template_label(preset['params'].get('model_template', ''), MODEL_TEMPLATES.get(preset['params'].get('model_template', ''), {'label': preset['params'].get('model_template', '-')})['label'], lang)}'>{_compact_text(_template_label(preset['params'].get('model_template', ''), MODEL_TEMPLATES.get(preset['params'].get('model_template', ''), {'label': preset['params'].get('model_template', '-')})['label'], lang), 24)}</td>"
        f"<td title='{_preset_summary(preset['params'])}'>{_compact_text(_preset_summary(preset['params']), 44)}</td>"
        f"<td>{'点击加载后查看' if lang == 'zh' else 'Load to view'}</td>"
        f"<td><a href='{_build_screen_query(preset['params'])}'>{_lang_text(lang, 'load')}</a></td>"
        f"<td><form method='post' action='/screeners/delete' style='margin:0;'><input type='hidden' name='preset_name' value='{preset['name']}' /><input type='hidden' name='lang' value='{lang}' /><button type='submit'>{_lang_text(lang, 'delete')}</button></form></td>"
        "</tr>"
        for preset in saved_presets
    ) or f"<tr><td colspan='6'>{_lang_text(lang, 'no_saved')}</td></tr>"
    banner_html = _banner_html(message, lang)
    risk_top_tags_html = "".join(
        f"<span class='linkbtn'>{tag} · {count}</span>" for tag, count in risk_top_tags
    ) or f"<span class='muted'>{_lang_text(lang, 'no_execution_risks')}</span>"
    risk_examples_html = " · ".join(
        f"{item['ticker']} ({' / '.join(item['tags'])})" for item in risk_examples
    ) or "-"
    hidden_fields = "".join(
        f"<input type='hidden' name='{key}' value='{value}' />"
        for key, value in current_params.items()
    )
    bulk_add_disabled = "disabled" if not total_results else ""
    bulk_add_label = _lang_text(lang, "add_current_results") if total_results else _lang_text(lang, "no_results_to_add")
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
          .app {{ display:grid; grid-template-columns:280px minmax(0, 1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .content {{ padding:28px; }}
          .wrap {{ max-width: 1120px; margin: 0 auto; padding: 0 0 56px; }}
          .toolbar {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:16px; }}
          .toolbar a {{ color: var(--accent); text-decoration:none; font-weight:700; }}
          .card {{ background: linear-gradient(180deg, rgba(21,34,49,0.98), rgba(17,28,40,0.98)); border:1px solid var(--line); border-radius:22px; padding:18px; box-shadow:0 24px 48px rgba(0,0,0,0.18); margin-bottom:16px; min-width:0; overflow:hidden; }}
          .nav-grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); margin-bottom:16px; }}
          .nav-card {{
            display:block;
            text-decoration:none;
            color:inherit;
            background:linear-gradient(180deg, rgba(17,28,40,0.98) 0%, rgba(21,34,49,0.98) 100%);
            border:1px solid var(--line);
            border-radius:18px;
            padding:18px;
            box-shadow:0 12px 30px rgba(0,0,0,0.12);
          }}
          .nav-card:hover {{ border-color:var(--accent); box-shadow:0 12px 28px rgba(61,217,182,0.08); }}
          .nav-head {{ display:flex; align-items:center; gap:12px; margin-bottom:10px; }}
          .nav-icon {{
            width:42px; height:42px; border-radius:14px; display:inline-flex; align-items:center; justify-content:center;
            background:rgba(61,217,182,0.10); color:var(--accent); font-size:12px; font-weight:900; letter-spacing:0.04em; border:1px solid rgba(61,217,182,0.18); flex:0 0 auto;
          }}
          .nav-title {{ font-size:18px; font-weight:800; color:var(--ink); }}
          .nav-kicker {{ color:var(--muted); font-size:12px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; }}
          .eyebrow {{ display:inline-block; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
          h1 {{ margin:0 0 8px; font-size:38px; }}
          .lead {{ margin:0; color:var(--muted); max-width:760px; }}
          .stack {{ display:grid; gap:12px; }}
          .section-stack {{ display:grid; gap:16px; }}
          .template-grid {{ display:grid; gap:14px; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); margin-top:14px; }}
          .template-card {{
            display:grid;
            gap:10px;
            padding:16px;
            border-radius:18px;
            border:1px solid var(--line);
            background:rgba(11,19,29,0.82);
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
          @media (max-width: 1120px) {{
            .app {{ grid-template-columns:1fr; }}
            .sidebar {{ position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }}
            .content {{ padding:20px 14px 40px; }}
          }}
        </style>
      </head>
      <body>
        <div class="app">
          <aside class="sidebar">
            <div class="brand">
              <span class="brand-tag">PQW</span>
              <h1>{'模型选股' if lang == 'zh' else 'Screeners'}</h1>
              <p>{'先选模板，再看结果，再决定是否加入自选或盯盘池。' if lang == 'zh' else 'Pick a template, review results, then move names into tracking.'}</p>
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
            <a href="/dashboard#cn-fundamental-tickers">{_lang_text(lang, 'sync_cn_fundamentals')}</a>
            <div class="lang-switch">
              <span class="muted">{_lang_text(lang, 'language')}:</span>
              {lang_switch_html}
            </div>
          </div>
          <section class="nav-grid">
            <a class="nav-card" href="/dashboard?lang={lang}">
              <div class="nav-head">
                <span class="nav-icon">HOME</span>
                <div>
                  <div class="nav-kicker">{'总览' if lang == 'zh' else 'Overview'}</div>
                  <div class="nav-title">Dashboard</div>
                </div>
              </div>
              <div class="muted">{'返回首页，看系统状态和主要入口。' if lang == 'zh' else 'Return to the hub for system status and primary navigation.'}</div>
            </a>
            <a class="nav-card" href="/watchlist">
              <div class="nav-head">
                <span class="nav-icon">LIST</span>
                <div>
                  <div class="nav-kicker">{'跟踪' if lang == 'zh' else 'Tracking'}</div>
                  <div class="nav-title">{_lang_text(lang, 'open_watchlist')}</div>
                </div>
              </div>
              <div class="muted">{'把筛出来的股票加入自选，并统一管理同步。' if lang == 'zh' else 'Move screened candidates into your watchlist and manage sync from one place.'}</div>
            </a>
            <a class="nav-card" href="/dashboard/continuous-leaders?lang={lang}">
              <div class="nav-head">
                <span class="nav-icon">RUN</span>
                <div>
                  <div class="nav-kicker">{'持续入选' if lang == 'zh' else 'Persistence'}</div>
                  <div class="nav-title">{'连续强势股' if lang == 'zh' else 'Continuous Leaders'}</div>
                </div>
              </div>
              <div class="muted">{'查看最近几次模型快照里持续入选的股票。' if lang == 'zh' else 'Inspect names that keep recurring across recent model snapshots.'}</div>
            </a>
            <a class="nav-card" href="/screeners/focus/today?lang={lang}">
              <div class="nav-head">
                <span class="nav-icon">FOCUS</span>
                <div>
                  <div class="nav-kicker">{'盯盘池' if lang == 'zh' else 'Focus'}</div>
                  <div class="nav-title">{_lang_text(lang, 'today_focus_pool')}</div>
                </div>
              </div>
              <div class="muted">{'把技术形态和模型信号最值得看的股票先放进今日重点盯盘池。' if lang == 'zh' else 'Collect today’s highest-priority names before deciding what goes into the watchlist.'}</div>
            </a>
            <a class="nav-card" href="/screeners/market-snapshot?lang={lang}">
              <div class="nav-head">
                <span class="nav-icon">SCAN</span>
                <div>
                  <div class="nav-kicker">{'盘面快照' if lang == 'zh' else 'Snapshot'}</div>
                  <div class="nav-title">{_lang_text(lang, 'market_snapshot')}</div>
                </div>
              </div>
              <div class="muted">{'把强势、收口、连阳、放量四类候选股集中成一页，方便盘前盘后快速扫一遍。' if lang == 'zh' else 'Open a single page for momentum, squeeze, candle continuation, and volume-breakout boards.'}</div>
            </a>
          </section>
          <div class="card">
            <div class="eyebrow">{_lang_text(lang, 'quant_screener')}</div>
            <h1>{_lang_text(lang, 'title')}</h1>
            <p class="lead">{active_template['description']}</p>
            <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:14px;">
              <span class="default-chip">{'Active template' if lang == 'en' else '当前模板'}: {_template_label(model_template, active_template['label'], lang)}</span>
              {active_defaults_html}
            </div>
          </div>
          {banner_html}
          <section class="section-stack">
            <article class="card">
              <div class="eyebrow">{_lang_text(lang, 'rules')}</div>
              <div class="template-grid">{template_cards_html}</div>
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
                    <label class="muted">{_lang_text(lang, 'universe')}</label>
                    <select name="universe">{universe_option_html}</select>
                  </div>
                  <div>
                    <label class="muted">{_lang_text(lang, 'market')}</label>
                    <select name="market">{market_option_html}</select>
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
                    <button type="submit" {bulk_add_disabled}>{bulk_add_label}</button>
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
                    <button type="submit" {bulk_add_disabled}>{_lang_text(lang, 'add_current_results_to_focus')}</button>
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
                    <button type="submit">{_lang_text(lang, 'sync_top_n_now')}</button>
                  </div>
                </form>
              </div>
              <div class="eyebrow">{_lang_text(lang, 'results')}</div>
              <div class="results-toolbar">
                <div class="muted">{total_results} {_lang_text(lang, 'stocks_matched')}</div>
                <form method="get" action="/screeners/export">
                  {hidden_fields}
                  <button type="submit">{_lang_text(lang, 'export_csv')}</button>
                </form>
              </div>
              <div class="muted" style="margin-bottom:12px;">{visible_note}</div>
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr><th class='sticky-col sticky-col-1'>{header_link(_lang_text(lang, 'ticker'), 'ticker')}</th><th class='sticky-col sticky-col-2'>{_lang_text(lang, 'name')}</th><th>{_lang_text(lang, 'market')}</th><th>{header_link(_lang_text(lang, 'trend'), 'trend_score')}</th><th>{_lang_text(lang, 'action')}</th><th>{header_link(_lang_text(lang, 'close'), 'latest_close')}</th><th>{header_link(_lang_text(lang, 'model'), 'model_signal_strength')}</th><th>{header_link(_lang_text(lang, 'watchlist'), 'watchlist_state')}</th><th>{header_link('Hits', 'snapshot_hits')}</th><th>{header_link('5D %', 'momentum_5')}</th><th>{header_link('20D %', 'momentum_20')}</th><th>{header_link('Volume', 'volume_ratio')}</th><th>{header_link('PE', 'pe_ttm')}</th><th>{header_link('ROE 3Y', 'roe_avg_3y')}</th><th>{header_link('Profit YoY', 'net_profit_yoy')}</th><th>{header_link('Dividend %', 'dividend_yield')}</th><th>{header_link('Breakout %', 'distance_to_breakout_pct')}</th><th>{_lang_text(lang, 'insight')}</th></tr>
                  </thead>
                  <tbody>{rows}</tbody>
                </table>
              </div>
              <div class="scroll-hint">{_lang_text(lang, 'drag_hint')}</div>
            </article>
          </section>
          <section class="card">
            <div class="eyebrow">{_lang_text(lang, 'saved_strategies')}</div>
            <div class="table-wrap">
            <table>
              <thead>
                <tr><th>{_lang_text(lang, 'name')}</th><th>{_lang_text(lang, 'model_template')}</th><th>{_lang_text(lang, 'summary')}</th><th>{_lang_text(lang, 'hits')}</th><th>{_lang_text(lang, 'load')}</th><th>{_lang_text(lang, 'delete')}</th></tr>
              </thead>
              <tbody>{preset_rows}</tbody>
            </table>
            </div>
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
    filtered.insert(0, {"name": clean_name, "params": params})
    _save_saved_presets(db, filtered[:20])
    return RedirectResponse(
        url=f"{_build_screen_query(params)}&message={urlencode({'m': f'Saved strategy: {clean_name}'})[2:]}",
        status_code=303,
    )


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
    results = _run_screen(ScreenerService(), params)
    buffer = StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=[
            "ticker",
            "name",
            "market",
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
        writer.writerow(row)
    filename = f"{model_template}_screener.csv"
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
    result = add_to_today_focus_pool(_run_screen(ScreenerService(), params), top_n=focus_top_n)
    message = (
        f"Added {result['added']} stock(s) to today focus pool"
        if lang == "en"
        else f"已将 {result['added']} 只股票加入今日重点盯盘池"
    )
    return RedirectResponse(
        url=f"{_build_screen_query(params)}&message={urlencode({'m': message})[2:]}",
        status_code=303,
    )


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
          .app {{ display:grid; grid-template-columns:280px minmax(0, 1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .content {{ padding:28px; }}
          .wrap {{ max-width:1080px; margin:0 auto; padding:0 0 56px; }}
          .toolbar {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:16px; }}
          .toolbar a {{ color:var(--accent); text-decoration:none; font-weight:700; }}
          .card {{ background:linear-gradient(180deg, rgba(21,34,49,0.98), rgba(17,28,40,0.98)); border:1px solid var(--line); border-radius:22px; padding:18px; box-shadow:0 24px 48px rgba(0,0,0,0.18); margin-bottom:16px; }}
          .eyebrow {{ display:inline-block; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:700; text-transform:uppercase; margin-bottom:12px; }}
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
            .content {{ padding:20px 14px 40px; }}
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
    message: str | None = Query(None),
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/screeners/market-snapshot")

    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)
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
    sentiment = get_or_set(
        "screener_market_sentiment",
        json.dumps({"mode": view_mode}, sort_keys=True, ensure_ascii=False),
        ttl_seconds=45.0,
        loader=lambda: build_market_sentiment_snapshot(boards=boards),
    )
    sentiment_chip = _signal_chip("Market", sentiment.get("sentiment") or "neutral")
    sentiment_summary = (
        f"Avg score {sentiment.get('average_snapshot_score', '-')} | "
        f"Bullish boards {sentiment.get('bullish_boards', '-')} | "
        f"Candidates {sentiment.get('total_candidates', '-')}"
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
            f"{_market_snapshot_table(board.get('rows') or [], watchlist_map, lang)}"
            "</section>"
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
          .app {{ display:grid; grid-template-columns:280px minmax(0, 1fr); min-height:100vh; }}
          {WORKSPACE_SIDEBAR_STYLE}
          .content {{ padding:28px; }}
          .wrap {{ max-width:1200px; margin:0 auto; padding:0 0 56px; }}
          .toolbar {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:16px; }}
          .toolbar a {{ color:var(--accent); text-decoration:none; font-weight:700; }}
          .card {{ background:linear-gradient(180deg, rgba(21,34,49,0.98), rgba(17,28,40,0.98)); border:1px solid var(--line); border-radius:22px; padding:18px; box-shadow:0 24px 48px rgba(0,0,0,0.18); margin-bottom:16px; }}
          .eyebrow {{ display:inline-block; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:700; text-transform:uppercase; margin-bottom:12px; }}
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
            .content {{ padding:20px 14px 40px; }}
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
          <div class="card">
            <div class="eyebrow">{_lang_text(lang, 'market_snapshot')}</div>
            <h1 style="margin:0 0 8px;">{_lang_text(lang, 'market_snapshot')}</h1>
            <p style="margin:0;color:#6b7280;">{'A compact trading board for today’s strongest local setups.' if lang == 'en' else '把今天最值得先看的强势、收口、连阳、放量候选股集中成一个快照页。'}</p>
            <div style="margin-top:14px;">
              <div class="muted" style="margin-bottom:8px;">{_lang_text(lang, 'view_mode')}</div>
              {_mode_switch_html('/screeners/market-snapshot', view_mode, lang)}
            </div>
          </div>
          <div class="card">
            <div class="eyebrow">{_lang_text(lang, 'market_sentiment')}</div>
            <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;">{sentiment_chip}</div>
            <p style="margin:12px 0 0;color:#6b7280;">{sentiment_summary}</p>
          </div>
          {_banner_html(message, lang)}
          {loading_hint}
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
        url=f"/screeners/market-snapshot?{urlencode({'lang': lang, 'message': message})}",
        status_code=303,
    )
