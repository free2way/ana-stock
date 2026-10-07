from __future__ import annotations

import html

from app.api.presentation.i18n import t
from app.api.presentation.screener_components import (
    ACTION_OPTIONS,
    CONFLUENCE_ACTION_OPTIONS,
    LANG_OPTIONS,
    MODEL_SIGNAL_OPTIONS,
    PATTERN_EVALUATION_TEMPLATES,
    SORT_BY_OPTIONS,
    SORT_ORDER_OPTIONS,
    _action_badge,
    _banner_html,
    _build_screen_query,
    _change_chip,
    _confluence_bucket_label,
    _detail_panel,
    _fmt_number,
    _hidden_fields_html,
    _kronos_compact_chip,
    _lang_text,
    _lightgbm_evaluation_card,
    _lightgbm_execution_bias_bar,
    _market_section_label,
    _model_cell,
    _next_tesla_evaluation_card,
    _number_badge,
    _pattern_as_of_chip,
    _pattern_hits_inline,
    _preset_display_label,
    _preset_hidden_fields_html,
    _preset_summary,
    _price_badge,
    _pseudo_strong_signal_html,
    _row_technical_rating_html,
    _snapshot_pending_message,
    _sync_status_badge,
    _technical_momentum_evaluation_card,
    _technical_pattern_evaluation_card,
    _template_groups_for_display,
    _template_interpretation_card,
    _template_label,
    _template_overview_brief_html,
    _trend_badge,
    _watchlist_action_cell,
)
from app.api.rendering import compact_text as _compact_text
from app.api.presentation.screener_pages import render_screener_main_page
from app.api.presentation.styles_screener import TEMPLATE_CARD_HREF_STYLE
from app.services.screener import MODEL_TEMPLATES
from app.services.screener_snapshots import build_base_precompute_params
from app.services.workspace_nav import render_workspace_nav_html


def render_main_screener_view(
    action_filter,
    confluence_action_filter,
    current_params,
    detail_rows_enabled,
    exclude_bottom_market_cap_pct,
    exclude_execution_tag_filter,
    execution_tag_filter,
    guidance_summary,
    lang,
    market,
    max_debt_to_assets,
    message,
    min_dividend_yield,
    min_listing_days,
    min_model_signal_strength,
    min_multi_model_hits,
    min_net_profit_yoy,
    min_revenue_yoy,
    min_roe_avg_3y,
    min_snapshot_hits,
    min_trend_score,
    min_volume_ratio,
    model_signal_filter,
    model_template,
    multi_screen_meta,
    multi_templates_active,
    pe_max,
    pe_min,
    quality_profile_status_html,
    recent_snapshot_runs,
    regression_discipline_html,
    result_limit,
    results,
    results_truncated,
    risk_examples,
    risk_top_tags,
    run_receipt_html,
    saved_presets,
    should_execute,
    show_evaluation,
    snapshot_ready,
    sort_by,
    sort_order,
    tagged_names,
    total_results,
    universe,
    visible_results,
    watchlist_map,
) -> str:
    """Compose the main screener view from controller-provided state.

    Component helpers and the page template live in the presentation package;
    the FastAPI route only supplies controller-produced state.
    """
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

    def _template_card_href(template_key: str, config: dict) -> str:
        """Start a clean full-market discovery run from a model card.

        Model cards used to inherit every parameter from the previous screen.
        That made a selected A-share pattern look empty when an old watchlist,
        confluence profile, or advanced rule remained in the URL.
        """
        card_market = str(config.get("market") or market or "ALL")
        card_params = build_base_precompute_params(
            model_template=template_key,
            universe="full_market",
            market=card_market,
        )
        card_params.update({"lang": lang, "run": 1})
        return _build_screen_query(card_params)

    template_cards_html = "".join(
        (
            "<div style='margin-bottom:18px;'>"
            f"<div style='display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:10px;'>"
            f"<div style='font-size:15px;font-weight:900;color:var(--ink);'>{html.escape(meta['title'])}</div>"
            f"<div class='muted' style='font-size:12px;'>{html.escape(meta['hint'])}</div>"
            "</div>"
            "<div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;'>"
            + "".join(
                f"<a class='template-card{' active' if value == model_template else ''}' href='{_template_card_href(value, config)}'>"
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
        f"<div class='eyebrow'>{t(lang, '模型评测', 'Model Evaluation')}</div>"
        f"<div class='muted'>{t(lang, '为保证全市场筛选秒开，默认不在本页加载完整评测。需要时再展开当前模板评测，或打开模型评测总览。', 'To keep full-market screening fast, the full evaluation panel is lazy-loaded. Open this template evaluation only when needed, or use the model overview.')}</div>"
        "<div style='display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;'>"
        f"<a class='default-chip' href='{evaluation_href}'>{t(lang, '展开本模型评测', 'Open this template evaluation')}</a>"
        f"<a class='default-chip' href='{evaluation_overview_href}'>{t(lang, '模型评测总览', 'Model Evaluation Overview')}</a>"
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
            f"<span class='default-chip'>{count} {t(lang, '只', 'names')} · {hit} {t(lang, '模型共振', 'model hits')}</span>"
            for hit, count in sorted(confluence_strength_counts.items(), reverse=True)
        )
        confluence_strength_html = (
            "<article class='card' style='background:#f8fbff;border-color:#d7e7f8;'>"
            f"<div class='eyebrow'>{t(lang, '共振强度', 'Confluence Strength')}</div>"
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
            matched_template_text = " / ".join(matched_templates[:3]) or (t(lang, "未识别模型", "No mapped templates"))
            bucket_text = " / ".join(
                _confluence_bucket_label(bucket, lang) for bucket in (item.get("matched_action_buckets") or [])[:3]
            ) or (t(lang, "未归类", "Unbucketed"))
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
                f"<div class='muted'>{t(lang, '模型命中', 'model hits')}</div>"
                f"<div style='margin-top:8px;font-size:15px;font-weight:700;color:#0f172a;'>{alignment_hits}</div>"
                f"<div class='muted'>{t(lang, '动作一致', 'aligned')}</div>"
                "</div></div>"
                "</div>"
            )
        confluence_leaderboard_html = (
            "<article class='card' style='background:#f8fbff;border-color:#d7e7f8;'>"
            f"<div class='eyebrow'>{t(lang, '共振排行榜', 'Confluence Leaderboard')}</div>"
            f"<div class='muted' style='margin-bottom:12px;'>{t(lang, '优先展示同时被更多模型命中、且动作更一致的股票。', 'Prioritizes names with more model overlap and tighter action alignment.')}</div>"
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
            f"<div class='eyebrow'>{t(lang, '按动作桶看结果', 'Results by Confluence Bucket')}</div>"
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
            f"<button type='submit' style='width:auto;min-width:0;padding:10px 12px;'>{t(lang, '保存', 'Save')}</button>"
            "</form>"
            "</div>"
        )
        for preset in quick_confluence_presets
    )
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
        f"{t(lang, '样本来源', 'Source')}："
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
        f"<div class='eyebrow'>{t(lang, '今日模型导航', 'Today Model Guide')}</div>"
        f"<h2 style='margin:0 0 6px;'>{t(lang, '先跑最有效的，再补充共振', 'Start with the best edge, then add confluence')}</h2>"
        f"<p class='lead'>{html.escape(str(guidance_source_text))}</p>"
        "<div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px;margin-top:14px;'>"
        "<article class='detail-card' style='background:#ffffff;'>"
        f"<div class='detail-label'>{t(lang, '优先模型', 'Priority Model')}</div>"
        f"<div style='font-size:22px;font-weight:900;color:#0f172a;margin-top:6px;'>{html.escape(str(guidance_summary.get('top_model_title') or '-'))}</div>"
        f"<div class='muted' style='margin-top:8px;'>{html.escape(str(guidance_summary.get('top_model_summary') or '-'))}</div>"
        f"<div class='muted' style='margin-top:8px;'>{html.escape(guidance_top_model_metrics)}</div>"
        f"<div style='display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;'><a class='detail-link' href='{top_model_href}'>{t(lang, '按这个模型筛', 'Run this model')}</a><a class='detail-link' href='{evaluation_overview_href}'>{t(lang, '看评测', 'View evaluation')}</a></div>"
        "</article>"
        "<article class='detail-card' style='background:#ffffff;'>"
        f"<div class='detail-label'>{t(lang, '优先组合', 'Priority Confluence')}</div>"
        f"<div style='font-size:22px;font-weight:900;color:#0f172a;margin-top:6px;'>{html.escape(str(guidance_summary.get('top_combo_title') or '-'))}</div>"
        f"<div class='muted' style='margin-top:8px;'>{html.escape(str(guidance_summary.get('top_combo_summary') or '-'))}</div>"
        f"<div class='muted' style='margin-top:8px;'>{html.escape(guidance_top_combo_metrics)}</div>"
        f"<div style='display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;'><a class='detail-link' href='{top_combo_href}'>{t(lang, '直接跑组合', 'Run confluence')}</a><a class='detail-link' href='/dashboard/model-performance/winner-traceback?lang={lang}&market={market if market in {'CN','US','ALL'} else 'CN'}'>{t(lang, '看强票归因', 'Winner traceback')}</a></div>"
        "</article>"
        "<article class='detail-card' style='background:#ffffff;'>"
        f"<div class='detail-label'>{t(lang, '今日使用建议', 'Today Workflow')}</div>"
        f"<div class='muted' style='margin-top:8px;'>{t(lang, '1. 先跑优先模型看当日基准候选。 2. 再跑优先组合做降噪。 3. 最后只处理 READY 且接近买点的票。', '1. Start with the priority model. 2. Use the priority confluence to reduce noise. 3. Only act on READY names near their planned entry zones.')}</div>"
        f"<div style='display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;'><a class='detail-link' href='{top_model_href}'>{t(lang, '先跑基准模型', 'Run baseline')}</a><a class='detail-link' href='{top_combo_href}'>{t(lang, '再跑共振', 'Then confluence')}</a></div>"
        "</article>"
        "</div>"
        "</section>"
    )
    strategy_template_cards_html = "".join(
        (
            "<article class='strategy-template-card'>"
            "<div class='template-top'>"
            f"<span class='template-mode'>{t(lang, '策略模板', 'Strategy')}</span>"
            f"<span class='template-market'>{preset['params'].get('market', 'CN')}</span>"
            "</div>"
            f"<div class='strategy-template-title'>{preset['label'][lang]}</div>"
            f"<div class='template-desc'>{preset.get('description', {}).get(lang, '')}</div>"
            "<div class='strategy-meta-row'>"
            f"<span class='default-chip'>{preset.get('best_for', {}).get(lang, '')}</span>"
            f"<span class='default-chip'>{t(lang, '至少命中', 'Min hits')} {preset['params'].get('min_multi_model_hits', 2)}</span>"
            f"<span class='default-chip'>{_confluence_bucket_label(str(preset['params'].get('confluence_action_filter') or 'ALL'), lang)}</span>"
            "</div>"
            "<div class='strategy-template-actions'>"
            f"<a class='detail-link' href='{_build_screen_query(preset['params'])}'>{t(lang, '运行策略', 'Run Strategy')}</a>"
            f"<a class='detail-link' href='{evaluation_overview_href}'>{t(lang, '查看评测', 'View Evaluation')}</a>"
            f"<form method='post' action='/screeners/save' style='margin:0;display:flex;align-items:center;'>"
            f"<input type='hidden' name='preset_name' value='{html.escape(preset['save_name'][lang])}' />"
            f"{_preset_hidden_fields_html(preset['params'])}"
            f"<button type='submit' style='width:auto;min-width:0;padding:10px 12px;'>{t(lang, '保存为我的策略', 'Save Strategy')}</button>"
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
        else:
            rating_html = _row_technical_rating_html(item, lang)
            if rating_html:
                row_chunks.append(
                    "<tr class='detail-row'>"
                    f"<td colspan='18'>{rating_html}</td>"
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
            f"<div class='mobile-result-chip-row'>{_number_badge(item.get('model_hit_count'), suffix=(t(lang, '模', ' hits')), higher_is_good=True)}{_number_badge(item.get('confluence_alignment_count'), suffix=(t(lang, '齐', ' aligned')), higher_is_good=True)}</div>"
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
            + (
                f"<div class='mobile-result-meta'>{_pattern_as_of_chip(item, lang)}</div>"
                if item.get("pattern_as_of_stale")
                else ""
            )
            + f"<div class='mobile-result-actions'><a class='detail-link' href='/insights/{item['ticker']}?lang={lang}'>{_lang_text(lang, 'open_insight')}</a>{_watchlist_action_cell(item, watchlist_map, current_params, lang)}</div>"
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
            f"<td>{t(lang, '点击运行后查看', 'Run to view')}</td>"
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
    market_scope_switch_html = "".join(
        f"<a class='market-scope-option{' active' if str(market or 'ALL').upper() == code else ''}' href='{_build_screen_query({**current_params, 'market': code})}'>"
        f"{label}</a>"
        for code, label in (
            ("CN", "A 股" if lang == "zh" else "A-Shares"),
            ("US", "美股" if lang == "zh" else "U.S. Stocks"),
            ("ALL", "全市场" if lang == "zh" else "All Markets"),
        )
    )
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
                <div class="eyebrow">{t(lang, '筛选总览', 'Screener Overview')}</div>
                <div style="font-size:28px;font-weight:900;letter-spacing:-0.03em;">{total_results if should_execute else active_model_count}</div>
                <div class="muted">{market_scope_label} · {universe_scope_label}</div>
              </div>
              <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;flex:1;min-width:min(100%,760px);">
                <article style="border:1px solid var(--line);border-radius:16px;padding:14px 15px;background:rgba(15,24,35,0.58);">
                  <div class="eyebrow">{t(lang, '当前模式', 'Current Mode')}</div>
                  <div style="margin-top:6px;font-size:22px;font-weight:900;">{active_model_count}</div>
                  <div class="muted">{(f'{active_model_count} 个模型参与 · 已运行' if should_execute else f'{active_model_count} 个模型参与 · 待运行') if lang == 'zh' else (f'{active_model_count} models active · Executed' if should_execute else f'{active_model_count} models active · Ready')}</div>
                </article>
                <article style="border:1px solid var(--line);border-radius:16px;padding:14px 15px;background:rgba(15,24,35,0.58);">
                  <div class="eyebrow">{t(lang, '命中结果', 'Matched Results')}</div>
                  <div style="margin-top:6px;font-size:22px;font-weight:900;">{total_results}</div>
                  <div class="muted">{(f'页面显示前 {len(visible_results)} 只 · 已截断至导出上限 {result_limit} 只') if (results_truncated and lang == 'zh') else (f'Top {len(visible_results)} shown · truncated at the {result_limit}-row export cap') if results_truncated else (f'页面显示前 {len(visible_results)} 只' if lang == 'zh' else f'Top {len(visible_results)} shown first')}</div>
                </article>
                <article style="border:1px solid var(--line);border-radius:16px;padding:14px 15px;background:rgba(15,24,35,0.58);">
                  <div class="eyebrow">{t(lang, '风险标记', 'Risk Tags')}</div>
                  <div style="margin-top:6px;font-size:22px;font-weight:900;">{tagged_names}</div>
                  <div class="muted">{t(lang, '带执行提醒的候选股', 'Candidates carrying execution warnings')}</div>
                </article>
                <article style="border:1px solid var(--line);border-radius:16px;padding:14px 15px;background:rgba(15,24,35,0.58);">
                  <div class="eyebrow">{t(lang, '自选重叠', 'Watchlist Overlap')}</div>
                  <div style="margin-top:6px;font-size:22px;font-weight:900;">{watchlist_overlap_count}</div>
                  <div class="muted">{('缺前置快照 ' + str(pending_snapshot_count)) if (should_execute and not snapshot_ready and pending_snapshot_count > 0 and lang == 'zh') else ('Pending snapshots ' + str(pending_snapshot_count)) if (should_execute and not snapshot_ready and pending_snapshot_count > 0) else (t(lang, '已在自选里的候选股', 'Candidates already in watchlist'))}</div>
                </article>
              </div>
            </div>
          </section>
        """
    )
    if should_execute and results_truncated:
        visible_note = (
            (
                f"当前共命中 {total_results} 只，已达到导出行数上限 {result_limit} 只"
                f"（页面显示前 {len(visible_results)} 只）；导出 CSV 只包含上限内的 {result_limit} 只，"
                "请用收窄条件或提高精度来缩小结果集。"
            )
            if lang == "zh"
            else (
                f"{total_results} names matched, hitting the {result_limit}-row export cap "
                f"(page shows the top {len(visible_results)}). CSV export contains only those "
                f"{result_limit} rows; narrow the filters to reach the rest."
            )
        )
    elif should_execute:
        visible_note = (
            f"当前共命中 {total_results} 只，页面先展示前 {len(visible_results)} 只；导出 CSV 包含全部 {total_results} 只。"
            if lang == "zh"
            else f"{total_results} names matched; the page shows the top {len(visible_results)} first, while CSV export includes all {total_results}."
        )
    else:
        visible_note = (
            "当前页面默认只展示模板和规则，避免首屏直接跑重计算。选择模板后点击“运行筛选”即可。"
            if lang == "zh"
            else "The first load only shows templates and rules to keep the page fast. Click Run Screen when you're ready."
        )
    if should_execute and not snapshot_ready:
        visible_note = _snapshot_pending_message(lang)
    if should_execute and snapshot_ready and total_results > 0 and not detail_rows_enabled:
        visible_note += (
            f" <a href='{detail_rows_href}' style='color:var(--accent);font-weight:800;text-decoration:none;'>{t(lang, '需要逐行解释时再展开行内详情', 'Open row details only when needed')}</a>."
        )
    lang_switch_html = "".join(
        f"<a href='{_build_screen_query({**current_params, 'lang': code})}' style='padding:8px 12px;border-radius:999px;border:1px solid var(--line);background:{'#eef8f5' if lang == code else '#fff'};text-decoration:none;color:var(--ink);font-weight:700;'>{label}</a>"
        for code, label in LANG_OPTIONS
    )

    return render_screener_main_page(
        fragments=[
            lang,
            _lang_text(lang, 'title'),
            TEMPLATE_CARD_HREF_STYLE,
            t(lang, '策略工作台', 'Strategy Workbench'),
            t(lang, '模型、模板、组合和评测放在同一条选股路径里。', 'Models, templates, confluence, and evaluation in one screening path.'),
            render_workspace_nav_html(lang=lang, active_key='screeners'),
            _lang_text(lang, 'back_to_dashboard'),
            _lang_text(lang, 'open_watchlist'),
            t(lang, '高级研究', 'Advanced research'),
            lang,
            _lang_text(lang, 'open_focus_pool'),
            lang,
            _lang_text(lang, 'open_market_snapshot'),
            lang,
            market if market in {'CN','US','ALL'} else 'ALL',
            t(lang, '模型评测总览', 'Model Evaluation Overview'),
            lang,
            t(lang, 'Kronos 二次验证池', 'Kronos Validation Pool'),
            _lang_text(lang, 'sync_cn_fundamentals'),
            _lang_text(lang, 'language'),
            lang_switch_html,
            banner_html,
            t(lang, '第 2 步 · 发现候选', 'Step 2 · Find candidates'),
            t(lang, '先选择市场和打法，再验证候选', 'Choose market and playbook, then validate candidates'),
            t(lang, '默认先用预计算模板查看已通过交易治理的候选；模型、共振和评测用于研究优先级，不替代入场触发与失效条件。需要做实验时再展开高级研究。', 'Start with precomputed candidates that passed trade governance. Models, confluence, and evaluation rank research priority; they do not replace triggers and invalidation. Open advanced research only when needed.'),
            'Active template' if lang == 'en' else '当前模板',
            _template_label(model_template, active_template['label'], lang),
            active_defaults_html,
            screen_overview_html,
            t(lang, '第 1 步 · 选择市场', 'Step 1 · Choose market'),
            t(lang, '先只看一个市场。A 股与美股交易日、候选池和风险口径不同，不建议在同一次筛选中混用。', 'Start with one market. A-shares and U.S. stocks have different sessions, candidate pools, and risk context, so do not mix them in one screen.'),
            market_scope_switch_html,
            t(lang, '高级研究与模型证据', 'Advanced research and model evidence'),
            run_receipt_html,
            template_read_html,
            template_overview_brief_html,
            lightgbm_bias_bar_html,
            template_evaluation_html,
            multi_template_summary_html,
            confluence_strength_html,
            confluence_leaderboard_html,
            confluence_bucket_groups_html,
            quality_profile_status_html,
            t(lang, '第 2 步 · 选择打法', 'Step 2 · Choose a playbook'),
            t(lang, '先选一种可理解的机会形态', 'Start with one understandable setup'),
            t(lang, '趋势延续、回踩确认和防守观察对应不同市场环境。先选一个模板，不必一开始叠加多个模型。', 'Trend continuation, pullback confirmation, and defensive observation fit different market conditions. Choose one template before combining models.'),
            template_cards_html,
            lang,
            html.escape(str(market), quote=True),
            t(lang, '限定候选范围', 'Set candidate scope'),
            t(lang, '全市场用于发现机会；自选用于复核已有想法。', 'Use full market to discover; use watchlist to review existing ideas.'),
            universe_option_html,
            t(lang, '只看可行动的结果', 'Keep actionable results'),
            t(lang, '默认保留全部，先用结果里的交易条件判断是否进入自选。', 'Keep all by default, then use each result’s trade gates before adding it to watchlist.'),
            action_option_html,
            t(lang, '运行并验证', 'Run and validate'),
            t(lang, '先查看触发条件、失效条件和风险，再加入自选。', 'Review triggers, invalidation, and risk before adding to watchlist.'),
            t(lang, '运行筛选', 'Run screener'),
            t(lang, '高级筛选：多模型、阈值与基本面条件', 'Advanced filters: models, thresholds, fundamentals'),
            t(lang, '只有当你已明确要研究什么时，再调整这些参数；日常选股优先使用上方模板和默认规则。', 'Adjust these only when you have a specific research question. For daily selection, prefer the template and defaults above.'),
            quick_confluence_presets_html,
            _lang_text(lang, 'model_template'),
            template_option_html,
            t(lang, '共振最少命中模型数', 'Minimum multi-model hits'),
            max(2, len(MODEL_TEMPLATES)),
            min_multi_model_hits,
            t(lang, '共振动作桶', 'Confluence action bucket'),
            confluence_option_html,
            t(lang, '结果排序', 'Result sort'),
            sort_by_option_html,
            t(lang, '排序方向', 'Sort order'),
            sort_order_option_html,
            _lang_text(lang, 'min_trend_score'),
            min_trend_score,
            _lang_text(lang, 'min_volume_strength'),
            min_volume_ratio,
            t(lang, '如果想做多模型共振，勾选两个或以上模板。系统会按同一只股票被多少个模型同时命中来排序。', 'For confluence screening, tick two or more templates. The screener will rank names by how many models hit the same ticker.'),
            multi_template_picker_html,
            _lang_text(lang, 'cn_rules'),
            t(lang, '这些参数保留给需要做精细筛选的时候。', 'Use these only when you need a more precise filter pass.'),
            _lang_text(lang, 'min_listing_days'),
            min_listing_days,
            _lang_text(lang, 'pe_range'),
            pe_min,
            pe_max,
            _lang_text(lang, 'min_roe_3y'),
            min_roe_avg_3y,
            _lang_text(lang, 'min_profit_yoy'),
            min_net_profit_yoy,
            _lang_text(lang, 'min_revenue_yoy'),
            min_revenue_yoy,
            _lang_text(lang, 'max_debt'),
            max_debt_to_assets,
            _lang_text(lang, 'min_dividend'),
            min_dividend_yield,
            _lang_text(lang, 'exclude_bottom_cap'),
            exclude_bottom_market_cap_pct,
            _lang_text(lang, 'recent_snapshot_runs'),
            recent_snapshot_runs,
            _lang_text(lang, 'min_snapshot_hits'),
            min_snapshot_hits,
            _lang_text(lang, 'model_signal_filter'),
            signal_option_html,
            _lang_text(lang, 'min_model_signal_strength'),
            min_model_signal_strength,
            _lang_text(lang, 'execution_tag_filter'),
            execution_tag_filter if str(execution_tag_filter).upper() != 'ALL' else '',
            _lang_text(lang, 'exclude_execution_tag_filter'),
            exclude_execution_tag_filter if str(exclude_execution_tag_filter).upper() != 'ALL' else '',
            'Quick Tags' if lang == 'en' else '快捷标签',
            'exclude gap-risk' if lang == 'en' else '排除 gap-risk',
            'Clear Tags' if lang == 'en' else '清空标签',
            t(lang, '应用高级条件并运行', 'Apply advanced filters and run'),
            regression_discipline_html,
            _lang_text(lang, 'risk_overview'),
            _lang_text(lang, 'tagged_names'),
            tagged_names,
            _lang_text(lang, 'risk_examples'),
            _lang_text(lang, 'common_risks'),
            risk_top_tags_html,
            _lang_text(lang, 'risk_examples'),
            risk_examples_html,
            t(lang, '运行结果后：加入自选、保存或同步', 'After reviewing results: add, save, or sync'),
            'Strategy' if lang == 'en' else '策略',
            _lang_text(lang, 'save_strategy'),
            _lang_text(lang, 'strategy_name'),
            _lang_text(lang, 'strategy_name'),
            hidden_fields,
            _lang_text(lang, 'save_as_strategy'),
            'Watchlist' if lang == 'en' else '自选股',
            _lang_text(lang, 'add_current_results'),
            hidden_fields,
            _lang_text(lang, 'only_add_top_n'),
            _lang_text(lang, 'auto_enable_sync'),
            bulk_add_disabled,
            bulk_add_label if snapshot_ready else _lang_text(lang, 'snapshot_pending_short'),
            'Focus' if lang == 'en' else '盯盘池',
            _lang_text(lang, 'add_current_results_to_focus'),
            hidden_fields,
            _lang_text(lang, 'focus_top_n'),
            bulk_add_disabled,
            _lang_text(lang, 'add_current_results_to_focus') if snapshot_ready else _lang_text(lang, 'snapshot_pending_short'),
            'Sync' if lang == 'en' else '同步',
            _lang_text(lang, 'sync_top_n_now'),
            hidden_fields,
            _lang_text(lang, 'sync_top_n_help'),
            'disabled' if not actions_available else '',
            _lang_text(lang, 'sync_top_n_now') if snapshot_ready else _lang_text(lang, 'snapshot_pending_short'),
            _lang_text(lang, 'results'),
            total_results,
            _lang_text(lang, 'stocks_matched'),
            hidden_fields,
            'disabled' if not actions_available else '',
            _lang_text(lang, 'export_csv') if snapshot_ready else _lang_text(lang, 'snapshot_pending_short'),
            visible_note,
            header_link(_lang_text(lang, 'ticker'), 'ticker'),
            _lang_text(lang, 'name'),
            _lang_text(lang, 'action'),
            header_link(_lang_text(lang, 'trend'), 'trend_score'),
            _lang_text(lang, 'market'),
            header_link(_lang_text(lang, 'close'), 'latest_close'),
            header_link(_lang_text(lang, 'model'), 'model_signal_strength'),
            header_link(_lang_text(lang, 'watchlist'), 'watchlist_state'),
            header_link('Hits', 'snapshot_hits'),
            header_link('5D %', 'momentum_5'),
            header_link('20D %', 'momentum_20'),
            header_link('Volume', 'volume_ratio'),
            header_link('PE', 'pe_ttm'),
            header_link('ROE 3Y', 'roe_avg_3y'),
            header_link('Profit YoY', 'net_profit_yoy'),
            header_link('Dividend %', 'dividend_yield'),
            header_link('Breakout %', 'distance_to_breakout_pct'),
            _lang_text(lang, 'insight'),
            rows,
            mobile_result_cards,
            _lang_text(lang, 'drag_hint'),
            guidance_bridge_html,
            strategy_workbench_html,
            t(lang, '策略模板', 'Strategy Templates'),
            t(lang, '一键运行常用组合', 'Run the common playbooks in one click'),
            t(lang, '这些模板会复用完整筛选参数，适合每天盘后先跑一遍，再把结果加入自选或今日盯盘池。', 'These templates reuse the full parameter set, making them a fast after-hours first pass before watchlist or focus-pool review.'),
            t(lang, '管理我的策略', 'Manage My Strategies'),
            strategy_template_cards_html,
            _lang_text(lang, 'saved_strategies'),
            t(lang, '我的策略管理', 'My Strategy Manager'),
            t(lang, '这里保存的是你自己的筛选模板。可以重新运行、改名、删除，也可以跳到模型评测先看近期表现。', 'Saved strategies keep your own screening templates. You can rerun, rename, delete, or jump into model evaluation before using them.'),
            _lang_text(lang, 'name'),
            _lang_text(lang, 'model_template'),
            _lang_text(lang, 'summary'),
            _lang_text(lang, 'hits'),
            _lang_text(lang, 'run_strategy'),
            _lang_text(lang, 'rename'),
            _lang_text(lang, 'delete'),
            preset_rows,
            mobile_preset_cards,
        ],
    )
