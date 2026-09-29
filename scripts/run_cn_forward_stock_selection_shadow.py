from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.db import SessionLocal
from app.services.market_lake import get_latest_lake_trade_date
from app.services.stock_selection.forward_shadow import create_cn_forward_shadow_snapshot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Freeze one immutable A-share next-session forward-shadow snapshot."
    )
    parser.add_argument("--feature-date", help="Price feature date; defaults to the latest CN lake date.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Persist the snapshot. Without this flag the command only prints the resolved plan.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    feature_date = args.feature_date or get_latest_lake_trade_date(market="CN")
    if not feature_date:
        raise SystemExit("No CN market-lake date is available.")
    if not args.execute:
        print(
            json.dumps(
                {
                    "status": "dry_run",
                    "market": "CN",
                    "feature_date": feature_date,
                    "scope": "forward_shadow_only",
                    "message": "Add --execute to persist the first immutable next-session snapshot.",
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return
    with SessionLocal() as db:
        result = create_cn_forward_shadow_snapshot(db, feature_date=feature_date)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
