from __future__ import annotations

import html
from typing import Any

from app.api.presentation.i18n import t
from app.services.dashboard_insights import _fmt_optional_float


def render_structured_evaluation_rows(
    evaluations: list[dict[str, Any]], *, lang: str
) -> str:
    rows = "".join(
        (
            "<tr>"
            f"<td>{html.escape(str(item.get('model_name') or '-'))}<div class='muted'>{html.escape(str(item.get('model_type') or '-'))} · run #{item.get('model_run_id') or '-'}</div></td>"
            f"<td>{html.escape(str(item.get('market') or '-'))}</td>"
            f"<td>{html.escape(str(item.get('sample_start_date') or '-'))} → {html.escape(str(item.get('sample_end_date') or '-'))}<div class='muted'>{'严格样本外' if item.get('is_out_of_sample') else '观察/未满足严格样本外'}</div></td>"
            f"<td>{int(item.get('oos_sample_count') or item.get('sample_count') or 0)}<div class='muted'>{int(item.get('oos_coverage_days') or 0)} {t(lang, '交易日', 'days')} · purge {item.get('purge_gap_days') if item.get('purge_gap_days') is not None else '-'}</div></td>"
            f"<td>{_fmt_optional_float(_horizon_metric(item, 'hit_rate'), suffix='%', digits=1)}</td>"
            f"<td>{_fmt_optional_float(_horizon_metric(item, 'avg_return'), suffix='%', digits=2)}</td>"
            f"<td>{_fmt_optional_float(_horizon_metric(item, 'max_drawdown'), suffix='%', digits=2)}</td>"
            f"<td>{_fmt_optional_float(item.get('round_trip_cost_bps'), suffix=' bps', digits=0)}</td>"
            f"<td>{html.escape(str(item.get('activation_status') or 'observation'))}</td>"
            "</tr>"
        )
        for item in evaluations
    )
    return rows or (
        f"<tr><td colspan='9'>{t(lang, '暂无结构化评测；可在任务中心运行“结构化模型评测”。', 'No structured evaluation yet; run Structured model evaluation from Ops.')}</td></tr>"
    )


def _horizon_metric(item: dict[str, Any], key: str, horizon: int = 5) -> Any:
    return next(
        (
            metric.get(key)
            for metric in (item.get("metrics") or [])
            if int(metric.get("horizon_days") or 0) == horizon
        ),
        None,
    )


def render_run_summary_cards(summary: dict[str, Any] | None, *, lang: str) -> str:
    if summary is None:
        return ""
    windows = summary.get("windows") or {}
    return "".join(
        (
            "<article class='metric-card'>"
            f"<div class='eyebrow'>{window}{t(lang, '日表现', 'D Window')}</div>"
            f"<div class='metric'>{_fmt_optional_float((windows.get(window) or {}).get('avg_return'), suffix='%', digits=2)}</div>"
            f"<div class='muted'>{t(lang, '上涨命中率', 'Positive hit rate')} {_fmt_optional_float((windows.get(window) or {}).get('hit_rate'), suffix='%', digits=1)}</div>"
            f"<div class='muted'>{t(lang, '强命中', 'Strong hit')} {_fmt_optional_float((windows.get(window) or {}).get('strong_hit_rate'), suffix='%', digits=1)} · "
            f"{t(lang, '失效率', 'Miss rate')} {_fmt_optional_float((windows.get(window) or {}).get('miss_rate'), suffix='%', digits=1)}</div>"
            f"<div class='muted'>{t(lang, '样本数', 'Samples')} {(windows.get(window) or {}).get('count') or 0}</div>"
            "</article>"
        )
        for window in (3, 5, 10)
    )


def render_training_diagnostic(
    *, run_artifact: dict[str, Any], run_config: dict[str, Any], lang: str
) -> str:
    symbol_context = run_artifact.get("symbol_context_summary") or run_config.get(
        "symbol_context_summary"
    ) or {}
    feature_families = list(
        run_artifact.get("feature_families") or run_config.get("feature_families") or []
    )
    target_profile = str(
        run_artifact.get("target_profile") or run_config.get("target_profile") or "-"
    )
    enhancement_mode = str(
        run_artifact.get("feature_enhancement_mode")
        or run_config.get("feature_enhancement_mode")
        or ""
    ).strip().lower()
    enhancement_note = str(
        run_artifact.get("feature_enhancement_note")
        or run_config.get("feature_enhancement_note")
        or ""
    ).strip()
    if enhancement_mode == "enhanced":
        state_label = "增强版已启用" if lang == "zh" else "Enhanced inputs active"
        state_style = "background:rgba(16,185,129,0.18);color:#6ee7b7;border:1px solid rgba(16,185,129,0.26);"
    elif enhancement_mode == "partial":
        state_label = "混合增强版" if lang == "zh" else "Partial enrichment"
        state_style = "background:rgba(96,165,250,0.18);color:#bfdbfe;border:1px solid rgba(96,165,250,0.26);"
    else:
        state_label = "价格量能版" if lang == "zh" else "Price-action mode"
        state_style = "background:rgba(245,158,11,0.18);color:#fcd34d;border:1px solid rgba(245,158,11,0.28);"

    family_text = " / ".join(
        {
            "price_trend": "价格趋势" if lang == "zh" else "Price Trend",
            "price_extension": "价格乖离" if lang == "zh" else "Price Extension",
            "volume_intensity": "量能强度" if lang == "zh" else "Volume Intensity",
            "intraday_structure": "日内结构" if lang == "zh" else "Intraday Structure",
            "liquidity_proxy": "流动性代理" if lang == "zh" else "Liquidity Proxy",
            "listing_maturity": "上市成熟度" if lang == "zh" else "Listing Maturity",
            "board_tier": "板块层级" if lang == "zh" else "Board Tier",
        }.get(str(item), str(item))
        for item in feature_families
    ) or t(lang, "未记录", "Not recorded")
    coverage_note = (
        "当前增强特征原料仍为空，先去任务中心运行 A 股基本面同步，再观察增强版 LightGBM 的变化。"
        if enhancement_mode == "price_action_only" and lang == "zh"
        else "Enhanced input tables are still empty. Run the CN fundamental sync from Ops first, then reassess the enriched LightGBM results."
        if enhancement_mode == "price_action_only"
        else "只有这些覆盖率上来，A 股增强版 LightGBM 的收益提升才值得认真解读。"
        if lang == "zh"
        else "Only once these coverage numbers rise is it worth seriously interpreting performance as an enriched A-share LightGBM run."
    )
    default_note = t(
        lang,
        "如果覆盖率很低，说明这次 run 仍主要依赖价格量能结构，基本面/概念增强尚未真正喂进训练。",
        "Low coverage means the run is still relying mostly on price/volume structure rather than enriched fundamentals or concept inputs.",
    )
    listing_cov = float(symbol_context.get("listing_date_coverage_pct") or 0.0)
    fund_cov = float(symbol_context.get("fundamental_history_coverage_pct") or 0.0)
    concept_cov = float(symbol_context.get("concept_history_coverage_pct") or 0.0)
    sync_href = f"/dashboard/ops/sync?lang={lang}&lookback_runs=5"
    return f"""
      <div class="metric-grid" style="margin-top:14px;">
        <article class="metric-card">
          <div class="eyebrow">{t(lang, '训练目标', 'Training Target')}</div>
          <div style="font-size:20px;font-weight:800;line-height:1.3;margin:6px 0 8px;">{html.escape(target_profile)}</div>
          <div class="muted">{t(lang, '当前 LightGBM 已切到短线复合目标，优先回答次日可执行性与 1D / 3D / 5D 跟随质量。', 'LightGBM is now running on a short-horizon composite target focused on next-session usability and 1D / 3D / 5D follow-through.')}</div>
        </article>
        <article class="metric-card">
          <div class="eyebrow">{t(lang, '特征模式', 'Feature Mode')}</div>
          <div style="display:inline-flex;align-items:center;padding:6px 10px;border-radius:999px;font-size:12px;font-weight:800;{state_style}">{html.escape(state_label)}</div>
          <div class="muted" style="margin-top:10px;">{html.escape(family_text)}</div>
          <div class="muted" style="margin-top:8px;">{html.escape(enhancement_note or default_note)}</div>
        </article>
        <article class="metric-card">
          <div class="eyebrow">{t(lang, '增强特征覆盖', 'Enriched Coverage')}</div>
          <div class="muted">{('上市日期 ' + _fmt_optional_float(listing_cov, suffix='%', digits=1)) if lang == 'zh' else ('Listing date ' + _fmt_optional_float(listing_cov, suffix='%', digits=1))}</div>
          <div class="muted">{('基本面历史 ' + _fmt_optional_float(fund_cov, suffix='%', digits=1)) if lang == 'zh' else ('Fundamental history ' + _fmt_optional_float(fund_cov, suffix='%', digits=1))}</div>
          <div class="muted">{('概念历史 ' + _fmt_optional_float(concept_cov, suffix='%', digits=1)) if lang == 'zh' else ('Concept history ' + _fmt_optional_float(concept_cov, suffix='%', digits=1))}</div>
          <div class="muted" style="margin-top:8px;">{html.escape(coverage_note)}</div>
          <div style="margin-top:12px;"><a class="pill" href="{html.escape(sync_href, quote=True)}">{t(lang, '去同步中心补原料', 'Open Sync Center')}</a></div>
        </article>
      </div>
    """


def render_run_options(
    runs: list[dict[str, Any]], *, selected_run_id: int | None
) -> str:
    return "".join(
        f"<option value='{int(item['id'])}' {'selected' if int(item['id']) == int(selected_run_id or 0) else ''}>"
        f"#{int(item['id'])} · {html.escape(str(item.get('name') or '-'))} · {html.escape(str(item.get('market') or '-'))}"
        "</option>"
        for item in runs
    )


def render_market_options(*, market: str, lang: str) -> str:
    options = (
        ("ALL", "全部市场" if lang == "zh" else "All markets"),
        ("CN", "A股" if lang == "zh" else "CN"),
        ("US", "美股" if lang == "zh" else "US"),
    )
    return "".join(
        f"<option value='{value}' {'selected' if market == value else ''}>{label}</option>"
        for value, label in options
    )


def render_aggregate_model_rows(
    aggregate_by_model: dict[tuple[str, str], dict[str, Any]], *, lang: str
) -> str:
    aggregate_rows = sorted(
        aggregate_by_model.values(),
        key=lambda item: (
            -_aggregate_value(item, 5, "hit"),
            -_aggregate_value(item, 5, "return"),
            -int(item.get("runs") or 0),
            str(item.get("name") or ""),
        ),
    )
    rows = "".join(_render_aggregate_model_row(item, lang=lang) for item in aggregate_rows)
    return rows or f"<tr><td colspan='7'>{t(lang, '暂无模型长期汇总。', 'No aggregate model summary yet.')}</td></tr>"


def _aggregate_value(item: dict[str, Any], window: int, kind: str) -> float:
    payload = ((item.get("window_sums") or {}).get(window) or {})
    count = int(payload.get("count") or 0)
    if count <= 0:
        return 0.0
    key = "weighted_return" if kind == "return" else "hit_weight"
    return float(payload.get(key) or 0.0) / count


def _aggregate_metric(item: dict[str, Any], window: int, kind: str) -> str:
    payload = ((item.get("window_sums") or {}).get(window) or {})
    if int(payload.get("count") or 0) <= 0:
        return "-"
    return _fmt_optional_float(
        _aggregate_value(item, window, kind),
        suffix="%",
        digits=2 if kind == "return" else 1,
    )


def _render_aggregate_model_row(item: dict[str, Any], *, lang: str) -> str:
    return (
        "<tr>"
        f"<td>{html.escape(str(item.get('name') or '-'))}<div class='muted'>{html.escape(str(item.get('market') or '-'))}</div></td>"
        f"<td>{int(item.get('runs') or 0)}</td>"
        f"<td>{int(item.get('trade_dates_covered') or 0)}<div class='muted'>{t(lang, '样本', 'Samples')} {int(item.get('sample_count') or 0)}</div></td>"
        f"<td>{html.escape(str(item.get('latest_trade_date') or '-'))}</td>"
        f"<td>{_aggregate_metric(item, 3, 'return')}<div class='muted'>{_aggregate_metric(item, 3, 'hit')}</div></td>"
        f"<td>{_aggregate_metric(item, 5, 'return')}<div class='muted'>{_aggregate_metric(item, 5, 'hit')}</div></td>"
        f"<td>{_aggregate_metric(item, 10, 'return')}<div class='muted'>{_aggregate_metric(item, 10, 'hit')}</div></td>"
        "</tr>"
    )


def render_grouped_return_rows(
    groups: list[dict[str, Any]], *, lang: str, empty_zh: str, empty_en: str
) -> str:
    rows = "".join(
        (
            "<tr>"
            f"<td>{html.escape(str(item.get('label') or '-'))}</td>"
            f"<td>{int(item.get('sample_count') or 0)}</td>"
            + "".join(
                f"<td>{_fmt_optional_float((item.get('windows') or {}).get(window, {}).get('avg_return'), suffix='%', digits=2)}"
                f"<div class='muted'>{_fmt_optional_float((item.get('windows') or {}).get(window, {}).get('hit_rate'), suffix='%', digits=1)}</div></td>"
                for window in (3, 5, 10)
            )
            + "</tr>"
        )
        for item in groups
    )
    return rows or f"<tr><td colspan='5'>{t(lang, empty_zh, empty_en)}</td></tr>"


def render_watchlist_fragments(
    summary: dict[str, Any] | None, *, lang: str
) -> tuple[str, str]:
    windows = (summary or {}).get("windows") or {}
    cards = "".join(
        (
            "<article class='metric-card'>"
            f"<div class='eyebrow'>{window}{t(lang, '日自选后表现', 'D Watchlist After Add')}</div>"
            f"<div class='metric'>{_fmt_optional_float((windows.get(window) or {}).get('avg_return'), suffix='%', digits=2)}</div>"
            f"<div class='muted'>{t(lang, '上涨命中率', 'Positive hit rate')} {_fmt_optional_float((windows.get(window) or {}).get('hit_rate'), suffix='%', digits=1)}</div>"
            f"<div class='muted'>{t(lang, '样本数', 'Samples')} {(windows.get(window) or {}).get('count') or 0}</div>"
            "</article>"
        )
        for window in (3, 5, 10)
    )
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('added_date') or '-'))}</td>"
        f"<td><a href='/insights/{html.escape(str(item.get('ticker') or ''), quote=True)}?lang={lang}'>{html.escape(str(item.get('ticker') or '-'))}</a><div class='muted'>{html.escape(str(item.get('name') or '-'))} · {html.escape(str(item.get('market') or '-'))}</div></td>"
        f"<td>{_fmt_optional_float(item.get('return_3d'), suffix='%', digits=2)}</td>"
        f"<td>{_fmt_optional_float(item.get('return_5d'), suffix='%', digits=2)}</td>"
        f"<td>{_fmt_optional_float(item.get('return_10d'), suffix='%', digits=2)}</td>"
        f"<td>{_fmt_optional_float(item.get('current_return'), suffix='%', digits=2)}</td>"
        f"<td>{html.escape(str(item.get('last_synced_date') or '-'))}<div class='muted'>{html.escape(str(item.get('sync_status') or '-'))}</div></td>"
        "</tr>"
        for item in ((summary or {}).get("rows") or [])[:40]
    )
    return cards, rows or f"<tr><td colspan='7'>{t(lang, '当前没有可统计的自选表现。', 'No measurable watchlist-after-add performance yet.')}</td></tr>"


def render_detail_rows(summary: dict[str, Any] | None, *, lang: str) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('trade_date') or '-'))}</td>"
        f"<td><a href='/insights/{html.escape(str(item.get('ticker') or ''), quote=True)}?lang={lang}'>{html.escape(str(item.get('ticker') or '-'))}</a><div class='muted'>{html.escape(str(item.get('name') or '-'))} · {html.escape(str(item.get('sector_group') or item.get('sector') or item.get('industry') or t(lang, '未分类', 'Unclassified')))}</div></td>"
        f"<td>{html.escape(str(item.get('regime_label') or '-'))}</td>"
        f"<td>{html.escape(str(item.get('signal_label') or '-'))}<div class='muted'>{html.escape(str(item.get('signal_strength') or '-'))}</div></td>"
        f"<td>{_fmt_optional_float(item.get('score'), digits=4)}</td>"
        f"<td>{_fmt_optional_float(item.get('return_3d'), suffix='%', digits=2)}</td>"
        f"<td>{_fmt_optional_float(item.get('return_5d'), suffix='%', digits=2)}</td>"
        f"<td>{_fmt_optional_float(item.get('return_10d'), suffix='%', digits=2)}</td>"
        "</tr>"
        for item in ((summary or {}).get("rows") or [])[:50]
    )
    return rows or f"<tr><td colspan='8'>{t(lang, '暂无可计算样本。', 'No measurable samples yet.')}</td></tr>"


def render_selected_run_identity(
    summary: dict[str, Any] | None, *, lang: str
) -> tuple[str, str]:
    run = (summary or {}).get("run") or {}
    title = html.escape(str(run.get("name") or "-"))
    subtitle = (
        f"{html.escape(str(run.get('market') or '-'))} · "
        f"{html.escape(str(run.get('universe') or '-'))} · "
        f"{t(lang, '最近交易日', 'Latest trade date')} {html.escape(str((summary or {}).get('latest_trade_date') or '-'))}"
    )
    return title, subtitle


def render_overview_cards(cards: list[dict[str, str]]) -> str:
    return (
        "<div class='metric-grid'>"
        + "".join(
            "<article class='metric-card'>"
            f"<div class='eyebrow'>{html.escape(str(card.get('eyebrow') or ''))}</div>"
            f"<div style='font-size:22px;font-weight:800;line-height:1.25;margin:6px 0 8px;'>{html.escape(str(card.get('title') or ''))}</div>"
            f"<div class='muted'>{html.escape(str(card.get('copy') or ''))}</div>"
            "</article>"
            for card in cards
        )
        + "</div>"
    )
