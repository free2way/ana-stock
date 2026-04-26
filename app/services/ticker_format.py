def normalize_ticker_for_market(ticker: str, market: str | None) -> str:
    normalized = ticker.strip().upper()
    market_value = (market or "").strip().upper()

    if not normalized:
        return normalized

    if market_value == "HK":
        core = normalized.replace(".HK", "")
        if core.isdigit():
            return f"{core.zfill(4)}.HK"
        return normalized if normalized.endswith(".HK") else f"{normalized}.HK"

    if market_value == "CN":
        if normalized.endswith(".SH"):
            return f"{normalized[:-3]}.SS"
        if normalized.endswith(".SS") or normalized.endswith(".SZ") or normalized.endswith(".BJ"):
            return normalized
        if normalized.isdigit() and len(normalized) == 6:
            if normalized.startswith(("4", "8", "92")):
                return f"{normalized}.BJ"
            if normalized.startswith(("5", "6")):
                return f"{normalized}.SS"
            return f"{normalized}.SZ"
        return normalized

    return normalized


def infer_market_from_ticker(ticker: str, market: str | None = None) -> str:
    market_value = (market or "").strip().upper()
    if market_value in {"CN", "HK", "US"}:
        return market_value

    normalized = str(ticker or "").strip().upper()
    if normalized.endswith((".SS", ".SH", ".SZ", ".BJ")):
        return "CN"
    if normalized.endswith(".HK"):
        return "HK"
    if normalized.isdigit() and len(normalized) == 6:
        return "CN"
    return "US"


def market_ticker_candidates(ticker: str, market: str | None) -> list[str]:
    normalized = normalize_ticker_for_market(ticker, market)
    market_value = (market or "").strip().upper()
    candidates = [normalized]

    if market_value == "HK" and normalized.endswith(".HK"):
        core = normalized[:-3]
        if core.isdigit():
            raw = core.lstrip("0") or "0"
            four = f"{raw.zfill(4)}.HK"
            five = f"{raw.zfill(5)}.HK"
            for candidate in (four, five):
                if candidate not in candidates:
                    candidates.append(candidate)

    return candidates


def provider_ticker_candidates(ticker: str, market: str | None) -> list[str]:
    candidates = market_ticker_candidates(ticker, market)
    market_value = (market or "").strip().upper()
    normalized = candidates[0] if candidates else normalize_ticker_for_market(ticker, market)
    if market_value == "US" and "." in normalized:
        dashed = normalized.replace(".", "-")
        if dashed not in candidates:
            candidates.append(dashed)
    return candidates
