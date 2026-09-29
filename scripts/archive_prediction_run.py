from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import uuid


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.db import SessionLocal  # noqa: E402
from app.services.prediction_archive_migration import (  # noqa: E402
    archive_model_run_from_postgres,
    archive_model_runs_batch,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive one PostgreSQL model run into an immutable Parquet artifact.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--model-run-id", type=int)
    target.add_argument("--market", choices=("CN", "US"), help="Archive a bounded batch for one market.")
    parser.add_argument("--limit", type=int, default=5, help="Maximum batch size. Default: 5.")
    parser.add_argument("--keep-latest-runs", type=int, default=20, help="Never archive the latest N successful runs.")
    parser.add_argument(
        "--max-predictions-per-run",
        type=int,
        default=250000,
        help="Skip larger runs so each batch remains bounded.",
    )
    parser.add_argument(
        "--max-total-rows-per-run",
        type=int,
        default=500000,
        help="Skip runs whose prediction/detail/explanation total exceeds this bound.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Publish and register the artifact. Without this flag the command is read-only.",
    )
    parser.add_argument("--receipt", type=Path, help="Atomically write the JSON result to this new file.")
    args = parser.parse_args()
    with SessionLocal() as db:
        if args.model_run_id is not None:
            result = archive_model_run_from_postgres(
                db,
                model_run_id=args.model_run_id,
                dry_run=not args.apply,
            )
        else:
            result = archive_model_runs_batch(
                db,
                market=args.market,
                limit=args.limit,
                keep_latest_runs=args.keep_latest_runs,
                max_predictions_per_run=args.max_predictions_per_run,
                max_total_rows_per_run=args.max_total_rows_per_run,
                dry_run=not args.apply,
            )
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if args.receipt is not None:
        receipt_path = args.receipt.resolve()
        if receipt_path.exists():
            raise FileExistsError(f"Receipt already exists: {receipt_path}")
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = receipt_path.with_name(f".{receipt_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary_path.write_text(serialized + "\n", encoding="utf-8")
            os.replace(temporary_path, receipt_path)
        finally:
            temporary_path.unlink(missing_ok=True)
    print(serialized)


if __name__ == "__main__":
    main()
