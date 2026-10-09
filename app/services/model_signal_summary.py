import bisect
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


CALIBRATED_ESTIMATE_SCHEMA_V1 = "calibrated_estimates_v1"

# Probability calibration provenance.  A raw regression/ranking score is never a
# probability; callers must surface one of these statuses next to any value.
CALIBRATION_STATUS_CALIBRATED = "calibrated"
CALIBRATION_STATUS_UNAVAILABLE = "unavailable"
CALIBRATION_STATUS_INSUFFICIENT_DATA = "unavailable:insufficient_calibration_data"
CALIBRATION_STATUS_NO_SPEC = "unavailable:no_calibration_spec"
CALIBRATION_STATUS_MISSING_SCORE = "unavailable:missing_calibration_score"


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _safe_percentile(percentile: object) -> float | None:
    """Coerce a stored cross-sectional percentile (0-100) to a float.

    Anything unparseable or out of range is treated as "not provided" so the
    badge falls back to the absolute score thresholds instead of guessing.
    """
    if percentile is None or isinstance(percentile, bool):
        return None
    try:
        value = float(percentile)
    except (TypeError, ValueError):
        return None
    if value != value or value < 0.0 or value > 100.0:  # NaN / out of contract
        return None
    return value


def model_confidence(score: float | None) -> int | None:
    # A raw regression score carries no calibrated probability or confidence.
    return None


# Cross-sectional percentile quintile boundaries. ``build_model_state`` and
# ``build_signal_label`` share these so a row's badge and its Buy/Watch/Hold/Sell
# text can never disagree once the payload carries a percentile.
PERCENTILE_STRONG = 80.0
PERCENTILE_POSITIVE = 60.0
PERCENTILE_CAUTIOUS = 40.0
PERCENTILE_WEAK = 20.0


def build_signal_label(
    score: float | None, *, lang: str, percentile: float | None = None
) -> str | None:
    if score is None:
        return None
    rank_percentile = _safe_percentile(percentile)
    if rank_percentile is not None:
        # Same quintile contract as ``build_model_state``: once the payload
        # carries a cross-sectional percentile the top quintile reads as a buy,
        # the second quintile as watch, the bottom quintile as sell, and the
        # middle band holds. Percentile-less callers keep the legacy absolute
        # thresholds below so old caches are unchanged.
        if rank_percentile >= PERCENTILE_STRONG:
            return "Buy" if lang == "en" else "买点"
        if rank_percentile >= PERCENTILE_POSITIVE:
            return "Watch" if lang == "en" else "观察"
        if rank_percentile <= PERCENTILE_WEAK:
            return "Sell" if lang == "en" else "卖点"
        return "Hold" if lang == "en" else "持有"
    value = float(score)
    if value >= 0.18:
        return "Buy" if lang == "en" else "买点"
    if value <= -0.05:
        return "Sell" if lang == "en" else "卖点"
    if value >= 0.05:
        return "Watch" if lang == "en" else "观察"
    return "Hold" if lang == "en" else "持有"


def signal_strength(score: float | None) -> int | None:
    if score is None:
        return None
    return min(100, max(8, int(abs(float(score)) * 280)))


def model_direction_rank(score: float | None) -> int:
    """Return 1 for the long side and 0 for the short side.

    Unknown scores keep the long-side rank so legacy rows are not demoted.
    """
    if score is None:
        return 1
    return 0 if float(score) < 0 else 1


def model_rank_strength(score: float | None) -> int:
    """Magnitude used for ranking; short-side (SELL) rows must not outrank longs.

    ``signal_strength`` stays an absolute display value. Ranking uses this
    direction-aware counterpart so a large negative score cannot top the list.
    """
    if score is None:
        return 0
    if float(score) < 0:
        return 0
    strength = signal_strength(score)
    return int(strength or 0)


def conviction_bucket(score: float | None, *, lang: str) -> str | None:
    strength = signal_strength(score)
    if strength is None:
        return None
    if strength >= 70:
        return "High Conviction" if lang == "en" else "高信念"
    if strength >= 40:
        return "Medium Conviction" if lang == "en" else "中信念"
    return "Low Conviction" if lang == "en" else "低信念"


def position_size_hint(
    score: float | None,
    *,
    lang: str,
    signal_strength_value: int | None = None,
    reward_risk_ratio: float | None = None,
) -> str | None:
    if score is None:
        return None

    value = float(score)
    strength = signal_strength_value if signal_strength_value is not None else signal_strength(score)
    rr = float(reward_risk_ratio) if reward_risk_ratio is not None else None

    if value <= -0.03:
        return "No Position" if lang == "en" else "不建议开仓"
    if strength is None:
        return "Starter" if lang == "en" else "试探仓"
    if strength >= 70 and (rr is None or rr >= 1.2):
        return "Aggressive" if lang == "en" else "进攻仓"
    if strength >= 40 and (rr is None or rr >= 0.8):
        return "Standard" if lang == "en" else "标准仓"
    return "Starter" if lang == "en" else "试探仓"


def entry_style(
    score: float | None,
    *,
    lang: str,
    signal_label_value: str | None = None,
    signal_strength_value: int | None = None,
    reward_risk_ratio: float | None = None,
    percentile: float | None = None,
) -> str | None:
    if score is None:
        return None

    label = (
        signal_label_value or build_signal_label(score, lang="en", percentile=percentile) or ""
    ).strip().lower()
    strength = signal_strength_value if signal_strength_value is not None else signal_strength(score)
    rr = float(reward_risk_ratio) if reward_risk_ratio is not None else None
    value = float(score)

    if label == "sell" or value <= -0.03:
        return "Avoid" if lang == "en" else "回避"
    if label == "buy":
        if strength is not None and strength >= 70 and (rr is None or rr >= 1.0):
            return "Breakout" if lang == "en" else "突破跟进"
        return "Pullback" if lang == "en" else "回踩吸纳"
    if label == "watch":
        if strength is not None and strength >= 45:
            return "Pullback" if lang == "en" else "回踩吸纳"
        return "Wait" if lang == "en" else "等待确认"
    return "Wait" if lang == "en" else "等待确认"


def _derive_target_horizon_days(score: float | None, existing: int | None = None) -> int | None:
    if existing is not None:
        return existing
    if score is None:
        return 20
    magnitude = abs(float(score))
    if magnitude >= 0.18:
        return 10
    if magnitude >= 0.08:
        return 15
    return 20



def _derive_model_reward_risk_ratio(expected_return_20d: float | None, expected_drawdown_20d: float | None) -> float | None:
    if expected_return_20d is None or expected_drawdown_20d in (None, 0):
        return None
    return round(abs(float(expected_return_20d)) / float(expected_drawdown_20d), 2)


def _validate_calibrated_estimates(model_output: dict) -> None:
    if model_output.get("estimate_schema_version") != CALIBRATED_ESTIMATE_SCHEMA_V1:
        return
    if model_output.get("probability_unit") != "ratio":
        raise ValueError("calibrated_estimates_v1 requires probability_unit=ratio")
    if model_output.get("return_unit") != "ratio":
        raise ValueError("calibrated_estimates_v1 requires return_unit=ratio")
    if not str(model_output.get("estimate_protocol_id") or "").strip():
        raise ValueError("calibrated estimates require estimate_protocol_id")
    for key in ("confidence", "bullish_prob", "bearish_prob"):
        value = model_output.get(key)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be a finite ratio") from exc
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            raise ValueError(f"{key} must be in [0, 1]")
    for key in ("expected_return_5d", "expected_return_20d", "expected_drawdown_20d"):
        value = model_output.get(key)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be finite") from exc
        if not math.isfinite(number):
            raise ValueError(f"{key} must be finite")


def estimate_display_percent(model_output: dict | None, key: str) -> float | None:
    if not model_output or model_output.get(key) is None:
        return None
    value = float(model_output[key])
    units = model_output.get("estimate_units") or {}
    unit_key = "probability" if key in {"confidence", "bullish_prob", "bearish_prob"} else "return"
    return value * 100.0 if units.get(unit_key) == "ratio" else value


def enrich_model_output(model_output: dict | None, *, lang: str) -> dict | None:
    if model_output is None:
        return None

    model_output = dict(model_output)
    _validate_calibrated_estimates(model_output)

    score = model_output.get("score")
    confidence = model_output.get("confidence")
    has_supplied_estimate = any(
        model_output.get(key) is not None
        for key in (
            "confidence", "bullish_prob", "bearish_prob", "expected_return_5d",
            "expected_return_20d", "expected_drawdown_20d",
        )
    )
    calibrated = model_output.get("estimate_schema_version") == CALIBRATED_ESTIMATE_SCHEMA_V1
    if calibrated:
        model_output.setdefault("estimate_source", "calibrated_protocol")
    elif has_supplied_estimate:
        claimed_source = model_output.get("estimate_source")
        if claimed_source and claimed_source != "supplied_unverified":
            model_output["legacy_estimate_source"] = claimed_source
        model_output["estimate_source"] = "supplied_unverified"
    else:
        model_output["estimate_source"] = "unavailable"
    model_output["estimate_certified"] = bool(calibrated)
    model_output["estimate_units"] = {
        "probability": "ratio" if calibrated else "legacy_percent" if has_supplied_estimate else "unavailable",
        "return": "ratio" if calibrated else "legacy_percent" if has_supplied_estimate else "unavailable",
    }
    if has_supplied_estimate and not calibrated:
        model_output.setdefault("estimate_protocol_id", "legacy_unverified")
    elif not calibrated:
        model_output.setdefault("estimate_protocol_id", None)
    if score is None:
        model_output["confidence"] = confidence
        model_output["state"] = build_model_state(None, lang=lang)
        model_output["signal_label"] = build_signal_label(None, lang=lang)
        model_output["signal_strength"] = signal_strength(None)
        if model_output.get("conviction_bucket") is None:
            model_output["conviction_bucket"] = conviction_bucket(None, lang=lang)
        if model_output.get("position_size_hint") is None:
            model_output["position_size_hint"] = position_size_hint(None, lang=lang)
        if model_output.get("entry_style") is None:
            model_output["entry_style"] = entry_style(None, lang=lang)
        if model_output.get("target_horizon_days") is None:
            model_output["target_horizon_days"] = _derive_target_horizon_days(None)
        if not model_output.get("summary_text"):
            model_output["summary_text"] = summarize_model_output(model_output, lang=lang)
        return model_output

    if model_output.get("confidence") is not None:
        model_output["state"] = build_model_state(
            score, lang=lang, percentile=model_output.get("percentile")
        )
        if model_output.get("target_horizon_days") is None:
            model_output["target_horizon_days"] = _derive_target_horizon_days(
                score,
                existing=model_output.get("target_horizon_days"),
            )
        model_output.setdefault("expected_drawdown_20d", None)
        if model_output.get("model_reward_risk_ratio") is None:
            model_output["model_reward_risk_ratio"] = _derive_model_reward_risk_ratio(
                model_output.get("expected_return_20d"),
                model_output.get("expected_drawdown_20d"),
            )
        if model_output.get("signal_label") is None:
            model_output["signal_label"] = build_signal_label(
                score, lang=lang, percentile=model_output.get("percentile")
            )
        if model_output.get("signal_strength") is None:
            model_output["signal_strength"] = signal_strength(score)
        if model_output.get("conviction_bucket") is None:
            model_output["conviction_bucket"] = conviction_bucket(score, lang=lang)
        if model_output.get("position_size_hint") is None:
            model_output["position_size_hint"] = position_size_hint(
                score,
                lang=lang,
                signal_strength_value=model_output.get("signal_strength"),
                reward_risk_ratio=model_output.get("model_reward_risk_ratio"),
            )
        if model_output.get("entry_style") is None:
            model_output["entry_style"] = entry_style(
                score,
                lang=lang,
                signal_label_value=model_output.get("signal_label"),
                signal_strength_value=model_output.get("signal_strength"),
                reward_risk_ratio=model_output.get("model_reward_risk_ratio"),
            )
        if not model_output.get("summary_text"):
            model_output["summary_text"] = summarize_model_output(model_output, lang=lang)
        return model_output

    # These labels describe the score only; they are not probability estimates.
    if float(score) >= 0.18:
        regime_label = "bullish_trend" if lang == "en" else "偏多趋势"
    elif float(score) <= -0.18:
        regime_label = "cautious_range" if lang == "en" else "谨慎震荡"
    else:
        regime_label = "balanced_range" if lang == "en" else "中性震荡"
    risk_score = _clamp(50.0 - float(score) * 55.0, 8.0, 92.0)

    model_output["confidence"] = confidence
    for key in ("bullish_prob", "bearish_prob", "expected_return_5d", "expected_return_20d"):
        model_output.setdefault(key, None)
    if model_output.get("regime_label") is None:
        model_output["regime_label"] = regime_label
    if model_output.get("risk_score") is None:
        model_output["risk_score"] = round(risk_score, 1)
    if model_output.get("target_horizon_days") is None:
        model_output["target_horizon_days"] = _derive_target_horizon_days(score)
    model_output.setdefault("expected_drawdown_20d", None)
    if model_output.get("model_reward_risk_ratio") is None:
        model_output["model_reward_risk_ratio"] = _derive_model_reward_risk_ratio(
            model_output.get("expected_return_20d"),
            model_output.get("expected_drawdown_20d"),
        )
    model_output["state"] = build_model_state(
        score, lang=lang, percentile=model_output.get("percentile")
    )
    if model_output.get("signal_label") is None:
        model_output["signal_label"] = build_signal_label(
            score, lang=lang, percentile=model_output.get("percentile")
        )
    if model_output.get("signal_strength") is None:
        model_output["signal_strength"] = signal_strength(score)
    if model_output.get("conviction_bucket") is None:
        model_output["conviction_bucket"] = conviction_bucket(score, lang=lang)
    if model_output.get("position_size_hint") is None:
        model_output["position_size_hint"] = position_size_hint(
            score,
            lang=lang,
            signal_strength_value=model_output.get("signal_strength"),
            reward_risk_ratio=model_output.get("model_reward_risk_ratio"),
        )
    if model_output.get("entry_style") is None:
        model_output["entry_style"] = entry_style(
            score,
            lang=lang,
            signal_label_value=model_output.get("signal_label"),
            signal_strength_value=model_output.get("signal_strength"),
            reward_risk_ratio=model_output.get("model_reward_risk_ratio"),
        )
    if not model_output.get("summary_text"):
        model_output["summary_text"] = summarize_model_output(model_output, lang=lang)
    return model_output


def build_model_state(score: float | None, *, lang: str, percentile: float | None = None) -> dict:
    """Render the model badge for one row.

    ``score`` is a cross-sectional ranking signal on the ``score_semantics`` /
    ``score_contract_version`` contract (``executable_next_open_net_return``),
    not a probability or a percent return. The absolute score thresholds below
    were calibrated against an older, wider score scale; once the trainer
    compressed scores to the net-return scale they pinned nearly every row to
    Neutral (and the top-N rows a dashboard shows are exactly the compressed
    ones). When the payload carries a cross-sectional ``percentile``
    (``score_source: lightgbm_prediction_v1:percentile_0_100``) the badge is
    normalized on that ranking in quintiles instead, so the badge keeps
    discriminating between rows. Percentile-less callers keep the previous
    absolute-threshold behaviour.
    """
    if score is None:
        label = "Neutral" if lang == "en" else "中性"
        return {"key": "neutral", "label": label, "bg": "#f3f4f6", "fg": "#374151"}

    rank_percentile = _safe_percentile(percentile)
    if rank_percentile is not None:
        if rank_percentile >= PERCENTILE_STRONG:
            return {
                "key": "strong",
                "label": "Strong" if lang == "en" else "强",
                "bg": "#dcfce7",
                "fg": "#166534",
            }
        if rank_percentile >= PERCENTILE_POSITIVE:
            return {
                "key": "positive",
                "label": "Positive" if lang == "en" else "偏强",
                "bg": "#ecfccb",
                "fg": "#3f6212",
            }
        if rank_percentile <= PERCENTILE_WEAK:
            return {
                "key": "weak",
                "label": "Weak" if lang == "en" else "偏弱",
                "bg": "#fee2e2",
                "fg": "#991b1b",
            }
        if rank_percentile <= PERCENTILE_CAUTIOUS:
            return {
                "key": "cautious",
                "label": "Cautious" if lang == "en" else "谨慎",
                "bg": "#fef3c7",
                "fg": "#92400e",
            }
        label = "Neutral" if lang == "en" else "中性"
        return {"key": "neutral", "label": label, "bg": "#f3f4f6", "fg": "#374151"}

    value = float(score)
    if value >= 0.12:
        return {
            "key": "strong",
            "label": "Strong" if lang == "en" else "强",
            "bg": "#dcfce7",
            "fg": "#166534",
        }
    if value >= 0.03:
        return {
            "key": "positive",
            "label": "Positive" if lang == "en" else "偏强",
            "bg": "#ecfccb",
            "fg": "#3f6212",
        }
    if value <= -0.12:
        return {
            "key": "weak",
            "label": "Weak" if lang == "en" else "偏弱",
            "bg": "#fee2e2",
            "fg": "#991b1b",
        }
    if value <= -0.03:
        return {
            "key": "cautious",
            "label": "Cautious" if lang == "en" else "谨慎",
            "bg": "#fef3c7",
            "fg": "#92400e",
        }
    return {
        "key": "neutral",
        "label": "Neutral" if lang == "en" else "中性",
        "bg": "#f3f4f6",
        "fg": "#374151",
    }


def summarize_explanations(explanations: list[dict], *, lang: str, limit: int = 3) -> list[str]:
    highlights: list[str] = []
    for item in explanations:
        contribution = item.get("contribution")
        if contribution is None:
            continue
        feature_name = str(item.get("feature_name") or "")
        if feature_name == "recent_daily_return":
            label = "recent move" if lang == "en" else "近日波动"
        elif feature_name.startswith("lag_return_"):
            label = feature_name.replace("lag_return_", "lag ").replace("d", "d")
            if lang == "zh":
                label = feature_name.replace("lag_return_", "滞后").replace("d", "日")
        elif feature_name == "price_vs_ma20":
            label = "price vs MA20" if lang == "en" else "价位相对MA20"
        elif feature_name == "ma_alignment":
            label = "MA alignment" if lang == "en" else "均线排列"
        elif feature_name == "volume_ratio_20d":
            label = "volume support" if lang == "en" else "量能支撑"
        elif feature_name.startswith("lookback_momentum_"):
            label = "lookback momentum" if lang == "en" else "回看动量"
        else:
            label = feature_name
        direction = "+" if float(contribution) >= 0 else ""
        highlights.append(f"{label} {direction}{float(contribution):.2f}")
    return highlights[:limit]


def summarize_model_output(model_output: dict | None, *, lang: str) -> str:
    if not model_output:
        return "暂无模型摘要。" if lang == "zh" else "No model summary yet."

    score = model_output.get("score")
    if score is None:
        return "暂无模型摘要。" if lang == "zh" else "No model summary yet."

    run_name = (model_output.get("model_run") or {}).get("name") or "-"
    confidence = estimate_display_percent(model_output, "confidence")
    if confidence is None:
        confidence = model_confidence(score)
    percentile = model_output.get("percentile")
    regime_label = model_output.get("regime_label")
    horizon = model_output.get("target_horizon_days")

    if lang == "zh":
        stance = "偏多" if float(score) >= 0 else "偏谨慎"
        confidence_text = f"置信度约 {int(confidence)}%" if confidence is not None else "置信度暂无"
        percentile_text = (
            f"大致位于市场前 {100 - float(percentile):.1f}%"
            if percentile is not None
            else "暂无市场分位参考"
        )
        horizon_text = f"，观察周期约 {int(horizon)} 天" if horizon is not None else ""
        regime_text = f"，当前节奏偏{regime_label}" if regime_label else ""
        return (
            f"最新模型 {run_name} 对这只股票给出 {float(score):.3f} 分，整体{stance}，"
            f"{confidence_text}{regime_text}{horizon_text}，{percentile_text}。"
        )

    stance = "bullish" if float(score) >= 0 else "cautious"
    confidence_text = f"about {int(confidence)}% confidence" if confidence is not None else "without a confidence reading"
    percentile_text = (
        f"roughly top {100 - float(percentile):.1f}% of its universe"
        if percentile is not None
        else "without a percentile reading yet"
    )
    horizon_text = f" over roughly {int(horizon)} trading days" if horizon is not None else ""
    regime_text = f", leaning {regime_label}" if regime_label else ""
    return (
        f"The latest model run {run_name} scores this stock at {float(score):.3f}, reads as {stance} "
        f"with {confidence_text}{regime_text}{horizon_text}, and lands {percentile_text}."
    )


@dataclass(frozen=True, slots=True)
class ProbabilityCalibrator:
    """A monotone score -> positive-probability map fitted from matured labels.

    ``method="isotonic"`` carries piece-wise linear interpolation so the map is
    non-decreasing; ``method="bins"`` carries the step function emitted by the
    quantile-binned ``selective_calibration`` research artifact.  Either way the
    output is only meaningful when ``source`` names the fitted artifact.
    """

    method: str
    thresholds: tuple[float, ...]
    probabilities: tuple[float, ...]
    sample_count: int
    positive_count: int
    source: str
    score_field: str
    version: str

    def predict(self, score: float) -> float:
        if not self.thresholds:
            raise ValueError("probability calibrator has no thresholds")
        value = float(score)
        if not math.isfinite(value):
            raise ValueError("probability calibration score must be finite")
        if self.method == "bins":
            index = bisect.bisect_left(self.thresholds, value)
            index = min(index, len(self.thresholds) - 1)
            return float(self.probabilities[index])
        if value <= self.thresholds[0]:
            return float(self.probabilities[0])
        if value >= self.thresholds[-1]:
            return float(self.probabilities[-1])
        upper = bisect.bisect_left(self.thresholds, value)
        lower = max(0, upper - 1)
        low_t, high_t = self.thresholds[lower], self.thresholds[upper]
        low_p, high_p = self.probabilities[lower], self.probabilities[upper]
        if high_t == low_t:
            return float(high_p)
        ratio = (value - low_t) / (high_t - low_t)
        return float(low_p + (high_p - low_p) * ratio)


def calibration_status(calibrator: ProbabilityCalibrator | None) -> str:
    return CALIBRATION_STATUS_CALIBRATED if calibrator is not None else CALIBRATION_STATUS_UNAVAILABLE


def _calibration_version(payload: Mapping) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:16]
    return f"probability_calibration_v1:{digest}"


def _iter_calibration_samples(samples: Sequence | None):
    for item in samples or ():
        if isinstance(item, Mapping):
            score = item.get("score", item.get("raw_score"))
            label = item.get("label", item.get("outcome", item.get("label_value")))
        else:
            values = list(item)
            if len(values) != 2:
                raise ValueError("calibration samples must be (score, label) pairs")
            score, label = values
        if score is None or label is None:
            raise ValueError("calibration samples require both score and label")
        score_value = float(score)
        label_value = float(label)
        if not math.isfinite(score_value) or not math.isfinite(label_value):
            raise ValueError("calibration samples must be finite")
        yield score_value, 1.0 if label_value > 0 else 0.0


def fit_isotonic_probability_calibrator(
    samples: Sequence | None,
    *,
    source: str,
    score_field: str = "model_score",
    min_samples: int = 2,
) -> ProbabilityCalibrator | None:
    """Pool-adjacent-violators fit of a monotone positive-probability curve.

    Returns ``None`` when fewer than ``min_samples`` matured observations are
    available.  It never fabricates a probability from a score alone.
    """
    if min_samples < 1:
        raise ValueError("min_samples must be positive")
    rows = list(_iter_calibration_samples(samples))
    if len(rows) < min_samples:
        return None
    rows.sort(key=lambda item: item[0])

    grouped: list[list[float]] = []  # [score, count, positive_count]
    for score_value, label_value in rows:
        if grouped and grouped[-1][0] == score_value:
            grouped[-1][1] += 1.0
            grouped[-1][2] += label_value
        else:
            grouped.append([score_value, 1.0, label_value])

    # PAVA over per-score positive rates, weighting each pooled block by its
    # observation count.  ``block_upper`` tracks the largest score each pooled
    # block covers so the step/interpolation map stays monotone.
    probabilities = [positive / count for _score, count, positive in grouped]
    weights = [count for _score, count, _positive in grouped]
    block_upper = [score_value for score_value, _count, _positive in grouped]
    index = 0
    while index < len(probabilities) - 1:
        if probabilities[index] <= probabilities[index + 1] + 1e-12:
            index += 1
            continue
        merged_weight = weights[index] + weights[index + 1]
        probabilities[index] = (
            probabilities[index] * weights[index]
            + probabilities[index + 1] * weights[index + 1]
        ) / merged_weight
        weights[index] = merged_weight
        block_upper[index] = block_upper[index + 1]
        del probabilities[index + 1]
        del weights[index + 1]
        del block_upper[index + 1]
        if index > 0:
            index -= 1
    thresholds = block_upper

    positive_count = int(sum(1 for _s, label in rows if label > 0))
    return ProbabilityCalibrator(
        method="isotonic",
        thresholds=tuple(thresholds),
        probabilities=tuple(probabilities),
        sample_count=len(rows),
        positive_count=positive_count,
        source=str(source or "provided_isotonic"),
        score_field=str(score_field or "model_score"),
        version=_calibration_version(
            {
                "method": "isotonic",
                "thresholds": thresholds,
                "probabilities": probabilities,
                "source": source,
                "sample_count": len(rows),
            }
        ),
    )


def build_probability_calibrator_from_bins(
    bins: Sequence | None,
    *,
    source: str,
    score_field: str = "model_score",
) -> ProbabilityCalibrator:
    """Adapt quantile bins (e.g. ``selective_calibration`` output) to a step map."""

    if not bins:
        raise ValueError("probability calibration bins must not be empty")
    parsed: list[tuple[float, float]] = []
    for item in bins:
        if not isinstance(item, Mapping):
            raise ValueError("calibration bins must be mappings")
        upper = item.get("upper_score", item.get("upper_bound", item.get("threshold")))
        probability = item.get(
            "calibrated_positive_probability",
            item.get("probability", item.get("positive_probability")),
        )
        if upper is None or probability is None:
            raise ValueError("calibration bins require an upper bound and a probability")
        upper_value = float(upper)
        probability_value = float(probability)
        if not math.isfinite(upper_value) or not 0.0 <= probability_value <= 1.0:
            raise ValueError("calibration bin values must be finite with probability in [0, 1]")
        parsed.append((upper_value, probability_value))
    parsed.sort(key=lambda item: item[0])
    thresholds: list[float] = []
    probabilities: list[float] = []
    running_max = 0.0
    for upper_value, probability_value in parsed:
        if thresholds and upper_value == thresholds[-1]:
            probabilities[-1] = probability_value
            continue
        running_max = max(running_max, probability_value)
        thresholds.append(upper_value)
        probabilities.append(running_max)
    return ProbabilityCalibrator(
        method="bins",
        thresholds=tuple(thresholds),
        probabilities=tuple(probabilities),
        sample_count=len(parsed),
        positive_count=-1,
        source=str(source or "provided_bins"),
        score_field=str(score_field or "model_score"),
        version=_calibration_version(
            {
                "method": "bins",
                "thresholds": thresholds,
                "probabilities": probabilities,
                "source": source,
            }
        ),
    )


def resolve_probability_calibrator(
    params: Mapping | None,
) -> tuple[ProbabilityCalibrator | None, str]:
    """Build a calibrator from a screen/run param spec, flagging provenance.

    The spec (``params["probability_calibration"]``) is intentionally explicit:
    ``{"method": "isotonic", "samples": [[score, label], ...], "source": ...}``
    or ``{"method": "bins", "bins": [...], "source": ...}``.  Without a spec the
    result is ``(None, "unavailable:no_calibration_spec")`` -- never a guessed
    probability.
    """
    if not params:
        return None, CALIBRATION_STATUS_NO_SPEC
    spec = params.get("probability_calibration")
    if spec is None:
        return None, CALIBRATION_STATUS_NO_SPEC
    if isinstance(spec, ProbabilityCalibrator):
        return spec, CALIBRATION_STATUS_CALIBRATED
    if not isinstance(spec, Mapping):
        raise ValueError("probability_calibration must be a mapping or ProbabilityCalibrator")
    method = str(spec.get("method") or "isotonic").strip().lower()
    score_field = str(spec.get("score_field") or "model_score")
    source = str(spec.get("source") or "provided")
    if method in {"bins", "quantile_bins"}:
        return (
            build_probability_calibrator_from_bins(spec.get("bins"), source=source, score_field=score_field),
            CALIBRATION_STATUS_CALIBRATED,
        )
    if method in {"isotonic", "pava"}:
        calibrator = fit_isotonic_probability_calibrator(
            spec.get("samples"),
            source=source,
            score_field=score_field,
            min_samples=int(spec.get("min_samples", 2)),
        )
        if calibrator is None:
            return None, CALIBRATION_STATUS_INSUFFICIENT_DATA
        return calibrator, CALIBRATION_STATUS_CALIBRATED
    raise ValueError(f"unsupported probability calibration method: {method}")


def expected_hit_probability(calibrator: ProbabilityCalibrator | None, score: object) -> float | None:
    """Return the calibrated probability or ``None`` -- never a raw-score guess."""
    if calibrator is None or score is None:
        return None
    try:
        value = float(score)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return round(calibrator.predict(value), 6)
