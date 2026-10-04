from __future__ import annotations

import html
from typing import Any

from app.api.presentation.i18n import t
from app.services.dashboard_insights import _fmt_optional_float
from app.services.template_evaluation import (
    lightgbm_bias,
    lightgbm_maturity,
    next_tesla_market_bias,
    next_tesla_maturity,
    technical_momentum_bias,
    technical_momentum_maturity,
)


def build_evaluation_fragments(
    *,
    next_tesla: dict[str, Any],
    next_tesla_maturity_state: dict[str, Any],
    technical: dict[str, Any],
    technical_maturity_state: dict[str, Any],
    lightgbm: dict[str, Any],
    lightgbm_maturity_state: dict[str, Any],
    lightgbm_prediction: dict[str, Any],
    lang: str,
) -> dict[str, Any]:
    next_windows = next_tesla.get("windows") or {}
    next_sector_windows = next_tesla.get("sector_windows") or {}
    next_sector_counts = next_tesla.get("sector_counts") or {}
    next_per_market = next_tesla.get("per_market") or {}
    next_snapshot_total = int(next_tesla.get("snapshot_total") or 0)
    next_clean_total = int(next_tesla.get("clean_snapshot_total") or 0)

    technical_windows = technical.get("windows") or {}
    technical_sector_windows = technical.get("sector_windows") or {}
    technical_sector_counts = technical.get("sector_counts") or {}
    technical_per_market = technical.get("per_market") or {}

    lightgbm_windows = lightgbm.get("windows") or {}
    lightgbm_sector_windows = lightgbm.get("sector_windows") or {}
    lightgbm_sector_counts = lightgbm.get("sector_counts") or {}
    lightgbm_per_market = lightgbm.get("per_market") or {}

    prediction_windows = lightgbm_prediction.get("windows") or {}
    prediction_execution = lightgbm_prediction.get("execution") or {}
    prediction_per_market = lightgbm_prediction.get("per_market") or {}

    return {
        "next_tesla_maturity_style": _maturity_style(next_tesla_maturity_state),
        "next_tesla_rows_html": "".join(
            _metric_row(next_windows, action, label, horizons=(3, 5, 10))
            for action, label in (
                ("buy_the_dip", "Buy The Dip"),
                ("wait_for_breakout", "Wait For Breakout"),
            )
        ),
        "next_tesla_sector_buy_html": _next_tesla_sector_summary(
            next_sector_windows,
            next_sector_counts,
            action="buy_the_dip",
            lang=lang,
        ),
        "next_tesla_sector_breakout_html": _next_tesla_sector_summary(
            next_sector_windows,
            next_sector_counts,
            action="wait_for_breakout",
            lang=lang,
        ),
        "next_tesla_market_split_html": _template_market_split(
            next_per_market, template="next_tesla", lang=lang
        ),
        "next_tesla_takeaway": _next_tesla_takeaway(next_windows, lang=lang),
        "next_tesla_note": (
            f"最近回看 {next_snapshot_total} 个快照，其中 {next_clean_total} 个是带 Buy The Dip / Wait For Breakout 干净标签的样本。"
            if lang == "zh"
            else f"Reviewing the latest {next_snapshot_total} snapshots, with {next_clean_total} carrying clean Buy The Dip / Wait For Breakout labels."
        ),
        "technical_maturity_style": _maturity_style(technical_maturity_state),
        "technical_buy_row": _metric_row(
            technical_windows, "buy", "BUY", horizons=(3, 5, 10)
        ),
        "technical_watch_row": _metric_row(
            technical_windows, "watch", "WATCH", horizons=(3, 5, 10)
        ),
        "technical_market_split_html": _template_market_split(
            technical_per_market, template="technical", lang=lang
        ),
        "technical_sector_buy_html": _sector_summary(
            technical_sector_windows,
            technical_sector_counts,
            action="buy",
            lang=lang,
        ),
        "technical_sector_watch_html": _sector_summary(
            technical_sector_windows,
            technical_sector_counts,
            action="watch",
            lang=lang,
        ),
        "technical_takeaway": _technical_takeaway(technical_windows, lang=lang),
        "technical_snapshot_total": int(technical.get("snapshot_total") or 0),
        "technical_labeled_total": int(technical.get("labeled_snapshot_total") or 0),
        "lightgbm_maturity_style": _maturity_style(lightgbm_maturity_state),
        "lightgbm_pullback_row": _metric_row(
            lightgbm_windows, "pullback", "Pullback", horizons=(1, 3, 5, 10)
        ),
        "lightgbm_breakout_row": _metric_row(
            lightgbm_windows, "breakout", "Breakout", horizons=(1, 3, 5, 10)
        ),
        "lightgbm_watch_row": _metric_row(
            lightgbm_windows, "watch", "Watch", horizons=(1, 3, 5, 10)
        ),
        "lightgbm_market_split_html": _template_market_split(
            lightgbm_per_market, template="lightgbm", lang=lang
        ),
        "lightgbm_sector_pullback_html": _sector_summary(
            lightgbm_sector_windows,
            lightgbm_sector_counts,
            action="pullback",
            lang=lang,
        ),
        "lightgbm_sector_breakout_html": _sector_summary(
            lightgbm_sector_windows,
            lightgbm_sector_counts,
            action="breakout",
            lang=lang,
        ),
        "lightgbm_short_cycle_1": _short_cycle_card(
            lightgbm_windows,
            window=1,
            label="次日 / 1D" if lang == "zh" else "Next Day / 1D",
            lang=lang,
        ),
        "lightgbm_short_cycle_3": _short_cycle_card(
            lightgbm_windows,
            window=3,
            label="3日 / 3D" if lang == "zh" else "3 Day / 3D",
            lang=lang,
        ),
        "lightgbm_short_cycle_5": _short_cycle_card(
            lightgbm_windows,
            window=5,
            label="5日 / 5D" if lang == "zh" else "5 Day / 5D",
            lang=lang,
        ),
        "lightgbm_takeaway": _lightgbm_takeaway(lightgbm_windows, lang=lang),
        "lightgbm_snapshot_total": int(lightgbm.get("snapshot_total") or 0),
        "lightgbm_labeled_total": int(lightgbm.get("labeled_snapshot_total") or 0),
        "prediction_pullback_row": _metric_row(
            prediction_windows, "pullback", "Pullback", horizons=(1, 3, 5)
        ),
        "prediction_breakout_row": _metric_row(
            prediction_windows, "breakout", "Breakout", horizons=(1, 3, 5)
        ),
        "prediction_watch_row": _metric_row(
            prediction_windows, "watch", "Watch", horizons=(1, 3, 5)
        ),
        "prediction_short_cycle_1": _short_cycle_card(
            prediction_windows,
            window=1,
            label="次日 / 1D" if lang == "zh" else "Next Day / 1D",
            lang=lang,
            historical=True,
        ),
        "prediction_short_cycle_3": _short_cycle_card(
            prediction_windows,
            window=3,
            label="3日 / 3D" if lang == "zh" else "3 Day / 3D",
            lang=lang,
            historical=True,
        ),
        "prediction_short_cycle_5": _short_cycle_card(
            prediction_windows,
            window=5,
            label="5日 / 5D" if lang == "zh" else "5 Day / 5D",
            lang=lang,
            historical=True,
        ),
        "prediction_market_split_html": _prediction_market_split(
            prediction_per_market, lang=lang
        ),
        "execution_pullback_row": _execution_row(
            prediction_execution, "pullback", "Pullback"
        ),
        "execution_breakout_row": _execution_row(
            prediction_execution, "breakout", "Breakout"
        ),
        "execution_watch_row": _execution_row(
            prediction_execution, "watch", "Watch"
        ),
        "prediction_run_count": int(lightgbm_prediction.get("run_count") or 0),
        "prediction_sample_count": int(lightgbm_prediction.get("sample_count") or 0),
        "prediction_latest_trade_date": str(
            lightgbm_prediction.get("latest_trade_date") or ""
        ),
    }


def _maturity_style(state: dict[str, Any]) -> str:
    tone = str(state.get("tone") or "")
    if tone == "good":
        return "background:#dcfce7;color:#166534;"
    if tone == "mid":
        return "background:#fef3c7;color:#92400e;"
    return "background:#e5eef7;color:#37516b;"


def _metric_row(
    windows: dict[str, Any], action: str, label: str, *, horizons: tuple[int, ...]
) -> str:
    payload = windows.get(action) or {}
    cells = "".join(
        f"<td>{int((payload.get(window) or {}).get('count') or 0)}</td>"
        f"<td>{_fmt_optional_float((payload.get(window) or {}).get('avg_return'), suffix='%', digits=2)}"
        f"<div class='muted'>{_fmt_optional_float((payload.get(window) or {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
        for window in horizons
    )
    return f"<tr><td>{label}</td>{cells}</tr>"


def _market_name(code: str, lang: str) -> str:
    if lang == "zh" and code == "CN":
        return "A股"
    if lang == "zh" and code == "US":
        return "美股"
    return code


def _template_market_split(
    per_market: dict[str, Any], *, template: str, lang: str
) -> str:
    market_codes = [code for code in ("CN", "US") if code in per_market]
    if len(market_codes) <= 1:
        return ""

    def card(code: str) -> str:
        payload = per_market.get(code) or {}
        if template == "next_tesla":
            maturity = next_tesla_maturity(payload, lang=lang)
            bias = next_tesla_market_bias(payload, lang=lang)
            sample_key = "clean_snapshot_total"
            sample_label = t(lang, "clean 样本", "Clean samples")
        elif template == "technical":
            maturity = technical_momentum_maturity(payload, lang=lang)
            bias = technical_momentum_bias(payload, lang=lang)
            sample_key = "labeled_snapshot_total"
            sample_label = t(lang, "带标签样本", "Labeled samples")
        else:
            maturity = lightgbm_maturity(payload, lang=lang)
            bias = lightgbm_bias(payload, lang=lang)
            sample_key = "labeled_snapshot_total"
            sample_label = t(lang, "带动作样本", "Action samples")
        return (
            "<div class='metric-card'>"
            f"<div class='eyebrow'>{_market_name(code, lang)}</div>"
            f"<div class='muted'>{html.escape(str(maturity.get('level') or '-'))}</div>"
            f"<div class='muted' style='margin-top:6px;'>{t(lang, '当前偏向', 'Current bias')}: {html.escape(bias)}</div>"
            f"<div class='muted' style='margin-top:6px;'>{t(lang, '快照', 'Snapshots')} {int(payload.get('snapshot_total') or 0)} · {sample_label} {int(payload.get(sample_key) or 0)}</div>"
            "</div>"
        )

    return (
        "<div style='display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));margin-top:12px;'>"
        + "".join(card(code) for code in market_codes)
        + "</div>"
    )


def _next_tesla_sector_summary(
    sector_windows: dict[str, Any],
    sector_counts: dict[str, Any],
    *,
    action: str,
    lang: str,
) -> str:
    groups = sector_windows.get(action) or {}
    counts = sector_counts.get(action) or {}
    ranked = sorted(
        set(groups) | set(counts),
        key=lambda sector: (
            -int(counts.get(sector, 0)),
            -int(((groups.get(sector) or {}).get(5) or {}).get("count") or 0),
            str(sector or ""),
        ),
    )[:3]
    return "".join(
        f"<div class='muted'>• {html.escape(str(sector or '-'))} · "
        f"{int(counts.get(sector, 0))} {t(lang, '次出现', 'hits')}"
        + (
            f" · {_fmt_optional_float(((groups.get(sector) or {}).get(5) or {}).get('avg_return'), suffix='%', digits=2)} / {_fmt_optional_float(((groups.get(sector) or {}).get(5) or {}).get('hit_rate'), suffix='%', digits=1)}"
            if int(((groups.get(sector) or {}).get(5) or {}).get("count") or 0) > 0
            else ""
        )
        + "</div>"
        for sector in ranked
    ) or "<div class='muted'>-</div>"


def _sector_summary(
    sector_windows: dict[str, Any],
    sector_counts: dict[str, Any],
    *,
    action: str,
    lang: str,
) -> str:
    groups = sector_windows.get(action) or {}
    counts = sector_counts.get(action) or {}
    ordered = sorted(
        counts.items(),
        key=lambda item: (
            -int(((groups.get(item[0]) or {}).get(5) or {}).get("count") or 0),
            -int(item[1] or 0),
            str(item[0] or ""),
        ),
    )[:3]
    if not ordered:
        return f"<div class='muted'>{t(lang, '当前还没有足够的行业样本。', 'No sector concentration yet.')}</div>"
    rows = []
    for sector_label, seen_count in ordered:
        stats = (groups.get(sector_label) or {}).get(5) or {}
        rows.append(
            "<div style='padding:8px 0;border-bottom:1px solid var(--line);'>"
            f"<div style='font-weight:700;color:var(--ink);'>{html.escape(str(sector_label or '-'))}</div>"
            f"<div class='muted'>{t(lang, '出现', 'Seen')} {int(seen_count)} {t(lang, '次', 'times')}"
            + (
                f" · 5D {_fmt_optional_float(stats.get('avg_return'), suffix='%', digits=2)} / {_fmt_optional_float(stats.get('hit_rate'), suffix='%', digits=1)}"
                if int(stats.get("count") or 0) > 0
                else ""
            )
            + "</div></div>"
        )
    return "".join(rows)


def _short_cycle_card(
    windows: dict[str, Any],
    *,
    window: int,
    label: str,
    lang: str,
    historical: bool = False,
) -> str:
    ranked = []
    for action, action_label in (
        ("pullback", "Pullback"),
        ("breakout", "Breakout"),
        ("watch", "Watch"),
    ):
        stats = (windows.get(action) or {}).get(window) or {}
        ranked.append(
            (
                int(stats.get("count") or 0),
                float(stats.get("hit_rate") or 0.0),
                float(stats.get("avg_return") or 0.0),
                action_label,
            )
        )
    ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
    count, hit_rate, avg_return, action_label = ranked[0]
    if count <= 0:
        summary = (
            ("当前没有成熟历史样本。" if historical else "当前没有成熟样本。")
            if lang == "zh"
            else ("No mature historical samples yet." if historical else "No mature samples yet.")
        )
        detail = (
            ("先继续累积跨日结果。" if historical else "先继续留样。")
            if lang == "zh"
            else ("Keep accumulating cross-session results." if historical else "Keep collecting samples first.")
        )
    else:
        summary = (
            f"{action_label} 当前更占优"
            if lang == "zh"
            else f"{action_label} currently leads"
        )
        detail = (
            f"命中率 {_fmt_optional_float(hit_rate, suffix='%', digits=1)} · 平均收益 {_fmt_optional_float(avg_return, suffix='%', digits=2)}"
            if lang == "zh"
            else f"Hit rate {_fmt_optional_float(hit_rate, suffix='%', digits=1)} · Avg {_fmt_optional_float(avg_return, suffix='%', digits=2)}"
        )
    return (
        "<article class='metric-card'>"
        f"<div class='eyebrow'>{label}</div>"
        f"<div style='font-size:22px;font-weight:800;line-height:1.25;margin:6px 0 8px;'>{html.escape(summary)}</div>"
        f"<div class='muted'>{html.escape(detail)}</div>"
        f"<div class='muted' style='margin-top:8px;'>{t(lang, '样本', 'Samples')} {count}</div>"
        "</article>"
    )


def _prediction_market_split(per_market: dict[str, Any], *, lang: str) -> str:
    market_codes = [code for code in ("CN", "US") if code in per_market]
    if len(market_codes) <= 1:
        return ""
    cards = []
    for code in market_codes:
        payload = per_market.get(code) or {}
        windows = payload.get("windows") or {}
        ranked = []
        for action, action_label in (
            ("pullback", "Pullback"),
            ("breakout", "Breakout"),
            ("watch", "Watch"),
        ):
            stats = (windows.get(action) or {}).get(1) or {}
            ranked.append(
                (
                    int(stats.get("count") or 0),
                    float(stats.get("hit_rate") or 0.0),
                    float(stats.get("avg_return") or 0.0),
                    action_label,
                )
            )
        ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
        count, hit_rate, avg_return, action_label = ranked[0]
        if count <= 0:
            summary = "样本观察中" if lang == "zh" else "Observation only"
            detail = (
                "当前还没有成熟次日样本。"
                if lang == "zh"
                else "No mature next-day samples yet."
            )
        else:
            summary = (
                f"次日更偏 {action_label}"
                if lang == "zh"
                else f"1D leans {action_label}"
            )
            detail = (
                f"命中率 {_fmt_optional_float(hit_rate, suffix='%', digits=1)} · 平均收益 {_fmt_optional_float(avg_return, suffix='%', digits=2)}"
                if lang == "zh"
                else f"Hit rate {_fmt_optional_float(hit_rate, suffix='%', digits=1)} · Avg {_fmt_optional_float(avg_return, suffix='%', digits=2)}"
            )
        cards.append(
            "<div class='metric-card'>"
            f"<div class='eyebrow'>{_market_name(code, lang)}</div>"
            f"<div style='font-size:20px;font-weight:800;line-height:1.25;margin:6px 0 8px;'>{html.escape(summary)}</div>"
            f"<div class='muted'>{html.escape(detail)}</div>"
            f"<div class='muted' style='margin-top:8px;'>{t(lang, '历史样本', 'Historical samples')} {int(payload.get('sample_count') or 0)}</div>"
            "</div>"
        )
    return (
        "<div style='display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));margin-top:12px;'>"
        + "".join(cards)
        + "</div>"
    )


def _execution_row(execution: dict[str, Any], action: str, label: str) -> str:
    stats = execution.get(action) or {}
    return (
        "<tr>"
        f"<td>{label}</td>"
        f"<td>{int(stats.get('count') or 0)}</td>"
        f"<td>{_fmt_optional_float(stats.get('execution_hit_rate'), suffix='%', digits=1)}</td>"
        f"<td>{_fmt_optional_float(stats.get('avg_next_open_gap'), suffix='%', digits=2)}</td>"
        f"<td>{_fmt_optional_float(stats.get('avg_next_open_to_high'), suffix='%', digits=2)}</td>"
        f"<td>{_fmt_optional_float(stats.get('avg_next_low_drawdown'), suffix='%', digits=2)}</td>"
        f"<td>{_fmt_optional_float(stats.get('gap_blocked_rate'), suffix='%', digits=1)}</td>"
        f"<td>{_fmt_optional_float(stats.get('high_open_fail_rate'), suffix='%', digits=1)}</td>"
        "</tr>"
    )


def _next_tesla_takeaway(windows: dict[str, Any], *, lang: str) -> str:
    dip = (windows.get("buy_the_dip") or {}).get(5) or {}
    breakout = (windows.get("wait_for_breakout") or {}).get(5) or {}
    if int(dip.get("count") or 0) <= 0 and int(breakout.get("count") or 0) <= 0:
        return (
            "当前还没有成熟窗口样本，先把这块当作样本沉淀看板，不宜下结论。"
            if lang == "zh"
            else "There are no mature forward-return windows yet, so treat this as sample accumulation rather than a verdict."
        )
    dip_hit = float(dip.get("hit_rate") or 0.0)
    breakout_hit = float(breakout.get("hit_rate") or 0.0)
    if dip_hit >= breakout_hit + 5:
        return (
            "目前回踩买点的 5 日盈利率更高，说明这套模板近期更偏向支撑承接。"
            if lang == "zh"
            else "Buy-the-dip currently shows the higher 5-day hit rate, which suggests better support-follow-through lately."
        )
    if breakout_hit >= dip_hit + 5:
        return (
            "目前突破确认的 5 日盈利率更高，说明这套模板近期更偏向等确认后再跟。"
            if lang == "zh"
            else "Breakout confirmation currently shows the higher 5-day hit rate, which suggests waiting for confirmation has been cleaner lately."
        )
    return (
        "两类打法目前差距不大，更适合把它当作两套独立 playbook 来执行。"
        if lang == "zh"
        else "The two playbooks are currently close enough that they should be treated as separate execution styles rather than one unified edge."
    )


def _technical_takeaway(windows: dict[str, Any], *, lang: str) -> str:
    buy = (windows.get("buy") or {}).get(5) or {}
    watch = (windows.get("watch") or {}).get(5) or {}
    if int(buy.get("count") or 0) <= 0 and int(watch.get("count") or 0) <= 0:
        return (
            "当前还没有成熟 5 日窗口，因此更适合作为观察看板，而不是直接给出偏向判断。"
            if lang == "zh"
            else "There are no mature 5-day windows yet, so treat this as an observation panel rather than a directional verdict."
        )
    buy_hit = float(buy.get("hit_rate") or 0.0)
    watch_hit = float(watch.get("hit_rate") or 0.0)
    if buy_hit >= watch_hit + 5:
        return (
            "近期直接 BUY 的 5 日命中率更高，说明确认后的直接跟随更顺。"
            if lang == "zh"
            else "Direct BUY currently has the higher 5-day hit rate, which suggests cleaner post-confirmation follow-through."
        )
    if watch_hit >= buy_hit + 5:
        return (
            "近期 WATCH 再确认更稳，说明动量信号更适合先观察、再等二次确认。"
            if lang == "zh"
            else "WATCH-first currently looks steadier, which suggests momentum names are rewarding confirmation more than immediate follow-through."
        )
    return (
        "BUY 和 WATCH 目前差距不大，更适合当成两套节奏不同的执行模板。"
        if lang == "zh"
        else "BUY and WATCH are currently close enough to be treated as two execution tempos rather than one dominant edge."
    )


def _lightgbm_takeaway(windows: dict[str, Any], *, lang: str) -> str:
    pullback = (windows.get("pullback") or {}).get(5) or {}
    breakout = (windows.get("breakout") or {}).get(5) or {}
    if int(pullback.get("count") or 0) <= 0 and int(breakout.get("count") or 0) <= 0:
        return (
            "当前还没有成熟 5 日窗口，因此先把 LightGBM 当作观察面板，不宜直接下动作强结论。"
            if lang == "zh"
            else "There are no mature 5-day windows yet, so treat LightGBM as an observation panel rather than an execution verdict."
        )
    pullback_hit = float(pullback.get("hit_rate") or 0.0)
    breakout_hit = float(breakout.get("hit_rate") or 0.0)
    if pullback_hit >= breakout_hit + 5:
        return (
            "近期 LightGBM 在回踩类机会上的 5 日命中率更高，说明更适合等支撑承接再介入。"
            if lang == "zh"
            else "LightGBM currently shows the stronger 5-day hit rate on pullback setups, which suggests waiting for support follow-through."
        )
    if breakout_hit >= pullback_hit + 5:
        return (
            "近期 LightGBM 在突破类机会上的 5 日命中率更高，说明顺势确认后的跟随更顺。"
            if lang == "zh"
            else "LightGBM currently shows the stronger 5-day hit rate on breakout setups, which suggests cleaner confirmation follow-through."
        )
    return (
        "回踩与突破两类机会目前差距不大，更适合把它们当作两套并行执行节奏。"
        if lang == "zh"
        else "Pullback and breakout are currently close enough to be treated as parallel execution styles."
    )
