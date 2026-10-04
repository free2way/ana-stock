"""Apply an explicit evidence policy to an immutable source-audit receipt.

This never rewrites the source receipt, database rows, model runs, or old
evaluations.  It only produces a new policy-decision receipt whose lineage
includes the exact SHA256 of the source audit.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


POLICY = {
    "version": "raw_price_volume_company_action_v2",
    "required": ["raw_price", "daily_provenance", "volume", "corporate_action_state"],
    "optional_non_blocking": ["historical_suspension_state", "explicit_daily_upper_lower_limits"],
    "limitation": "Research replay does not certify exchange fillability when optional facts are absent.",
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_policy(source: dict, *, source_path: Path) -> dict:
    if source.get("schema") != "hithink_cn_execution_fact_gap_v1":
        raise ValueError("unsupported source receipt schema")
    price = source.get("price_crosscheck") or {}
    actions = source.get("corporate_action_crosscheck") or {}
    tickers = list((source.get("scope") or {}).get("tickers") or [])
    dates = list((source.get("scope") or {}).get("trading_dates") or [])
    if not tickers or not dates or set(price) != set(tickers) or set(actions) != set(tickers):
        raise ValueError("source receipt has inconsistent scope")
    rows = []
    for ticker in sorted(tickers):
        required_failures = []
        if price[ticker].get("status") != "PASS":
            required_failures.extend(["raw_price", "daily_provenance", "volume"])
        if actions[ticker].get("status") != "PASS":
            required_failures.append("corporate_action_state")
        for day in dates:
            rows.append({
                "ticker": ticker,
                "date": day,
                "status": "READY" if not required_failures else "BLOCKED",
                "required_failures": sorted(set(required_failures)),
                "optional_evidence_gaps": [
                    "historical_suspension_state",
                    "explicit_daily_upper_lower_limits",
                ],
            })
    ready = sum(row["status"] == "READY" for row in rows)
    return {
        "schema": "execution_evidence_policy_decision_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_receipt": {"path": str(source_path), "sha256": sha256(source_path)},
        "scope": source["scope"],
        "policy": POLICY,
        "rows": rows,
        "counts": {"total": len(rows), "ready": ready, "blocked": len(rows) - ready},
        "small_scope_status": "READY" if rows and ready == len(rows) else "BLOCKED",
        "training_authorized_by_policy_receipt": bool(rows and ready == len(rows)),
        "training_run_id": None,
        "old_results_rewritten": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; immutable receipts are never overwritten")
    payload = apply_policy(json.loads(args.source.read_text(encoding="utf-8")), source_path=args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"output": str(args.output), "status": payload["small_scope_status"],
                      "counts": payload["counts"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
