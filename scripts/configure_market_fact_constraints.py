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

from app.core.db import engine, init_db  # noqa: E402
from app.services.market_fact_constraints import (  # noqa: E402
    apply_market_fact_constraints,
    inspect_market_fact_constraints,
)
from app.services.time_utils import app_now_iso  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit/apply composite symbol-market foreign keys."
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()

    init_db()
    if args.apply:
        with engine.begin() as connection:
            details = apply_market_fact_constraints(connection)
    else:
        with engine.connect() as connection:
            state = inspect_market_fact_constraints(connection)
        details = {"status": state["status"], "changed": [], "before": state, "after": state}
    result = {
        "audit_version": "market-fact-composite-fk-v1",
        "generated_at": app_now_iso(),
        "applied": bool(args.apply),
        **details,
    }
    serialized = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    if args.receipt is not None:
        target = args.receipt.resolve()
        if target.exists():
            raise FileExistsError(f"Receipt already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(serialized + "\n", encoding="utf-8")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    print(serialized)
    if args.apply and result["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
