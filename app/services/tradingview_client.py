import json
import subprocess
from dataclasses import dataclass

from app.services.runtime_cache import get_or_set


@dataclass(slots=True)
class TradingViewMarketConfig:
    symbol: str
    screener: str
    exchange: str


class TradingViewClient:
    def get_multi_timeframe_analysis(
        self,
        *,
        ticker: str,
        market: str | None = None,
        exchange: str | None = None,
        intervals: list[str] | None = None,
    ) -> dict | None:
        selected_intervals = intervals or ["1w", "1d", "4h", "1h", "15m"]
        cache_key = json.dumps(
            {
                "ticker": ticker.upper(),
                "market": market,
                "exchange": exchange,
                "intervals": selected_intervals,
            },
            sort_keys=True,
        )

        def _load() -> dict | None:
            ratings: dict[str, dict] = {}
            bullish = 0
            bearish = 0
            neutral = 0
            for interval in selected_intervals:
                payload = self.get_technical_rating(
                    ticker=ticker,
                    market=market,
                    exchange=exchange,
                    interval=interval,
                )
                if payload is None:
                    return None
                ratings[interval] = payload
                recommendation = str(payload.get("recommendation") or "").upper()
                if recommendation in {"BUY", "STRONG_BUY"}:
                    bullish += 1
                elif recommendation in {"SELL", "STRONG_SELL"}:
                    bearish += 1
                else:
                    neutral += 1

            status = "mixed"
            if bullish >= max(3, len(selected_intervals) - 1) and bearish == 0:
                status = "bullish_alignment"
            elif bearish >= max(3, len(selected_intervals) - 1) and bullish == 0:
                status = "bearish_alignment"
            elif bullish > bearish:
                status = "bullish_bias"
            elif bearish > bullish:
                status = "bearish_bias"

            return {
                "ticker": ticker.upper(),
                "market": market,
                "exchange": exchange,
                "status": "success",
                "alignment": status,
                "bullish_count": bullish,
                "bearish_count": bearish,
                "neutral_count": neutral,
                "ratings": ratings,
            }

        return get_or_set("tradingview_multi_timeframe", cache_key, ttl_seconds=90.0, loader=_load)

    def get_technical_rating(
        self,
        *,
        ticker: str,
        market: str | None = None,
        exchange: str | None = None,
        interval: str = "1d",
    ) -> dict | None:
        config = self._resolve_market_config(ticker=ticker, market=market, exchange=exchange)
        if config is None:
            return None

        cache_key = json.dumps(
            {
                "ticker": ticker.upper(),
                "market": market,
                "exchange": exchange or config.exchange,
                "interval": interval,
            },
            sort_keys=True,
        )

        def _load() -> dict:
            try:
                from tradingview_ta import Interval, TA_Handler
            except ImportError as exc:
                raise RuntimeError("tradingview-ta is not installed.") from exc

            tv_interval = self._map_interval(interval, Interval)
            handler = TA_Handler(
                symbol=config.symbol,
                screener=config.screener,
                exchange=config.exchange,
                interval=tv_interval,
            )

            try:
                analysis = handler.get_analysis()
            except Exception as exc:
                try:
                    analysis = self._get_analysis_via_curl(
                        config=config,
                        interval=interval,
                    )
                except Exception as curl_exc:
                    return {
                        "ticker": ticker.upper(),
                        "market": market,
                        "exchange": exchange,
                        "interval": interval,
                        "status": "failed",
                        "message": f"{exc}; curl fallback failed: {curl_exc}",
                    }

            summary = getattr(analysis, "summary", {}) or {}
            oscillators = getattr(analysis, "oscillators", {}) or {}
            moving_averages = getattr(analysis, "moving_averages", {}) or {}
            indicators = getattr(analysis, "indicators", {}) or {}

            return {
                "ticker": ticker.upper(),
                "market": market,
                "exchange": exchange or config.exchange,
                "interval": interval,
                "status": "success",
                "recommendation": summary.get("RECOMMENDATION"),
                "buy_signals": summary.get("BUY"),
                "sell_signals": summary.get("SELL"),
                "neutral_signals": summary.get("NEUTRAL"),
                "oscillator_recommendation": oscillators.get("RECOMMENDATION"),
                "moving_average_recommendation": moving_averages.get("RECOMMENDATION"),
                "indicators": {
                    "RSI": indicators.get("RSI"),
                    "RSI[1]": indicators.get("RSI[1]"),
                    "MACD.macd": indicators.get("MACD.macd"),
                    "MACD.signal": indicators.get("MACD.signal"),
                    "Mom": indicators.get("Mom"),
                    "close": indicators.get("close"),
                },
                "source": getattr(analysis, "_source", "tradingview_ta"),
            }

        return get_or_set("tradingview_rating", cache_key, ttl_seconds=90.0, loader=_load)

    def _get_analysis_via_curl(
        self,
        *,
        config: TradingViewMarketConfig,
        interval: str,
    ):
        try:
            from tradingview_ta.main import TradingView, calculate
        except ImportError as exc:
            raise RuntimeError("tradingview-ta is not installed.") from exc

        symbol = f"{config.exchange}:{config.symbol}"
        indicators_key = TradingView.indicators.copy()
        payload = TradingView.data([symbol], interval, indicators_key)
        scan_url = f"{TradingView.scan_url}{config.screener.lower()}/scan"

        command = [
            "curl",
            "-sS",
            scan_url,
            "-H",
            "content-type: application/json",
            "--data",
            json.dumps(payload, separators=(",", ":")),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or f"curl exited with code {completed.returncode}")

        response = json.loads(completed.stdout or "{}")
        rows = response.get("data") or []
        if not rows:
            raise RuntimeError("TradingView scanner returned no data.")

        values = rows[0].get("d") or []
        indicators = {}
        for index, key in enumerate(indicators_key):
            indicators[key] = values[index] if index < len(values) else None

        analysis = calculate(
            indicators=indicators,
            indicators_key=indicators_key,
            screener=config.screener,
            symbol=config.symbol,
            exchange=config.exchange,
            interval=interval,
        )
        if analysis is None:
            raise RuntimeError("TradingView calculate returned no analysis.")
        analysis._source = "tradingview_curl"
        return analysis

    def _resolve_market_config(
        self,
        *,
        ticker: str,
        market: str | None = None,
        exchange: str | None = None,
    ) -> TradingViewMarketConfig | None:
        upper = ticker.strip().upper()
        normalized_market = (market or self._infer_market(upper) or "").upper()
        normalized_exchange = (exchange or "").upper()

        if normalized_market == "CN":
            if upper.endswith(".SS") or upper.endswith(".SH"):
                return TradingViewMarketConfig(symbol=upper.split(".")[0], screener="china", exchange="SSE")
            if upper.endswith(".SZ"):
                return TradingViewMarketConfig(symbol=upper.split(".")[0], screener="china", exchange="SZSE")
            return None

        if normalized_market == "HK":
            symbol = upper.replace(".HK", "")
            return TradingViewMarketConfig(symbol=symbol, screener="hongkong", exchange=normalized_exchange or "HKEX")

        if normalized_market == "US":
            return TradingViewMarketConfig(
                symbol=upper.split(".")[0],
                screener="america",
                exchange=normalized_exchange or "NASDAQ",
            )

        return None

    def _infer_market(self, ticker: str) -> str | None:
        if ticker.endswith(".HK"):
            return "HK"
        if ticker.endswith((".SS", ".SH", ".SZ")):
            return "CN"
        if "." not in ticker:
            return "US"
        return None

    def _map_interval(self, interval: str, interval_enum) -> str:
        mapping = {
            "1m": interval_enum.INTERVAL_1_MINUTE,
            "5m": interval_enum.INTERVAL_5_MINUTES,
            "15m": interval_enum.INTERVAL_15_MINUTES,
            "1h": interval_enum.INTERVAL_1_HOUR,
            "4h": interval_enum.INTERVAL_4_HOURS,
            "1d": interval_enum.INTERVAL_1_DAY,
            "1w": interval_enum.INTERVAL_1_WEEK,
            "1M": interval_enum.INTERVAL_1_MONTH,
        }
        return mapping.get(interval, interval_enum.INTERVAL_1_DAY)
