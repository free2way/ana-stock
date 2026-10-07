from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from app.api.presentation.i18n import t
from app.api.presentation.styles_dashboard import BOARD_DETAIL_ROWS_STYLE
from app.api.presentation.templates import render_template
from app.services.time_utils import format_app_datetime


def render_dashboard_market_page(view: dict[str, Any]) -> str:
    """Render the market workspace from controller-prepared view state."""
    lang = "zh" if view.get("lang") == "zh" else "en"
    market_filter = str(view.get("market_filter") or "CN")
    common_params = {
        "lookback_runs": view["lookback_runs"],
        "heatmap_sort": view["heatmap_sort"],
        "market_filter": market_filter,
        "kpi_focus": view["kpi_focus"],
        "mode": view["market_mode"],
        "signal_filter": view["signal_filter"],
        "min_signal_strength": view["min_signal_strength"],
        "min_buy_signal_count": view["min_buy_signal_count"],
        "execution_tag_filter": view["execution_tag_filter"],
        "exclude_execution_tag_filter": view["exclude_execution_tag_filter"],
    }
    language_hrefs = {
        language: f"/dashboard/market?{urlencode({**common_params, 'lang': language})}"
        for language in ("en", "zh")
    }
    heatmap_href = "/dashboard/market/heatmap?" + urlencode(
        {
            "lang": lang,
            "lookback_runs": view["lookback_runs"],
            "heatmap_sort": view["heatmap_sort"],
            "heatmap_metric": "flow" if market_filter == "US" else "model",
            "market_filter": market_filter,
            "signal_filter": view["signal_filter"],
            "min_signal_strength": view["min_signal_strength"],
            "min_buy_signal_count": view["min_buy_signal_count"],
            "execution_tag_filter": view["execution_tag_filter"],
            "exclude_execution_tag_filter": view["exclude_execution_tag_filter"],
        }
    )
    if market_filter == "US":
        secondary_title = "美股连续强势跟踪" if lang == "zh" else "U.S. Continuous Leaders"
        secondary_preview_html = view["continuous_preview_html"]
        secondary_href = "/dashboard/continuous-leaders?" + urlencode(
            {
                "lang": lang,
                "lookback_runs": view["lookback_runs"],
                "continuous_market": market_filter,
            }
        )
        secondary_open = "打开连续强势" if lang == "zh" else "Open continuous leaders"
    else:
        secondary_title = "概念异动追踪" if lang == "zh" else "Concept Activity Tracker"
        secondary_preview_html = view["concept_preview_html"]
        secondary_href = "/dashboard/market/concepts?" + urlencode(
            {
                "lang": lang,
                "lookback_runs": view["lookback_runs"],
                "market_filter": market_filter,
                "signal_filter": view["signal_filter"],
                "min_signal_strength": view["min_signal_strength"],
                "min_buy_signal_count": view["min_buy_signal_count"],
                "execution_tag_filter": view["execution_tag_filter"],
                "exclude_execution_tag_filter": view["exclude_execution_tag_filter"],
            }
        )
        secondary_open = t(lang, "打开概念追踪", "Open concepts")

    copy = {
        "page_title": t(lang, "市场脉冲", "Market Pulse"),
        "sidebar_title": t(lang, "市场概览", "Market Overview"),
        "sidebar_description": t(
            lang,
            "按盘前、盘中、盘后组织市场信息，先判断节奏，再下钻到板块、概念和个股。",
            "Organize market intelligence by premarket, live monitoring, and postmarket review before drilling into sectors, themes, and names.",
        ),
        "sidebar_note": t(
            lang,
            "这页只保留市场工作流入口与摘要，详细板块和概念页继续往下看。",
            "This page keeps the market workflow summary and entry points while deeper pages handle the detail.",
        ),
        "back": t(lang, "返回总览", "Back to dashboard"),
        "operations": t(lang, "运维操作台", "Operations"),
        "data_status": t(lang, "数据状态", "Data Status"),
        "latest_snapshot": t(lang, "最近快照", "Latest snapshot"),
        "risk_examples": t(lang, "风险样例", "Risk examples"),
        "focused_names": t(lang, "焦点候选", "Focused names"),
        "buy_signals": t(lang, "买点信号", "Buy signals"),
        "material_risk": t(lang, "强风险标签", "Material risk"),
        "board_names": t(lang, "行动榜候选", "Board names"),
        "step_1": t(lang, "第一步", "Step 1"),
        "step_2": t(lang, "第二步", "Step 2"),
        "step_3": t(lang, "第三步", "Step 3"),
        "heatmap_title": "美股热力图" if lang == "zh" and market_filter == "US" else t(lang, "板块热力图", "Sector Heatmap"),
        "heatmap_help": t(lang, "看资金和模型信号集中在哪些行业/主题，先确认当前市场主线。", "See where model signals cluster by sector/theme and confirm the current market leadership first."),
        "open_heatmap": t(lang, "打开板块热力图", "Open heatmap"),
        "secondary_title": secondary_title,
        "secondary_help": t(lang, "美股先看连续命中和强势延续，A股继续看概念的命中变化、连续性和扩散广度。", "For U.S. names, track persistence and repeated hits; for A-shares, keep using concept delta, streak, and breadth."),
        "secondary_open": secondary_open,
        "action_boards": t(lang, "今日行动榜单", "Action Boards"),
        "action_boards_help": t(lang, "把市场主线落到股票清单，适合继续筛选、加入自选或进入个股分析。", "Turn market themes into names for screening, watchlist actions, or insight review."),
        "precomputed_candidates": t(lang, "当前预计算候选数", "precomputed candidates"),
        "open_boards": t(lang, "打开榜单", "Open boards"),
        "signal_distribution": t(lang, "信号分布", "Signal Distribution"),
        "candidate_preview": t(lang, "轻量候选预览", "Candidate Preview"),
        "candidate_preview_help": t(lang, "这里只放少量候选，完整股票筛选请去今日行动榜单或模型选股。", "Only a few names stay here. Use action boards or screeners for full screening."),
        "advanced": t(lang, "高级筛选和窗口设置", "Advanced Filters & Window"),
        "snapshot_window": t(lang, "快照窗口", "Snapshot Window"),
        "market_scope": t(lang, "市场范围", "Market Scope"),
        "signal_focus": "信号聚焦" if lang == "zh" else "Signal Focus",
        "execution_tag": "执行提醒标签" if lang == "zh" else "Execution Tag",
        "exclude_tag": "排除标签" if lang == "zh" else "Exclude Tag",
        "min_buy_hits": "最少买点数" if lang == "zh" else "Min BUY Hits In Window",
        "min_strength": "最低强度" if lang == "zh" else "Min Strength",
        "filter_help": "这个字段会按当前快照窗口内，这只股票最近被标记为 BUY 的次数来过滤首页候选。" if lang == "zh" else "This field filters homepage candidates by how many recent snapshots tagged the stock as BUY inside the current window.",
        "apply": t(lang, "应用筛选", "Apply Filters"),
    }
    return render_template(
        "dashboard/market.html",
        **view,
        copy=copy,
        page_style=BOARD_DETAIL_ROWS_STYLE,
        language_hrefs=language_hrefs,
        heatmap_href=heatmap_href,
        secondary_href=secondary_href,
        secondary_preview_html=secondary_preview_html,
        latest_snapshot=format_app_datetime(view.get("heatmap_updated_at"), with_tz=True),
        tone_sentence=("当前市场状态：" if lang == "zh" else "Current market tone: ") + str(view["market_tone"]),
        execution_tag_value="" if str(view["execution_tag_filter"]).upper() == "ALL" else view["execution_tag_filter"],
        exclude_execution_tag_value="" if str(view["exclude_execution_tag_filter"]).upper() == "ALL" else view["exclude_execution_tag_filter"],
    )
