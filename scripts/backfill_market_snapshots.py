from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import uuid

from sqlalchemy import inspect, text


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.db import SessionLocal, init_db  # noqa: E402
from app.services.market_snapshot_backfill import (  # noqa: E402
    load_market_snapshot_source,
    publish_market_snapshots,
    snapshot_semantic_summary,
)
from app.services.market_storage_routing import physical_snapshot_models  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill market-specific fundamental, PIT feature, and technical tables."
    )
    parser.add_argument("--market", choices=("CN", "HK", "US"), default="CN")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()

    init_db()
    with SessionLocal() as db:
        if args.apply:
            result = publish_market_snapshots(db, market=args.market)
            tables = physical_snapshot_models(args.market)
            for table in tables:
                db.execute(text(f"ANALYZE {table.__tablename__}"))
            db.commit()
            inspector = inspect(db.get_bind())
            result.update(
                {
                    "indexes": {
                        table.__tablename__: sorted(
                            str(item.get("name") or "")
                            for item in inspector.get_indexes(table.__tablename__)
                        )
                        for table in tables
                    },
                    "constraints": {
                        table.__tablename__: sorted(
                            str(item.get("name") or "")
                            for item in inspector.get_check_constraints(
                                table.__tablename__
                            )
                        )
                        for table in tables
                    },
                    "applied": True,
                }
            )
        else:
            source_rows = load_market_snapshot_source(db, market=args.market)
            result = {
                "status": "dry_run",
                "market": args.market,
                **snapshot_semantic_summary(source_rows),
                "applied": False,
            }

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


if __name__ == "__main__":
    main()
