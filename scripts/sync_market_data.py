import sys
import argparse
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.market_sync import sync_market_data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync historical market data into local CSV files.")
    parser.add_argument("--tickers", nargs="*", help="Ticker list to sync. Defaults to all symbols in the database.")
    parser.add_argument("--start-date", help="Optional start date in YYYY-MM-DD.")
    parser.add_argument("--end-date", help="Optional end date in YYYY-MM-DD.")
    parser.add_argument("--provider", default="yfinance", help="OpenBB provider to use. Default: yfinance.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    results = sync_market_data(
        tickers=args.tickers,
        start_date=args.start_date,
        end_date=args.end_date,
        provider=args.provider,
    )
    for result in results:
        if result["status"] == "success":
            print(f"{result['ticker']}: wrote {result['rows']} rows to {result['raw_path']}")
        else:
            print(f"{result['ticker']}: sync failed: {result.get('message', 'unknown error')}")


if __name__ == "__main__":
    main()
