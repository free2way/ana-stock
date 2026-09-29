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
from app.services.legacy_market_fact_archive import archive_legacy_market_facts  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive shared-table facts by actual symbol market.")
    parser.add_argument("--market", choices=("CN", "HK", "US"), required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    with SessionLocal() as db:
        result = archive_legacy_market_facts(db, market=args.market, apply=args.apply)
    serialized = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.receipt is not None:
        target = args.receipt.resolve()
        if target.exists():
            raise FileExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(serialized + "\n", encoding="utf-8")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    print(serialized)


if __name__ == "__main__":
    main()
