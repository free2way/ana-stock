from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from time import monotonic

import httpx

from app.core.config import get_settings
from app.services.runtime_cache import get_or_set
from app.services.tushare_client import TushareClient

_CN_INTRADAY_SUCCESS_CACHE_TTL_SECONDS = 120.0
_CN_INTRADAY_SUCCESS_CACHE: dict[str, tuple[float, dict]] = {}


def load_us_latest_trades(tickers: list[str]) -> dict[str, dict]:
    symbols = sorted({str(ticker or "").strip().upper() for ticker in tickers if str(ticker or "").strip()})
    if not symbols:
        return {}
    cache_key = ",".join(symbols)
    return get_or_set("us_latest_trades", cache_key, ttl_seconds=20.0, loader=lambda: _fetch_us_latest_trades(symbols))


def load_us_intraday_bars(ticker: str, *, timeframe: str = "5Min", lookback_hours: int = 8) -> dict:
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        return {"status": "error", "bars": [], "message": "Missing ticker."}
    normalized_timeframe = timeframe if timeframe in {"1Min", "5Min", "15Min", "30Min"} else "5Min"
    cache_key = f"{symbol}:{normalized_timeframe}:{int(lookback_hours)}"
    return get_or_set(
        "us_intraday_bars",
        cache_key,
        ttl_seconds=20.0,
        loader=lambda: _fetch_us_intraday_bars(symbol, timeframe=normalized_timeframe, lookback_hours=lookback_hours),
    )


def load_cn_intraday_bars(ticker: str, *, timeframe: str = "5Min", lookback_hours: int = 8) -> dict:
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        return {"status": "error", "bars": [], "message": "Missing ticker."}
    normalized_timeframe = timeframe if timeframe in {"1Min", "5Min", "15Min", "30Min"} else "5Min"
    cache_key = f"{symbol}:{normalized_timeframe}:{int(lookback_hours)}"
    cached = _CN_INTRADAY_SUCCESS_CACHE.get(cache_key)
    now = monotonic()
    if cached and cached[0] > now:
        return cached[1]
    # A-share minute sources are occasionally bursty. Cache only successful
    # responses so a transient provider hiccup does not make the modal stale.
    payload = _fetch_cn_intraday_bars(symbol, timeframe=normalized_timeframe, lookback_hours=lookback_hours)
    if payload.get("bars"):
        _CN_INTRADAY_SUCCESS_CACHE[cache_key] = (now + _CN_INTRADAY_SUCCESS_CACHE_TTL_SECONDS, payload)
    return payload


def _fetch_cn_intraday_bars(symbol: str, *, timeframe: str, lookback_hours: int) -> dict:
    sina_payload = _fetch_cn_intraday_bars_sina(symbol, timeframe=timeframe)
    if sina_payload.get("bars"):
        return sina_payload

    akshare_payload = _fetch_cn_intraday_bars_akshare(symbol, timeframe=timeframe)
    if akshare_payload.get("bars"):
        return akshare_payload

    eastmoney_payload = _fetch_cn_intraday_bars_eastmoney(symbol, timeframe=timeframe)
    if eastmoney_payload.get("bars"):
        return eastmoney_payload

    client = TushareClient()
    tushare_message = ""
    bars: list[dict] = []
    if not client.is_configured():
        tushare_message = "TuShare token is not configured."
    else:
        try:
            bars = client.fetch_cn_intraday_bars(symbol, timeframe=timeframe, lookback_hours=lookback_hours)
        except Exception as exc:
            tushare_message = str(exc)
    if bars:
        return {
            "status": "success",
            "ticker": symbol,
            "timeframe": timeframe,
            "feed": "tushare",
            "bars": bars,
            "message": f"Loaded {len(bars)} A-share intraday bar(s).",
        }
    return {
        "status": "empty",
        "ticker": symbol,
        "timeframe": timeframe,
        "feed": "tushare",
        "bars": [],
        "message": (
            "No A-share intraday bars returned. Sina/AkShare/Eastmoney returned no bars and TuShare minute "
            f"data may require additional permission. {tushare_message}".strip()
        ),
    }


def _fetch_cn_intraday_bars_sina(symbol: str, *, timeframe: str) -> dict:
    sina_symbol = _sina_cn_symbol(symbol)
    if not sina_symbol:
        return {"status": "unsupported", "ticker": symbol, "bars": [], "message": "Unsupported A-share ticker."}
    period = {"1Min": "1", "5Min": "5", "15Min": "15", "30Min": "30"}.get(timeframe, "5")
    params = {
        "symbol": sina_symbol,
        "scale": period,
        "ma": "no",
        "datalen": "240",
    }
    last_error = ""
    payload = None
    for url in (
        "https://quotes.sina.cn/cn/api/jsonp_v2.php/=/CN_MarketDataService.getKLineData",
        f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_{sina_symbol}_{period}_1658852984203=/CN_MarketDataService.getKLineData",
    ):
        try:
            response = httpx.get(
                url,
                params=params,
                timeout=8.0,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"},
                follow_redirects=True,
            )
            response.raise_for_status()
            text = response.text
            payload = json.loads(text.split("=(", 1)[1].rsplit(");", 1)[0])
            break
        except Exception as exc:
            last_error = str(exc)
            payload = None
    if not isinstance(payload, list):
        return {"status": "empty", "ticker": symbol, "bars": [], "message": f"Sina returned no intraday bars. {last_error}".strip()}
    bars = _parse_cn_minute_rows(payload[-240:], time_key="day")
    return {
        "status": "success" if bars else "empty",
        "ticker": symbol,
        "timeframe": timeframe,
        "feed": "sina",
        "bars": bars,
        "message": f"Loaded {len(bars)} A-share intraday bar(s) from Sina." if bars else "Sina returned no parsable bars.",
    }


def _fetch_cn_intraday_bars_akshare(symbol: str, *, timeframe: str) -> dict:
    ak_symbol = _akshare_cn_symbol(symbol)
    if not ak_symbol:
        return {"status": "unsupported", "ticker": symbol, "bars": [], "message": "Unsupported A-share ticker."}
    period = {"1Min": "1", "5Min": "5", "15Min": "15", "30Min": "30"}.get(timeframe, "5")
    last_error = ""
    minute_df = None
    for _attempt in range(3):
        try:
            import akshare as ak  # type: ignore

            minute_df = ak.stock_zh_a_minute(symbol=ak_symbol, period=period, adjust="")
            if minute_df is not None and not minute_df.empty:
                break
        except Exception as exc:
            last_error = str(exc)
            minute_df = None
    if minute_df is None or minute_df.empty:
        return {"status": "empty", "ticker": symbol, "bars": [], "message": f"AkShare returned no intraday bars. {last_error}".strip()}
    rows = _parse_cn_minute_rows((row.to_dict() for _, row in minute_df.tail(240).iterrows()))
    return {
        "status": "success" if rows else "empty",
        "ticker": symbol,
        "timeframe": timeframe,
        "feed": "akshare",
        "bars": rows,
        "message": f"Loaded {len(rows)} A-share intraday bar(s) from AkShare." if rows else "AkShare returned no parsable bars.",
    }


def _fetch_cn_intraday_bars_eastmoney(symbol: str, *, timeframe: str) -> dict:
    secid = _eastmoney_secid(symbol)
    if not secid:
        return {"status": "unsupported", "ticker": symbol, "bars": [], "message": "Unsupported A-share ticker."}
    klt = {"1Min": "1", "5Min": "5", "15Min": "15", "30Min": "30"}.get(timeframe, "5")
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "klt": klt,
        "fqt": "1",
        "beg": "0",
        "end": "20500101",
        "lmt": "240",
    }
    last_error = ""
    payload = None
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://quote.eastmoney.com/",
    }
    endpoints = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get",
        "http://push2his.eastmoney.com/api/qt/stock/kline/get",
    )
    for endpoint in endpoints:
        for _attempt in range(2):
            try:
                response = httpx.get(
                    endpoint,
                    params=params,
                    timeout=12.0,
                    headers=headers,
                    follow_redirects=True,
                )
                response.raise_for_status()
                payload = response.json()
                break
            except Exception as exc:
                last_error = str(exc)
        if payload is not None:
            break
    if payload is None:
        return {"status": "error", "ticker": symbol, "bars": [], "message": f"Eastmoney intraday fallback failed: {last_error}"}
    klines = ((payload or {}).get("data") or {}).get("klines") if isinstance(payload, dict) else None
    if not isinstance(klines, list):
        return {"status": "empty", "ticker": symbol, "bars": [], "message": "Eastmoney returned no intraday bars."}
    bars: list[dict] = []
    for item in klines[-240:]:
        parts = str(item or "").split(",")
        if len(parts) < 6:
            continue
        try:
            bars.append(
                {
                    "t": parts[0].replace(" ", "T") + "+08:00",
                    "o": float(parts[1]),
                    "c": float(parts[2]),
                    "h": float(parts[3]),
                    "l": float(parts[4]),
                    "v": float(parts[5] or 0),
                }
            )
        except (TypeError, ValueError):
            continue
    return {
        "status": "success" if bars else "empty",
        "ticker": symbol,
        "timeframe": timeframe,
        "feed": "eastmoney",
        "bars": bars,
        "message": f"Loaded {len(bars)} A-share intraday bar(s) from Eastmoney fallback." if bars else "Eastmoney returned no parsable bars.",
    }


def _eastmoney_secid(symbol: str) -> str | None:
    normalized = str(symbol or "").strip().upper()
    code = normalized.split(".", 1)[0]
    if not code.isdigit() or len(code) != 6:
        return None
    if normalized.endswith((".SS", ".SH")) or code.startswith(("5", "6", "9")):
        return f"1.{code}"
    return f"0.{code}"


def _sina_cn_symbol(symbol: str) -> str | None:
    return _akshare_cn_symbol(symbol)


def _akshare_cn_symbol(symbol: str) -> str | None:
    normalized = str(symbol or "").strip().upper()
    code = normalized.split(".", 1)[0]
    if not code.isdigit() or len(code) != 6:
        return None
    if normalized.endswith((".SS", ".SH")) or code.startswith(("5", "6", "9")):
        return f"sh{code}"
    return f"sz{code}"


def _parse_cn_minute_rows(raw_rows, *, time_key: str | None = None) -> list[dict]:
    bars: list[dict] = []
    for raw_row in raw_rows:
        row = raw_row if isinstance(raw_row, dict) else {}
        trade_time = row.get(time_key or "") or row.get("day") or row.get("时间") or row.get("datetime")
        try:
            bars.append(
                {
                    "t": str(trade_time or "").replace(" ", "T") + "+08:00",
                    "o": float(row.get("open") or row.get("开盘")),
                    "h": float(row.get("high") or row.get("最高")),
                    "l": float(row.get("low") or row.get("最低")),
                    "c": float(row.get("close") or row.get("收盘")),
                    "v": float(row.get("volume") or row.get("成交量") or 0),
                }
            )
        except (TypeError, ValueError):
            continue
    return bars


def _fetch_us_intraday_bars(symbol: str, *, timeframe: str, lookback_hours: int) -> dict:
    settings = get_settings()
    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        return {"status": "not_configured", "bars": [], "message": "Alpaca API key is not configured."}
    endpoint = str(settings.alpaca_data_endpoint or "https://data.alpaca.markets/v2").rstrip("/")
    end_at = datetime.now(timezone.utc)
    start_at = end_at - timedelta(hours=max(2, min(int(lookback_hours or 8), 48)))
    params = {
        "timeframe": timeframe,
        "adjustment": "raw",
        "feed": settings.alpaca_data_feed or "iex",
        "start": start_at.isoformat().replace("+00:00", "Z"),
        "end": end_at.isoformat().replace("+00:00", "Z"),
        "limit": 500,
    }
    headers = {
        "APCA-API-KEY-ID": settings.alpaca_api_key or "",
        "APCA-API-SECRET-KEY": settings.alpaca_api_secret or "",
        "Accept": "application/json",
    }
    try:
        response = httpx.get(
            f"{endpoint}/stocks/{symbol}/bars",
            params=params,
            headers=headers,
            timeout=8.0,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return {"status": "error", "bars": [], "message": str(exc)}
    raw_bars = payload.get("bars") if isinstance(payload, dict) else None
    if not isinstance(raw_bars, list):
        return {"status": "empty", "bars": [], "message": "No intraday bars returned."}
    bars: list[dict] = []
    for bar in raw_bars:
        if not isinstance(bar, dict):
            continue
        try:
            bars.append(
                {
                    "t": bar.get("t"),
                    "o": float(bar.get("o")),
                    "h": float(bar.get("h")),
                    "l": float(bar.get("l")),
                    "c": float(bar.get("c")),
                    "v": float(bar.get("v") or 0),
                }
            )
        except (TypeError, ValueError):
            continue
    return {
        "status": "success" if bars else "empty",
        "ticker": symbol,
        "timeframe": timeframe,
        "feed": params["feed"],
        "bars": bars,
        "message": f"Loaded {len(bars)} intraday bar(s).",
    }


def _fetch_us_latest_trades(symbols: list[str]) -> dict[str, dict]:
    settings = get_settings()
    if not settings.alpaca_api_key or not settings.alpaca_api_secret:
        return {}
    endpoint = str(settings.alpaca_data_endpoint or "https://data.alpaca.markets/v2").rstrip("/")
    headers = {
        "APCA-API-KEY-ID": settings.alpaca_api_key or "",
        "APCA-API-SECRET-KEY": settings.alpaca_api_secret or "",
        "Accept": "application/json",
    }
    feed = settings.alpaca_data_feed or "iex"
    output: dict[str, dict] = {}
    for index in range(0, len(symbols), 25):
        output.update(_fetch_us_latest_trades_batch(endpoint, headers, symbols[index : index + 25], feed=feed))
    return output


def _fetch_us_latest_trades_batch(
    endpoint: str,
    headers: dict[str, str],
    symbols: list[str],
    *,
    feed: str,
) -> dict[str, dict]:
    if not symbols:
        return {}
    params = {
        "symbols": ",".join(symbols),
        "feed": feed,
    }
    try:
        response = httpx.get(
            f"{endpoint}/stocks/trades/latest",
            params=params,
            headers=headers,
            timeout=8.0,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        if len(symbols) > 1:
            midpoint = max(1, len(symbols) // 2)
            return {
                **_fetch_us_latest_trades_batch(endpoint, headers, symbols[:midpoint], feed=feed),
                **_fetch_us_latest_trades_batch(endpoint, headers, symbols[midpoint:], feed=feed),
            }
        return {}
    trades = payload.get("trades") if isinstance(payload, dict) else None
    if not isinstance(trades, dict):
        return {}
    output: dict[str, dict] = {}
    for symbol, trade in trades.items():
        if not isinstance(trade, dict):
            continue
        price = trade.get("p")
        try:
            price_value = float(price)
        except (TypeError, ValueError):
            continue
        output[str(symbol).upper()] = {
            "price": price_value,
            "timestamp": trade.get("t"),
            "source": "alpaca_latest_trade",
            "feed": feed,
        }
    return output
