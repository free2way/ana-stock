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
from app.services.storage_architecture_benchmark import run_storage_architecture_benchmark  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark prewarmed CN hot and Parquet cold prediction reads.")
    parser.add_argument("--cold-model-run-id", type=int, help="Verified CN artifact to scan; defaults to the largest.")
    parser.add_argument("--iterations", type=int, default=20, help="Measured iterations per normal query.")
    parser.add_argument("--cold-full-iterations", type=int, default=10, help="Measured full-artifact scans.")
    parser.add_argument("--warmups", type=int, default=2, help="Warmup calls excluded from percentiles.")
    parser.add_argument("--receipt", type=Path, help="Atomically write the JSON result to this new file.")
    args = parser.parse_args()
    with SessionLocal() as db:
        result = run_storage_architecture_benchmark(
            db,
            market="CN",
            cold_model_run_id=args.cold_model_run_id,
            iterations=args.iterations,
            cold_full_iterations=args.cold_full_iterations,
            warmups=args.warmups,
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
