from statistics import mean

from app.services.symbol_details import SymbolDataService


class InsightEngine:
    """Build a user-facing stock insight from price history."""

    def __init__(self) -> None:
        self.symbol_data = SymbolDataService()

    def get_insight(self, ticker: str, limit: int = 180, lang: str = "en") -> dict | None:
        lang = "zh" if lang == "zh" else "en"
        history = self.symbol_data.get_history(ticker, limit=limit)
        if not history:
            return None

        closes = [row["close"] for row in history if row.get("close") is not None]
        highs = [row["high"] for row in history if row.get("high") is not None]
        lows = [row["low"] for row in history if row.get("low") is not None]
        volumes = [row["volume"] for row in history if row.get("volume") is not None]
        latest = history[-1]
        latest_close = latest["close"] or 0.0

        ma5 = self._moving_average(closes, 5)
        ma20 = self._moving_average(closes, 20)
        ma60 = self._moving_average(closes, 60)
        atr14 = self._atr(history, 14)
        momentum_5 = self._percent_change(closes, 5)
        momentum_20 = self._percent_change(closes, 20)
        avg_volume_20 = mean(volumes[-20:]) if volumes else None
        recent_support = min(lows[-20:]) if lows else latest_close
        recent_resistance = max(highs[-20:]) if highs else latest_close

        trend_score = 50
        if ma20 is not None and latest_close > ma20:
            trend_score += 12
        if ma60 is not None and latest_close > ma60:
            trend_score += 12
        if ma20 is not None and ma60 is not None and ma20 > ma60:
            trend_score += 8
        if momentum_5 is not None:
            trend_score += max(-10, min(10, momentum_5 * 120))
        if momentum_20 is not None:
            trend_score += max(-12, min(12, momentum_20 * 80))
        if avg_volume_20 and latest.get("volume"):
            volume_ratio = latest["volume"] / avg_volume_20 if avg_volume_20 else 1.0
            trend_score += max(-6, min(6, (volume_ratio - 1.0) * 10))

        trend_score = int(max(1, min(99, round(trend_score))))
        trend_label = self._trend_label(trend_score)
        setup_label = self._setup_label(
            latest_close=latest_close,
            ma5=ma5,
            ma20=ma20,
            resistance=recent_resistance,
            atr14=atr14,
            trend_score=trend_score,
        )
        recommendation = self._recommendation(trend_label, setup_label, lang=lang)

        atr_value = atr14 or max(latest_close * 0.03, 0.01)
        entry_mid = ma20 or recent_support or latest_close
        entry_zone = {
            "low": round(max(0.01, entry_mid - atr_value * 0.35), 2),
            "high": round(max(0.01, entry_mid + atr_value * 0.35), 2),
        }
        breakout_level = round(recent_resistance, 2)
        take_profit_zone = {
            "low": round(max(breakout_level, latest_close + atr_value * 0.8), 2),
            "high": round(max(breakout_level, latest_close + atr_value * 1.8), 2),
        }
        risk_level = round(max(0.01, recent_support - atr_value * 0.5), 2)

        explanation = self._build_explanations(
            latest_close=latest_close,
            ma5=ma5,
            ma20=ma20,
            ma60=ma60,
            momentum_5=momentum_5,
            momentum_20=momentum_20,
            resistance=recent_resistance,
            support=recent_support,
            lang=lang,
        )

        confidence = round(min(0.9, max(0.35, 0.45 + abs(trend_score - 50) / 100)), 2)
        action_label, action_summary = self._action_plan(
            latest_close=latest_close,
            entry_zone=entry_zone,
            breakout_level=breakout_level,
            take_profit_zone=take_profit_zone,
            risk_level=risk_level,
            trend_label=trend_label,
            setup_label=setup_label,
            lang=lang,
        )
        volume_ratio = round((latest.get("volume") / avg_volume_20), 2) if avg_volume_20 and latest.get("volume") else None
        upside = max(0.0, take_profit_zone["high"] - latest_close)
        downside = max(0.01, latest_close - risk_level)
        reward_risk_ratio = round(upside / downside, 2) if downside else None

        return {
            "ticker": ticker.upper(),
            "lang": lang,
            "as_of_date": latest["date"],
            "trend_score": trend_score,
            "trend_label": trend_label,
            "setup_label": setup_label,
            "confidence": confidence,
            "expected_horizon": "10d",
            "recommendation": recommendation,
            "action_label": action_label,
            "action_summary": action_summary,
            "entry_zone": entry_zone,
            "breakout_level": breakout_level,
            "take_profit_zone": take_profit_zone,
            "risk_level": risk_level,
            "support_level": round(recent_support, 2),
            "resistance_level": round(recent_resistance, 2),
            "latest_close": round(latest_close, 2),
            "distance_to_entry_pct": round(((entry_zone["high"] / latest_close) - 1.0) * 100, 2),
            "distance_to_breakout_pct": round(((breakout_level / latest_close) - 1.0) * 100, 2),
            "reward_risk_ratio": reward_risk_ratio,
            "volume_ratio": volume_ratio,
            "ma5": round(ma5, 2) if ma5 is not None else None,
            "ma20": round(ma20, 2) if ma20 is not None else None,
            "ma60": round(ma60, 2) if ma60 is not None else None,
            "momentum_5": round(momentum_5 * 100, 2) if momentum_5 is not None else None,
            "momentum_20": round(momentum_20 * 100, 2) if momentum_20 is not None else None,
            "history": history,
            "explanation": explanation,
        }

    def _moving_average(self, values: list[float], window: int) -> float | None:
        if len(values) < window:
            return mean(values) if values else None
        return mean(values[-window:])

    def _percent_change(self, values: list[float], window: int) -> float | None:
        if len(values) <= window:
            return None
        previous = values[-window - 1]
        current = values[-1]
        if not previous:
            return None
        return (current / previous) - 1.0

    def _atr(self, history: list[dict], window: int) -> float | None:
        if len(history) < 2:
            return None
        true_ranges: list[float] = []
        prev_close = history[0].get("close")
        for row in history[1:]:
            high = row.get("high")
            low = row.get("low")
            close = row.get("close")
            if high is None or low is None or prev_close is None:
                prev_close = close
                continue
            true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
            prev_close = close
        if not true_ranges:
            return None
        sample = true_ranges[-window:] if len(true_ranges) >= window else true_ranges
        return mean(sample)

    def _trend_label(self, trend_score: int) -> str:
        if trend_score >= 67:
            return "bullish"
        if trend_score <= 38:
            return "bearish"
        return "neutral"

    def _setup_label(
        self,
        *,
        latest_close: float,
        ma5: float | None,
        ma20: float | None,
        resistance: float,
        atr14: float | None,
        trend_score: int,
    ) -> str:
        atr_value = atr14 or max(latest_close * 0.03, 0.01)
        if trend_score >= 67 and ma20 is not None and latest_close <= ma20 + atr_value * 0.4:
            return "pullback_buy"
        if trend_score >= 60 and latest_close >= resistance - atr_value * 0.25:
            return "breakout_watch"
        if trend_score <= 38:
            return "avoid_or_wait"
        if ma5 is not None and ma20 is not None and latest_close < ma5 < ma20:
            return "cooling_off"
        return "range_watch"

    def _recommendation(self, trend_label: str, setup_label: str, *, lang: str) -> str:
        mapping_en = {
            ("bullish", "pullback_buy"): "Trend is strong. A pullback into support looks more attractive than chasing immediately.",
            ("bullish", "breakout_watch"): "Trend is strong, but price is close to resistance. Waiting for a volume-backed breakout is cleaner.",
            ("bullish", "range_watch"): "Trend still leans bullish, but there is no especially comfortable entry right now.",
            ("neutral", "cooling_off"): "The stock is cooling off. Watch whether it can reclaim the key moving averages first.",
            ("neutral", "range_watch"): "This still looks range-bound. Patience is better than forcing an entry.",
            ("bearish", "avoid_or_wait"): "The setup is weak for now. Treat it as a watchlist name until structure improves.",
        }
        mapping_zh = {
            ("bullish", "pullback_buy"): "趋势偏强，适合等回踩支撑区后再考虑分批布局。",
            ("bullish", "breakout_watch"): "趋势偏强，但位置接近压力位，更适合等放量突破确认。",
            ("bullish", "range_watch"): "趋势仍偏多，但当前没有特别舒服的入场位置。",
            ("neutral", "cooling_off"): "短线在降温，先观察价格是否重新站稳关键均线。",
            ("neutral", "range_watch"): "整体偏震荡，适合耐心等方向更明确后再行动。",
            ("bearish", "avoid_or_wait"): "当前偏弱，先把它当观察标的，不急着接。",
        }
        fallback = "Observe the trend before acting." if lang == "en" else "先观察趋势变化，再决定是否参与。"
        mapping = mapping_zh if lang == "zh" else mapping_en
        return mapping.get((trend_label, setup_label), fallback)

    def _build_explanations(
        self,
        *,
        latest_close: float,
        ma5: float | None,
        ma20: float | None,
        ma60: float | None,
        momentum_5: float | None,
        momentum_20: float | None,
        resistance: float,
        support: float,
        lang: str,
    ) -> list[str]:
        notes: list[str] = []
        if ma20 is not None and ma60 is not None:
            if latest_close > ma20 > ma60:
                notes.append(
                    "价格位于 20 日均线和 60 日均线之上，中期结构偏强。"
                    if lang == "zh"
                    else "Price is above both the 20-day and 60-day moving averages, which supports a stronger medium-term trend."
                )
            elif latest_close < ma20 < ma60:
                notes.append(
                    "价格位于 20 日均线和 60 日均线之下，趋势仍偏弱。"
                    if lang == "zh"
                    else "Price is below both the 20-day and 60-day moving averages, so the trend still leans weak."
                )
            else:
                notes.append(
                    "价格与关键均线交错，当前更像震荡而不是单边趋势。"
                    if lang == "zh"
                    else "Price is tangled around the key moving averages, which looks more like a range than a clean trend."
                )
        if momentum_5 is not None:
            if momentum_5 > 0.05:
                notes.append(
                    "近 5 个交易日涨幅较快，短线有强势特征，也更容易出现追高风险。"
                    if lang == "zh"
                    else "The 5-day move is strong, which supports momentum but also increases chasing risk."
                )
            elif momentum_5 < -0.04:
                notes.append(
                    "近 5 个交易日回撤较明显，市场仍在消化抛压。"
                    if lang == "zh"
                    else "The last 5 trading days show a noticeable pullback, so sellers are still being absorbed."
                )
        if momentum_20 is not None:
            if momentum_20 > 0.12:
                notes.append(
                    "近 20 日动量保持向上，说明不是只有一天的异动。"
                    if lang == "zh"
                    else "The 20-day momentum is still positive, which suggests this is more than a one-day spike."
                )
            elif momentum_20 < -0.08:
                notes.append(
                    "近 20 日趋势仍向下，暂时还没看到完整扭转。"
                    if lang == "zh"
                    else "The 20-day trend is still down, so a full reversal has not been confirmed yet."
                )
        notes.append(
            f"当前主要支撑参考在 {support:.2f} 附近，短期压力参考在 {resistance:.2f} 附近。"
            if lang == "zh"
            else f"Nearby support sits around {support:.2f}, while short-term resistance sits around {resistance:.2f}."
        )
        if ma5 is not None and ma20 is not None and latest_close > ma5 > ma20:
            notes.append(
                "短期均线在长期均线上方，说明回调时更值得观察支撑能否承接。"
                if lang == "zh"
                else "Short-term moving averages are above longer ones, which makes pullback support more worth watching."
            )
        return notes[:4]

    def _action_plan(
        self,
        *,
        latest_close: float,
        entry_zone: dict,
        breakout_level: float,
        take_profit_zone: dict,
        risk_level: float,
        trend_label: str,
        setup_label: str,
        lang: str,
    ) -> tuple[str, str]:
        if trend_label == "bearish":
            if lang == "zh":
                return "先观察", f"当前更适合观察，只有重新站回关键区间后再考虑。跌破 {risk_level:.2f} 前后都不适合激进参与。"
            return "Wait", f"This setup is better treated as a watch. Aggressive entries do not look attractive around a break of {risk_level:.2f}."
        if setup_label == "pullback_buy":
            if lang == "zh":
                return "等回踩", (
                    f"更舒服的关注区在 {entry_zone['low']:.2f} - {entry_zone['high']:.2f}。"
                    f" 如果价格在这附近稳住，胜率通常比直接追高更好。"
                )
            return "Buy The Dip", (
                f"The cleaner watch zone is {entry_zone['low']:.2f} - {entry_zone['high']:.2f}. "
                "If price stabilizes there, odds usually look better than chasing at current levels."
            )
        if setup_label == "breakout_watch":
            if lang == "zh":
                return "等突破确认", f"现在离突破位 {breakout_level:.2f} 很近。更好的做法是等放量站上后再跟。"
            return "Wait For Breakout", (
                f"Price is already close to the breakout trigger at {breakout_level:.2f}. Waiting for a volume-confirmed move is cleaner."
            )
        if latest_close >= take_profit_zone["low"]:
            if lang == "zh":
                return "偏向止盈", (
                    f"价格已经靠近模型止盈区 {take_profit_zone['low']:.2f} - {take_profit_zone['high']:.2f}，"
                    " 更适合谨慎兑现部分利润。"
                )
            return "Trim Into Strength", (
                f"Price is already near the model take-profit zone of {take_profit_zone['low']:.2f} - {take_profit_zone['high']:.2f}, "
                "so trimming into strength looks more reasonable."
            )
        if lang == "zh":
            return "继续观察", "结构还没坏，但也没有特别漂亮的新开仓位置，先盯住关键均线和支撑区。"
        return "Hold And Watch", "The structure is not broken, but there is no especially clean new entry yet. Keep watching support and the key moving averages."
