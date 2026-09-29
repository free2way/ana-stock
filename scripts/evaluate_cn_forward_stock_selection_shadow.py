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
from app.services.market_lake import get_latest_lake_trade_date  # noqa: E402
from app.services.stock_selection.forward_shadow_evaluation import (  # noqa: E402
    create_cn_forward_shadow_evaluation_snapshot,
)


def _write_receipt(path: Path, payload: dict) -> None:
    target = path.resolve()
    if target.exists():
        raise FileExistsError(f"Receipt already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate matured A-share forward-shadow observations."
    )
    parser.add_argument("--as-of-date")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    as_of_date = args.as_of_date or get_latest_lake_trade_date(market="CN")
    if not as_of_date:
        raise SystemExit("No CN lake date is available.")
    if not args.execute:
        print(
            json.dumps(
                {
                    "status": "dry_run",
                    "market": "CN",
                    "as_of_date": as_of_date,
                    "message": "Add --execute to persist the idempotent evaluation snapshot.",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    with SessionLocal() as db:
        result = create_cn_forward_shadow_evaluation_snapshot(
            db,
            as_of_date=as_of_date,
        )
    if args.receipt is not None:
        _write_receipt(
            args.receipt,
            {
                "receipt_version": "cn-forward-shadow-evaluation-receipt-v1",
                "market": "CN",
                "as_of_date": as_of_date,
                "database_mutated": not bool(result.get("reused_existing")),
                **result,
            },
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
