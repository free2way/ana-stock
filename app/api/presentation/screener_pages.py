from __future__ import annotations

from urllib.parse import urlencode

from app.api.presentation.styles_screener import (
    FILTER_HREF_STYLE,
    MARKET_SNAPSHOT_PAGE_STYLE,
    SELECTION_QUALITY_PAGE_STYLE,
    TODAY_FOCUS_POOL_PAGE_STYLE,
)
from app.api.presentation.templates import render_template
from app.services.stock_selection.kronos_pool import kronos_decision_tone


_TODAY_FOCUS_COPY = {
    "zh": {
        "title": "今日重点盯盘池",
        "description": "把今天最值得先看的股票先放进一个临时研究池；该列表不构成买入授权。",
        "sidebar_note": "这页更像盘前/盘后优先级列表，决定谁先看，而不是最终持有清单。",
        "back_to_screener": "量化选股器",
        "open_watchlist": "打开自选股",
        "holding_area": "把最值得优先盯盘和复盘的股票，先放进今天的重点池。",
        "ticker": "代码",
        "name": "名称",
        "market": "市场",
        "patterns": "命中形态",
        "signal": "模型信号",
        "strength": "最低信号强度",
        "watchlist": "自选状态",
        "last_sync": "最近同步",
        "insight": "分析页",
        "open_insight": "打开分析页",
        "empty": "当前没有股票符合筛选规则。",
    },
    "en": {
        "title": "Today Focus Pool",
        "description": "Collect the names you want to review first into a temporary focus pool; this list is not buy authorization.",
        "sidebar_note": "This page acts like a premarket/postmarket priority list rather than a final holdings list.",
        "back_to_screener": "Quant Screener",
        "open_watchlist": "Open Watchlist",
        "holding_area": "A holding area for the names you want to study first today.",
        "ticker": "Ticker",
        "name": "Name",
        "market": "Market",
        "patterns": "Pattern Hits",
        "signal": "Model Signal",
        "strength": "Minimum Signal Strength",
        "watchlist": "Watchlist",
        "last_sync": "Last Sync",
        "insight": "Insight",
        "open_insight": "Open Insight",
        "empty": "No stocks matched the current rules.",
    },
}


def render_today_focus_page(*, lang: str, rows: list[dict], nav_html: str) -> str:
    language = "zh" if lang == "zh" else "en"
    return render_template(
        "screeners/today_focus.html",
        lang=language,
        copy=_TODAY_FOCUS_COPY[language],
        rows=rows,
        nav_html=nav_html,
        page_style=TODAY_FOCUS_POOL_PAGE_STYLE,
    )


_KRONOS_COPY = {
    "zh": {
        "page_title": "Kronos 二次验证池",
        "sidebar_title": "Kronos 验证池",
        "sidebar_description": "这里展示模型候选经过 K 线路径模型二次验证后的结果。",
        "back": "返回模型选股",
        "settings": "Kronos 配置",
        "report": "AI 日报",
        "refresh": "刷新验证池",
        "eyebrow": "二次验证，不是单独选股",
        "lead": "这里的股票来自 LightGBM、多模型共振和技术模板的 Top 候选。Kronos 负责验证未来 1-3 日 K 线路径是否支持，而不是重新扫描全市场。",
        "latest": "最新快照",
        "status": "状态",
        "model": "模型",
        "candidates": "候选",
        "validated": "已验证",
        "updated": "更新时间",
        "results": "验证结果",
        "results_help": "优先看“已验证 + Kronos 支持 + 预期收益为正 + 回撤可控”的股票；待配置状态说明主流程已接上，但独立 PyTorch/Kronos runner 还没有启用。",
        "ticker": "股票",
        "market": "市场",
        "decision": "结论 / 原因",
        "score": "Kronos 分",
        "precheck": "预检",
        "expected": "预期收益",
        "three_day": "3日",
        "drawdown": "最大回撤",
        "latest_close": "最新价",
        "source": "候选来源",
        "signal": "原模型信号",
        "strength": "强度",
        "bars": "历史条数",
        "empty": "当前筛选条件下没有 Kronos 验证候选。可以先在任务中心刷新 Kronos 验证快照，或等待收盘预计算自动生成。",
    },
    "en": {
        "page_title": "Kronos Validation Pool",
        "sidebar_title": "Kronos Pool",
        "sidebar_description": "Review model candidates after K-line path validation.",
        "back": "Back to Screeners",
        "settings": "Kronos Settings",
        "report": "AI Report",
        "refresh": "Refresh Pool",
        "eyebrow": "Secondary validation, not standalone screening",
        "lead": "These names come from LightGBM, multi-model confluence, and technical-template top candidates. Kronos validates the 1-3 day path instead of scanning the whole market.",
        "latest": "Latest Snapshot",
        "status": "Status",
        "model": "Model",
        "candidates": "Candidates",
        "validated": "Validated",
        "updated": "Updated",
        "results": "Validation Results",
        "results_help": "Focus on validated names with Kronos support, positive expected return, and controlled drawdown. Needs-setup means the pipeline is wired, but the isolated PyTorch/Kronos runner is not enabled yet.",
        "ticker": "Ticker",
        "market": "Market",
        "decision": "Decision / Reason",
        "score": "Kronos Score",
        "precheck": "Precheck",
        "expected": "Expected Return",
        "three_day": "3D",
        "drawdown": "Max Drawdown",
        "latest_close": "Latest Close",
        "source": "Source",
        "signal": "Original Signal",
        "strength": "Strength",
        "bars": "Bars",
        "empty": "No Kronos validation candidates under current filters. Refresh the snapshot or wait for post-close precompute.",
    },
}


def _format_number(value: object, *, suffix: str = "", digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "-"


def _kronos_status_view(value: object, *, lang: str) -> dict[str, str]:
    status = str(value or "-").strip().upper()
    status_class = (
        "ready"
        if status == "READY"
        else "pending"
        if status in {"NOT_CONFIGURED", "PENDING"}
        else "skipped"
        if status == "SKIPPED"
        else "failed"
        if status == "FAILED"
        else "idle"
    )
    labels = {
        "READY": {"zh": "已验证", "en": "Validated"},
        "NOT_CONFIGURED": {"zh": "待配置", "en": "Needs setup"},
        "PENDING": {"zh": "待运行", "en": "Pending"},
        "SKIPPED": {"zh": "已跳过", "en": "Skipped"},
        "FAILED": {"zh": "失败", "en": "Failed"},
    }
    return {"class": status_class, "label": (labels.get(status) or {}).get(lang, status)}


def _kronos_row_view(item: dict, *, lang: str) -> dict:
    return {
        "ticker": str(item.get("ticker") or "-"),
        "name": str(item.get("name") or "-"),
        "market": str(item.get("market") or "-"),
        "status": _kronos_status_view(item.get("kronos_status"), lang=lang),
        "decision": str(item.get("kronos_decision") or "-"),
        "decision_tone": kronos_decision_tone(item.get("kronos_decision")),
        "reason": str(item.get("kronos_reason") or "-"),
        "score": _format_number(item.get("kronos_score"), digits=1),
        "precheck_score": _format_number((item.get("path_precheck") or {}).get("score"), digits=1),
        "expected_1d": _format_number(item.get("kronos_expected_return_1d_pct"), suffix="%"),
        "expected_3d": _format_number(item.get("kronos_expected_return_3d_pct"), suffix="%"),
        "drawdown": _format_number(item.get("kronos_max_drawdown_pct"), suffix="%"),
        "latest_close": _format_number(item.get("latest_close")),
        "latest_date": str(item.get("latest_date") or "-"),
        "source": str(item.get("source") or "-"),
        "source_templates": " / ".join(str(value) for value in (item.get("source_templates") or [])) or "-",
        "signal_label": str(item.get("model_signal_label") or "-"),
        "signal_strength": _format_number(item.get("model_signal_strength"), digits=1),
        "history_count": str(item.get("history_count") or "-"),
    }


def render_kronos_validation_pool_page(
    *,
    lang: str,
    pool: dict,
    snapshot_created_at: object,
    nav_html: str,
) -> str:
    language = "zh" if lang == "zh" else "en"
    selected_market = str(pool.get("selected_market") or "ALL")
    selected_status = str(pool.get("selected_status") or "ALL")
    market_counts = pool.get("market_counts") or {}
    status_counts = pool.get("status_counts") or {}

    def href(*, market: str = selected_market, status: str = selected_status) -> str:
        return "/screeners/kronos-validation?" + urlencode(
            {"lang": language, "market": market, "status": status}
        )

    market_filters = [
        {"value": "ALL", "label": "全部市场" if language == "zh" else "All", "href": href(market="ALL")},
        {"value": "CN", "label": f"{'A股' if language == 'zh' else 'CN'} {market_counts.get('CN', 0)}", "href": href(market="CN")},
        {"value": "US", "label": f"{'美股' if language == 'zh' else 'US'} {market_counts.get('US', 0)}", "href": href(market="US")},
    ]
    status_filters = [
        {"value": "ALL", "label": "全部状态" if language == "zh" else "All status", "href": href(status="ALL")},
        {"value": "READY", "label": f"{'已验证' if language == 'zh' else 'Validated'} {status_counts.get('READY', 0)}", "href": href(status="READY")},
        {"value": "NOT_CONFIGURED", "label": f"{'待配置' if language == 'zh' else 'Needs setup'} {status_counts.get('NOT_CONFIGURED', 0)}", "href": href(status="NOT_CONFIGURED")},
        {"value": "SKIPPED", "label": f"{'跳过' if language == 'zh' else 'Skipped'} {status_counts.get('SKIPPED', 0)}", "href": href(status="SKIPPED")},
        {"value": "FAILED", "label": f"{'失败' if language == 'zh' else 'Failed'} {status_counts.get('FAILED', 0)}", "href": href(status="FAILED")},
    ]
    return render_template(
        "screeners/kronos_validation_pool.html",
        lang=language,
        copy=_KRONOS_COPY[language],
        pool=pool,
        rows=[_kronos_row_view(item, lang=language) for item in (pool.get("rows") or [])],
        selected_market=selected_market,
        selected_status=selected_status,
        market_filters=market_filters,
        status_filters=status_filters,
        snapshot_time=pool.get("updated_at") or snapshot_created_at or "-",
        nav_html=nav_html,
        page_style=FILTER_HREF_STYLE,
    )


_SELECTION_QUALITY_COPY = {
    "zh": {
        "page_title": "命中率闭环",
        "eyebrow": "选股质量闭环",
        "samples": "总样本",
        "execution_hit": "执行命中",
        "avg_1d": "平均1D",
        "back": "返回模型选股",
        "factor_lab": "因子实验室",
        "refresh": "刷新质量快照",
        "playbook": "使用建议",
        "how_to_use": "今天怎么用",
        "default_rule": "继续累计样本，优先使用多模型共振候选。",
        "source": "来源",
        "by_source": "按来源评估",
        "converting": "谁真的有兑现率",
        "available_total": "有效/总数",
        "open_high": "开盘冲高",
        "open_low": "开盘回撤",
        "gap_block": "高开拦截",
        "no_samples": "还没有可评估样本。",
        "recent": "最近样本",
        "records": "逐票验证记录",
        "ticker": "股票",
        "market": "市场",
        "signal_next": "信号/验证日",
        "score": "分数",
        "high": "冲高",
        "low": "回撤",
        "risk": "风险",
        "no_recent": "暂无最近样本。",
    },
    "en": {
        "page_title": "Selection Quality",
        "eyebrow": "Selection Quality Loop",
        "samples": "Samples",
        "execution_hit": "Execution Hit",
        "avg_1d": "Avg 1D",
        "back": "Back to Screeners",
        "factor_lab": "Factor Lab",
        "refresh": "Refresh Snapshot",
        "playbook": "Playbook",
        "how_to_use": "How to use today",
        "default_rule": "Keep accumulating samples and prioritize multi-model confluence candidates.",
        "source": "Source",
        "by_source": "By Source",
        "converting": "What is really converting",
        "available_total": "Available/Total",
        "open_high": "Open-High",
        "open_low": "Open-Low",
        "gap_block": "Gap Block",
        "no_samples": "No evaluable samples yet.",
        "recent": "Recent Samples",
        "records": "Per-name validation records",
        "ticker": "Ticker",
        "market": "Market",
        "signal_next": "Signal/Next",
        "score": "Score",
        "high": "High",
        "low": "Low",
        "risk": "Risk",
        "no_recent": "No recent samples.",
    },
}


def _selection_metric(value: object, *, suffix: str = "") -> str:
    if value in (None, ""):
        return "-"
    try:
        return f"{float(value):.2f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def render_selection_quality_page(*, lang: str, payload: dict, nav_html: str) -> str:
    language = "zh" if lang == "zh" else "en"
    copy = _SELECTION_QUALITY_COPY[language]
    summary = payload.get("summary") or {}
    all_metrics = summary.get("all") or {}
    guidance = payload.get("guidance") or {}
    source_counts = payload.get("source_counts") or {}
    meta = payload.get("snapshot_meta") or {}
    source_rows = []
    for item in list(summary.get("by_source") or [])[:24]:
        metrics = item.get("metrics") or {}
        source_rows.append(
            {
                "source_name": str(item.get("source_name") or "-"),
                "source_type": str(item.get("source_type") or "-"),
                "available": str(metrics.get("available_1d") or 0),
                "count": str(metrics.get("count") or 0),
                "hit_rate_1d": _selection_metric(metrics.get("hit_rate_1d_pct"), suffix="%"),
                "execution_hit_rate": _selection_metric(metrics.get("execution_hit_rate_pct"), suffix="%"),
                "avg_return_1d": _selection_metric(metrics.get("avg_return_1d_pct"), suffix="%"),
                "avg_open_to_high": _selection_metric(metrics.get("avg_open_to_high_pct"), suffix="%"),
                "avg_open_to_low": _selection_metric(metrics.get("avg_open_to_low_pct"), suffix="%"),
                "gap_blocked_rate": _selection_metric(metrics.get("gap_blocked_rate_pct"), suffix="%"),
            }
        )
    records = []
    for record in list(payload.get("recent_records") or [])[:60]:
        ticker = str(record.get("ticker") or "-")
        records.append(
            {
                "ticker": ticker,
                "name": str(record.get("name") or ""),
                "insight_href": f"/insights/{urlencode({'ticker': ticker})[7:]}?lang={language}" if ticker != "-" else "#",
                "market": str(record.get("market") or "-"),
                "signal_date": str(record.get("signal_date") or "-"),
                "next_date": str(record.get("next_date") or "pending"),
                "source_name": str(record.get("source_name") or "-"),
                "source_type": str(record.get("source_type") or "-"),
                "score": _selection_metric(record.get("score")),
                "return_1d": _selection_metric(record.get("return_1d_pct"), suffix="%"),
                "open_to_high": _selection_metric(record.get("open_to_high_pct"), suffix="%"),
                "open_to_low": _selection_metric(record.get("open_to_low_pct"), suffix="%"),
                "risk_flags": [str(flag) for flag in list(record.get("risk_flags") or [])[:3]],
            }
        )
    rules = list(guidance.get(f"rules_{language}") or guidance.get("rules_zh") or [])[:5]
    return render_template(
        "screeners/selection_quality.html",
        lang=language,
        copy=copy,
        headline=str(guidance.get(f"headline_{language}") or ""),
        sample_count=str(payload.get("sample_count") or 0),
        all_metrics={
            "hit_rate_1d": _selection_metric(all_metrics.get("hit_rate_1d_pct"), suffix="%"),
            "execution_hit_rate": _selection_metric(all_metrics.get("execution_hit_rate_pct"), suffix="%"),
            "avg_return_1d": _selection_metric(all_metrics.get("avg_return_1d_pct"), suffix="%"),
        },
        rules=[str(rule) for rule in rules],
        source_name=str(meta.get("source") or "live"),
        ai_count=str(source_counts.get("ai_daily_report") or 0),
        factor_count=str(source_counts.get("factor_experiment") or 0),
        source_rows=source_rows,
        records=records,
        nav_html=nav_html,
        page_style=SELECTION_QUALITY_PAGE_STYLE,
    )


def render_screener_regression_discipline(
    *,
    lang: str,
    recommendation_regression: dict,
    regression_guidance: dict,
    status_counts: dict,
    visible_count: int,
) -> str:
    sample_count = int(recommendation_regression.get("sample_count") or 0)
    if sample_count <= 0 and visible_count <= 0:
        return ""
    language = "zh" if lang == "zh" else "en"
    policy = recommendation_regression.get("policy") or {}
    min_quality = policy.get("min_actionable_quality_score")
    max_actionable = policy.get("max_actionable_count")
    if language == "zh":
        title = "手动筛选纪律"
        subtitle = "这里不强行删掉筛选结果，而是提醒你哪些票只能观察，哪些才值得进入明日盯盘。"
        stats = [
            ("当前展示", f"{visible_count} 只"),
            ("READY", f"{status_counts.get('ready', 0)} 只"),
            ("不要追高", f"{status_counts.get('do_not_chase', 0)} 只"),
            ("阻断", f"{status_counts.get('blocked', 0)} 只"),
            ("低就绪", f"{status_counts.get('low_readiness', 0)} 只"),
            ("质量门槛", str(min_quality) if min_quality is not None else "常规"),
            ("最多可执行", f"{max_actionable} 只" if max_actionable is not None else "不额外限制"),
        ]
        workflow = "使用建议：先按模型/共振筛出候选，再只把 READY + 接近买点 + 无硬风险标签的股票加入重点池；其余放观察池等二次确认。"
    else:
        title = "Manual Screening Discipline"
        subtitle = "The screener keeps results visible, but tells you which names should stay on watch versus move into the next-session focus list."
        stats = [
            ("Visible", str(visible_count)),
            ("READY", str(status_counts.get("ready", 0))),
            ("Do not chase", str(status_counts.get("do_not_chase", 0))),
            ("Blocked", str(status_counts.get("blocked", 0))),
            ("Low readiness", str(status_counts.get("low_readiness", 0))),
            ("Quality gate", str(min_quality) if min_quality is not None else "normal"),
            ("Max actionable", str(max_actionable) if max_actionable is not None else "no extra cap"),
        ]
        workflow = "Workflow: screen for model/confluence candidates first, then only move READY names near buy zones with no hard risk tags into the focus pool."
    metrics = [item for item in (regression_guidance.get("metrics") or [])[:4] if isinstance(item, dict)]
    rules = [str(item).strip() for item in (regression_guidance.get("rules") or []) if str(item).strip()]
    warnings = [str(item).strip() for item in (regression_guidance.get("warnings") or []) if str(item).strip()]
    return render_template(
        "screeners/_regression_discipline.html",
        title=title,
        headline=str(regression_guidance.get("headline") or title),
        subtitle=subtitle,
        stats=[{"label": label, "value": value} for label, value in stats],
        metrics=[{"label": str(item.get("label") or "-"), "value": str(item.get("value") or "-")} for item in metrics],
        workflow=workflow,
        notes=warnings[:3] or rules[:3],
    )


def render_quality_profile_status(
    *,
    lang: str,
    regime: object,
    breadth: object,
    candidate_count: int,
) -> str:
    language = "zh" if lang == "zh" else "en"
    paused = candidate_count <= 0
    if language == "zh":
        title = "A 级质量策略已暂停开仓" if paused else "A 级质量策略候选"
        detail = (
            "当前市场处于防御/低广度状态，所有共振票只保留观察，不强行凑单。"
            if paused
            else "以下候选已通过双模型、市场状态及可成交性硬门槛。"
        )
    else:
        title = "A-tier quality strategy: no new entries" if paused else "A-tier quality candidates"
        detail = (
            "The market gate is defensive or breadth is weak; confluence names remain watch-only."
            if paused
            else "These names passed the confluence, market-state, and tradability gates."
        )
    return render_template(
        "screeners/_quality_profile_status.html",
        title=title,
        detail=detail,
        regime=str(regime or "unknown"),
        breadth="-" if breadth is None else str(breadth),
        candidate_count=candidate_count,
    )


_MARKET_SNAPSHOT_COPY = {
    "zh": {
        "title": "市场快照榜单",
        "sidebar_description": "把强势、收口、连阳、放量候选放进一个盘面快照板。",
        "sidebar_note": "这个页面适合盘前盘后扫榜，不适合做深度研究；看中某只票再进入洞察页。",
        "back": "量化选股器",
        "focus": "今日重点盯盘池",
        "watchlist": "打开自选股",
        "all": "全部市场",
        "cn": "A股",
        "us": "美股",
        "boards": "榜单",
        "candidates": "候选",
        "heat": "热度",
        "top_trend": "最高趋势",
        "history": "近6次同模式快照",
        "mode": "当前模式",
        "warming": "升温",
        "cooling": "降温",
        "flat": "持平",
        "methodology": "口径说明：这里的热度统一表示 0-100 的平均强度分。行动榜单页面使用当前模式下候选股 trend_score 的均值。",
        "lead": "把今天最值得先看的强势、收口、连阳、放量候选股集中成一个快照页。",
        "scope": "当前范围",
        "view_mode": "查看模式",
        "sentiment": "市场情绪",
        "avg_score": "平均分",
        "bullish_boards": "强势榜单",
        "total_candidates": "候选",
        "loading_title": "后台预计算",
        "loading": "市场快照仍在后台生成，稍后刷新即可。",
        "empty": "当前市场范围下还没有可展示的快照榜单。",
        "ticker": "代码",
        "name": "名称",
        "snapshot_score": "快照分",
        "trend": "趋势",
        "patterns": "命中形态",
        "breakdown": "分数驱动",
        "rating": "技术评级",
        "focus_action": "加入今日重点",
        "in_watchlist": "已在自选",
        "sync_on": "同步开启",
        "off": "未加入",
        "add": "加入今日重点盯盘池",
        "premarket": "盘前",
        "monitor": "盘中观察",
        "postmarket": "盘后复盘",
    },
    "en": {
        "title": "Market Snapshot",
        "sidebar_description": "Collect momentum, squeeze, candle, and volume candidates into one market snapshot board.",
        "sidebar_note": "Use this page for a fast premarket/postmarket scan, then open insight for deep work.",
        "back": "Quant Screener",
        "focus": "Today Focus Pool",
        "watchlist": "Open Watchlist",
        "all": "All Markets",
        "cn": "A-Shares",
        "us": "U.S. Stocks",
        "boards": "Boards",
        "candidates": "Names",
        "heat": "Heat",
        "top_trend": "Top Trend",
        "history": "Last 6 same-mode snapshots",
        "mode": "Mode",
        "warming": "warming",
        "cooling": "cooling",
        "flat": "Flat",
        "methodology": "Methodology: heat is a unified 0-100 average strength score. On action boards it is the mean trend_score of candidates under the current mode.",
        "lead": "A compact trading board for today’s strongest local setups.",
        "scope": "Current scope",
        "view_mode": "View Mode",
        "sentiment": "Market Sentiment",
        "avg_score": "Avg score",
        "bullish_boards": "Bullish boards",
        "total_candidates": "Candidates",
        "loading_title": "Background Precompute",
        "loading": "Market snapshot boards are still being generated in the background. Please refresh shortly.",
        "empty": "There are no snapshot boards to show under the current market scope.",
        "ticker": "Ticker",
        "name": "Name",
        "snapshot_score": "Snapshot Score",
        "trend": "Trend",
        "patterns": "Pattern Hits",
        "breakdown": "Score Drivers",
        "rating": "Technical Rating",
        "focus_action": "Today Focus Pool",
        "in_watchlist": "In Watchlist",
        "sync_on": "Sync On",
        "off": "Off",
        "add": "Add To Today Focus",
        "premarket": "Premarket",
        "monitor": "Monitor",
        "postmarket": "Postmarket",
    },
}


def _numeric_view(value: object, *, digits: int = 1, suffix: str = "") -> str:
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "-"


def _score_tone(value: object, *, kind: str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "neutral"
    if kind == "change":
        return "good" if number > 0 else "bad" if number < 0 else "neutral"
    if kind == "trend":
        return "good" if number >= 80 else "positive" if number >= 65 else "warn" if number >= 50 else "bad"
    return "good" if number >= 85 else "positive" if number >= 70 else "warn" if number >= 55 else "neutral"


def _market_snapshot_row(item: dict, *, existing: dict | None, copy: dict) -> dict:
    ticker = str(item.get("ticker") or "").upper()
    ratings = []
    for interval in ("1d", "1w", "1M"):
        recommendation = str(((item.get("tradingview_ratings") or {}).get(interval) or {}).get("recommendation") or "-").upper()
        tone = "good" if recommendation in {"BUY", "STRONG_BUY"} else "bad" if recommendation in {"SELL", "STRONG_SELL"} else "warn" if recommendation == "NEUTRAL" else "neutral"
        ratings.append({"interval": {"1d": "1D", "1w": "1W", "1M": "1M"}[interval], "value": recommendation, "tone": tone})
    watchlist_chips = [copy["off"]]
    if existing:
        watchlist_chips = [copy["in_watchlist"]]
        if existing.get("sync_enabled"):
            watchlist_chips.append(copy["sync_on"])
    momentum = item.get("momentum_5")
    momentum_text = _numeric_view(momentum, suffix="%")
    try:
        if float(momentum) > 0:
            momentum_text = "+" + momentum_text
    except (TypeError, ValueError):
        pass
    return {
        "ticker": ticker or "-",
        "name": str(item.get("name") or ticker or "-"),
        "market": str(item.get("market") or "CN"),
        "snapshot_score": _numeric_view(item.get("snapshot_score"), digits=0),
        "snapshot_tone": _score_tone(item.get("snapshot_score"), kind="snapshot"),
        "trend_score": _numeric_view(item.get("trend_score"), digits=0),
        "trend_tone": _score_tone(item.get("trend_score"), kind="trend"),
        "momentum_5": momentum_text,
        "momentum_tone": _score_tone(momentum, kind="change"),
        "volume_ratio": _numeric_view(item.get("volume_ratio")),
        "patterns": " / ".join(str(value) for value in (item.get("matched_patterns") or [])[:3]) or "-",
        "breakdown": [str(value) for value in (item.get("snapshot_score_breakdown") or [])[:4]],
        "ratings": ratings,
        "watchlist_chips": watchlist_chips,
        "selection_reason": str(item.get("selection_reason") or ""),
    }


def render_market_snapshot_page(
    *,
    lang: str,
    view: dict,
    sentiment: dict,
    watchlist_map: dict[str, dict],
    message: object,
    nav_html: str,
) -> str:
    language = "zh" if lang == "zh" else "en"
    copy = _MARKET_SNAPSHOT_COPY[language]
    mode = str(view.get("mode") or "monitor")
    selected_market = str(view.get("selected_market") or "CN")

    def href(*, selected_mode: str = mode, market: str = selected_market) -> str:
        return "/screeners/market-snapshot?" + urlencode(
            {"lang": language, "mode": selected_mode, "market_filter": market}
        )

    market_filters = [
        {"value": value, "label": copy[key], "href": href(market=value)}
        for value, key in (("ALL", "all"), ("CN", "cn"), ("US", "us"))
    ]
    mode_filters = [
        {"value": value, "label": copy[value], "href": href(selected_mode=value)}
        for value in ("premarket", "monitor", "postmarket")
    ]
    summaries = []
    for item in view.get("market_summaries") or []:
        delta = int(item.get("delta") or 0)
        delta_label = f"+{delta} {copy['warming']}" if delta > 0 else f"{delta} {copy['cooling']}" if delta < 0 else copy["flat"]
        values = item.get("history") or []
        highest = max(values, default=0)
        summaries.append(
            {
                **item,
                "label": copy["cn"] if item.get("market") == "CN" else copy["us"],
                "href": href(market=str(item.get("market") or "CN")),
                "delta_label": delta_label,
                "history_bars": [
                    {"height": 6 + round((int(value) / max(highest, 1)) * 22), "value": int(value)}
                    for value in values
                ],
            }
        )
    boards = []
    for board in view.get("boards") or []:
        boards.append(
            {
                "title": str(board.get(f"title_{language}") or board.get("title_en") or "-"),
                "description": str(board.get(f"description_{language}") or board.get("description_en") or ""),
                "rows": [
                    _market_snapshot_row(
                        row,
                        existing=watchlist_map.get(str(row.get("ticker") or "").upper()),
                        copy=copy,
                    )
                    for row in (board.get("rows") or [])
                    if isinstance(row, dict)
                ],
            }
        )
    sentiment_value = str(sentiment.get("sentiment") or "neutral").replace("_", " ").upper()
    sentiment_tone = "good" if any(value in sentiment_value for value in ("RISK ON", "BUY", "BULLISH")) else "bad" if any(value in sentiment_value for value in ("RISK OFF", "SELL", "BEARISH")) else "warn"
    return render_template(
        "screeners/market_snapshot.html",
        lang=language,
        copy=copy,
        view=view,
        boards=boards,
        summaries=summaries,
        market_filters=market_filters,
        mode_filters=mode_filters,
        selected_market=selected_market,
        market_label={"ALL": copy["all"], "CN": copy["cn"], "US": copy["us"]}[selected_market],
        mode=mode,
        sentiment={
            "value": sentiment_value,
            "tone": sentiment_tone,
            "average": str(sentiment.get("average_snapshot_score", "-")),
            "bullish_boards": str(sentiment.get("bullish_boards", "-")),
            "total_candidates": str(sentiment.get("total_candidates", "-")),
        },
        message=str(message or ""),
        nav_html=nav_html,
        page_style=MARKET_SNAPSHOT_PAGE_STYLE,
    )


def render_screener_main_page(*, fragments: list[object]) -> str:
    """Render the main screener shell from explicitly ordered presentation fragments.

    This is a transitional boundary: legacy component helpers still produce some
    trusted HTML fragments, but the page document itself is owned by Jinja rather
    than the FastAPI route.
    """
    return render_template("screeners/main.html", fragments=fragments)
