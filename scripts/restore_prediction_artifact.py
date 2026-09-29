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

from app.services.prediction_artifact_restore import restore_prediction_artifact_to_sqlite  # noqa: E402


def _write_receipt(path: Path, payload: dict) -> None:
    target = path.resolve()
    if target.exists():
        raise FileExistsError(f"Restore receipt already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore one prediction artifact into an isolated SQLite DB.")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=100)
    parser.add_argument(
        "--receipt",
        type=Path,
        help="Atomically write an immutable JSON acceptance receipt.",
    )
    args = parser.parse_args()
    if args.receipt is not None and args.receipt.resolve().exists():
        raise FileExistsError(f"Restore receipt already exists: {args.receipt.resolve()}")
    result = restore_prediction_artifact_to_sqlite(
        args.manifest,
        destination=args.output,
        top_k=args.top_k,
    )
    if args.receipt is not None:
        _write_receipt(args.receipt, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "success":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
