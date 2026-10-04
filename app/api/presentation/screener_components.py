from __future__ import annotations

import html
from urllib.parse import urlencode

from app.api.presentation.i18n import t
from app.api.presentation.screener_tactical import format_lightgbm_history_bias as _lightgbm_history_bias
from app.services.ai_daily_report import build_trade_explain_text, format_trade_gate_reason, format_trade_status
from app.services.screener import MODEL_TEMPLATES
from app.services.stock_selection.multi_model_confluence import normalize_multi_model_templates as _normalize_multi_model_templates
from app.services.stock_selection.screener_query import normalize_screen_params as _normalize_screen_params
from app.services.stock_selection.selection_policy import normalize_action_filter as _normalize_action_filter
from app.services.template_evaluation import (
    build_lightgbm_evaluation,
    build_lightgbm_prediction_evaluation,
    build_next_tesla_evaluation,
    build_pattern_template_evaluation,
    build_technical_momentum_evaluation,
    lightgbm_maturity,
    next_tesla_market_bias,
    next_tesla_maturity,
    pattern_template_bias,
    pattern_template_maturity,
    technical_momentum_bias,
    technical_momentum_maturity,
)

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
        + (t(lang, "伪强势", "False Strength"))
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
        ) or "<div class='muted'>-</div>"

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
            f"{int(counts.get(sector, 0))} {t(lang, '次出现', 'hits')}"
            + (
                f" · {_fmt_number((((groups.get(sector) or {}).get(5) or {}).get('avg_return')), suffix='%', digits=2)} / {_fmt_number((((groups.get(sector) or {}).get(5) or {}).get('hit_rate')), suffix='%', digits=1)}"
                if int((((groups.get(sector) or {}).get(5) or {}).get('count') or 0)) > 0
                else ""
            )
            + "</div>"
            for sector in ranked
        ) or "<div class='muted'>-</div>"

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
                    f"<div class='muted' style='margin-top:6px;'>{t(lang, '当前偏向', 'Current bias')}: {html.escape(next_tesla_market_bias(per_market.get(code) or {}, lang=lang))}</div>"
                    f"<div class='muted' style='margin-top:6px;'>{t(lang, '快照', 'Snapshots')} {int((per_market.get(code) or {}).get('snapshot_total') or 0)} · {t(lang, 'clean 样本', 'Clean samples')} {int((per_market.get(code) or {}).get('clean_snapshot_total') or 0)}</div>"
                    "</div>"
                )
                for code in market_codes
            )
            + "</div>"
        )

    return (
        "<article class='card' style='background:#f7faf8;border-color:#dce8e1;'>"
        f"<div class='eyebrow'>{t(lang, '模型评测', 'Template Evaluation')}</div>"
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
                f"<thead><tr><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>窗口</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{t(lang, '样本', 'Samples')}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{t(lang, '平均收益', 'Avg Return')}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{t(lang, '盈利率', 'Hit Rate')}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{t(lang, '强命中', 'Strong Hit')}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{t(lang, '失效率', 'Miss Rate')}</th></tr></thead>"
                f"<tbody>{_metric_rows(action_key)}</tbody>"
                "</table></div>"
                f"<div style='margin-top:10px;font-weight:700;color:#334155;'>{samples_label}</div>"
                f"{_sample_rows(action_key)}"
                f"<div style='margin-top:10px;font-weight:700;color:#334155;'>{t(lang, '高频板块', 'Most Frequent Sectors')}</div>"
                f"{_sector_rows(action_key)}"
                "</div>"
            )
            for action_key in ("buy_the_dip", "wait_for_breakout")
        )
        + "</div>"
        f"<div class='muted' style='margin-top:10px;'>{sample_note}</div>"
        f"<div class='muted' style='margin-top:8px;'>{html.escape(str(maturity.get('summary') or ''))}</div>"
        f"<div class='muted' style='margin-top:12px;font-weight:700;'>{t(lang, '结论', 'Takeaway')}: {takeaway}</div>"
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
        f"<div class='eyebrow'>{t(lang, '模型评测摘要', 'Evaluation Brief')}</div>"
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
    per_market = evaluation.get("per_market") or {}
    windows = evaluation.get("windows") or {}
    sector_windows = evaluation.get("sector_windows") or {}
    sector_counts = evaluation.get("sector_counts") or {}

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
                    f"<div class='muted' style='margin-top:6px;'>{t(lang, '当前偏向', 'Current bias')}: {html.escape(technical_momentum_bias(per_market.get(code) or {}, lang=lang))}</div>"
                    f"<div class='muted' style='margin-top:6px;'>{t(lang, '快照', 'Snapshots')} {int((per_market.get(code) or {}).get('snapshot_total') or 0)} · {t(lang, '带标签样本', 'Labeled samples')} {int((per_market.get(code) or {}).get('labeled_snapshot_total') or 0)}</div>"
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
            return f"<div class='muted'>{t(lang, '当前还没有足够的板块样本。', 'No sector concentration yet.')}</div>"
        rows: list[str] = []
        for sector_label, seen_count in ordered:
            stats_5 = ((groups.get(sector_label) or {}).get(5) or {})
            rows.append(
                "<div style='padding:8px 0;border-bottom:1px solid #e2e8f0;'>"
                f"<div style='font-weight:700;color:#0f172a;'>{html.escape(str(sector_label or '-'))}</div>"
                f"<div class='muted'>{t(lang, '出现', 'Seen')} {int(seen_count)} {t(lang, '次', 'times')}"
                + (
                    f" · 5D {_fmt_number(stats_5.get('avg_return'), suffix='%', digits=2)} / {_fmt_number(stats_5.get('hit_rate'), suffix='%', digits=1)}"
                    if int(stats_5.get("count") or 0) > 0
                    else ""
                )
                + "</div></div>"
            )
        return "".join(rows)



def _evaluation_metric_row(windows: dict, action_key: str, label: str) -> str:
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
        return _evaluation_metric_row(windows, action_key, label)

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
            return f"<div class='muted'>{t(lang, '当前还没有足够的板块样本。', 'No sector concentration yet.')}</div>"
        rows: list[str] = []
        for sector_label in ordered:
            stats_5 = ((groups.get(sector_label) or {}).get(5) or {})
            rows.append(
                "<div style='padding:8px 0;border-bottom:1px solid #e2e8f0;'>"
                f"<div style='font-weight:700;color:#0f172a;'>{html.escape(str(sector_label or '-'))}</div>"
                f"<div class='muted'>{t(lang, '出现', 'Seen')} {int(counts.get(sector_label, 0))} {t(lang, '次', 'times')}"
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
        f"<div class='eyebrow'>{t(lang, '模型评测', 'Template Evaluation')}</div>"
        f"<div class='muted' style='margin-bottom:10px;'>{t(lang, '这块直接看历史 screener 快照的 1D / 3D / 5D 结果，更适合判断模板是否适合次日交易。', 'This block reads historical screener snapshots over 1D / 3D / 5D windows to judge whether the template is suitable for next-session trading.')}</div>"
        f"<div style='display:inline-flex;align-items:center;padding:8px 12px;border-radius:999px;margin-bottom:12px;{maturity_style}font-weight:800;font-size:12px;'>{html.escape(str(maturity.get('level') or '-'))}</div>"
        + "<div style='overflow-x:auto;border:1px solid #e2e8f0;border-radius:12px;background:white;'>"
        + "<table style='width:100%;min-width:760px;border-collapse:collapse;font-size:13px;'>"
        + f"<thead><tr><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{t(lang, '动作', 'Action')}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>1D {t(lang, '样本', 'Samples')}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>1D</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>3D {t(lang, '样本', 'Samples')}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>3D</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>5D {t(lang, '样本', 'Samples')}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>5D</th></tr></thead>"
        + f"<tbody>{_metric_row('buy_the_dip', 'Buy The Dip')}{_metric_row('wait_for_breakout', 'Wait For Breakout')}{_metric_row('hold_and_watch', 'Hold And Watch')}</tbody>"
        + "</table></div>"
        + "<div style='overflow-x:auto;border:1px solid #e2e8f0;border-radius:12px;background:white;margin-top:12px;'>"
        + "<table style='width:100%;min-width:860px;border-collapse:collapse;font-size:13px;'>"
        + f"<thead><tr><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{t(lang, '执行动作', 'Execution')}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{t(lang, '样本', 'Samples')}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{t(lang, '次日可交易命中', 'Tradable Hit')}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{t(lang, '次日开盘缺口', 'Next Open Gap')}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{t(lang, '开盘后最大冲高', 'Open To High')}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{t(lang, '盘中最大回撤', 'Intraday Drawdown')}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{t(lang, '高开受阻', 'Gap Blocked')}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{t(lang, '一字/买不到', 'Limit Unbuyable')}</th></tr></thead>"
        + f"<tbody>{_execution_row('buy_the_dip', 'Buy The Dip')}{_execution_row('wait_for_breakout', 'Wait For Breakout')}{_execution_row('hold_and_watch', 'Hold And Watch')}</tbody>"
        + "</table></div>"
        + f"<div class='muted' style='margin-top:10px;'>{note}</div>"
        + f"<div class='muted' style='margin-top:8px;'>{html.escape(str(maturity.get('summary') or ''))}</div>"
        + "<div style='display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));margin-top:12px;'>"
        + "<div style='border:1px solid #d9e5df;border-radius:18px;padding:14px;background:rgba(255,255,255,0.68);'>"
        + f"<div class='eyebrow'>{t(lang, '突破确认主导板块', 'Breakout sectors')}</div>"
        + _sector_summary("wait_for_breakout")
        + "</div>"
        + "<div style='border:1px solid #d9e5df;border-radius:18px;padding:14px;background:rgba(255,255,255,0.68);'>"
        + f"<div class='eyebrow'>{t(lang, '观察等待主导板块', 'Watch sectors')}</div>"
        + _sector_summary("hold_and_watch")
        + "</div>"
        + "</div>"
        + f"<div class='muted' style='margin-top:12px;'>{html.escape(tactical_note)}</div>"
        + f"<div class='muted' style='margin-top:12px;font-weight:700;'>{t(lang, '结论', 'Takeaway')}: {html.escape(takeaway)}</div>"
        + "</article>"
    )


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
        f"<div style='font-size:12px;font-weight:800;letter-spacing:0.06em;text-transform:uppercase;margin-bottom:6px;'>{t(lang, '今日执行偏向', 'Today Execution Bias')}</div>"
        f"<div style='font-size:20px;font-weight:800;line-height:1.3;margin-bottom:6px;'>{html.escape(title)}</div>"
        f"<div style='font-size:14px;line-height:1.6;opacity:0.92;'>{html.escape(body)}</div>"
        "</article>"
    )

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
        return _evaluation_metric_row(windows, action_key, label)

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
                    f"<div class='muted'>{t(lang, '历史样本', 'Historical samples')} {int((per_market.get(code) or {}).get('sample_count') or 0)}</div>"
                    f"<div class='muted' style='margin-top:6px;'>{t(lang, '当前偏向', 'Current bias')}: {html.escape(_lightgbm_history_bias(per_market.get(code) or {}, lang=lang))}</div>"
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
            f"<div class='muted' style='margin-top:8px;'>{t(lang, '样本', 'Samples')} {count}</div>"
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
        f"<div class='eyebrow'>{t(lang, '模型评测', 'Template Evaluation')}</div>"
        f"<div class='muted' style='margin-bottom:10px;'>{t(lang, '这块直接看历史 LightGBM predictions/model_runs 的 1D / 3D / 5D 验证，更贴近第二天操作。', 'This block validates historical LightGBM predictions/model_runs over 1D / 3D / 5D horizons for a closer next-session read.')}</div>"
        f"<div style='display:inline-flex;align-items:center;padding:8px 12px;border-radius:999px;margin-bottom:12px;{maturity_style}font-weight:800;font-size:12px;'>{html.escape(str(maturity.get('level') or '-'))}</div>"
        + _market_split_html()
        + "<div style='display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));margin-bottom:12px;'>"
        + _short_card(1, "次日 / 1D" if lang == "zh" else "Next Day / 1D")
        + _short_card(3, "3日 / 3D" if lang == "zh" else "3 Day / 3D")
        + _short_card(5, "5日 / 5D" if lang == "zh" else "5 Day / 5D")
        + "</div>"
        + "<div style='overflow-x:auto;border:1px solid #e2e8f0;border-radius:12px;background:white;'>"
        + "<table style='width:100%;min-width:760px;border-collapse:collapse;font-size:13px;'>"
        + f"<thead><tr><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>{t(lang, '动作', 'Action')}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>1D {t(lang, '样本', 'Samples')}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>1D</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>3D {t(lang, '样本', 'Samples')}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>3D</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>5D {t(lang, '样本', 'Samples')}</th><th style='text-align:left;padding:8px;border-bottom:1px solid #e2e8f0;'>5D</th></tr></thead>"
        + f"<tbody>{_metric_row('pullback', 'Pullback')}{_metric_row('breakout', 'Breakout')}{_metric_row('watch', 'Watch')}</tbody>"
        + "</table></div>"
        + f"<div class='muted' style='margin-top:10px;'>{note}</div>"
        + f"<div class='muted' style='margin-top:12px;font-weight:700;'>{t(lang, '结论', 'Takeaway')}: {html.escape(_lightgbm_history_bias(history_eval, lang=lang))}</div>"
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


def _pattern_as_of_chip(item: dict, lang: str) -> str:
    """S-5: explicitly flag pattern labels that are not from the latest session."""
    if not item.get("pattern_as_of_stale"):
        return ""
    as_of = html.escape(str(item.get("pattern_as_of_date") or "-"))
    expected = html.escape(str(item.get("pattern_expected_as_of_date") or "-"))
    label = (
        f"形态数据滞后：{as_of}（最新已完成 {expected}）"
        if lang == "zh"
        else f"Pattern data stale: {as_of} (latest {expected})"
    )
    return (
        "<span style='display:inline-flex;align-items:center;padding:5px 10px;border-radius:999px;"
        "background:rgba(246,193,119,0.16);color:#b45309;font-weight:800;font-size:12px;'>"
        f"{label}</span>"
    )


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
        else "<div style='margin-top:8px;color:#6b7280;'>-</div>"
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
        f"<div style='margin-top:10px;font-size:13px;color:#334155;line-height:1.55;'><strong>{t(lang, '模型下一步', 'Model next step')}:</strong> {html.escape(lightgbm_tactical_note)}</div>"
        if lightgbm_tactical_note
        else ""
    )
    why_selected_html = _why_selected_cell(item.get("selection_reason"), lang)
    pattern_as_of_html = _pattern_as_of_chip(item, lang)
    pattern_as_of_html = (
        f"<div style='margin-top:8px;'>{pattern_as_of_html}</div>" if pattern_as_of_html else ""
    )
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
        f"{pattern_as_of_html}"
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
    model_activation_status = str(item.get("model_activation_status") or "unverified")
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
        meta_bits.append(f"{int(model_hit_count)} {t(lang, '模型共振', 'model hits')}")
    if confluence_alignment_count is not None and int(confluence_alignment_count or 0) > 0:
        meta_bits.append(f"{int(confluence_alignment_count)} {t(lang, '动作一致', 'aligned')}")
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
    if model_activation_status.startswith("observation") or model_activation_status == "unverified":
        meta_bits.append(t(lang, "模型仅观察", "Model observation-only"))
    elif model_activation_status == "eligible_for_champion_review":
        meta_bits.append(t(lang, "模型待赛马审核", "Model pending champion review"))
    friendly_explain = build_trade_explain_text(item, lang=lang)
    if friendly_explain and friendly_explain != "-":
        meta_bits.append(friendly_explain)
    if matched_action_buckets:
        meta_bits.append((t(lang, "动作桶 ", "Buckets ")) + "/".join(matched_action_buckets[:2]))
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
        ("#e5e7eb", "#374151", readiness_bucket or (t(lang, "待确认", "Review"))),
    )
    readiness_html = (
        f"<div style='margin-top:6px;display:inline-flex;align-items:center;gap:6px;padding:4px 8px;border-radius:999px;background:{readiness_bg};color:{readiness_fg};font-size:12px;font-weight:800;'>"
        f"{t(lang, '就绪度', 'Readiness')} {float(readiness_score):.1f} · {html.escape(readiness_label)}</div>"
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

def _banner_html(message: str | None, lang: str) -> str:
    if not message:
        return ""
    actions = ""
    if "watchlist" in message.lower():
        actions = (
            "<div style='display:flex;gap:10px;flex-wrap:wrap;margin-top:10px;'>"
            f"<a href='/dashboard?lang={lang}' style='display:inline-flex;align-items:center;padding:8px 12px;border-radius:999px;"
            "background:#0f172a;color:#dff5ef;border:1px solid #223246;font-weight:700;text-decoration:none;'>"
            f"{t(lang, '打开 Dashboard', 'Open Dashboard')}</a>"
            "<a href='/watchlist' style='display:inline-flex;align-items:center;padding:8px 12px;border-radius:999px;"
            f"background:#eef8f5;color:#0f766e;font-weight:700;text-decoration:none;'>{_lang_text(lang, 'open_watchlist')}</a>"
            "<a href='/watchlist' style='display:inline-flex;align-items:center;padding:8px 12px;border-radius:999px;"
            f"background:#fff;color:#0f766e;border:1px solid #bfe6dd;font-weight:700;text-decoration:none;'>{_lang_text(lang, 'review_sync_settings')}</a>"
            "</div>"
        )
    return (
        "<div class='card' style='margin-bottom:16px;color:var(--accent);font-weight:700;'>"
        f"{html.escape(message)}"
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
            else (t(lang, "任意动作", "Any action"))
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

def _snapshot_pending_message(lang: str) -> str:
    return _lang_text(lang, "snapshot_pending")
