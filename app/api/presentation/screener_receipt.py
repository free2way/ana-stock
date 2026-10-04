from __future__ import annotations

import html
from typing import Any

from app.api.presentation.i18n import t
from app.api.presentation.screener_components import (
    _lang_text,
    _preset_display_label,
    _preset_summary,
    _template_label,
)
from app.api.rendering import compact_text
from app.services.screener import MODEL_TEMPLATES


def render_screener_run_receipt(receipt: dict[str, Any], *, lang: str) -> str:
    normalized = receipt.get("normalized_params") or {}
    snapshot = receipt.get("snapshot") or {}
    payload = receipt.get("snapshot_payload") or {}
    source_kind = str(receipt.get("source_kind") or "page_result")
    source_label = {
        "multi": "多模型组合快照" if lang == "zh" else "Multi-model snapshot",
        "base_precompute": "基础预计算快照" if lang == "zh" else "Base precompute snapshot",
        "exact": "精确参数快照" if lang == "zh" else "Exact parameter snapshot",
        "page_result": "页面实时结果 / 待预计算" if lang == "zh" else "Page result / pending precompute",
    }.get(source_kind, source_kind)
    available_labels = [
        _template_label(key, MODEL_TEMPLATES[key]["label"], lang)
        for key in receipt.get("available_templates") or []
        if key in MODEL_TEMPLATES
    ]
    missing_labels = [
        _template_label(key, MODEL_TEMPLATES[key]["label"], lang)
        for key in receipt.get("missing_templates") or []
        if key in MODEL_TEMPLATES
    ]
    lineage_rows = [
        ("策略/模型" if lang == "zh" else "Strategy/Model", _preset_display_label(normalized, lang)),
        ("参数摘要" if lang == "zh" else "Parameter Summary", _preset_summary(normalized, lang)),
        ("结果数量" if lang == "zh" else "Result Count", str(receipt.get("result_count") or 0)),
        (
            "快照状态" if lang == "zh" else "Snapshot Status",
            "已就绪" if receipt.get("snapshot_ready") else t(lang, "待生成", "Pending"),
        ),
        ("来源类型" if lang == "zh" else "Source Type", source_label),
        ("快照 ID" if lang == "zh" else "Snapshot ID", str(snapshot.get("id") or "-")),
        ("来源 Job" if lang == "zh" else "Source Job", str(snapshot.get("source_job_id") or "-")),
        (
            "生成时间" if lang == "zh" else "Created At",
            compact_text(str(snapshot.get("created_at") or payload.get("updated_at") or "-"), 32),
        ),
        ("参数指纹" if lang == "zh" else "Param Fingerprint", str(receipt.get("param_digest") or "-")),
    ]

    candidate_stats = receipt.get("candidate_stats") or {}
    if candidate_stats:
        returned_count = candidate_stats.get("returned_count")
        persisted_count = candidate_stats.get("persisted_count")
        source_limit = candidate_stats.get("limit")
        stats_text = (
            f"筛选返回 {returned_count} / 快照落库 {persisted_count} / 上限 {source_limit}"
            if lang == "zh"
            else f"Returned {returned_count} / persisted {persisted_count} / limit {source_limit}"
        )
        lineage_rows.append(("候选口径" if lang == "zh" else "Candidate Scope", stats_text))

    empty_reason = str(receipt.get("empty_reason") or "").strip()
    if empty_reason:
        lineage_rows.append(("空结果原因" if lang == "zh" else "Empty Reason", empty_reason))

    regime_diagnostics = receipt.get("regime_diagnostics") or {}
    if regime_diagnostics:
        policy = regime_diagnostics.get("regime_policy")
        if not isinstance(policy, dict):
            policy = {}
        gate = str(policy.get("buy_gate") or regime_diagnostics.get("status") or "-")
        regime = str(policy.get("risk_regime") or "-")
        counts = (
            f"观察 {regime_diagnostics.get('observation_count', 0)} / 体制短名单 {regime_diagnostics.get('regime_shortlist_count', 0)} / 正式候选 0"
            if lang == "zh"
            else f"Observe {regime_diagnostics.get('observation_count', 0)} / regime shortlist {regime_diagnostics.get('regime_shortlist_count', 0)} / formal candidates 0"
        )
        lineage_rows.extend(
            [
                ("体制门" if lang == "zh" else "Regime Gate", f"{regime} / {gate}"),
                ("候选分层" if lang == "zh" else "Candidate Layers", counts),
                (
                    "结果语义" if lang == "zh" else "Result Semantics",
                    "研究观察，不构成买入授权"
                    if lang == "zh"
                    else "Research observation, not buy authorization",
                ),
            ]
        )

    if receipt.get("multi_model"):
        lineage_rows.extend(
            [
                ("参与模型" if lang == "zh" else "Available Models", " / ".join(available_labels) or "-"),
                (
                    "缺失模型" if lang == "zh" else "Missing Models",
                    " / ".join(missing_labels) or t(lang, "无", "None"),
                ),
            ]
        )

    rows_html = "".join(
        "<div class='receipt-row'>"
        f"<span>{html.escape(label)}</span>"
        f"<strong title='{html.escape(value)}'>{html.escape(compact_text(value, 72))}</strong>"
        "</div>"
        for label, value in lineage_rows
    )
    return (
        "<article class='card run-receipt-card'>"
        f"<div class='eyebrow'>{_lang_text(lang, 'run_receipt')}</div>"
        f"<p class='lead'>{t(lang, '这张收据用于回看：本次结果来自哪组参数、哪个快照、哪个后台 job。后续日报和历史命中评测可以继续沿这个指纹关联。', 'This receipt records which parameters, snapshot, and background job produced the current result set.')}</p>"
        f"<div class='receipt-grid'>{rows_html}</div>"
        "</article>"
    )
