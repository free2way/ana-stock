from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.stock_selection.history_backfill import (
    CNHistoryBackfillConfig,
    backfill_cn_stock_selection_history,
    summarize_cn_history_backfill_result,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan or execute resumable full-market CN history partition backfill."
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--target-history-sessions", type=int, default=252)
    parser.add_argument("--minimum-partition-coverage", type=float, default=0.60)
    parser.add_argument("--max-dates", type=int, default=5)
    parser.add_argument("--maximum-consecutive-failures", type=int, default=2)
    parser.add_argument("--inter-date-delay-seconds", type=float, default=4.0)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = backfill_cn_stock_selection_history(
        config=CNHistoryBackfillConfig(
            target_history_sessions=args.target_history_sessions,
            minimum_partition_coverage=args.minimum_partition_coverage,
            max_dates_per_run=args.max_dates,
            maximum_consecutive_failures=args.maximum_consecutive_failures,
            inter_date_delay_seconds=args.inter_date_delay_seconds,
            dry_run=not args.execute,
        )
    )
    payload = (
        {
            **summarize_cn_history_backfill_result(result),
            "partition_symbol_counts": result.plan_after.partition_symbol_counts,
            "all_pending_dates": result.plan_after.pending_dates,
        }
        if args.verbose
        else summarize_cn_history_backfill_result(result)
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
