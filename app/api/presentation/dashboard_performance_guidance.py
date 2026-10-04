from __future__ import annotations

import html
from typing import Any

from app.api.presentation.i18n import t
from app.services.dashboard_insights import _fmt_optional_float
from app.services.model_selection_guidance import ACTION_BUCKET_LABELS
from app.services.model_selection_usage import select_discouraged_recommendations


def build_guidance_fragments(
    *,
    guidance: dict[str, Any] | None,
    guidance_summary: dict[str, Any],
    validation_summary: dict[str, Any],
    market: str,
    lang: str,
) -> dict[str, str]:
    recommendations = list((guidance or {}).get("recommendations") or [])
    combos = list((guidance or {}).get("combos") or [])
    winners = list((guidance or {}).get("winner_attribution") or [])
    top_guidance = recommendations[0] if recommendations else {}
    top_combo = combos[0] if combos else {}
    top_guidance_title, top_guidance_copy = _top_model_copy(
        top_guidance, lang=lang
    )
    top_combo_title, top_combo_copy = _top_combo_copy(top_combo, lang=lang)
    playbook_title, playbook_copy = _playbook_copy(
        str(top_guidance.get("action_bucket") or top_combo.get("action_bucket") or ""),
        lang=lang,
    )
    discouraged = select_discouraged_recommendations(recommendations)
    discouraged_text = " / ".join(
        str(item.get("template_label") or item.get("template") or "-")
        for item in discouraged
    ) or t(lang, "暂无明确禁用模型", "No clear avoid-list yet")
    discouraged_copy = (
        "这些模型近期样本表现偏弱，今天不建议作为唯一入口。"
        if discouraged and lang == "zh"
        else "These models have weaker recent samples, so avoid using them as the only entry point today."
        if discouraged
        else "样本还不足以给出明确负面名单，继续用组合共振做约束。"
        if lang == "zh"
        else "There is not enough evidence for a hard avoid-list; keep using confluence as the guardrail."
    )
    model_href = str(
        guidance_summary.get("top_model_href")
        or f"/screeners?lang={lang}&market={market}"
    )
    combo_href = str(
        guidance_summary.get("top_combo_href")
        or f"/screeners?lang={lang}&market={market}"
    )
    snapshot_meta = guidance_summary.get("snapshot_meta") or {}
    winner_total = int((guidance or {}).get("winner_total") or 0)
    decision_cards = (
        "<div class='decision-grid'>"
        f"<article class='decision-card primary'><div class='eyebrow'>{t(lang, '今天先用', 'Use first today')}</div><h3>{top_guidance_title}</h3><p>{html.escape(top_guidance_copy)}</p><a class='pill' href='{html.escape(model_href, quote=True)}'>{t(lang, '打开模型筛选', 'Open model screen')}</a></article>"
        f"<article class='decision-card'><div class='eyebrow'>{t(lang, '组合优先级', 'Combo priority')}</div><h3>{top_combo_title}</h3><p>{html.escape(top_combo_copy)}</p><a class='pill' href='{html.escape(combo_href, quote=True)}'>{t(lang, '打开组合筛选', 'Open combo screen')}</a></article>"
        f"<article class='decision-card'><div class='eyebrow'>{t(lang, '打法偏向', 'Playbook bias')}</div><h3>{html.escape(playbook_title)}</h3><p>{html.escape(playbook_copy)}</p></article>"
        f"<article class='decision-card caution'><div class='eyebrow'>{t(lang, '暂不优先', 'Do not prioritize')}</div><h3>{html.escape(discouraged_text)}</h3><p>{html.escape(discouraged_copy)}</p></article>"
        "</div>"
    )
    snapshot_source = (
        t(lang, "快照来源：后台预计算", "Source: background snapshot")
        if str(snapshot_meta.get("source") or "") == "snapshot"
        else t(lang, "快照缺失：当前为实时回退", "Snapshot missing: live fallback in use")
    )
    cards = (
        "<div class='metric-grid'>"
        f"<article class='metric-card'><div class='eyebrow'>{t(lang, '当前优先模型', 'Priority model')}</div><div style='font-size:22px;font-weight:800;line-height:1.25;margin:6px 0 8px;'>{top_guidance_title}</div><div class='muted'>{html.escape(top_guidance_copy)}</div><div style='margin-top:12px;'><a class='pill' href='{html.escape(model_href, quote=True)}'>{t(lang, '用这套模型去筛股', 'Screen with this model')}</a></div></article>"
        f"<article class='metric-card'><div class='eyebrow'>{t(lang, '优先模型组合', 'Priority combo')}</div><div style='font-size:22px;font-weight:800;line-height:1.25;margin:6px 0 8px;'>{top_combo_title}</div><div class='muted'>{html.escape(top_combo_copy)}</div><div style='margin-top:12px;'><a class='pill' href='{html.escape(combo_href, quote=True)}'>{t(lang, '用这套组合去筛股', 'Screen with this combo')}</a></div></article>"
        f"<article class='metric-card'><div class='eyebrow'>{t(lang, '反向归因样本', 'Winner traceback')}</div><div class='metric'>{winner_total}</div><div class='muted'>{t(lang, '近期次日涨幅不低于 3% 的强势样本，用来检查哪些模型前一天提前命中。', 'Recent 1D movers above 3%, used to trace which models caught them one session earlier.')}</div><div class='muted' style='margin-top:8px;'>{html.escape(snapshot_source)} · {html.escape(str(snapshot_meta.get('snapshot_date') or snapshot_meta.get('generated_at') or '-'))}</div></article>"
        "</div>"
    )
    validation_cards, validation_rows = _render_validation(
        validation_summary, lang=lang
    )
    return {
        "decision_cards_html": decision_cards,
        "cards_html": cards,
        "rows_html": _render_guidance_rows(recommendations, lang=lang),
        "combo_rows_html": _render_combo_rows(combos, lang=lang),
        "winner_rows_html": _render_winner_rows(winners, lang=lang),
        "validation_cards_html": validation_cards,
        "validation_rows_html": validation_rows,
    }


def _bucket_label(value: str | None, *, lang: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or normalized == "unclassified":
        return "未归类" if lang == "zh" else "Unclassified"
    if normalized == "ALL":
        return "任意动作" if lang == "zh" else "Any Action"
    return ACTION_BUCKET_LABELS.get(normalized, {}).get(lang, normalized)


def _market_label(value: str | None, *, lang: str) -> str:
    normalized = str(value or "").upper()
    if normalized == "CN":
        return "A股" if lang == "zh" else "CN"
    if normalized == "US":
        return "美股" if lang == "zh" else "US"
    return normalized or "-"


def _top_model_copy(item: dict[str, Any], *, lang: str) -> tuple[str, str]:
    if not item:
        return (
            ("样本继续沉淀", "当前还没有足够样本给出明确偏好。")
            if lang == "zh"
            else ("Still collecting samples", "There is not enough sample depth for a clear preference yet.")
        )
    title = html.escape(str(item.get("template_label") or item.get("template") or "-"))
    if item.get("action_bucket"):
        title += f" · {html.escape(_bucket_label(str(item.get('action_bucket')), lang=lang))}"
    stats = item.get("stats_1d") or {}
    copy = (
        f"次日均值 {_fmt_optional_float(stats.get('avg_return'), suffix='%', digits=2)}，命中率 {_fmt_optional_float(stats.get('hit_rate'), suffix='%', digits=1)}，提前覆盖强票 {int(item.get('winner_capture_count') or 0)} 只。"
        if lang == "zh"
        else f"1D avg {_fmt_optional_float(stats.get('avg_return'), suffix='%', digits=2)}, hit rate {_fmt_optional_float(stats.get('hit_rate'), suffix='%', digits=1)}, captured {int(item.get('winner_capture_count') or 0)} strong movers."
    )
    return title, copy


def _top_combo_copy(item: dict[str, Any], *, lang: str) -> tuple[str, str]:
    if not item:
        return (
            ("组合样本继续沉淀", "还没有足够组合样本。")
            if lang == "zh"
            else ("Combo samples are still accumulating", "Not enough confluence samples yet.")
        )
    label = (item.get("label") or {}).get(lang) or (item.get("label") or {}).get("zh") or "-"
    stats = item.get("stats_1d") or {}
    copy = (
        f"次日均值 {_fmt_optional_float(stats.get('avg_return'), suffix='%', digits=2)}，命中率 {_fmt_optional_float(stats.get('hit_rate'), suffix='%', digits=1)}，强票覆盖率 {_fmt_optional_float(item.get('winner_capture_rate'), suffix='%', digits=1)}。"
        if lang == "zh"
        else f"1D avg {_fmt_optional_float(stats.get('avg_return'), suffix='%', digits=2)}, hit rate {_fmt_optional_float(stats.get('hit_rate'), suffix='%', digits=1)}, strong-mover coverage {_fmt_optional_float(item.get('winner_capture_rate'), suffix='%', digits=1)}."
    )
    return html.escape(str(label)), copy


def _playbook_copy(action: str, *, lang: str) -> tuple[str, str]:
    choices = {
        "buy_the_dip": (
            "当前更适合回踩低吸",
            "优先等支撑承接或回踩确认，避免在急拉后追高。",
            "Current bias: buy-the-dip",
            "Wait for support confirmation and avoid chasing extended moves.",
        ),
        "breakout_confirmation": (
            "当前更适合突破确认",
            "优先看放量突破与均线结构，突破失败或缩量时放弃。",
            "Current bias: breakout confirmation",
            "Prioritize volume-backed breakouts and MA structure; stand down on failed or thin breakouts.",
        ),
        "bullish_entry": (
            "当前更适合偏多入场",
            "可关注质量和动量同时满足的候选，但仍要用风控标签过滤。",
            "Current bias: bullish entry",
            "Focus on names where quality and momentum align, while still respecting risk tags.",
        ),
    }
    selected = choices.get(
        action,
        (
            "当前先做组合观察",
            "动作桶优势还不够明显，先用多模型共振缩小候选池。",
            "Current bias: observe confluence",
            "No action bucket has a decisive edge yet, so use confluence to narrow the list first.",
        ),
    )
    return selected[:2] if lang == "zh" else selected[2:]


def _render_guidance_rows(items: list[dict[str, Any]], *, lang: str) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(str(item.get('template_label') or item.get('template') or '-'))}<div class='muted'>{html.escape(_bucket_label(str(item.get('action_bucket') or ''), lang=lang))}</div></td>"
        f"<td>{int(item.get('sample_count') or 0)}</td>"
        f"<td>{_stat_cell(item.get('stats_1d'))}</td><td>{_stat_cell(item.get('stats_3d'))}</td>"
        f"<td>{int(item.get('winner_capture_count') or 0)}<div class='muted'>{_fmt_optional_float(item.get('winner_capture_rate'), suffix='%', digits=1)}</div></td>"
        f"<td>{_fmt_optional_float(item.get('score'), digits=1)}</td></tr>"
        for item in items[:6]
    )
    return rows or f"<tr><td colspan='6'>{t(lang, '暂无足够样本。', 'Not enough samples yet.')}</td></tr>"


def _stat_cell(stats: dict[str, Any] | None) -> str:
    stats = stats or {}
    return f"{_fmt_optional_float(stats.get('avg_return'), suffix='%', digits=2)}<div class='muted'>{_fmt_optional_float(stats.get('hit_rate'), suffix='%', digits=1)}</div>"


def _stat_inline(stats: dict[str, Any] | None) -> str:
    stats = stats or {}
    return (
        f"{_fmt_optional_float(stats.get('avg_return'), suffix='%', digits=2)} / "
        f"{_fmt_optional_float(stats.get('hit_rate'), suffix='%', digits=1)}"
    )


def _render_combo_rows(items: list[dict[str, Any]], *, lang: str) -> str:
    rows = "".join(
        "<tr>"
        f"<td><a href='{html.escape(str(item.get('screener_href') or '#'), quote=True)}'>{html.escape(str((item.get('label') or {}).get(lang) or (item.get('label') or {}).get('zh') or '-'))}</a><div class='muted'>{html.escape(' / '.join(str(value) for value in (item.get('available_templates') or [])[:4]))}</div></td>"
        f"<td>{html.escape(_bucket_label(str(item.get('action_bucket') or 'ALL'), lang=lang))}<div class='muted'>{t(lang, '至少', 'Min')} {int(item.get('min_hits') or 2)} {t(lang, '模型命中', 'hits')}</div></td>"
        f"<td>{_stat_cell(item.get('stats_1d'))}</td><td>{_stat_cell(item.get('stats_3d'))}</td><td>{_stat_cell(item.get('stats_5d'))}</td><td>{_stat_cell(item.get('stats_10d'))}</td>"
        f"<td>{int(item.get('winner_capture_count') or 0)}<div class='muted'>{_fmt_optional_float(item.get('winner_capture_rate'), suffix='%', digits=1)}</div></td></tr>"
        for item in items[:6]
    )
    return rows or f"<tr><td colspan='7'>{t(lang, '暂无组合评测样本。', 'No combo evaluation samples yet.')}</td></tr>"


def _render_winner_rows(items: list[dict[str, Any]], *, lang: str) -> str:
    rows = []
    for item in items[:10]:
        hit_labels = " / ".join(
            str(hit.get("template_label") or hit.get("template") or "")
            for hit in (item.get("hits") or [])[:3]
            if str(hit.get("template_label") or hit.get("template") or "").strip()
        ) or t(lang, "未提前命中", "No prior hit")
        rows.append(
            "<tr>"
            f"<td><a href='/insights/{html.escape(str(item.get('ticker') or ''), quote=True)}?lang={lang}'>{html.escape(str(item.get('ticker') or '-'))}</a><div class='muted'>{html.escape(str(item.get('name') or '-'))}</div></td>"
            f"<td>{_market_label(str(item.get('market') or ''), lang=lang)}</td>"
            f"<td>{html.escape(str(item.get('signal_date') or '-'))}<div class='muted'>{html.escape(str(item.get('winner_date') or '-'))}</div></td>"
            f"<td>{_fmt_optional_float(item.get('return_1d'), suffix='%', digits=2)}</td>"
            f"<td>{int(item.get('hit_count') or 0)}<div class='muted'>{html.escape(hit_labels)}</div></td></tr>"
        )
    return "".join(rows) or f"<tr><td colspan='5'>{t(lang, '暂无可归因的大涨样本。', 'No attributable winner samples yet.')}</td></tr>"


def _render_validation(summary: dict[str, Any], *, lang: str) -> tuple[str, str]:
    cards = []
    rows = []
    for row in summary.get("rows") or []:
        windows = row.get("windows") or {}
        cards.append(
            "<article class='metric-card'>"
            f"<div class='eyebrow'>{html.escape(str(row.get('label') or '-'))}</div>"
            f"<div style='font-size:20px;font-weight:800;line-height:1.3;margin:4px 0 8px;'>{html.escape(str(row.get('title') or '-'))}</div>"
            f"<div class='muted'>{html.escape(str(row.get('note') or ''))}</div>"
            f"<div class='muted' style='margin-top:8px;'>{t(lang, '5日', '5D')} {_stat_inline(windows.get(5))} · {t(lang, '10日', '10D')} {_stat_inline(windows.get(10))}</div>"
            f"<div style='margin-top:12px;'><a class='pill' href='{html.escape(str(row.get('href') or '#'), quote=True)}'>{t(lang, '打开明细', 'Open detail')}</a></div></article>"
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(row.get('label') or '-'))}<div class='muted'>{html.escape(str(row.get('title') or '-'))}</div></td>"
            f"<td>{int(row.get('count') or 0)}<div class='muted'>{html.escape(str(row.get('note') or ''))}</div></td>"
            + "".join(f"<td>{_stat_cell(windows.get(window))}</td>" for window in (1, 3, 5, 10))
            + "</tr>"
        )
    empty = t(
        lang,
        "当前还没有足够样本形成推荐历史验证。",
        "Not enough samples yet for recommendation validation.",
    )
    return (
        "".join(cards) or f"<div class='muted'>{empty}</div>",
        "".join(rows) or f"<tr><td colspan='6'>{empty}</td></tr>",
    )
