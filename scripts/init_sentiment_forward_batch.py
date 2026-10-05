"""Freeze one immutable forward-validation batch for the ``sentiment_v1`` family.

The HiThink sentiment family is forward-only (roughly one trailing year of
coverage), so it cannot be replayed over the long price history.  This script
pre-registers the batch contract exactly once: factor set, latest PIT universe
version, label version, ``cost_bps=50`` round-trip, EOD 16:00 cutoff semantics,
coverage window, operator/created-at provenance, ``dataset_hash``, and the
acceptance criteria (matured-date threshold, post-cost net hit rate, significance).

Re-running with the same contract reuses the frozen file.  Re-running with a
drifted contract fails closed; pick a new ``--batch-path`` for a new hypothesis.

Example::

    PQW_OPTIN_OPERATOR=jackyhu python scripts/init_sentiment_forward_batch.py
    python scripts/init_sentiment_forward_batch.py --dry-run --print-spec
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.stock_selection.sentiment_forward_batch import (  # noqa: E402
    DEFAULT_BATCH_PATH,
    DEFAULT_COST_BPS,
    DEFAULT_HORIZON_DAYS,
    DEFAULT_LABEL_VERSION,
    DEFAULT_TOP_N,
    SentimentForwardBatchError,
    build_sentiment_forward_batch_spec,
    default_start_date,
    freeze_sentiment_forward_batch,
    resolve_latest_pit_universe_version,
    resolve_operator,
)

DEFAULT_UNIVERSES_ROOT = ROOT_DIR / "data" / "artifacts" / "stock_selection_research" / "universes"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-path", type=Path, default=DEFAULT_BATCH_PATH)
    parser.add_argument(
        "--universe-version",
        default=None,
        help="Explicit universe_version; defaults to the newest local PIT universe artifact.",
    )
    parser.add_argument("--universes-root", type=Path, default=DEFAULT_UNIVERSES_ROOT)
    parser.add_argument(
        "--start-date",
        default=None,
        help="First frozen decision date; defaults to the first available CN trading day.",
    )
    parser.add_argument("--label-version", default=DEFAULT_LABEL_VERSION)
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument("--horizon-days", type=int, default=DEFAULT_HORIZON_DAYS)
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument("--coverage-end", default=None, help="Optional inclusive coverage end date.")
    parser.add_argument("--source-version", default=None, help="Optional market source_version.")
    parser.add_argument("--operator", default=None, help="Defaults to env PQW_OPTIN_OPERATOR or 'unknown'.")
    parser.add_argument(
        "--include-auction-path",
        action="store_true",
        help="Record the pre-open 09:25 auction path as part of the first batch (default: excluded).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Build and print without persisting.")
    parser.add_argument("--print-spec", action="store_true", help="Print the full spec payload.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    operator = args.operator or resolve_operator()
    try:
        universe_version = args.universe_version or resolve_latest_pit_universe_version(
            args.universes_root
        )
        start_date = args.start_date or default_start_date()
        spec = build_sentiment_forward_batch_spec(
            universe_version=universe_version,
            start_date=start_date,
            operator=operator,
            label_version=args.label_version,
            cost_bps=args.cost_bps,
            horizon_days=args.horizon_days,
            top_n=args.top_n,
            coverage_end=args.coverage_end,
            source_version=args.source_version,
            auction_path_included=args.include_auction_path,
        )
    except SentimentForwardBatchError as exc:
        raise SystemExit(str(exc)) from exc

    if args.dry_run:
        result = {
            "status": "dry_run",
            "batch_id": spec.batch_id,
            "dataset_hash": spec.dataset_hash,
            "universe_version": spec.universe_version,
            "start_date": spec.start_date,
            "operator": spec.operator,
            "batch_path": str(args.batch_path),
        }
    else:
        result = freeze_sentiment_forward_batch(args.batch_path, spec)
    if args.print_spec:
        result = {**result, "spec": spec.to_payload()}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
