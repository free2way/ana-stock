from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Final

from app.services.template_evaluation import normalize_lightgbm_action, normalize_lightgbm_prediction_action


LIGHTGBM_ACTIONS: Final[frozenset[str]] = frozenset({"pullback", "breakout", "watch"})
_BIAS_PRIORITY: Final[dict[str, int]] = {"pullback": 0, "breakout": 1, "watch": 2}


@dataclass(frozen=True)
class LightGBMHistoryBias:
    action_key: str | None
    sample_count: int
    hit_rate: float


@dataclass(frozen=True)
class LightGBMTacticalGuidance:
    action_key: str
    bias_action_key: str | None
    sample_count: int
    peer_hit_rate: float
    tag_code: str
    note_code: str


def lightgbm_history_bias(payload: dict | None) -> LightGBMHistoryBias:
    """Resolve the dominant 1D action without allowing UI language to affect it."""
    windows = (payload or {}).get("windows") or {}
    candidates: list[tuple[int, float, str]] = []
    for action_key in ("pullback", "breakout", "watch"):
        stats = ((windows.get(action_key) or {}).get(1) or {})
        candidates.append(
            (
                int(stats.get("count") or 0),
                float(stats.get("hit_rate") or 0.0),
                action_key,
            )
        )
    count, hit_rate, action_key = sorted(
        candidates,
        key=lambda item: (-item[0], -item[1], _BIAS_PRIORITY[item[2]]),
    )[0]
    return LightGBMHistoryBias(
        action_key=action_key if count > 0 else None,
        sample_count=count,
        hit_rate=hit_rate,
    )


def lightgbm_tactical_guidance(
    *,
    action_key: str,
    market_payload: dict,
    fallback_payload: dict,
) -> LightGBMTacticalGuidance:
    """Derive a language-neutral tactical decision for one LightGBM action."""
    payload = market_payload if int((market_payload or {}).get("sample_count") or 0) > 0 else fallback_payload
    sample_count = int((payload or {}).get("sample_count") or 0)
    one_day = ((((payload or {}).get("windows") or {}).get(action_key) or {}).get(1) or {})
    hit_rate = float(one_day.get("hit_rate") or 0.0)
    bias = lightgbm_history_bias(payload or fallback_payload)

    if sample_count <= 0:
        tag_code = "observe_first"
        note_code = "insufficient_samples"
    elif action_key == "pullback" and bias.action_key == "breakout":
        tag_code = "avoid_early_pullback"
        note_code = "market_leans_breakout"
    elif action_key == "pullback":
        tag_code = "wait_for_pullback"
        note_code = "pullback_confirmation"
    elif action_key == "breakout" and bias.action_key == "pullback":
        tag_code = "breakout_needs_proof"
        note_code = "market_leans_pullback"
    elif action_key == "breakout":
        tag_code = "follow_breakout"
        note_code = "breakout_confirmation"
    else:
        tag_code = "observe_first"
        note_code = "watch_only"

    return LightGBMTacticalGuidance(
        action_key=action_key,
        bias_action_key=bias.action_key,
        sample_count=sample_count,
        peer_hit_rate=hit_rate,
        tag_code=tag_code,
        note_code=note_code,
    )


def annotate_lightgbm_tactical_context(
    items: list[dict],
    *,
    selected_market: str,
    history_evaluation: dict,
    force_apply: bool = False,
) -> None:
    """Attach structured tactical facts; presentation copy is added elsewhere."""
    per_market = (history_evaluation or {}).get("per_market") or {}
    for item in items:
        matched_templates = {
            str(value).strip()
            for value in (item.get("matched_model_templates") or [])
            if str(value).strip()
        }
        if not force_apply and "lightgbm_top_picks" not in matched_templates:
            continue
        action_key = normalize_lightgbm_prediction_action(
            entry_style=item.get("model_entry_style"),
            signal_label=item.get("model_signal_label"),
        )
        if not action_key:
            action_key = normalize_lightgbm_action(item.get("action_label"))
        if action_key not in LIGHTGBM_ACTIONS:
            continue
        market_code = str(item.get("market") or selected_market or "CN").upper()
        guidance = lightgbm_tactical_guidance(
            action_key=action_key,
            market_payload=per_market.get(market_code) or {},
            fallback_payload=history_evaluation,
        )
        item["lightgbm_tactical_action"] = action_key
        item["lightgbm_tactical_guidance"] = asdict(guidance)
