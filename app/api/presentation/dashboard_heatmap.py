from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from app.api.presentation.i18n import t
from app.api.presentation.styles_dashboard import HEATMAP_METRIC_FOR_MARKET_LINK_STYLE
from app.api.presentation.templates import render_template


def render_dashboard_heatmap_page(view: dict[str, Any]) -> str:
    lang = "zh" if view.get("lang") == "zh" else "en"
    market_filter = str(view.get("market_filter") or "CN")
    common_params = {
        "lookback_runs": view["lookback_runs"],
        "heatmap_sort": view["heatmap_sort"],
        "heatmap_metric": view["heatmap_metric"],
        "market_filter": market_filter,
        "signal_filter": view["signal_filter"],
        "min_signal_strength": view["min_signal_strength"],
        "min_buy_signal_count": view["min_buy_signal_count"],
        "execution_tag_filter": view["execution_tag_filter"],
        "exclude_execution_tag_filter": view["exclude_execution_tag_filter"],
    }
    back_href = "/dashboard/market?" + urlencode(
        {key: value for key, value in {**common_params, "lang": lang}.items() if key != "heatmap_metric"}
    )
    language_hrefs = {
        language: f"/dashboard/market/heatmap?{urlencode({**common_params, 'lang': language})}"
        for language in ("en", "zh")
    }
    if market_filter == "US":
        secondary_href = "/dashboard/continuous-leaders?" + urlencode(
            {"lang": lang, "lookback_runs": view["lookback_runs"], "continuous_market": "US"}
        )
        secondary_label = t(lang, "连续强势跟踪", "Continuous Leaders")
        headline = t(lang, "美股板块资金流热力图", "U.S. Sector Flow Heatmap")
        description = t(
            lang,
            "聚焦美股行业轮动、成交额放大和上涨广度；颜色默认代表资金流代理，面积代表模型命中密度。",
            "Focus on U.S. sector rotation, turnover expansion, and breadth. Color defaults to flow proxy; tile size is model-hit density.",
        )
    else:
        secondary_href = "/dashboard/market/concepts?" + urlencode(
            {
                "lang": lang,
                "lookback_runs": view["lookback_runs"],
                "signal_filter": view["signal_filter"],
                "min_signal_strength": view["min_signal_strength"],
                "min_buy_signal_count": view["min_buy_signal_count"],
                "execution_tag_filter": view["execution_tag_filter"],
                "exclude_execution_tag_filter": view["exclude_execution_tag_filter"],
            }
        )
        secondary_label = t(lang, "概念异动追踪", "Concept Activity Tracker")
        headline = t(lang, "板块热力图", "Sector Heatmap")
        description = t(
            lang,
            "快速查看最新模型命中集中在哪些概念。热力同时参考 Top-N 集中度、概念 5 日强弱和上涨广度。",
            "A quick view of where the latest model picks cluster. Heat blends Top-N concentration, concept-level 5D strength, and breadth.",
        )
    copy = {
        "title": t(lang, "板块热力图", "Sector Heatmap"),
        "sidebar_description": t(lang, "这里专门看板块热度、信号分布和筛选后的热力排序。", "Use this page for sector heat, signal distribution, and filtered ranking."),
        "sidebar_note": t(lang, "板块页聚焦在“哪里最热、哪里最强、哪里带风险标签”。", "The heatmap focuses on where the market is hottest, strongest, and carrying execution tags."),
        "back": t(lang, "返回市场脉冲", "Back to Market Pulse"),
        "secondary": secondary_label,
        "headline": headline,
        "description": description,
        "snapshot_window": t(lang, "快照窗口", "Snapshot Window"),
        "market_scope": t(lang, "市场范围", "Market Scope"),
        "color_metric": t(lang, "颜色指标", "Color Metric"),
        "signal_focus": "信号聚焦" if lang == "zh" else "Signal Focus",
        "execution_tag": "执行提醒标签" if lang == "zh" else "Execution Tag",
        "exclude_tag": "排除标签" if lang == "zh" else "Exclude Tag",
        "quick_tags": "快捷标签" if lang == "zh" else "Quick Tags",
        "exclude_gap": "排除 gap-risk" if lang == "zh" else "exclude gap-risk",
        "clear_tags": "清空标签" if lang == "zh" else "Clear Tags",
        "min_buy_count": "最少买点数" if lang == "zh" else "Min Buy Count",
        "min_strength": "最低强度" if lang == "zh" else "Min Strength",
        "apply": t(lang, "应用筛选", "Apply Filters"),
        "risk_overview": t(lang, "风险概览", "Risk Overview"),
        "tagged_names": t(lang, "风险标记数", "Tagged Names"),
        "risk_examples": t(lang, "风险样例", "Risk Examples"),
        "common_risks": t(lang, "常见风险", "Common Risks"),
        "heatmap_sort": t(lang, "热力排序", "Heatmap Sort"),
        "heatmap_help": t(lang, "面积代表模型命中数量；颜色代表当前选择的强弱指标。点击任意色块进入概念详情和股票明细。", "Tile size represents model-hit density; color represents the selected strength metric. Click any tile for concept detail and ticker breakdown."),
        "current_metric": t(lang, "当前颜色指标", "Current color metric"),
        "signal_distribution": t(lang, "信号分布", "Signal Distribution"),
        "flow_proxy": t(lang, "资金流代理", "Flow Proxy"),
        "flow_help": t(lang, "建议这样看：先用面积看板块被命中的密度，再切到资金流代理看哪些板块在放量升温。两者同时强，才更像有持续轮动支持。", "Use tile size for hit density first, then switch color to flow proxy to see where turnover is expanding. The most actionable groups usually show both."),
        "concept_resonance": t(lang, "概念共振", "Concept Resonance"),
        "concept_resonance_help": t(lang, "观察多个概念同时出现模型命中的程度。", "Track how broadly model hits resonate across themes."),
        "tracked_signals": t(lang, "跟踪信号", "Tracked Signals"),
    }
    return render_template(
        "dashboard/heatmap.html",
        **view,
        copy=copy,
        page_style=HEATMAP_METRIC_FOR_MARKET_LINK_STYLE,
        back_href=back_href,
        secondary_href=secondary_href,
        language_hrefs=language_hrefs,
        execution_tag_value="" if str(view["execution_tag_filter"]).upper() == "ALL" else view["execution_tag_filter"],
        exclude_execution_tag_value="" if str(view["exclude_execution_tag_filter"]).upper() == "ALL" else view["exclude_execution_tag_filter"],
    )
