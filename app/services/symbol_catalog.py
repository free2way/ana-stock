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


def search_symbol_catalog(query: str, market: str | None = None, limit: int = 8) -> list[dict]:
    text = query.strip().upper()
    market_value = (market or "").strip().upper()
    if not text:
        return []

    results = []
    for item in CATALOG:
        if market_value and item["market"] != market_value:
            continue
        ticker_aliases = market_ticker_candidates(item["ticker"], item["market"])
        if any(text in alias for alias in ticker_aliases) or text in item["name"].upper():
            results.append(item)
        elif any(alias.startswith(text) for alias in ticker_aliases):
            results.append(item)
        if len(results) >= limit:
            break
    return results
