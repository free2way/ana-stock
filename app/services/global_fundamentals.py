from dataclasses import asdict, dataclass

from app.core.db import SessionLocal
from app.models.schema import SymbolCreate
from app.services.providers import resolve_fundamental_provider
from app.services.repository import FundamentalSnapshotRepository, SymbolRepository, WatchlistRepository
from app.services.ticker_format import normalize_ticker_for_market


@dataclass(slots=True)
class GlobalFundamentalRow:
    ticker: str
    market: str
    report_date: str
    source: str = "openbb_fundamentals"
    name: str | None = None
    exchange: str | None = None
    listing_date: str | None = None
    pe_ttm: float | None = None
    dividend_yield: float | None = None
    market_cap: float | None = None
    roe_avg_3y: float | None = None
    net_profit_yoy: float | None = None
    revenue_yoy: float | None = None
    debt_to_assets: float | None = None
    raw_data: dict | None = None


def _priority_us_tickers() -> list[str]:
    """Use the watchlist as the default scope for public official endpoints.

    EDGAR is excellent for enrichment, but it is not a replacement for a
    commercial fundamental bulk feed.  A bounded priority scope makes manual
    maintenance predictable and respects the source's fair-access guidance.
    """
    with SessionLocal() as db:
        watchlist = WatchlistRepository(db).get_or_create_default()
        rows = WatchlistRepository(db).list_items(watchlist.id)
    return sorted(
        {
            str(row.get("ticker") or "").strip().upper()
            for row in rows
            if str(row.get("market") or "").strip().upper() == "US" and str(row.get("ticker") or "").strip()
        }
    )


def sync_global_fundamentals(tickers: list[str] | None = None, *, provider_name: str = "openbb") -> dict:
    normalized_tickers = [_normalize_any_ticker(ticker) for ticker in (tickers or []) if ticker.strip()]
    normalized_provider = str(provider_name or "openbb").strip().lower()
    if not normalized_tickers and normalized_provider in {"global_stock_data", "global_stock_data_sec", "sec", "sec_edgar"}:
        normalized_tickers = _priority_us_tickers()
    if not normalized_tickers:
        return {
            "status": "empty",
            "message": (
                "No eligible U.S. watchlist tickers were found for SEC enrichment."
                if normalized_provider in {"global_stock_data", "global_stock_data_sec", "sec", "sec_edgar"}
                else "No US/HK tickers were provided for global fundamental sync."
            ),
            "rows_written": 0,
            "tickers": [],
        }

    rows: list[GlobalFundamentalRow] = []
    failures: list[str] = []
    provider_sources: set[str] = set()
    for ticker in normalized_tickers:
        market = _infer_market(ticker)
        provider = resolve_fundamental_provider(normalized_provider, market=market)
        snapshot = provider.fetch_snapshot(ticker)
        provider_sources.add(getattr(provider, "last_source_used", "openbb"))
        if not snapshot:
            failures.append(ticker)
            continue
        rows.append(
            GlobalFundamentalRow(
                ticker=ticker,
                market=_infer_market(ticker),
                report_date=snapshot["report_date"],
                source=getattr(provider, "last_source_used", "openbb_fundamentals") or "openbb_fundamentals",
                name=snapshot.get("name"),
                exchange=snapshot.get("exchange"),
                listing_date=snapshot.get("listing_date"),
                pe_ttm=snapshot.get("pe_ttm"),
                dividend_yield=snapshot.get("dividend_yield"),
                market_cap=snapshot.get("market_cap"),
                roe_avg_3y=snapshot.get("roe_avg_3y"),
                net_profit_yoy=snapshot.get("net_profit_yoy"),
                revenue_yoy=snapshot.get("revenue_yoy"),
                debt_to_assets=snapshot.get("debt_to_assets"),
                raw_data=snapshot.get("raw_data"),
            )
        )

    if not rows:
        not_configured = provider_sources == {"sec_edgar_not_configured"}
        return {
            "status": "not_configured" if not_configured else "empty",
            "message": (
                "Set PQW_SEC_USER_AGENT to a real name and contact email before using the official SEC EDGAR source."
                if not_configured
                else "No US/HK fundamental rows returned from the selected provider."
            ),
            "rows_written": 0,
            "tickers": normalized_tickers,
            "failed_tickers": failures,
        }

    written = 0
    touched: list[str] = []
    with SessionLocal() as db:
        symbol_repo = SymbolRepository(db)
        fundamental_repo = FundamentalSnapshotRepository(db)
        for row in rows:
            symbol = symbol_repo.get_or_create_symbol(
                SymbolCreate(
                    ticker=row.ticker,
                    name=row.name,
                    market=row.market,
                    exchange=row.exchange,
                )
            )
            fundamental_repo.upsert_snapshot(
                symbol_id=symbol.id,
                report_date=row.report_date,
                source=row.source,
                listing_date=row.listing_date,
                pe_ttm=row.pe_ttm,
                dividend_yield=row.dividend_yield,
                market_cap=row.market_cap,
                roe_avg_3y=row.roe_avg_3y,
                net_profit_yoy=row.net_profit_yoy,
                revenue_yoy=row.revenue_yoy,
                debt_to_assets=row.debt_to_assets,
                data=row.raw_data or asdict(row),
            )
            written += 1
            if row.ticker not in touched:
                touched.append(row.ticker)

    status = "success" if not failures else "partial"
    failure_text = f" {len(failures)} failed." if failures else ""
    provider_text = ", ".join(sorted(provider_sources)) if provider_sources else "unknown"
    return {
        "status": status,
        "message": f"Synced {written} US/HK fundamental row(s) for {len(touched)} stock(s) via {provider_text}.{failure_text}",
        "rows_written": written,
        "tickers": touched,
        "failed_tickers": failures,
    }


def _infer_market(ticker: str) -> str:
    upper = ticker.upper()
    if upper.endswith(".HK"):
        return "HK"
    if upper.endswith(".SS") or upper.endswith(".SZ") or upper.endswith(".SH"):
        return "CN"
    return "US"


def _normalize_any_ticker(ticker: str) -> str:
    upper = ticker.strip().upper()
    if upper.endswith(".HK"):
        return normalize_ticker_for_market(upper, "HK")
    if upper.endswith(".SS") or upper.endswith(".SZ") or upper.endswith(".SH"):
        return normalize_ticker_for_market(upper, "CN")
    return upper
