from app.services.ticker_format import market_ticker_candidates, normalize_ticker_for_market


CATALOG = [
    {"ticker": "ASTS", "name": "AST SpaceMobile", "market": "US", "exchange": "NASDAQ"},
    {"ticker": "AAPL", "name": "Apple", "market": "US", "exchange": "NASDAQ"},
    {"ticker": "MSFT", "name": "Microsoft", "market": "US", "exchange": "NASDAQ"},
    {"ticker": "NVDA", "name": "NVIDIA", "market": "US", "exchange": "NASDAQ"},
    {"ticker": "TSLA", "name": "Tesla", "market": "US", "exchange": "NASDAQ"},
    {"ticker": "600519.SS", "name": "贵州茅台", "market": "CN", "exchange": "SSE"},
    {"ticker": "000001.SZ", "name": "平安银行", "market": "CN", "exchange": "SZSE"},
    {"ticker": "600330.SS", "name": "天通股份", "market": "CN", "exchange": "SSE"},
    {"ticker": "603778.SS", "name": "国晟科技", "market": "CN", "exchange": "SSE"},
    {"ticker": "002364.SZ", "name": "中恒电气", "market": "CN", "exchange": "SZSE"},
    {"ticker": "0700.HK", "name": "腾讯控股", "market": "HK", "exchange": "HKEX"},
    {"ticker": "9988.HK", "name": "阿里巴巴-W", "market": "HK", "exchange": "HKEX"},
    {"ticker": "0100.HK", "name": "MINIMAX-W", "market": "HK", "exchange": "HKEX"},
    {"ticker": "0883.HK", "name": "中国海洋石油", "market": "HK", "exchange": "HKEX"},
    {"ticker": "1378.HK", "name": "中国宏桥", "market": "HK", "exchange": "HKEX"},
]


def infer_symbol_name(ticker: str, market: str | None) -> str | None:
    record = infer_symbol_record(ticker, market)
    return record["name"] if record else None


def infer_symbol_record(ticker: str, market: str | None) -> dict | None:
    normalized = normalize_ticker_for_market(ticker, market)
    candidates = market_ticker_candidates(normalized, market)
    for item in CATALOG:
        if item["ticker"] in candidates:
            return item
    return None


def _query_ticker_candidates(query: str, market: str | None) -> set[str]:
    text = query.strip().upper()
    market_value = (market or "").strip().upper()
    candidates = set(market_ticker_candidates(text, market_value))
    if market_value == "US":
        if text.endswith(".US"):
            candidates.add(text[:-3])
        elif text and "." not in text:
            candidates.add(f"{text}.US")
    return {item for item in candidates if item}


def search_symbol_catalog(query: str, market: str | None = None, limit: int = 8) -> list[dict]:
    text = query.strip().upper()
    market_value = (market or "").strip().upper()
    if not text:
        return []

    ticker_candidates = _query_ticker_candidates(text, market_value)
    ranked = []
    for item in CATALOG:
        if market_value and item["market"] != market_value:
            continue
        ticker_aliases = market_ticker_candidates(item["ticker"], item["market"])
        alias_set = {alias.upper() for alias in ticker_aliases}
        name_text = item["name"].upper()
        rank = None
        if alias_set & ticker_candidates:
            rank = 0
        elif any(alias.startswith(text) for alias in alias_set):
            rank = 1
        elif any(text in alias for alias in alias_set):
            rank = 2
        elif len(text) >= 3 and text in name_text:
            rank = 3
        if rank is not None:
            ranked.append((rank, item["ticker"], item))
    ranked.sort(key=lambda row: (row[0], row[1]))
    return [item for _, _, item in ranked[: max(1, int(limit))]]


def search_symbol_records(
    symbols,
    query: str,
    market: str | None = None,
    *,
    initial: list[dict] | None = None,
    limit: int = 8,
) -> list[dict]:
    text = query.strip().upper()
    market_value = (market or "").strip().upper()
    results = list(initial or [])
    seen = {(str(item.get("ticker") or ""), str(item.get("market") or "")) for item in results}
    if not text:
        return results[: max(1, int(limit))]

    ticker_candidates = _query_ticker_candidates(text, market_value)
    ranked: list[tuple[int, str, dict]] = []
    for symbol in symbols:
        symbol_market = (symbol.market or "").upper()
        if market_value and symbol_market != market_value:
            continue
        ticker = (symbol.ticker or "").upper()
        if not ticker:
            continue
        key = (ticker, symbol.market)
        if key in seen:
            continue
        name = symbol.name or ""
        rank = None
        if ticker in ticker_candidates:
            rank = 0
        elif ticker.startswith(text):
            rank = 1
        elif text in ticker:
            rank = 2
        elif len(text) >= 3 and text in name.upper():
            rank = 3
        if rank is None:
            continue
        ranked.append(
            (
                rank,
                ticker,
                {
                    "ticker": symbol.ticker,
                    "name": name or symbol.ticker,
                    "market": symbol.market or market_value or "",
                    "exchange": symbol.exchange or "",
                },
            )
        )
    ranked.sort(key=lambda row: (row[0], row[1]))
    for _, _, item in ranked:
        key = (item["ticker"], item["market"])
        if key in seen:
            continue
        results.append(item)
        seen.add(key)
        if len(results) >= limit:
            break
    return results[: max(1, int(limit))]
