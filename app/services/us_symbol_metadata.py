from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.services.openbb_client import OpenBBClient
from app.services.repository import AppSettingRepository, SymbolRepository, utc_now_iso


US_SYMBOL_METADATA_SKIP_KEY = "us_symbol_metadata_skip_tickers"
US_SYMBOL_METADATA_POLYGON_NEXT_URL_KEY = "us_symbol_metadata_polygon_next_url"
POLYGON_REFERENCE_COMMON_TYPES = {"CS", "ADRC", "ADRP", "ADRR"}


def _strip_api_key_from_url(url: str | None) -> str | None:
    raw = str(url or "").strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    query = urlencode([(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key.lower() != "apikey"])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


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


def _fetch_polygon_reference_tickers(
    *,
    limit: int = 1000,
    max_pages: int = 20,
    start_url: str | None = None,
) -> tuple[list[dict], dict]:
    settings = get_settings()
    if not settings.polygon_api_key:
        return [], {"pages_fetched": 0, "next_url": None, "stopped_reason": "not_configured"}
    endpoint = str(settings.polygon_endpoint or "https://api.polygon.io").rstrip("/")
    params = {
        "market": "stocks",
        "active": "true",
        "limit": min(1000, max(1, int(limit))),
        "sort": "ticker",
        "order": "asc",
        "apiKey": settings.polygon_api_key or "",
    }
    url = str(start_url or "").strip() or f"{endpoint}/v3/reference/tickers?{urlencode(params)}"
    if url and "apiKey=" not in url:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}{urlencode({'apiKey': settings.polygon_api_key or ''})}"
    rows: list[dict] = []
    pages = 0
    stopped_reason = "completed"
    while url and pages < max(1, int(max_pages)):
        pages += 1
        try:
            payload = json.loads(urlopen(Request(url, headers={"Accept": "application/json"}), timeout=45).read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code == 429 and rows:
                stopped_reason = "rate_limited"
                break
            raise
        results = payload.get("results") or []
        if isinstance(results, list):
            rows.extend(item for item in results if isinstance(item, dict))
        next_url = str(payload.get("next_url") or "").strip()
        if not next_url:
            url = ""
            break
        separator = "&" if "?" in next_url else "?"
        url = next_url if "apiKey=" in next_url else f"{next_url}{separator}{urlencode({'apiKey': settings.polygon_api_key or ''})}"
    if url and pages >= max(1, int(max_pages)) and stopped_reason == "completed":
        stopped_reason = "page_limit"
    safe_next_url = _strip_api_key_from_url(url)
    return rows, {
        "pages_fetched": pages,
        "next_url": safe_next_url,
        "stopped_reason": stopped_reason,
    }


def refresh_us_symbol_metadata_from_polygon_reference(
    *,
    page_limit: int = 1000,
    max_pages: int = 20,
) -> dict:
    with SessionLocal() as db:
        setting_repo = AppSettingRepository(db)
        start_url = setting_repo.get(US_SYMBOL_METADATA_POLYGON_NEXT_URL_KEY)
    rows, fetch_meta = _fetch_polygon_reference_tickers(limit=page_limit, max_pages=max_pages, start_url=start_url)
    if not rows:
        return {
            "status": "not_configured",
            "message": "Polygon reference tickers returned no rows. Check PQW_POLYGON_API_KEY.",
            "source": "polygon_reference_tickers",
            "rows_returned": 0,
            "updated_count": 0,
        }

    normalized_by_ticker: dict[str, dict] = {}
    type_counts: dict[str, int] = {}
    common_count = 0
    non_common_count = 0
    for item in rows:
        ticker = str(item.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        asset_type = str(item.get("type") or "").strip().upper()
        type_counts[asset_type or "UNKNOWN"] = type_counts.get(asset_type or "UNKNOWN", 0) + 1
        if asset_type in POLYGON_REFERENCE_COMMON_TYPES:
            common_count += 1
        else:
            non_common_count += 1
        candidate = {
            "ticker": ticker,
            "name": item.get("name") or ticker,
            "market": "US",
            "exchange": item.get("primary_exchange") or "",
            "asset_type": asset_type,
            "is_common": asset_type in POLYGON_REFERENCE_COMMON_TYPES,
        }
        existing = normalized_by_ticker.get(ticker)
        if existing is None or (candidate["is_common"] and not existing.get("is_common")):
            normalized_by_ticker[ticker] = candidate
    normalized = list(normalized_by_ticker.values())

    now = utc_now_iso()
    updated_count = 0
    with SessionLocal() as db:
        db.execute(
            text(
                """
                INSERT INTO symbols (ticker, name, market, exchange, sector, industry, is_active, created_at, updated_at)
                SELECT
                    payload.ticker,
                    payload.name,
                    'US',
                    NULLIF(payload.exchange, ''),
                    CASE
                        WHEN payload.is_common THEN NULL
                        ELSE payload.asset_type
                    END,
                    CASE
                        WHEN payload.is_common THEN NULL
                        ELSE payload.asset_type
                    END,
                    1,
                    :now,
                    :now
                FROM jsonb_to_recordset(CAST(:rows_json AS JSONB)) AS payload(
                    ticker TEXT,
                    name TEXT,
                    exchange TEXT,
                    asset_type TEXT,
                    is_common BOOLEAN
                )
                ON CONFLICT (ticker) DO UPDATE SET
                    name = CASE
                        WHEN EXCLUDED.name IS NOT NULL AND (symbols.name IS NULL OR symbols.name = '' OR upper(symbols.name) = upper(symbols.ticker))
                        THEN EXCLUDED.name
                        ELSE symbols.name
                    END,
                    market = 'US',
                    exchange = CASE
                        WHEN EXCLUDED.exchange IS NOT NULL AND EXCLUDED.exchange <> ''
                        THEN EXCLUDED.exchange
                        ELSE symbols.exchange
                    END,
                    sector = CASE
                        WHEN EXCLUDED.sector IS NOT NULL AND EXCLUDED.sector <> '' AND EXCLUDED.sector <> symbols.sector
                        THEN COALESCE(symbols.sector, EXCLUDED.sector)
                        ELSE symbols.sector
                    END,
                    industry = CASE
                        WHEN EXCLUDED.industry IS NOT NULL AND EXCLUDED.industry <> '' AND EXCLUDED.industry <> symbols.industry
                        THEN COALESCE(symbols.industry, EXCLUDED.industry)
                        ELSE symbols.industry
                    END,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            {"rows_json": json.dumps(normalized, ensure_ascii=False), "now": now},
        )
        updated_count = len(normalized)
        db.commit()
        coverage = _us_metadata_coverage(db)
        setting_repo = AppSettingRepository(db)
        if fetch_meta.get("next_url"):
            setting_repo.set(US_SYMBOL_METADATA_POLYGON_NEXT_URL_KEY, str(fetch_meta.get("next_url")))
        else:
            setting_repo.set(US_SYMBOL_METADATA_POLYGON_NEXT_URL_KEY, "")

    return {
        "status": "success",
        "message": (
            f"Polygon reference metadata refreshed {updated_count} U.S. ticker(s): "
            f"{common_count} common/ADR, {non_common_count} non-common."
        ),
        "source": "polygon_reference_tickers",
        "rows_returned": len(rows),
        "updated_count": updated_count,
        "common_count": common_count,
        "non_common_count": non_common_count,
        "type_counts": dict(sorted(type_counts.items(), key=lambda item: (-item[1], item[0]))),
        "fetch_meta": fetch_meta,
        "coverage": coverage,
    }


def refresh_us_symbol_metadata(
    *,
    limit: int = 300,
    only_missing: bool = True,
    tickers: list[str] | None = None,
    prefer_polygon_reference: bool = True,
) -> dict:
    if prefer_polygon_reference and not tickers:
        reference_result = refresh_us_symbol_metadata_from_polygon_reference(max_pages=max(1, (int(limit) + 999) // 1000))
        if reference_result.get("status") == "success":
            return reference_result

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
