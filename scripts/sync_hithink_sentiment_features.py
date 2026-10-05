"""Fetch public HiThink featured-data/auction endpoints into the feature store.

Writes only to ``<artifacts_dir>/hithink_features`` (never the main CN price lake)
with provider/source_reference/fetched_at provenance and a content hash. Feature
construction is left to :mod:`app.services.stock_selection.sentiment_features`;
this script is a thin operational entry point around the client wrappers.

Example::

    python scripts/sync_hithink_sentiment_features.py --trade-date 2026-09-30 \
        --tickers 600519.SS,000001.SZ
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.services.hithink_feature_store import hithink_feature_root, persist_hithink_feature
from app.services.hithink_finance_client import HithinkFinanceClient
from app.services.market_freshness import latest_completed_market_date


def _fetch_all(client: HithinkFinanceClient, *, trade_date: str, tickers: list[str]) -> dict[str, dict]:
    payloads = {
        "limit_up_pool": client.fetch_limit_up_pool(trade_date=trade_date),
        "limit_down_pool": client.fetch_limit_down_pool(trade_date=trade_date),
        "limit_break_pool": client.fetch_limit_break_pool(trade_date=trade_date),
        "limit_up_ladder": client.fetch_limit_up_ladder(),
        "dragon_tiger_list": client.fetch_dragon_tiger_list(board_type="all", trade_date=trade_date),
        "hot_stock_list": client.fetch_hot_stock_list(period="day"),
        "skyrocket_list": client.fetch_skyrocket_list(period="day"),
        "hot_stock_list_history": client.fetch_hot_stock_list_history(trade_date=trade_date),
        "anomaly_analysis_list": client.fetch_anomaly_analysis_list(),
        "auction_short_term_benchmark": client.fetch_auction_short_term_benchmark(trade_date=trade_date),
    }
    if tickers:
        payloads["auction_snapshot"] = client.fetch_auction_snapshot(tickers=tickers, stage="final")
    return payloads


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", default=None, help="trading date (yyyy-MM-dd); defaults to latest completed CN session")
    parser.add_argument("--root", default=None, help="artifacts root override; defaults to settings.artifacts_dir")
    parser.add_argument("--tickers", default="", help="comma-separated A-share tickers for the auction snapshot")
    parser.add_argument("--dry-run", action="store_true", help="fetch and report without persisting")
    args = parser.parse_args()

    trade_date = args.trade_date or latest_completed_market_date("CN")
    tickers = [token.strip() for token in args.tickers.split(",") if token.strip()]
    client = HithinkFinanceClient()
    if not client.is_configured():
        raise SystemExit("PQW_HITHINK_FINANCE_API_KEY is not configured.")
    root = Path(args.root) if args.root else None

    payloads = _fetch_all(client, trade_date=trade_date, tickers=tickers)
    persisted: dict[str, dict] = {}
    for name, payload in payloads.items():
        slot = "final" if name == "auction_snapshot" else None
        if args.dry_run:
            persisted[name] = {"source_reference": payload["source_reference"], "persisted": False}
            continue
        result = persist_hithink_feature(
            name=name, trade_date=trade_date, payload=payload, root=root, slot=slot
        )
        persisted[name] = {
            "path": str(result.path),
            "content_sha256": result.content_sha256,
            "reused_existing": result.reused_existing,
            "source_reference": payload["source_reference"],
        }
    print(
        json.dumps(
            {
                "trade_date": trade_date,
                "store_root": str(hithink_feature_root(root)),
                "dry_run": args.dry_run,
                "endpoints": persisted,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
