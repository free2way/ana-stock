from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.core.config import get_settings
from app.services.market_lake import load_lake_price_history
from app.services.ticker_format import market_ticker_candidates


@dataclass(slots=True)
class TechnicalPatternSnapshot:
    ticker: str
    as_of_date: str | None
    limit_up_yesterday: bool = False
    volume_breakout: bool = False
    ma_cluster: bool = False
    bullish_ma_stack: bool = False
    macd_underwater_cross: bool = False
    bollinger_squeeze: bool = False
    three_white_soldiers: bool = False
    bullish_engulfing: bool = False
    hammer_reversal: bool = False
    matched_patterns: list[str] | None = None


class TechnicalPatternService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def evaluate_ticker(self, ticker: str) -> TechnicalPatternSnapshot | None:
        frame = self._load_price_frame(ticker)
        if frame is None or len(frame) < 35:
            return None

        enriched = self._build_indicators(frame)
        latest = enriched.iloc[-1]
        previous = enriched.iloc[-2] if len(enriched) >= 2 else None
        matched: list[str] = []

        limit_up_yesterday = self._is_limit_up_yesterday(enriched)
        if limit_up_yesterday:
            matched.append("昨日涨停")

        volume_breakout = self._is_volume_breakout(enriched)
        if volume_breakout:
            matched.append("底部放量突破")

        ma_cluster = self._is_ma_cluster(latest)
        if ma_cluster:
            matched.append("均线密集缠绕")

        bullish_ma_stack = self._is_bullish_ma_stack(enriched)
        if bullish_ma_stack:
            matched.append("均线多头排列")

        macd_underwater_cross = self._is_macd_underwater_cross(latest, previous)
        if macd_underwater_cross:
            matched.append("MACD水下金叉")

        bollinger_squeeze = self._is_bollinger_squeeze(enriched)
        if bollinger_squeeze:
            matched.append("布林带收口")

        three_white_soldiers = self._is_three_white_soldiers(enriched)
        if three_white_soldiers:
            matched.append("三连阳")

        bullish_engulfing = self._is_bullish_engulfing(enriched)
        if bullish_engulfing:
            matched.append("看涨吞没")

        hammer_reversal = self._is_hammer_reversal(enriched)
        if hammer_reversal:
            matched.append("锤子线")

        return TechnicalPatternSnapshot(
            ticker=ticker,
            as_of_date=str(latest.get("date") or latest.name),
            limit_up_yesterday=limit_up_yesterday,
            volume_breakout=volume_breakout,
            ma_cluster=ma_cluster,
            bullish_ma_stack=bullish_ma_stack,
            macd_underwater_cross=macd_underwater_cross,
            bollinger_squeeze=bollinger_squeeze,
            three_white_soldiers=three_white_soldiers,
            bullish_engulfing=bullish_engulfing,
            hammer_reversal=hammer_reversal,
            matched_patterns=matched,
        )

    def get_bollinger_band_analysis(self, ticker: str) -> dict | None:
        frame = self._load_price_frame(ticker)
        if frame is None or len(frame) < 25:
            return None
        enriched = self._build_indicators(frame)
        latest = enriched.iloc[-1]
        required = ("close", "bb_mid", "bb_upper", "bb_lower", "bb_width_pct")
        if any(pd.isna(latest.get(field)) for field in required):
            return None

        close_value = float(latest.get("close"))
        mid_value = float(latest.get("bb_mid"))
        upper_value = float(latest.get("bb_upper"))
        lower_value = float(latest.get("bb_lower"))
        width_pct = float(latest.get("bb_width_pct")) * 100.0
        band_span = max(upper_value - lower_value, 1e-9)
        position = (close_value - lower_value) / band_span

        rating = 0
        signal = "neutral"
        if close_value >= upper_value:
            rating, signal = 3, "strongly_overbought"
        elif position >= 0.82:
            rating, signal = 2, "upper_band_strength"
        elif position >= 0.62:
            rating, signal = 1, "bullish_bias"
        elif close_value <= lower_value:
            rating, signal = -3, "strongly_oversold"
        elif position <= 0.18:
            rating, signal = -2, "lower_band_pressure"
        elif position <= 0.38:
            rating, signal = -1, "bearish_bias"

        squeeze = self._is_bollinger_squeeze(enriched)
        return {
            "ticker": ticker.upper(),
            "as_of_date": str(latest.get("date") or latest.name),
            "status": "success",
            "rating": rating,
            "signal": signal,
            "close": close_value,
            "middle_band": round(mid_value, 4),
            "upper_band": round(upper_value, 4),
            "lower_band": round(lower_value, 4),
            "bandwidth_pct": round(width_pct, 2),
            "band_position_pct": round(position * 100.0, 2),
            "squeeze": squeeze,
        }

    def get_candlestick_patterns(self, ticker: str) -> dict | None:
        snapshot = self.evaluate_ticker(ticker)
        if snapshot is None:
            return None
        all_patterns = list(snapshot.matched_patterns or [])
        candlestick_patterns = [
            pattern
            for pattern in all_patterns
            if pattern in {"三连阳", "看涨吞没", "锤子线"}
        ]
        return {
            "ticker": snapshot.ticker,
            "as_of_date": snapshot.as_of_date,
            "status": "success",
            "patterns": candlestick_patterns,
            "all_patterns": all_patterns,
        }

    def _load_price_frame(self, ticker: str) -> pd.DataFrame | None:
        candidate_paths = [
            self.settings.normalized_data_dir / f"{ticker}.csv",
            self.settings.raw_data_dir / f"{ticker}.csv",
        ]
        path = next((item for item in candidate_paths if item.exists()), None)
        if path is not None:
            try:
                frame = pd.read_csv(path)
            except Exception:
                return None
        else:
            rows = []
            for market, symbol in self._lake_candidates(ticker):
                rows = load_lake_price_history(market=market, ticker=symbol, limit=240)
                if rows:
                    break
            if not rows:
                return None
            frame = pd.DataFrame(rows)
        if frame.empty or "date" not in frame.columns or "close" not in frame.columns:
            return None
        if "volume" not in frame.columns:
            frame["volume"] = 0.0
        if "high" not in frame.columns:
            frame["high"] = frame["close"]
        if "low" not in frame.columns:
            frame["low"] = frame["close"]
        if "open" not in frame.columns:
            frame["open"] = frame["close"]
        return frame.sort_values("date").reset_index(drop=True)

    def _lake_candidates(self, ticker: str) -> list[tuple[str, str]]:
        upper = ticker.upper().strip()
        if not upper:
            return []
        if upper.endswith((".SS", ".SZ", ".SH", ".BJ")) or (upper.isdigit() and len(upper) == 6):
            return [("CN", candidate) for candidate in market_ticker_candidates(upper, "CN")]
        if upper.endswith(".HK"):
            return []
        return [("US", upper)]

    def _build_indicators(self, frame: pd.DataFrame) -> pd.DataFrame:
        data = frame.copy()
        data["ma5"] = data["close"].rolling(5).mean()
        data["ma10"] = data["close"].rolling(10).mean()
        data["ma20"] = data["close"].rolling(20).mean()
        data["ma60"] = data["close"].rolling(60).mean()
        data["vol_ma20"] = data["volume"].rolling(20).mean()
        data["ret"] = data["close"].pct_change()
        data["low_60"] = data["low"].rolling(60, min_periods=20).min()
        data["high_20_prev"] = data["high"].shift(1).rolling(20, min_periods=10).max()
        data["bb_mid"] = data["close"].rolling(20).mean()
        data["bb_std"] = data["close"].rolling(20).std(ddof=0)
        data["bb_upper"] = data["bb_mid"] + 2 * data["bb_std"]
        data["bb_lower"] = data["bb_mid"] - 2 * data["bb_std"]
        data["bb_width_pct"] = (data["bb_upper"] - data["bb_lower"]) / data["bb_mid"].replace(0, pd.NA)
        data["candle_body"] = (data["close"] - data["open"]).abs()
        data["candle_range"] = (data["high"] - data["low"]).replace(0, pd.NA)

        ema12 = data["close"].ewm(span=12, adjust=False).mean()
        ema26 = data["close"].ewm(span=26, adjust=False).mean()
        data["dif"] = ema12 - ema26
        data["dea"] = data["dif"].ewm(span=9, adjust=False).mean()
        data["macd_hist"] = (data["dif"] - data["dea"]) * 2
        return data

    def _is_limit_up_yesterday(self, frame: pd.DataFrame) -> bool:
        if len(frame) < 3:
            return False
        prev = frame.iloc[-2]
        prev_prev = frame.iloc[-3]
        prev_close = float(prev_prev.get("close") or 0)
        current_close = float(prev.get("close") or 0)
        if prev_close <= 0:
            return False
        pct = (current_close / prev_close - 1.0) * 100.0
        return pct >= 9.8

    def _is_volume_breakout(self, frame: pd.DataFrame) -> bool:
        if len(frame) < 25:
            return False
        latest = frame.iloc[-1]
        volume = float(latest.get("volume") or 0)
        vol_ma20 = float(latest.get("vol_ma20") or 0)
        high_20_prev = float(latest.get("high_20_prev") or 0)
        close = float(latest.get("close") or 0)
        low_60 = float(latest.get("low_60") or 0)
        if vol_ma20 <= 0 or high_20_prev <= 0 or low_60 <= 0:
            return False
        is_breakout = close >= high_20_prev * 1.01
        volume_expansion = volume >= vol_ma20 * 1.6
        base_not_extended = close <= low_60 * 1.35
        return is_breakout and volume_expansion and base_not_extended

    def _is_ma_cluster(self, latest: pd.Series) -> bool:
        values = [latest.get("ma5"), latest.get("ma10"), latest.get("ma20")]
        if any(pd.isna(value) for value in values):
            return False
        ma_values = [float(value) for value in values]
        spread = max(ma_values) - min(ma_values)
        baseline = max(sum(ma_values) / len(ma_values), 1e-9)
        return (spread / baseline) <= 0.03

    def _is_bullish_ma_stack(self, frame: pd.DataFrame) -> bool:
        if len(frame) < 65:
            return False
        latest = frame.iloc[-1]
        ma_values = [latest.get("ma5"), latest.get("ma10"), latest.get("ma20"), latest.get("ma60")]
        if any(pd.isna(value) for value in ma_values):
            return False
        ma5, ma10, ma20, ma60 = [float(value) for value in ma_values]
        if not (ma5 > ma10 > ma20 > ma60):
            return False
        earlier_ma20 = frame.iloc[-6].get("ma20")
        if pd.isna(earlier_ma20):
            return False
        return ma20 > float(earlier_ma20)

    def _is_macd_underwater_cross(self, latest: pd.Series, previous: pd.Series | None) -> bool:
        if previous is None:
            return False
        latest_dif = latest.get("dif")
        latest_dea = latest.get("dea")
        previous_dif = previous.get("dif")
        previous_dea = previous.get("dea")
        if any(pd.isna(value) for value in (latest_dif, latest_dea, previous_dif, previous_dea)):
            return False
        latest_dif = float(latest_dif)
        latest_dea = float(latest_dea)
        previous_dif = float(previous_dif)
        previous_dea = float(previous_dea)
        return previous_dif <= previous_dea and latest_dif > latest_dea and latest_dif < 0 and latest_dea < 0

    def _is_bollinger_squeeze(self, frame: pd.DataFrame) -> bool:
        if len(frame) < 25:
            return False
        latest = frame.iloc[-1]
        bb_width_pct = latest.get("bb_width_pct")
        ma20 = latest.get("ma20")
        close = latest.get("close")
        if any(pd.isna(value) for value in (bb_width_pct, ma20, close)):
            return False
        recent_width = frame["bb_width_pct"].dropna().tail(20)
        if recent_width.empty:
            return False
        width_value = float(bb_width_pct)
        trailing_window = recent_width.tail(5)
        if trailing_window.empty:
            return False
        recent_min = float(trailing_window.min())
        return recent_min <= 0.04 and width_value <= 0.08 and float(close) >= float(ma20) * 0.98

    def _is_three_white_soldiers(self, frame: pd.DataFrame) -> bool:
        if len(frame) < 5:
            return False
        latest_three = frame.tail(3)
        previous_close = float(frame.iloc[-4].get("close") or 0)
        closes: list[float] = []
        for _, row in latest_three.iterrows():
            open_value = row.get("open")
            close_value = row.get("close")
            body = row.get("candle_body")
            candle_range = row.get("candle_range")
            if any(pd.isna(value) for value in (open_value, close_value, body, candle_range)):
                return False
            open_numeric = float(open_value)
            close_numeric = float(close_value)
            if close_numeric <= open_numeric:
                return False
            if float(candle_range) <= 0:
                return False
            if float(body) / float(candle_range) < 0.45:
                return False
            closes.append(close_numeric)
        if not (closes[0] > previous_close and closes[1] > closes[0] and closes[2] > closes[1]):
            return False
        return True

    def _is_bullish_engulfing(self, frame: pd.DataFrame) -> bool:
        if len(frame) < 3:
            return False
        previous = frame.iloc[-2]
        latest = frame.iloc[-1]
        required = ("open", "close", "candle_body", "candle_range")
        if any(pd.isna(previous.get(field)) for field in required):
            return False
        if any(pd.isna(latest.get(field)) for field in required):
            return False
        prev_open = float(previous.get("open"))
        prev_close = float(previous.get("close"))
        latest_open = float(latest.get("open"))
        latest_close = float(latest.get("close"))
        if prev_close >= prev_open:
            return False
        if latest_close <= latest_open:
            return False
        if latest_open > prev_close or latest_close < prev_open:
            return False
        latest_body = float(latest.get("candle_body") or 0)
        previous_body = float(previous.get("candle_body") or 0)
        return latest_body >= max(previous_body * 1.05, 0.01)

    def _is_hammer_reversal(self, frame: pd.DataFrame) -> bool:
        if len(frame) < 6:
            return False
        latest = frame.iloc[-1]
        open_value = latest.get("open")
        close_value = latest.get("close")
        high_value = latest.get("high")
        low_value = latest.get("low")
        body = latest.get("candle_body")
        candle_range = latest.get("candle_range")
        if any(pd.isna(value) for value in (open_value, close_value, high_value, low_value, body, candle_range)):
            return False
        open_numeric = float(open_value)
        close_numeric = float(close_value)
        high_numeric = float(high_value)
        low_numeric = float(low_value)
        body_numeric = float(body)
        range_numeric = float(candle_range)
        if range_numeric <= 0 or body_numeric <= 0:
            return False
        lower_shadow = min(open_numeric, close_numeric) - low_numeric
        upper_shadow = high_numeric - max(open_numeric, close_numeric)
        recent_closes = [float(value) for value in frame["close"].tail(5).tolist()]
        if len(recent_closes) < 5:
            return False
        prior_weakness = recent_closes[-2] <= min(recent_closes[:-2])
        return lower_shadow >= body_numeric * 2.0 and upper_shadow <= body_numeric * 0.5 and prior_weakness
