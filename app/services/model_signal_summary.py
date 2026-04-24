def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def model_confidence(score: float | None) -> int | None:
    if score is None:
        return None
    return min(92, max(38, int(45 + abs(float(score)) * 18)))


def build_signal_label(score: float | None, *, lang: str) -> str | None:
    if score is None:
        return None
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
) -> str | None:
    if score is None:
        return None

    label = (signal_label_value or build_signal_label(score, lang="en") or "").strip().lower()
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


def _derive_expected_drawdown_20d(score: float | None, risk_score: float | None) -> float | None:
    if score is None:
        return None
    base_risk = float(risk_score) if risk_score is not None else _clamp(50.0 - float(score) * 55.0, 8.0, 92.0)
    drawdown = 2.5 + (base_risk / 100.0) * 14.0
    if float(score) < 0:
        drawdown += min(6.0, abs(float(score)) * 12.0)
    return round(_clamp(drawdown, 2.5, 22.0), 2)


def _derive_model_reward_risk_ratio(expected_return_20d: float | None, expected_drawdown_20d: float | None) -> float | None:
    if expected_return_20d is None or expected_drawdown_20d in (None, 0):
        return None
    return round(abs(float(expected_return_20d)) / float(expected_drawdown_20d), 2)


def enrich_model_output(model_output: dict | None, *, lang: str) -> dict | None:
    if model_output is None:
        return None

    score = model_output.get("score")
    confidence = model_confidence(score)
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
        model_output["state"] = build_model_state(score, lang=lang)
        if model_output.get("target_horizon_days") is None:
            model_output["target_horizon_days"] = _derive_target_horizon_days(
                score,
                existing=model_output.get("target_horizon_days"),
            )
        if model_output.get("expected_drawdown_20d") is None:
            model_output["expected_drawdown_20d"] = _derive_expected_drawdown_20d(
                score,
                model_output.get("risk_score"),
            )
        if model_output.get("model_reward_risk_ratio") is None:
            model_output["model_reward_risk_ratio"] = _derive_model_reward_risk_ratio(
                model_output.get("expected_return_20d"),
                model_output.get("expected_drawdown_20d"),
            )
        if model_output.get("signal_label") is None:
            model_output["signal_label"] = build_signal_label(score, lang=lang)
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

    bounded = _clamp(float(score) * 8, -1.5, 1.5)
    bullish_prob = _clamp(0.5 + (bounded / 3.5) * 0.25, 0.05, 0.95)
    bearish_prob = _clamp(1.0 - bullish_prob, 0.05, 0.95)
    expected_return_5d = _clamp(float(score) * 0.35, -0.25, 0.25)
    expected_return_20d = _clamp(float(score) * 0.8, -0.45, 0.45)

    if bullish_prob >= 0.64:
        regime_label = "bullish_trend" if lang == "en" else "偏多趋势"
    elif bearish_prob >= 0.6:
        regime_label = "cautious_range" if lang == "en" else "谨慎震荡"
    else:
        regime_label = "balanced_range" if lang == "en" else "中性震荡"
    risk_score = _clamp(50.0 - float(score) * 55.0, 8.0, 92.0)

    model_output["confidence"] = confidence
    if model_output.get("bullish_prob") is None:
        model_output["bullish_prob"] = round(bullish_prob * 100, 1)
    if model_output.get("bearish_prob") is None:
        model_output["bearish_prob"] = round(bearish_prob * 100, 1)
    if model_output.get("expected_return_5d") is None:
        model_output["expected_return_5d"] = round(expected_return_5d * 100, 2)
    if model_output.get("expected_return_20d") is None:
        model_output["expected_return_20d"] = round(expected_return_20d * 100, 2)
    if model_output.get("regime_label") is None:
        model_output["regime_label"] = regime_label
    if model_output.get("risk_score") is None:
        model_output["risk_score"] = round(risk_score, 1)
    if model_output.get("target_horizon_days") is None:
        model_output["target_horizon_days"] = _derive_target_horizon_days(score)
    if model_output.get("expected_drawdown_20d") is None:
        model_output["expected_drawdown_20d"] = _derive_expected_drawdown_20d(score, model_output.get("risk_score"))
    if model_output.get("model_reward_risk_ratio") is None:
        model_output["model_reward_risk_ratio"] = _derive_model_reward_risk_ratio(
            model_output.get("expected_return_20d"),
            model_output.get("expected_drawdown_20d"),
        )
    model_output["state"] = build_model_state(score, lang=lang)
    if model_output.get("signal_label") is None:
        model_output["signal_label"] = build_signal_label(score, lang=lang)
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


def build_model_state(score: float | None, *, lang: str) -> dict:
    if score is None:
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
    confidence = model_output.get("confidence")
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
