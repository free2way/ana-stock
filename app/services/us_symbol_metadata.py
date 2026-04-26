from __future__ import annotations

import json

from app.core.db import SessionLocal
from app.services.openbb_client import OpenBBClient
from app.services.repository import AppSettingRepository, SymbolRepository


US_SYMBOL_METADATA_SKIP_KEY = "us_symbol_metadata_skip_tickers"


def _is_supported_us_ticker(ticker: str) -> bool:
    upper = str(ticker or "").strip().upper()
    if not upper or upper.endswith((".HK", ".SS", ".SZ", ".SH", ".BJ")):
        return False
    if upper.endswith((".U", ".WS", ".W", ".R", ".RT", ".WT")):
        return False
    if "." not in upper and "-" not in upper and len(upper) == 5 and upper[-1] in {"W", "R", "U"}:
        return False
    if "." not in upper and "-" not in upper and len(upper) >= 5 and upper[-2:] in {
        "PA",
        "PB",
        "PC",
        "PD",
        "PE",
        "PF",
        "PG",
        "PH",
        "PI",
        "PJ",
        "PK",
        "PL",
        "PM",
        "PN",
        "PO",
        "PP",
    }:
        return False
    return True


def _load_skip_tickers(setting_repo: AppSettingRepository) -> set[str]:
    raw = setting_repo.get(US_SYMBOL_METADATA_SKIP_KEY)
    if not raw:
        return set()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    if not isinstance(payload, list):
        return set()
    return {str(item).strip().upper() for item in payload if str(item).strip()}


def _save_skip_tickers(setting_repo: AppSettingRepository, values: set[str]) -> None:
    setting_repo.set(US_SYMBOL_METADATA_SKIP_KEY, json.dumps(sorted(values), ensure_ascii=False))


def _add_skip_ticker(setting_repo: AppSettingRepository, skip_tickers: set[str], ticker: str) -> None:
    normalized = str(ticker or "").strip().upper()
    if not normalized or normalized in skip_tickers:
        return
    skip_tickers.add(normalized)
    _save_skip_tickers(setting_repo, skip_tickers)


def _us_metadata_coverage(db) -> dict:
    symbols = SymbolRepository(db).list_symbols_for_metadata_refresh(market="US", limit=1_000_000, only_missing=False)
    total = len(symbols)
    missing_sector = sum(1 for symbol in symbols if not symbol.sector)
    missing_industry = sum(1 for symbol in symbols if not symbol.industry)
    weak_name = sum(1 for symbol in symbols if not symbol.name or symbol.name.upper() == symbol.ticker.upper())
    return {
        "total": total,
        "missing_sector": missing_sector,
        "missing_industry": missing_industry,
        "weak_name": weak_name,
        "all_complete": total > 0 and missing_sector == 0 and missing_industry == 0 and weak_name == 0,
    }


def refresh_us_symbol_metadata(
    *,
    limit: int = 300,
    only_missing: bool = True,
    tickers: list[str] | None = None,
) -> dict:
    normalized_tickers = [str(ticker or "").strip().upper() for ticker in (tickers or []) if str(ticker or "").strip()]
    client = OpenBBClient()
    updated_count = 0
    success_count = 0
    failure_count = 0
    skipped_count = 0
    provider_counts: dict[str, int] = {}
    examples: list[dict] = []

    with SessionLocal() as db:
        symbol_repo = SymbolRepository(db)
        setting_repo = AppSettingRepository(db)
        skip_tickers = _load_skip_tickers(setting_repo)
        if normalized_tickers:
            symbols = [symbol_repo.get_by_ticker(ticker) for ticker in normalized_tickers]
            symbols = [symbol for symbol in symbols if symbol is not None and (symbol.market or "").upper() == "US"]
        else:
            symbols = symbol_repo.list_symbols_for_metadata_refresh(
                market="US",
                limit=max(1, int(limit)),
                only_missing=only_missing,
            )
        for symbol in symbols:
            ticker = symbol.ticker.upper()
            if ticker in skip_tickers or not _is_supported_us_ticker(ticker):
                if ticker not in skip_tickers and not _is_supported_us_ticker(ticker):
                    _add_skip_ticker(setting_repo, skip_tickers, ticker)
                skipped_count += 1
                continue
            before = {
                "name": symbol.name,
                "exchange": symbol.exchange,
                "sector": symbol.sector,
                "industry": symbol.industry,
            }
            try:
                profile = client.fetch_symbol_profile(ticker)
            except Exception as exc:
                message = str(exc)
                if "quote not found" in message.lower() or "404" in message.lower():
                    _add_skip_ticker(setting_repo, skip_tickers, ticker)
                    skipped_count += 1
                else:
                    failure_count += 1
                if len(examples) < 8:
                    examples.append({"ticker": ticker, "status": "failed", "message": message})
                continue
            if not any(profile.get(key) for key in ("name", "exchange", "sector", "industry")):
                _add_skip_ticker(setting_repo, skip_tickers, ticker)
                skipped_count += 1
                continue
            updated_symbol = symbol_repo.update_symbol_metadata(
                symbol.id,
                name=profile.get("name"),
                market="US",
                exchange=profile.get("exchange"),
                sector=profile.get("sector"),
                industry=profile.get("industry"),
                overwrite_name=bool(profile.get("name")) and (not symbol.name or symbol.name.upper() == symbol.ticker.upper()),
                overwrite_exchange=bool(profile.get("exchange")),
                overwrite_sector=bool(profile.get("sector")),
                overwrite_industry=bool(profile.get("industry")),
            )
            success_count += 1
            provider = str(client.last_source_used or "unknown").strip() or "unknown"
            provider_counts[provider] = provider_counts.get(provider, 0) + 1
            after = {
                "name": updated_symbol.name if updated_symbol else before["name"],
                "exchange": updated_symbol.exchange if updated_symbol else before["exchange"],
                "sector": updated_symbol.sector if updated_symbol else before["sector"],
                "industry": updated_symbol.industry if updated_symbol else before["industry"],
            }
            if after != before:
                updated_count += 1
            if len(examples) < 8:
                examples.append(
                    {
                        "ticker": ticker,
                        "provider": provider,
                        "sector": after.get("sector"),
                        "industry": after.get("industry"),
                        "name": after.get("name"),
                    }
                )
        _save_skip_tickers(setting_repo, skip_tickers)
        coverage = _us_metadata_coverage(db)

    status = "success" if failure_count == 0 else "partial" if success_count else "failed"
    target_count = len(normalized_tickers) if normalized_tickers else max(1, int(limit))
    if coverage.get("all_complete"):
        message = "U.S. symbol metadata is fully backfilled."
    else:
        message = (
            f"U.S. symbol metadata refreshed: {updated_count} updated, {success_count} fetched, "
            f"{skipped_count} skipped, {failure_count} failed."
        )
    return {
        "status": status,
        "message": message,
        "market": "US",
        "requested_count": target_count,
        "processed_count": success_count + skipped_count + failure_count,
        "updated_count": updated_count,
        "success_count": success_count,
        "skipped_count": skipped_count,
        "failure_count": failure_count,
        "only_missing": only_missing,
        "provider_counts": provider_counts,
        "coverage": coverage,
        "all_complete": bool(coverage.get("all_complete")),
        "examples": examples,
    }
