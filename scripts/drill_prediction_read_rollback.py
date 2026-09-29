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
from app.services.prediction_read_rollback import run_prediction_read_rollback_drill  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Simulate a market-isolated CN or US prediction cold-read rollback switch."
    )
    parser.add_argument("--market", choices=("CN", "US"), default="CN")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--receipt", type=Path, help="Atomically write the JSON result to this new file.")
    args = parser.parse_args()
    with SessionLocal() as db:
        result = run_prediction_read_rollback_drill(db, market=args.market, limit=args.limit)
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
