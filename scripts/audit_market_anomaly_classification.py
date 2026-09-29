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
from app.services.data_quality import market_data_gate  # noqa: E402
from app.services.repository import PriceSyncStateRepository  # noqa: E402
from app.services.time_utils import app_now_iso  # noqa: E402
from app.services.us_trade_universe import build_us_trade_universe  # noqa: E402


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
        description="Audit reconciled CN/US per-symbol market-data anomaly classifications."
    )
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    us_production_universe, us_universe_summary = build_us_trade_universe(
        include_summary=True
    )
    with SessionLocal() as db:
        overview = PriceSyncStateRepository(db).get_market_freshness_overview(("CN", "US"))
        gates = {market: market_data_gate(db, market=market) for market in ("CN", "US")}
        us_production_gate = market_data_gate(
            db,
            market="US",
            tickers=us_production_universe,
        )
    classifications = {
        market: (overview.get(market) or {}).get("anomaly_classification") or {}
        for market in ("CN", "US")
    }
    checks = {
        "cn_classification_contract_present": classifications["CN"].get(
            "classification_version"
        )
        == "market-symbol-anomaly-v1",
        "us_classification_contract_present": classifications["US"].get(
            "classification_version"
        )
        == "market-symbol-anomaly-v1",
        "cn_gate_uses_reconciled_count": int(
            gates["CN"].get("blocking_anomaly_count") or 0
        )
        == int(classifications["CN"].get("blocking_anomaly_count") or 0),
        "us_gate_uses_reconciled_count": int(
            gates["US"].get("blocking_anomaly_count") or 0
        )
        == int(classifications["US"].get("blocking_anomaly_count") or 0),
        "us_production_universe_has_current_rows": bool(us_production_universe),
        "us_production_input_gate_ready": us_production_gate.get("status")
        in {"ready", "graded"},
    }
    payload = {
        "audit_version": "market-symbol-anomaly-audit-v2",
        "generated_at": app_now_iso(),
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "markets": {
            market: {
                "authoritative_as_of_date": (overview.get(market) or {}).get(
                    "authoritative_as_of_date"
                ),
                "raw_stale_count": int((overview.get(market) or {}).get("stale_count") or 0),
                "raw_missing_count": int((overview.get(market) or {}).get("missing_count") or 0),
                "classification": classifications[market],
                "gate": gates[market],
            }
            for market in ("CN", "US")
        },
        "us_production_input": {
            "scope": "latest_lake_partition_common_liquid_stocks",
            "universe": us_universe_summary,
            "gate": us_production_gate,
        },
        "database_connected": True,
        "database_mutated": False,
    }
    _write_receipt(args.receipt, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    if payload["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
