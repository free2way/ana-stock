from __future__ import annotations

from app.services.stock_selection.lightgbm_tactical import (
    LightGBMHistoryBias,
    LightGBMTacticalGuidance,
    lightgbm_history_bias,
)


_ACTION_LABELS = {
    "pullback": {"zh": "回踩布局", "en": "Pullback"},
    "breakout": {"zh": "突破确认", "en": "Breakout"},
    "watch": {"zh": "观察等待", "en": "Watch"},
}

_TAG_COPY = {
    "observe_first": {"zh": "先观察", "en": "Observe First"},
    "avoid_early_pullback": {"zh": "回踩不抢", "en": "Avoid Early Pullback"},
    "wait_for_pullback": {"zh": "回踩确认", "en": "Wait For Pullback"},
    "breakout_needs_proof": {"zh": "突破慎追", "en": "Breakout Needs Proof"},
    "follow_breakout": {"zh": "突破跟随", "en": "Follow Breakout"},
}


def _language(lang: str) -> str:
    return "zh" if lang == "zh" else "en"


def format_lightgbm_history_bias(
    payload_or_bias: dict | LightGBMHistoryBias,
    *,
    lang: str,
) -> str:
    bias = payload_or_bias if isinstance(payload_or_bias, LightGBMHistoryBias) else lightgbm_history_bias(payload_or_bias)
    language = _language(lang)
    if bias.action_key is None or bias.sample_count <= 0:
        return "历史样本观察中" if language == "zh" else "Historical samples are still observational"
    label = _ACTION_LABELS[bias.action_key][language]
    if language == "zh":
        return f"当前次日更偏 {label}，命中率 {bias.hit_rate:.1f}%。"
    return f"1D currently leans {label} with a {bias.hit_rate:.1f}% hit rate."


def format_lightgbm_tactical_guidance(
    guidance_or_payload: LightGBMTacticalGuidance | dict,
    *,
    lang: str,
) -> tuple[str, str]:
    guidance = (
        guidance_or_payload
        if isinstance(guidance_or_payload, LightGBMTacticalGuidance)
        else LightGBMTacticalGuidance(**guidance_or_payload)
    )
    language = _language(lang)
    tag = _TAG_COPY[guidance.tag_code][language]
    hit_rate = guidance.peer_hit_rate
    if guidance.note_code == "insufficient_samples":
        note = (
            "历史样本仍不足，先把这只票当作观察对象。"
            if language == "zh"
            else "Historical samples are still too thin, so treat this name as watch-only for now."
        )
    elif guidance.note_code == "market_leans_breakout":
        note = (
            f"当前整体更偏突破确认，回踩只做缩量确认；同类 1D 命中率 {hit_rate:.1f}%。"
            if language == "zh"
            else f"The tape currently leans breakout confirmation, so only buy pullbacks after a cleaner reset. Peer 1D hit rate {hit_rate:.1f}%."
        )
    elif guidance.note_code == "pullback_confirmation":
        note = (
            f"当前更适合等回踩企稳再处理；同类 1D 命中率 {hit_rate:.1f}%。"
            if language == "zh"
            else f"Lean toward pullback confirmation before acting. Peer 1D hit rate {hit_rate:.1f}%."
        )
    elif guidance.note_code == "market_leans_pullback":
        note = (
            f"当前整体更偏回踩布局，突破单需要等放量确认；同类 1D 命中率 {hit_rate:.1f}%。"
            if language == "zh"
            else f"The tape leans pullbacks, so only chase breakouts after real volume confirmation. Peer 1D hit rate {hit_rate:.1f}%."
        )
    elif guidance.note_code == "breakout_confirmation":
        note = (
            f"当前更适合等放量突破确认；同类 1D 命中率 {hit_rate:.1f}%。"
            if language == "zh"
            else f"Lean toward confirmed breakouts with volume. Peer 1D hit rate {hit_rate:.1f}%."
        )
    else:
        note = (
            f"当前这类信号更适合作为观察名单；同类 1D 命中率 {hit_rate:.1f}%。"
            if language == "zh"
            else f"This setup still behaves best as a watchlist candidate. Peer 1D hit rate {hit_rate:.1f}%."
        )
    return tag, note


def localize_lightgbm_tactical_results(items: list[dict], *, lang: str) -> None:
    """Add escaped-by-caller UI copy to rows that already contain structured facts."""
    for item in items:
        payload = item.get("lightgbm_tactical_guidance")
        if not isinstance(payload, dict):
            continue
        tactical_tag, tactical_note = format_lightgbm_tactical_guidance(payload, lang=lang)
        item["lightgbm_tactical_tag"] = tactical_tag
        item["lightgbm_tactical_note"] = tactical_note
        highlights = [
            str(value).strip()
            for value in (item.get("model_highlights") or [])
            if str(value).strip()
        ]
        if tactical_note and tactical_note not in highlights:
            item["model_highlights"] = [tactical_note, *highlights]
