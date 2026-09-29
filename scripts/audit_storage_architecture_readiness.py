from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
import os
from pathlib import Path
import sys
import uuid

from sqlalchemy import func, text


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.db import SessionLocal  # noqa: E402
from app.models.tables import CNPrediction, USPrediction  # noqa: E402
from app.services.failed_prediction_audit import audit_failed_prediction_rows  # noqa: E402
from app.services.market_physical_storage_audit import (  # noqa: E402
    audit_market_table_isolation_contract,
)
from app.services.cn_market_scheduler import cn_market_scheduler_service  # noqa: E402
from app.services.market_storage_routing import (  # noqa: E402
    market_physical_cutover_marker,
    physical_only_cutover_active,
)
from app.services.prediction_dual_write_audit import (  # noqa: E402
    audit_recent_compact_dual_writes,
)
from app.services.storage_architecture_readiness import (  # noqa: E402
    audit_registered_hard_gate_evidence,
    post_cutover_backup_restore_passed,
    summarize_multi_market_storage_architecture_readiness,
    verify_us_independent_signoff,
    verify_acceptance_manifest,
)
from app.services.storage_capacity import storage_capacity_report  # noqa: E402
from app.services.storage_retention import (  # noqa: E402
    _physical_only_canary_passed,
    clean_model_history,
)
from app.services.time_utils import app_now_iso  # noqa: E402
from app.services.us_market_scheduler import us_market_scheduler_service  # noqa: E402


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
        description="Read-only end-to-end PostgreSQL hot/cold architecture readiness audit."
    )
    parser.add_argument("--evidence-manifest", type=Path, required=True)
    parser.add_argument("--post-cutover-backup-receipt", type=Path)
    parser.add_argument("--post-cutover-restore-receipt", type=Path)
    parser.add_argument("--us-signoff-receipt", type=Path)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()

    manifest = verify_acceptance_manifest(args.evidence_manifest)
    frozen_evidence = audit_registered_hard_gate_evidence(args.evidence_manifest)
    with SessionLocal() as db:
        markets = ("CN", "US")
        dual_writes = {
            market: audit_recent_compact_dual_writes(
                db,
                market=market,
                required_runs=5,
                scan_limit=50,
            )
            for market in markets
        }
        cutover_markers = {
            market: market_physical_cutover_marker(db, market)
            for market in markets
        }
        cutover_active = {
            market: physical_only_cutover_active(db, market)
            for market in markets
        }
        capacity_not_before = None
        if all(cutover_active.values()) and all(cutover_markers.values()):
            activated_dates = [
                date.fromisoformat(str(cutover_markers[market]["activated_at"])[:10])
                for market in markets
                if str(cutover_markers[market].get("activated_at") or "")
            ]
            if len(activated_dates) == len(markets):
                capacity_not_before = max(activated_dates) + timedelta(days=1)
        capacity = storage_capacity_report(
            db,
            not_before_date=capacity_not_before,
            maximum_database_bytes=(
                5 * 1024 * 1024 * 1024
                if all(cutover_active.values())
                else None
            ),
            acceptance_scope=(
                "post_cutover_under_size"
                if all(cutover_active.values())
                else "pre_cutover_baseline"
            ),
        )
        isolation = audit_market_table_isolation_contract(db)
        retention_previews = {
            market: clean_model_history(
                db,
                markets=[market],
                apply=False,
                purge_workspace_snapshots=False,
            )
            for market in markets
        }
        failed_predictions = {
            market: audit_failed_prediction_rows(db, market=market)
            for market in markets
        }
        hot_prediction_rows = {
            "CN": int(db.scalar(func.count(CNPrediction.id)) or 0),
            "US": int(db.scalar(func.count(USPrediction.id)) or 0),
        }
        scheduler_enabled = {
            "CN": bool(cn_market_scheduler_service.get_config(db=db).get("enabled")),
            "US": bool(us_market_scheduler_service.get_config(db=db).get("enabled")),
        }
        database_bytes = int(
            db.scalar(text("SELECT pg_database_size(current_database())")) or 0
        )

    physical_only_passed: dict[str, bool] = {}
    physical_only_generated_at: dict[str, str | None] = {}
    for market in ("CN", "US"):
        passed = False
        generated_at = None
        if cutover_active[market]:
            passed, _ = _physical_only_canary_passed(
                dual_writes[market],
                market=market,
            )
            passed_runs = [
                row
                for row in dual_writes[market].get("runs") or []
                if isinstance(row, dict)
                and row.get("status") == "pass"
                and row.get("legacy_hot_dual_write") is False
            ]
            if passed and passed_runs:
                generated_at = str(
                    dual_writes[market].get("generated_at")
                    or passed_runs[0].get("finished_at")
                    or ""
                )
        physical_only_passed[market] = passed
        physical_only_generated_at[market] = generated_at
    latest_canary_at = None
    if all(physical_only_passed.values()):
        canary_times = [
            str(physical_only_generated_at[market] or "")
            for market in ("CN", "US")
        ]
        if all(canary_times):
            latest_canary_at = max(canary_times)
    post_cutover = post_cutover_backup_restore_passed(
        evidence_manifest_path=args.evidence_manifest,
        backup_receipt_path=args.post_cutover_backup_receipt,
        restore_receipt_path=args.post_cutover_restore_receipt,
        physical_only_generated_at=latest_canary_at,
    )
    us_signoff = verify_us_independent_signoff(
        evidence_manifest_path=args.evidence_manifest,
        receipt_path=args.us_signoff_receipt,
    )
    market_states = {
        market: {
            "dual_write": dual_writes[market],
            "retention_preview": retention_previews[market],
            "failed_predictions": failed_predictions[market],
            "scheduler_enabled": scheduler_enabled[market],
            "cutover_active": cutover_active[market],
            "physical_only_canary_passed": physical_only_passed[market],
            "physical_only_generated_at": physical_only_generated_at[market],
        }
        for market in ("CN", "US")
    }
    summary = summarize_multi_market_storage_architecture_readiness(
        manifest=manifest,
        capacity=capacity,
        isolation=isolation,
        market_states=market_states,
        frozen_evidence=frozen_evidence,
        hot_prediction_rows_by_market=hot_prediction_rows,
        post_cutover_backup_restore=post_cutover,
        database_bytes=database_bytes,
        us_signoff=us_signoff,
    )
    payload = {
        **summary,
        "generated_at": app_now_iso(),
        "database_connected": True,
        "database_mutated": False,
        "evidence_manifest": manifest,
        "market_states": market_states,
        "dual_write_by_market": dual_writes,
        "capacity": capacity,
        "isolation": isolation,
        "retention_preview_by_market": retention_previews,
        "frozen_evidence": frozen_evidence,
        "failed_predictions_by_market": failed_predictions,
        "post_cutover_backup_restore": post_cutover,
        "us_signoff_evidence": us_signoff,
    }
    _write_receipt(args.receipt, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
