import json
import csv
from io import StringIO
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.db import get_db_session
from app.services.auth import is_authenticated, login_redirect
from app.models.schema import SymbolCreate
from app.services.market_sync import sync_market_data
from app.services.repository import AppSettingRepository, SymbolRepository, WatchlistRepository
from app.services.screener import MODEL_TEMPLATES, ScreenerService


router = APIRouter(prefix="/screeners", tags=["screeners"])


ACTION_OPTIONS = [
    ("ALL", "All setups"),
    ("buy_the_dip", "Buy The Dip"),
    ("wait_for_breakout", "Wait For Breakout"),
    ("hold_and_watch", "Hold And Watch"),
    ("wait", "Wait"),
]

SCREENERS_PRESETS_KEY = "screener_saved_presets"

LANG_OPTIONS = [("en", "English"), ("zh", "中文")]

SCREEN_TEXT = {
    "en": {
        "back_to_dashboard": "Back to dashboard",
        "open_watchlist": "Open Watchlist",
        "sync_cn_fundamentals": "Sync CN Fundamentals",
        "quant_screener": "Quant Screener",
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
        "run_screener": "Run Screener",
        "save_strategy": "Save Current Strategy",
        "strategy_name": "My strategy name",
        "save_as_strategy": "Save As My Strategy",
        "export_csv": "Export CSV",
        "only_add_top_n": "Only add top N results (0 = all)",
        "auto_enable_sync": "Auto-enable Sync for added stocks",
        "add_current_results": "Add Current Results To Watchlist",
        "no_results_to_add": "No Results To Add",
        "stocks_matched": "stocks matched your current rules.",
        "ticker": "Ticker",
        "name": "Name",
        "trend": "Trend",
        "action": "Action",
        "close": "Close",
        "model": "Model",
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
    },
    "zh": {
        "back_to_dashboard": "返回总览",
        "open_watchlist": "打开自选股",
        "sync_cn_fundamentals": "同步A股基本面",
        "quant_screener": "量化选股器",
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
        "run_screener": "开始选股",
        "save_strategy": "保存当前策略",
        "strategy_name": "我的策略名称",
        "save_as_strategy": "保存为我的策略",
        "export_csv": "导出 CSV",
        "only_add_top_n": "只加入前 N 名（0 代表全部）",
        "auto_enable_sync": "加入后自动开启同步",
        "add_current_results": "将当前结果加入自选",
        "no_results_to_add": "当前没有可加入结果",
        "stocks_matched": "只股票符合当前规则。",
        "ticker": "代码",
        "name": "名称",
        "trend": "趋势",
        "action": "动作",
        "close": "收盘价",
        "model": "模型",
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
    },
}

TEMPLATE_LABELS = {
    "technical_momentum": {"en": "Technical Momentum", "zh": "技术动量"},
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


def _detail_panel(item: dict, watchlist_map: dict[str, dict], current_params: dict, lang: str) -> str:
    details_label = "Details" if lang == "en" else "展开"
    collapse_label = "Collapse" if lang == "en" else "收起"
    model_highlights = item.get("model_highlights") or []
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
    why_selected_html = _why_selected_cell(item.get("selection_reason"), lang)
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
        "</div>"
        "</div>"
        f"<div class='detail-collapse-note'>{collapse_label}</div>"
        "</details>"
    )


def _model_cell(item: dict, lang: str) -> str:
    summary = item.get("model_summary")
    highlights = item.get("model_highlights") or []
    if not summary and not highlights:
        return "-"
    score = None
    summary_lower = str(summary or "").lower()
    try:
        if summary_lower.startswith("model "):
            score = float(summary_lower.split(",")[0].replace("model ", "").strip())
    except (TypeError, ValueError):
        score = None
    bg = "#f3f4f6"
    fg = "#374151"
    badge_text = "Neutral" if lang == "en" else "中性"
    if score is not None:
        if score >= 0.18:
            bg, fg, badge_text = "#dcfce7", "#166534", ("Strong" if lang == "en" else "强")
        elif score >= 0.05:
            bg, fg, badge_text = "#ecfccb", "#3f6212", ("Positive" if lang == "en" else "偏强")
        elif score <= -0.05:
            bg, fg, badge_text = "#fee2e2", "#991b1b", ("Weak" if lang == "en" else "偏弱")
    compact = highlights[0] if highlights else (_lang_text(lang, "drag_hint") if False else "")
    details_label = "Details" if lang == "en" else "展开"
    detail_rows = "".join(
        f"<li style='margin:4px 0;color:#4b5563;line-height:1.45;white-space:normal;'>{highlight}</li>"
        for highlight in highlights
    )
    detail_block = (
        "<details style='margin-top:4px;'>"
        f"<summary style='cursor:pointer;color:#6b7280;font-size:12px;font-weight:700;list-style:none;'>{compact or details_label}</summary>"
        f"<ul style='margin:8px 0 0 18px;padding:0;'>{detail_rows}</ul>"
        f"<div style='margin-top:6px;font-size:12px;color:#6b7280;'>{details_label}</div>"
        "</details>"
        if highlights
        else ""
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
        "sort_by": sort_by,
        "sort_order": sort_order,
        "lang": lang,
    }


def _run_screen(service: ScreenerService, params: dict) -> list[dict]:
    return service.screen(
        model_template=str(params.get("model_template", "technical_momentum")),
        universe=str(params.get("universe", "watchlist")),
        market=str(params.get("market", "ALL")),
        min_trend_score=int(float(params.get("min_trend_score", 60))),
        action_filter=str(params.get("action_filter", "ALL")),
        min_volume_ratio=float(params.get("min_volume_ratio", 0.0)),
        min_listing_days=int(float(params.get("min_listing_days", 365))),
        pe_min=float(params.get("pe_min", 0.0)),
        pe_max=float(params.get("pe_max", 30.0)),
        min_roe_avg_3y=float(params.get("min_roe_avg_3y", 12.0)),
        min_net_profit_yoy=float(params.get("min_net_profit_yoy", 20.0)),
        min_revenue_yoy=float(params.get("min_revenue_yoy", 0.0)),
        max_debt_to_assets=float(params.get("max_debt_to_assets", 100.0)),
        min_dividend_yield=float(params.get("min_dividend_yield", 0.0)),
        exclude_bottom_market_cap_pct=float(params.get("exclude_bottom_market_cap_pct", 10.0)),
        sort_by=str(params.get("sort_by", "default")),
        sort_order=str(params.get("sort_order", "desc")),
        limit=500,
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
    sort_by: str = Query("default"),
    sort_order: str = Query("desc"),
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/screeners")

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
        sort_by=sort_by,
        sort_order=sort_order,
    )
    results = _run_screen(service, current_params)
    if sort_by == "watchlist_state":
        reverse = sort_order != "asc"
        results = sorted(
            results,
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
    universe_option_html = "".join(
        f"<option value='{value}' {'selected' if universe == value else ''}>{label}</option>"
        for value, label in universe_options
    )
    market_option_html = "".join(
        f"<option value='{value}' {'selected' if market == value else ''}>{label}</option>"
        for value, label in market_options
    )

    row_chunks: list[str] = []
    previous_market = None
    for item in results:
        current_market = (item.get("market") or "").upper()
        sync_badge = _sync_status_badge(watchlist_map.get(item["ticker"]), lang)
        if current_market != previous_market:
            row_chunks.append(
                "<tr class='market-section-row'>"
                f"<td colspan='17'>{_market_section_label(current_market, lang)}</td>"
                "</tr>"
            )
            previous_market = current_market
        row_chunks.append(
            "<tr>"
            f"<td class='sticky-col sticky-col-1'><a href='/insights/{item['ticker']}?lang={lang}'>{item['ticker']}</a></td>"
            f"<td class='sticky-col sticky-col-2'>{item.get('name') or '-'}</td>"
            f"<td>{item['market']}</td>"
            f"<td>{_trend_badge(item['trend_score'])}</td>"
            f"<td>{_action_badge(item['action_label'], lang)}</td>"
            f"<td>{_price_badge(item['latest_close'])}</td>"
            f"<td>{_model_cell(item, lang)}</td>"
            f"<td>{sync_badge}</td>"
            f"<td>{_change_chip(item['momentum_5'])}</td>"
            f"<td>{_change_chip(item['momentum_20'])}</td>"
            f"<td>{_number_badge(item['volume_ratio'], suffix='x', higher_is_good=True)}</td>"
            f"<td>{_number_badge(item.get('pe_ttm'), higher_is_good=False)}</td>"
            f"<td>{_number_badge(item.get('roe_avg_3y'), suffix='%', higher_is_good=True)}</td>"
            f"<td>{_number_badge(item.get('net_profit_yoy'), suffix='%', higher_is_good=True)}</td>"
            f"<td>{_number_badge(item.get('dividend_yield'), suffix='%', higher_is_good=True)}</td>"
            f"<td>{_number_badge(item['distance_to_breakout_pct'], suffix='%', higher_is_good=False)}</td>"
            f"<td><a class='main-open-link' href='/insights/{item['ticker']}?lang={lang}'>{_lang_text(lang, 'open_insight')}</a></td>"
            "</tr>"
            "<tr class='detail-row'>"
            f"<td colspan='17'>{_detail_panel(item, watchlist_map, current_params, lang)}</td>"
            "</tr>"
        )
    rows = "".join(row_chunks) or f"<tr><td colspan='17'>{_lang_text(lang, 'no_match')}</td></tr>"
    preset_rows = "".join(
        "<tr>"
        f"<td>{preset['name']}</td>"
        f"<td>{_template_label(preset['params'].get('model_template', ''), MODEL_TEMPLATES.get(preset['params'].get('model_template', ''), {'label': preset['params'].get('model_template', '-')})['label'], lang)}</td>"
        f"<td>{_preset_summary(preset['params'])}</td>"
        f"<td>{len(_run_screen(service, preset['params']))}</td>"
        f"<td><a href='{_build_screen_query(preset['params'])}'>{_lang_text(lang, 'load')}</a></td>"
        f"<td><form method='post' action='/screeners/delete' style='margin:0;'><input type='hidden' name='preset_name' value='{preset['name']}' /><input type='hidden' name='lang' value='{lang}' /><button type='submit'>{_lang_text(lang, 'delete')}</button></form></td>"
        "</tr>"
        for preset in saved_presets
    ) or f"<tr><td colspan='6'>{_lang_text(lang, 'no_saved')}</td></tr>"
    banner_html = _banner_html(message, lang)
    hidden_fields = "".join(
        f"<input type='hidden' name='{key}' value='{value}' />"
        for key, value in current_params.items()
    )
    bulk_add_disabled = "disabled" if not results else ""
    bulk_add_label = _lang_text(lang, "add_current_results") if results else _lang_text(lang, "no_results_to_add")
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
            --bg: #f5efe2;
            --panel: #fffdf7;
            --ink: #1f2937;
            --muted: #6b7280;
            --line: #d6cfc2;
            --accent: #0f766e;
            --accent-soft: #dff5ef;
          }}
          * {{ box-sizing: border-box; }}
          body {{
            margin: 0;
            font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            color: var(--ink);
            background:
              radial-gradient(circle at top left, #fff6d8 0, transparent 30%),
              radial-gradient(circle at top right, #d9f3ee 0, transparent 35%),
              var(--bg);
          }}
          .wrap {{ max-width: 1120px; margin: 0 auto; padding: 28px 20px 56px; }}
          .toolbar {{ display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:16px; }}
          .toolbar a {{ color: var(--accent); text-decoration:none; font-weight:700; }}
          .card {{ background: var(--panel); border:1px solid var(--line); border-radius:18px; padding:18px; box-shadow:0 8px 24px rgba(31,41,55,0.05); margin-bottom:16px; min-width:0; overflow:hidden; }}
          .eyebrow {{ display:inline-block; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }}
          h1 {{ margin:0 0 8px; font-size:38px; }}
          .lead {{ margin:0; color:var(--muted); max-width:760px; }}
          .stack {{ display:grid; gap:12px; }}
          .section-stack {{ display:grid; gap:16px; }}
          .rules-grid {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); align-items:end; }}
          input, select, button {{
            border-radius:12px;
            border:1px solid var(--line);
            padding:10px 12px;
            font:inherit;
            background:#fff;
            width:100%;
            max-width:100%;
          }}
          button {{ background:var(--accent); color:#fff; border-color:var(--accent); font-weight:700; }}
          .muted {{ color:var(--muted); font-size:14px; }}
          .table-wrap {{ width:100%; max-width:100%; overflow-x:auto; overflow-y:hidden; border-radius:14px; border:1px solid var(--line); background:#fff; padding-bottom:8px; scrollbar-gutter:stable both-edges; }}
          .table-wrap::-webkit-scrollbar {{ height:12px; }}
          .table-wrap::-webkit-scrollbar-track {{ background:#efe7d7; border-radius:999px; }}
          .table-wrap::-webkit-scrollbar-thumb {{ background:#c6b79e; border-radius:999px; border:2px solid #efe7d7; }}
          .table-wrap::-webkit-scrollbar-thumb:hover {{ background:#a9987d; }}
          table {{ width:100%; border-collapse:collapse; min-width:1560px; font-size:14px; table-layout:auto; }}
          th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; white-space:nowrap; }}
          th {{ color:var(--muted); font-weight:600; white-space:nowrap; }}
          td {{ white-space:nowrap; }}
          .table-wrap th:nth-child(7), .table-wrap td:nth-child(7) {{ min-width:220px; width:220px; }}
          .table-wrap th:nth-child(8), .table-wrap td:nth-child(8) {{ min-width:120px; width:120px; }}
          .table-wrap th:nth-child(17), .table-wrap td:nth-child(17) {{ min-width:110px; width:110px; }}
          .sticky-col {{ position:sticky; background:var(--panel); z-index:2; }}
          th.sticky-col {{ z-index:4; }}
          .sticky-col-1 {{ left:0; min-width:124px; box-shadow: 10px 0 14px rgba(31,41,55,0.05); }}
          .sticky-col-2 {{ left:124px; min-width:180px; box-shadow: 10px 0 14px rgba(31,41,55,0.05); }}
          .market-section-row td {{ background:#f7f4ec; color:#0f766e; font-weight:800; letter-spacing:0.03em; border-top:1px solid var(--line); }}
          .detail-row td {{ white-space:normal; background:#fcfaf4; padding:12px 10px 14px; }}
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
            border:1px dashed #c5ddda;
            border-radius:14px;
            background:#f6fbfa;
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
            background:#fff;
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
            background:#eef8f5;
            color:#0f766e;
            font-weight:800;
            text-decoration:none;
          }}
          .main-open-link {{
            display:inline-flex;
            align-items:center;
            justify-content:center;
            padding:6px 9px;
            border-radius:999px;
            background:#eef8f5;
            color:#0f766e;
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
            .wrap {{ padding: 20px 14px 40px; }}
            h1 {{ font-size:30px; }}
            .sticky-col, .sticky-col-1, .sticky-col-2 {{ position:static; box-shadow:none; min-width:auto; }}
            .table-wrap th:nth-child(7), .table-wrap td:nth-child(7),
            .table-wrap th:nth-child(8), .table-wrap td:nth-child(8),
            .table-wrap th:nth-child(17), .table-wrap td:nth-child(17) {{ width:auto; min-width:unset; }}
            .detail-card-wide {{ grid-column:span 1; }}
            .row-detail-toggle > summary {{ align-items:flex-start; flex-direction:column; }}
            .detail-summary-meta {{ margin-left:0; }}
          }}
        </style>
      </head>
      <body>
        <main class="wrap">
          <div class="toolbar">
            <a href="/dashboard">← {_lang_text(lang, 'back_to_dashboard')}</a>
            <a href="/watchlist">{_lang_text(lang, 'open_watchlist')}</a>
            <a href="/dashboard#cn-fundamental-tickers">{_lang_text(lang, 'sync_cn_fundamentals')}</a>
            <div class="lang-switch">
              <span class="muted">{_lang_text(lang, 'language')}:</span>
              {lang_switch_html}
            </div>
          </div>
          <div class="card">
            <div class="eyebrow">{_lang_text(lang, 'quant_screener')}</div>
            <h1>{_lang_text(lang, 'title')}</h1>
            <p class="lead">{active_template['description']}</p>
          </div>
          {banner_html}
          <section class="section-stack">
            <article class="card">
              <div class="eyebrow">{_lang_text(lang, 'rules')}</div>
              <form class="stack" method="get" action="/screeners">
                <input type="hidden" name="lang" value="{lang}" />
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
                <div style="border-top:1px solid var(--line);padding-top:12px;">
                  <div class="muted" style="margin-bottom:8px;font-weight:700;">{_lang_text(lang, 'cn_rules')}</div>
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
                  </div>
                </div>
                <button type="submit">{_lang_text(lang, 'run_screener')}</button>
              </form>
            </article>
            <article class="card">
              <div class="rules-grid" style="margin-bottom:14px;">
                <form class="stack" method="post" action="/screeners/save">
                  <label class="muted">{_lang_text(lang, 'save_strategy')}</label>
                  <input type="text" name="preset_name" placeholder="{_lang_text(lang, 'strategy_name')}" required />
                  {hidden_fields}
                  <button type="submit">{_lang_text(lang, 'save_as_strategy')}</button>
                </form>
                <form class="stack" method="get" action="/screeners/export">
                  <label class="muted">{_lang_text(lang, 'export_csv')}</label>
                  {hidden_fields}
                  <button type="submit">{_lang_text(lang, 'export_csv')}</button>
                </form>
                <form class="stack" method="post" action="/screeners/add-all-to-watchlist">
                  <label class="muted">{_lang_text(lang, 'only_add_top_n')}</label>
                  {hidden_fields}
                  <input type="number" name="bulk_top_n" min="0" value="0" />
                  <label style="display:flex;align-items:center;gap:8px;" class="muted">
                    <input type="checkbox" name="auto_enable_sync" value="1" />
                    {_lang_text(lang, 'auto_enable_sync')}
                  </label>
                  <button type="submit" {bulk_add_disabled}>{bulk_add_label}</button>
                </form>
                <form class="stack" method="post" action="/screeners/sync-top-results">
                  <label class="muted">{_lang_text(lang, 'sync_top_n_help')}</label>
                  {hidden_fields}
                  <input type="number" name="sync_top_n" min="0" value="5" />
                  <button type="submit">{_lang_text(lang, 'sync_top_n_now')}</button>
                </form>
              </div>
              <div class="eyebrow">{_lang_text(lang, 'results')}</div>
              <div class="muted" style="margin-bottom:12px;">{len(results)} {_lang_text(lang, 'stocks_matched')}</div>
              <div class="table-wrap">
                <table>
                  <thead>
                    <tr><th class='sticky-col sticky-col-1'>{header_link(_lang_text(lang, 'ticker'), 'ticker')}</th><th class='sticky-col sticky-col-2'>{_lang_text(lang, 'name')}</th><th>{_lang_text(lang, 'market')}</th><th>{header_link(_lang_text(lang, 'trend'), 'trend_score')}</th><th>{_lang_text(lang, 'action')}</th><th>{header_link(_lang_text(lang, 'close'), 'latest_close')}</th><th>{_lang_text(lang, 'model')}</th><th>{header_link(_lang_text(lang, 'watchlist'), 'watchlist_state')}</th><th>{header_link('5D %', 'momentum_5')}</th><th>{header_link('20D %', 'momentum_20')}</th><th>{header_link('Volume', 'volume_ratio')}</th><th>{header_link('PE', 'pe_ttm')}</th><th>{header_link('ROE 3Y', 'roe_avg_3y')}</th><th>{header_link('Profit YoY', 'net_profit_yoy')}</th><th>{header_link('Dividend %', 'dividend_yield')}</th><th>{header_link('Breakout %', 'distance_to_breakout_pct')}</th><th>{_lang_text(lang, 'insight')}</th></tr>
                  </thead>
                  <tbody>{rows}</tbody>
                </table>
              </div>
              <div class="scroll-hint">{_lang_text(lang, 'drag_hint')}</div>
            </article>
          </section>
          <section class="card">
            <div class="eyebrow">{_lang_text(lang, 'saved_strategies')}</div>
            <table>
              <thead>
                <tr><th>{_lang_text(lang, 'name')}</th><th>{_lang_text(lang, 'model_template')}</th><th>{_lang_text(lang, 'summary')}</th><th>{_lang_text(lang, 'hits')}</th><th>{_lang_text(lang, 'load')}</th><th>{_lang_text(lang, 'delete')}</th></tr>
              </thead>
              <tbody>{preset_rows}</tbody>
            </table>
          </section>
        </main>
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
            "momentum_5",
            "momentum_20",
            "volume_ratio",
            "pe_ttm",
            "roe_avg_3y",
            "net_profit_yoy",
            "revenue_yoy",
            "dividend_yield",
            "debt_to_assets",
            "selection_reason",
        ],
    )
    writer.writeheader()
    for item in results:
        writer.writerow({key: item.get(key) for key in writer.fieldnames})
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
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return RedirectResponse(
        url=f"{_build_screen_query(params)}&message={urlencode({'m': f'Added {ticker} to watchlist'})[2:]}",
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
        sort_by=sort_by,
        sort_order=sort_order,
    )
    added, already_in_watchlist, sync_enabled_count = _add_screen_results_to_watchlist(
        db=db,
        params=params,
        top_n=bulk_top_n,
        auto_enable_sync=auto_enable_sync == "1",
    )
    if added:
        message = f"Added {added} screener results to watchlist"
    elif already_in_watchlist:
        message = "All matching stocks are already in your watchlist"
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
    sort_by: str = Form("default"),
    sort_order: str = Form("desc"),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners")
    watchlist_repo = WatchlistRepository(db)
    if item_id is not None:
        watchlist_repo.set_sync_enabled(item_id, True)
    results = sync_market_data(tickers=[ticker], start_date="2025-01-01", provider="yfinance")
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
    sync_results = sync_market_data(tickers=tickers, start_date="2025-01-01", provider="yfinance")
    success_count = sum(1 for item in sync_results if item["status"] == "success")
    return RedirectResponse(
        url=f"{_build_screen_query(params)}&message={urlencode({'m': f'Synced {success_count}/{len(sync_results)} screener results'})[2:]}",
        status_code=303,
    )
