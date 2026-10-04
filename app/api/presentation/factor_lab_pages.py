from __future__ import annotations

from urllib.parse import quote

from app.api.presentation.styles_screener import FACTOR_LAB_PAGE_STYLE, OUTCOME_CELL_STYLE
from app.api.presentation.templates import render_template


_COPY = {
    "zh": {
        "title": "因子实验室",
        "eyebrow": "盈利导向因子实验",
        "lead": "这里不是替换现有模型，而是在模型结果上增加一层可解释、可回测、可保存的因子配置。你可以把技术条件、基本面、风险标签、LightGBM、多模型命中、Kronos 和利润断层条件组合成自己的选股策略。",
        "back": "返回模型选股",
        "evaluation": "模型评测总览",
        "current": "当前策略",
        "run": "运行策略实验",
        "project": "策略项目",
        "save_filters": "保存过滤 + 权重",
        "base": "基于模板",
        "new_name": "新策略名称",
        "min_readiness": "最低交易就绪度",
        "min_trend": "最低趋势分",
        "min_profit": "最低利润同比",
        "readiness_weight": "交易就绪度权重",
        "save": "保存为我的策略",
        "structure": "已选策略结构",
        "no_weights": "暂无权重",
        "factor": "因子",
        "value": "阈值",
        "type": "类型",
        "hard": "硬条件",
        "optional": "可缺失",
        "no_filters": "暂无过滤条件",
        "runs": "实验 Run 历史",
        "runs_help": "自动统计 1D/3D/5D 命中率、回撤和高开买不到",
        "strategy": "策略",
        "source_matched": "源样本/命中",
        "gap_blocked": "高开买不到",
        "actions": "操作",
        "detail": "详情",
        "no_runs": "还没有实验 run。先点击运行策略。",
        "meaning": "含义",
        "usage": "用途",
        "risk": "风险",
        "regime": "适用环境",
        "filters": "个过滤条件",
        "weighted": "个权重因子",
        "snapshot_ready": "已找到预计算快照，可直接运行。",
        "snapshot_missing": "没有找到对应预计算快照，请先等待/触发模型预计算 job。",
    },
    "en": {
        "title": "Factor Lab",
        "eyebrow": "Profit-Oriented Factor Experiments",
        "lead": "This layer does not replace the existing models. It adds explainable, backtestable, saved factor strategies on top of model results.",
        "back": "Back to Screeners",
        "evaluation": "Model Evaluation",
        "current": "Current Strategy",
        "run": "Run Experiment",
        "project": "Strategy Project",
        "save_filters": "Save Filters + Weights",
        "base": "Base strategy",
        "new_name": "New strategy name",
        "min_readiness": "Min readiness",
        "min_trend": "Min trend score",
        "min_profit": "Min profit YoY",
        "readiness_weight": "Readiness weight",
        "save": "Save Strategy",
        "structure": "Selected Strategy",
        "no_weights": "No weights",
        "factor": "Factor",
        "value": "Value",
        "type": "Type",
        "hard": "Hard",
        "optional": "Optional",
        "no_filters": "No filters",
        "runs": "Experiment Runs",
        "runs_help": "Tracks 1D/3D/5D hit rate, drawdown, and gap-unbuyable rate",
        "strategy": "Strategy",
        "source_matched": "Source/Matched",
        "gap_blocked": "Gap blocked",
        "actions": "Actions",
        "detail": "Detail",
        "no_runs": "No runs yet. Run a strategy first.",
        "meaning": "Meaning",
        "usage": "Usage",
        "risk": "Risk",
        "regime": "Best Regime",
        "filters": "filters",
        "weighted": "weighted factors",
        "snapshot_ready": "Snapshot ready.",
        "snapshot_missing": "No matching precomputed snapshot yet. Run or wait for screener precompute first.",
    },
}


_DETAIL_COPY = {
    "zh": {
        "title": "因子实验详情",
        "eyebrow": "因子实验 Run",
        "back": "返回因子实验室",
        "refresh": "用最新行情刷新表现",
        "refreshed": "刷新自",
        "source": "源样本",
        "matched": "命中",
        "gap_blocked": "高开买不到",
        "details": "候选明细",
        "details_help": "看每只股票为什么入选，以及后续真实表现",
        "ticker": "股票",
        "market": "市场",
        "factor_score": "因子分",
        "top_factors": "主要因子",
        "signal_date": "信号日",
        "next_gap": "次日高开",
        "blocked": "买不到",
        "yes": "是",
        "no": "否",
        "pending": "待行情",
        "empty": "这个 run 没有候选明细。",
    },
    "en": {
        "title": "Factor Run Detail",
        "eyebrow": "Factor Experiment Run",
        "back": "Back to Factor Lab",
        "refresh": "Refresh Outcomes",
        "refreshed": "Refreshed from",
        "source": "Source",
        "matched": "Matched",
        "gap_blocked": "Gap blocked",
        "details": "Candidate Details",
        "details_help": "Why each name passed and how it performed afterwards",
        "ticker": "Ticker",
        "market": "Market",
        "factor_score": "Factor Score",
        "top_factors": "Top Factors",
        "signal_date": "Signal Date",
        "next_gap": "Next Gap",
        "blocked": "Blocked",
        "yes": "Yes",
        "no": "No",
        "pending": "pending",
        "empty": "This run has no row details.",
    },
}


def _metric(value: object, *, suffix: str = "", digits: int = 2) -> str:
    if value in (None, ""):
        return "-"
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def _factor_label(factor: dict, language: str) -> str:
    return str(factor.get(f"label_{language}") or factor.get("label_en") or factor.get("label_zh") or factor.get("key") or "")


def render_factor_lab_page(
    *,
    lang: str,
    strategies: list[dict],
    selected_strategy: dict,
    factor_defs: list[dict],
    runs: list[dict],
    snapshot_ready: bool,
    message: object,
    nav_html: str,
) -> str:
    language = "zh" if lang == "zh" else "en"
    copy = _COPY[language]
    factor_map = {str(item.get("key") or ""): item for item in factor_defs}
    category_labels = {
        "technical": ("技术条件", "Technical"),
        "fundamental": ("基本面条件", "Fundamental"),
        "risk": ("风险标签", "Risk"),
        "model": ("模型信号", "Model"),
        "profit_gap": ("利润断层", "Profit Gap"),
    }
    categories = []
    for key, labels in category_labels.items():
        items = [item for item in factor_defs if item.get("category") == key]
        if not items:
            continue
        categories.append(
            {
                "title": labels[0 if language == "zh" else 1],
                "rows": [
                    {
                        "label": _factor_label(item, language),
                        "key": str(item.get("key") or ""),
                        "description": str(item.get(f"description_{language}") or item.get("description_zh") or ""),
                        "usage": str(item.get(f"usage_{language}") or item.get("usage_zh") or ""),
                        "risk": str(item.get(f"risk_{language}") or item.get("risk_zh") or ""),
                        "market_fit": str(item.get(f"market_fit_{language}") or item.get("market_fit_zh") or ""),
                    }
                    for item in items
                ],
            }
        )
    selected_filters = []
    for item in selected_strategy.get("filters") or []:
        key = str(item.get("factor_key") or "")
        raw_value = item.get("value")
        value = raw_value if raw_value is not None else f"{item.get('min', '')} - {item.get('max', '')}"
        selected_filters.append(
            {
                "label": _factor_label(factor_map.get(key, {"key": key}), language),
                "op": str(item.get("op") or ""),
                "value": str(value),
                "type": copy["hard"] if item.get("required", True) else copy["optional"],
            }
        )
    weights = [
        {"label": _factor_label(factor_map.get(str(key), {"key": key}), language), "value": _metric(value, digits=0)}
        for key, value in sorted(
            (selected_strategy.get("weights") or {}).items(),
            key=lambda pair: -float(pair[1] or 0),
        )[:16]
    ]
    run_rows = []
    for snapshot in runs:
        payload = snapshot.get("payload") or {}
        metrics = payload.get("metrics") or {}
        run_rows.append(
            {
                "id": str(snapshot.get("id") or ""),
                "created_at": str(snapshot.get("created_at") or ""),
                "strategy_name": str((payload.get("strategy") or {}).get("name") or "-"),
                "source_count": str(payload.get("source_count") or 0),
                "matched_count": str(payload.get("matched_count") or 0),
                "hit_1d": _metric(metrics.get("hit_rate_1d_pct"), suffix="%"),
                "hit_3d": _metric(metrics.get("hit_rate_3d_pct"), suffix="%"),
                "hit_5d": _metric(metrics.get("hit_rate_5d_pct"), suffix="%"),
                "drawdown_5d": _metric(metrics.get("avg_max_drawdown_5d_pct"), suffix="%"),
                "gap_blocked": _metric(metrics.get("gap_unbuyable_rate_pct"), suffix="%"),
            }
        )
    filter_count = len(selected_strategy.get("filters") or [])
    weight_count = len(selected_strategy.get("weights") or {})
    strategy_summary = f"{filter_count} {copy['filters']} · {weight_count} {copy['weighted']}"
    return render_template(
        "screeners/factor_lab.html",
        lang=language,
        copy=copy,
        strategies=strategies,
        selected_strategy=selected_strategy,
        categories=categories,
        selected_filters=selected_filters,
        weights=weights,
        run_rows=run_rows,
        snapshot_ready=snapshot_ready,
        source_hint=copy["snapshot_ready"] if snapshot_ready else copy["snapshot_missing"],
        strategy_summary=strategy_summary,
        message=str(message or ""),
        nav_html=nav_html,
        page_style=FACTOR_LAB_PAGE_STYLE,
    )


def _outcome(value: object, *, pending: str) -> dict[str, str]:
    if value in (None, ""):
        return {"text": pending, "tone": "muted"}
    try:
        number = float(value)
        return {"text": f"{number:.2f}%", "tone": "pos" if number > 0 else "neg" if number < 0 else "flat"}
    except (TypeError, ValueError):
        return {"text": str(value), "tone": "flat"}


def render_factor_lab_run_detail_page(
    *,
    lang: str,
    snapshot_id: int,
    snapshot: dict,
    factor_defs: list[dict],
    nav_html: str,
) -> str:
    language = "zh" if lang == "zh" else "en"
    copy = _DETAIL_COPY[language]
    payload = snapshot.get("payload") or {}
    strategy = payload.get("strategy") or {}
    metrics = payload.get("metrics") or {}
    factor_map = {str(item.get("key") or ""): item for item in factor_defs}
    rows = []
    for row in payload.get("rows") or []:
        if not isinstance(row, dict):
            continue
        scores = row.get("factor_scores") if isinstance(row.get("factor_scores"), dict) else {}
        values = row.get("factor_values") if isinstance(row.get("factor_values"), dict) else {}
        factors = []
        for key, score in sorted(scores.items(), key=lambda pair: -float(pair[1] or -1))[:4]:
            value = values.get(key)
            factors.append(
                {
                    "label": _factor_label(factor_map.get(str(key), {"key": key}), language),
                    "score": _metric(score),
                    "value": "-" if value in (None, "") else str(value)[:22],
                }
            )
        outcome = row.get("forward_outcome") or {}
        ticker = str(row.get("ticker") or "-")
        rows.append(
            {
                "ticker": ticker,
                "ticker_path": quote(ticker, safe=""),
                "name": str(row.get("name") or "-"),
                "market": str(row.get("market") or "-"),
                "factor_score": _metric(row.get("factor_score")),
                "trend_score": _metric(row.get("trend_score"), digits=1),
                "factors": factors,
                "signal_date": str(outcome.get("trade_date") or row.get("factor_signal_trade_date") or "-"),
                "next_gap": _outcome(outcome.get("next_open_gap_pct"), pending=copy["pending"]),
                "return_1d": _outcome(outcome.get("return_1d_pct"), pending=copy["pending"]),
                "return_3d": _outcome(outcome.get("return_3d_pct"), pending=copy["pending"]),
                "return_5d": _outcome(outcome.get("return_5d_pct"), pending=copy["pending"]),
                "drawdown_5d": _outcome(outcome.get("max_drawdown_5d_pct"), pending=copy["pending"]),
                "blocked": copy["yes"] if outcome.get("gap_unbuyable") else copy["no"],
            }
        )
    return render_template(
        "screeners/factor_lab_run_detail.html",
        lang=language,
        copy=copy,
        snapshot_id=snapshot_id,
        strategy=strategy,
        source_count=str(payload.get("source_count") or 0),
        matched_count=str(payload.get("matched_count") or 0),
        metrics={
            "hit_1d": _metric(metrics.get("hit_rate_1d_pct"), suffix="%"),
            "hit_3d": _metric(metrics.get("hit_rate_3d_pct"), suffix="%"),
            "hit_5d": _metric(metrics.get("hit_rate_5d_pct"), suffix="%"),
            "gap_blocked": _metric(metrics.get("gap_unbuyable_rate_pct"), suffix="%"),
        },
        refreshed_from=payload.get("refreshed_from_snapshot_id"),
        rows=rows,
        nav_html=nav_html,
        page_style=OUTCOME_CELL_STYLE,
    )


def render_factor_lab_run_not_found(*, lang: str, nav_html: str) -> str:
    language = "zh" if lang == "zh" else "en"
    return render_template(
        "screeners/factor_lab_run_not_found.html",
        lang=language,
        title="实验 run 不存在" if language == "zh" else "Run not found",
        back="返回因子实验室" if language == "zh" else "Back to Factor Lab",
        nav_html=nav_html,
        page_style=OUTCOME_CELL_STYLE,
    )
