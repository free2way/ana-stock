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
from app.services.market_physical_storage_audit import (  # noqa: E402
    audit_market_physical_hot_storage,
    audit_market_physical_live_storage,
    audit_market_table_isolation_contract,
)
from app.services.market_snapshot_storage_audit import (  # noqa: E402
    audit_market_physical_snapshots,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit CN/HK/US physical table isolation and parity."
    )
    parser.add_argument("--market", choices=("CN", "HK", "US"), default="CN")
    parser.add_argument(
        "--layer",
        choices=("isolation", "live", "hot", "snapshots"),
        default="live",
    )
    parser.add_argument("--model-run-id", type=int)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()

    with SessionLocal() as db:
        if args.layer == "isolation":
            result = audit_market_table_isolation_contract(db)
        elif args.layer == "snapshots":
            result = audit_market_physical_snapshots(db, market=args.market)
        elif args.layer == "hot":
            result = audit_market_physical_hot_storage(
                db,
                market=args.market,
                model_run_id=args.model_run_id,
            )
        else:
            result = audit_market_physical_live_storage(db, market=args.market)
    serialized = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.receipt is not None:
        receipt_path = args.receipt.resolve()
        if receipt_path.exists():
            raise FileExistsError(f"Receipt already exists: {receipt_path}")
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = receipt_path.with_name(
            f".{receipt_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary_path.write_text(serialized + "\n", encoding="utf-8")
            os.replace(temporary_path, receipt_path)
        finally:
            temporary_path.unlink(missing_ok=True)
    print(serialized)
    if result["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
