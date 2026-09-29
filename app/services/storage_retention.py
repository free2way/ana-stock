from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.prediction_artifacts import verify_prediction_artifact
from app.services.market_calendar import is_market_open_date, next_market_open_date
from app.services.prediction_dual_write_audit import dual_write_runtime_evidence_passed
from app.services.market_storage_routing import (
    MARKET_PHYSICAL_CUTOVER_VERSION,
    cn_physical_only_cutover_active,
    normalize_fact_market,
    physical_only_cutover_active,
)
from app.services.time_utils import app_now_iso
from app.services.workspace_snapshot_retention import select_workspace_snapshot_retention


RETENTION_APPLY_TOKEN = "DELETE_VERIFIED_CN_PREDICTION_OUTPUTS"
FAILED_OUTPUT_RETENTION_APPLY_TOKEN = "DELETE_FAILED_CN_US_PREDICTION_OUTPUTS"


def retention_apply_token(market: str | None) -> str:
    return f"DELETE_VERIFIED_{normalize_fact_market(market)}_PREDICTION_OUTPUTS"


def user_authorized_retention_override_token(market: str | None) -> str:
    return (
        "DELETE_USER_AUTHORIZED_VERIFIED_"
        f"{normalize_fact_market(market)}_PREDICTION_OUTPUTS"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Acceptance evidence is not a JSON object: {path}")
    return payload


def _same_device(first: Path, second: Path) -> bool:
    return os.stat(first).st_dev == os.stat(second).st_dev


def _evidence_time(payload: dict, *, label: str) -> datetime:
    value = str(payload.get("generated_at") or "").strip()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError(f"{label} receipt is missing a valid generated_at timestamp.") from exc
    if parsed.tzinfo is None:
        raise RuntimeError(f"{label} receipt generated_at must be timezone-aware.")
    return parsed


def validate_retention_foundation_gate(
    *,
    markets: list[str],
    approval_token: str | None,
    evidence_manifest_path: Path | str | None,
    backup_receipt_path: Path | str | None,
    restore_receipt_path: Path | str | None,
    dual_write_receipt_path: Path | str | None,
) -> dict:
    """Validate the frozen pre-cutover backup and dual-write evidence."""

    normalized_markets = sorted(
        {normalize_fact_market(value) for value in markets}
    )
    if len(normalized_markets) != 1:
        raise RuntimeError(
            "Retention apply must target exactly one market per transaction."
        )
    market = normalized_markets[0]
    if str(approval_token or "") != retention_apply_token(market):
        raise RuntimeError("Retention apply requires the explicit destructive approval token.")
    required_paths = {
        "evidence_manifest": evidence_manifest_path,
        "backup_receipt": backup_receipt_path,
        "restore_receipt": restore_receipt_path,
        "dual_write_receipt": dual_write_receipt_path,
    }
    missing = [name for name, value in required_paths.items() if value is None]
    if missing:
        raise RuntimeError(f"Retention apply is missing acceptance evidence: {', '.join(missing)}")
    paths = {name: Path(value).resolve() for name, value in required_paths.items()}
    absent = [name for name, path in paths.items() if not path.is_file()]
    if absent:
        raise RuntimeError(f"Retention acceptance evidence files do not exist: {', '.join(absent)}")

    evidence_manifest = _load_json(paths["evidence_manifest"])
    evidence_files = evidence_manifest.get("files") or {}
    for name in ("backup_receipt", "restore_receipt", "dual_write_receipt"):
        receipt_path = paths[name]
        registered = evidence_files.get(receipt_path.name) or {}
        if str(registered.get("sha256") or "") != _sha256(receipt_path):
            raise RuntimeError(f"Acceptance manifest hash mismatch or missing registration: {receipt_path.name}")

    backup = _load_json(paths["backup_receipt"])
    restore = _load_json(paths["restore_receipt"])
    dual_write = _load_json(paths["dual_write_receipt"])
    if backup.get("status") != "pass":
        raise RuntimeError("PostgreSQL backup receipt has not passed.")
    backup_path = Path(str(backup.get("backup_path") or "")).resolve()
    if not backup_path.is_file() or _sha256(backup_path) != str(backup.get("backup_sha256") or ""):
        raise RuntimeError("PostgreSQL backup file is missing or its SHA-256 changed.")
    if _same_device(backup_path, paths["evidence_manifest"]):
        raise RuntimeError("PostgreSQL backup and acceptance evidence must use separate storage devices.")

    restore_passed = (
        restore.get("status") == "pass"
        and (restore.get("critical_table_parity") or {}).get("exact_match") is True
        and restore.get("restore_database_dropped") is True
        and restore.get("source_database_mutated") is False
        and str(restore.get("backup_sha256") or "") == str(backup.get("backup_sha256") or "")
    )
    if not restore_passed:
        raise RuntimeError("PostgreSQL isolated restore receipt has not passed all hard checks.")

    passed_runs = [int(value) for value in (dual_write.get("passed_runs") or [])]
    sequence_runs = [int(value) for value in (dual_write.get("sequence_runs") or [])]
    sequence_trade_dates = [
        str(value)[:10] for value in (dual_write.get("sequence_trade_dates") or [])
    ]
    consecutive_trade_dates = (
        len(sequence_trade_dates) >= 5
        and len(set(sequence_trade_dates)) == len(sequence_trade_dates)
        and all(is_market_open_date(market, value) for value in sequence_trade_dates)
        and all(
            next_market_open_date(market, left, include_self=False) == right
            for left, right in zip(sequence_trade_dates, sequence_trade_dates[1:])
        )
    )
    dual_write_passed = (
        dual_write.get("status") == "pass"
        and dual_write.get("market") == market
        and dual_write_runtime_evidence_passed(dual_write)
        and int(dual_write.get("required_runs") or 0) >= 5
        and len(passed_runs) >= 5
        and int(dual_write.get("remaining_runs") or 0) == 0
        and not (dual_write.get("failed_runs") or [])
        and not (dual_write.get("pending_runs") or [])
        and dual_write.get("consecutive_trade_dates") is True
        and consecutive_trade_dates
        and len(sequence_runs) >= 5
        and set(sequence_runs).issubset(set(passed_runs))
    )
    if not dual_write_passed:
        raise RuntimeError("Five verified production compact dual writes have not passed.")
    backup_time = _evidence_time(backup, label="PostgreSQL backup")
    restore_time = _evidence_time(restore, label="PostgreSQL restore")
    dual_write_time = _evidence_time(dual_write, label="Production dual-write")
    if backup_time < dual_write_time:
        raise RuntimeError(
            "PostgreSQL backup must be generated after the final production dual-write audit."
        )
    if restore_time < backup_time:
        raise RuntimeError(
            "PostgreSQL restore verification must be generated after its backup."
        )
    return {
        "status": "pass",
        "markets": normalized_markets,
        "backup_sha256": backup["backup_sha256"],
        "restore_status": restore["status"],
        "dual_write_run_ids": passed_runs,
        "dual_write_trade_dates": sequence_trade_dates,
        "backup_generated_at": backup_time.isoformat(),
        "restore_generated_at": restore_time.isoformat(),
        "dual_write_generated_at": dual_write_time.isoformat(),
        "separate_backup_device": True,
    }


def validate_user_authorized_retention_override_gate(
    *,
    markets: list[str],
    approval_token: str | None,
    evidence_manifest_path: Path | str | None,
    backup_receipt_path: Path | str | None,
) -> dict:
    """Validate the minimum non-negotiable safeguards for a user-authorized override.

    This intentionally bypasses the consecutive-production, physical-cutover,
    canary, and isolated-restore acceptance gates. It does not bypass market
    isolation, a fresh recoverable backup, the latest-run retention window, or
    per-artifact integrity verification immediately before DELETE.
    """

    normalized_markets = sorted(
        {normalize_fact_market(value) for value in markets}
    )
    if len(normalized_markets) != 1:
        raise RuntimeError(
            "User-authorized retention override must target exactly one market."
        )
    market = normalized_markets[0]
    if str(approval_token or "") != user_authorized_retention_override_token(market):
        raise RuntimeError(
            "User-authorized retention override requires its explicit destructive token."
        )
    if evidence_manifest_path is None or backup_receipt_path is None:
        raise RuntimeError(
            "User-authorized retention override requires an evidence manifest and backup receipt."
        )
    manifest_path = Path(evidence_manifest_path).resolve()
    receipt_path = Path(backup_receipt_path).resolve()
    if not manifest_path.is_file() or not receipt_path.is_file():
        raise RuntimeError(
            "User-authorized retention override evidence files do not exist."
        )
    manifest = _load_json(manifest_path)
    registered = (manifest.get("files") or {}).get(receipt_path.name) or {}
    if str(registered.get("sha256") or "") != _sha256(receipt_path):
        raise RuntimeError(
            "User-authorized retention backup receipt is missing from the manifest or changed."
        )
    backup = _load_json(receipt_path)
    backup_path = Path(str(backup.get("backup_path") or "")).resolve()
    backup_passed = (
        backup.get("status") == "pass"
        and backup.get("database_mutated") is False
        and backup_path.is_file()
        and _sha256(backup_path) == str(backup.get("backup_sha256") or "")
        and not _same_device(backup_path, manifest_path)
    )
    if not backup_passed:
        raise RuntimeError(
            "User-authorized retention override backup is missing, changed, or not on separate storage."
        )
    return {
        "status": "pass",
        "gate": "user_authorized_retention_override_v1",
        "market": market,
        "bypassed_acceptance_checks": [
            "five_consecutive_dual_writes",
            "physical_only_cutover",
            "physical_only_production_canary",
            "isolated_restore_drill",
        ],
        "preserved_safeguards": [
            "single_market_transaction",
            "latest_success_runs_retained",
            "verified_cold_artifact_required",
            "artifact_integrity_rechecked_before_delete",
            "separate_device_backup",
            "post_delete_residual_check",
        ],
        "backup_sha256": backup["backup_sha256"],
        "backup_generated_at": str(backup.get("generated_at") or ""),
        "separate_backup_device": True,
    }


def _physical_only_canary_passed(
    payload: dict,
    *,
    market: str,
) -> tuple[bool, int | None]:
    passed_runs = [int(value) for value in payload.get("passed_runs") or []]
    runs = payload.get("runs") or []
    if not passed_runs or not isinstance(runs, list):
        return False, None
    selected = next(
        (
            item
            for item in runs
            if isinstance(item, dict)
            and int(item.get("model_run_id") or 0) in passed_runs
        ),
        None,
    )
    if selected is None:
        return False, None
    contract = selected.get("storage_contract") or {}
    predictions = selected.get("predictions") or {}
    details = selected.get("prediction_details") or {}
    explanations = selected.get("prediction_explanations") or {}
    passed = (
        payload.get("status") == "pass"
        and dual_write_runtime_evidence_passed(payload)
        and int(payload.get("required_runs") or 0) >= 1
        and int(payload.get("remaining_runs") or 0) == 0
        and not (payload.get("failed_runs") or [])
        and not (payload.get("pending_runs") or [])
        and selected.get("status") == "pass"
        and selected.get("market") == market
        and selected.get("hot_source_layer") == "physical_market_tables"
        and selected.get("legacy_hot_dual_write") is False
        and selected.get("legacy_hot_comparisons") is None
        and (selected.get("publication_runtime") or {}).get("status") == "pass"
        and contract.get("legacy_hot_dual_write") is False
        and predictions.get("status") == "pass"
        and (predictions.get("compact_rows") or {}).get("exact_match") is True
        and details.get("exact_match") is True
        and explanations.get("status") == "pass"
        and (explanations.get("selected_rows") or {}).get("exact_match") is True
    )
    return passed, int(selected["model_run_id"]) if passed else None


def validate_retention_acceptance_gate(
    *,
    markets: list[str],
    approval_token: str | None,
    evidence_manifest_path: Path | str | None,
    backup_receipt_path: Path | str | None,
    restore_receipt_path: Path | str | None,
    dual_write_receipt_path: Path | str | None,
    cutover_receipt_path: Path | str | None,
    physical_only_receipt_path: Path | str | None,
) -> dict:
    """Validate frozen post-cutover evidence before destructive retention."""

    base_gate = validate_retention_foundation_gate(
        markets=markets,
        approval_token=approval_token,
        evidence_manifest_path=evidence_manifest_path,
        backup_receipt_path=backup_receipt_path,
        restore_receipt_path=restore_receipt_path,
        dual_write_receipt_path=dual_write_receipt_path,
    )
    required_paths = {
        "cutover_receipt": cutover_receipt_path,
        "physical_only_receipt": physical_only_receipt_path,
    }
    missing = [name for name, value in required_paths.items() if value is None]
    if missing:
        raise RuntimeError(
            "Retention apply is missing post-cutover evidence: " + ", ".join(missing)
        )
    paths = {name: Path(value).resolve() for name, value in required_paths.items()}
    absent = [name for name, path in paths.items() if not path.is_file()]
    if absent:
        raise RuntimeError(
            "Retention post-cutover evidence files do not exist: " + ", ".join(absent)
        )
    manifest_path = Path(evidence_manifest_path).resolve()
    evidence_manifest = _load_json(manifest_path)
    evidence_files = evidence_manifest.get("files") or {}
    for receipt_path in paths.values():
        registered = evidence_files.get(receipt_path.name) or {}
        if str(registered.get("sha256") or "") != _sha256(receipt_path):
            raise RuntimeError(
                "Acceptance manifest hash mismatch or missing registration: "
                f"{receipt_path.name}"
            )

    cutover = _load_json(paths["cutover_receipt"])
    activation = cutover.get("activation") or {}
    marker = activation.get("marker") or {}
    cutover_passed = (
        cutover.get("status") == "success"
        and cutover.get("action") == "activate"
        and cutover.get("database_connected") is True
        and cutover.get("database_mutated") is True
        and activation.get("status") == "success"
        and activation.get("action") == "activated"
        and marker.get("cutover_version") == MARKET_PHYSICAL_CUTOVER_VERSION
        and marker.get("status") == "active"
        and marker.get("market") == base_gate["markets"][0]
    )
    if not cutover_passed:
        raise RuntimeError("Market physical-only cutover receipt has not passed all hard checks.")
    cutover_time = _evidence_time(
        {"generated_at": marker.get("activated_at")},
        label="Market physical-only cutover",
    )

    physical_only = _load_json(paths["physical_only_receipt"])
    physical_only_passed, physical_only_run_id = _physical_only_canary_passed(
        physical_only,
        market=base_gate["markets"][0],
    )
    if not physical_only_passed:
        raise RuntimeError("Market physical-only production canary has not passed all hard checks.")
    physical_only_time = _evidence_time(
        physical_only,
        label="Market physical-only production canary",
    )
    if physical_only_time < cutover_time:
        raise RuntimeError(
            "Market physical-only production canary must be generated after cutover activation."
        )
    backup_time = datetime.fromisoformat(base_gate["backup_generated_at"])
    if backup_time < physical_only_time:
        raise RuntimeError(
            "PostgreSQL backup must be generated after the physical-only production canary."
        )
    return {
        **base_gate,
        "cutover_generated_at": cutover_time.isoformat(),
        "physical_only_generated_at": physical_only_time.isoformat(),
        "physical_only_run_id": physical_only_run_id,
    }


def _split_successful_run_candidates(
    rows: list[dict],
    *,
    keep_runs_per_market: int,
) -> tuple[list[int], list[int]]:
    """Return (purgeable, blocked) stale runs.

    A successful run is purgeable only after its immutable cold artifact is
    registered as verified. This is the hard guard that prevents retention
    from turning a database cleanup into data loss.
    """

    by_market: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_market[str(row.get("market") or "")].append(row)
    purgeable: list[int] = []
    blocked: list[int] = []
    for values in by_market.values():
        for row in values[max(1, int(keep_runs_per_market)) :]:
            run_id = int(row["id"])
            if str(row.get("artifact_status") or "").lower() == "verified":
                purgeable.append(run_id)
            else:
                blocked.append(run_id)
    return purgeable, blocked


def _bounded_retention_ids(values: list[int], *, limit: int) -> list[int]:
    """Select one deterministic oldest-first retention batch."""

    return sorted({int(value) for value in values})[: max(1, int(limit))]


def _related_row_counts(db: Session, run_ids: list[int]) -> dict[str, int]:
    counts = {
        "cn_live_predictions": 0,
        "cn_predictions": 0,
        "cn_prediction_details": 0,
        "cn_prediction_explanations": 0,
        "cn_model_chart_signals": 0,
        "cn_prediction_trade_plans": 0,
        "live_predictions": 0,
        "predictions": 0,
        "prediction_details": 0,
        "prediction_explanations": 0,
        "prediction_trade_plans": 0,
        "model_chart_signals": 0,
        "us_live_predictions": 0,
        "us_predictions": 0,
        "us_prediction_details": 0,
        "us_prediction_explanations": 0,
        "us_model_chart_signals": 0,
        "us_prediction_trade_plans": 0,
        "hk_live_predictions": 0,
        "hk_predictions": 0,
        "hk_prediction_details": 0,
        "hk_prediction_explanations": 0,
        "hk_model_chart_signals": 0,
        "hk_prediction_trade_plans": 0,
    }
    if not run_ids:
        return counts
    params = {"ids": run_ids}
    for table in counts:
        where = (
            "model_run_id = ANY(CAST(:ids AS INTEGER[]))"
            if table in {
                "cn_live_predictions",
                "cn_predictions",
                "cn_model_chart_signals",
                "live_predictions",
                "model_chart_signals",
                "predictions",
                "us_live_predictions",
                "us_predictions",
                "us_model_chart_signals",
                "hk_live_predictions",
                "hk_predictions",
                "hk_model_chart_signals",
            }
            else (
                "prediction_id IN (SELECT id FROM cn_predictions WHERE model_run_id = ANY(CAST(:ids AS INTEGER[])))"
                if table in {"cn_prediction_details", "cn_prediction_explanations", "cn_prediction_trade_plans"}
                else "prediction_id IN (SELECT id FROM us_predictions WHERE model_run_id = ANY(CAST(:ids AS INTEGER[])))"
                if table in {"us_prediction_details", "us_prediction_explanations", "us_prediction_trade_plans"}
                else "prediction_id IN (SELECT id FROM hk_predictions WHERE model_run_id = ANY(CAST(:ids AS INTEGER[])))"
                if table in {"hk_prediction_details", "hk_prediction_explanations", "hk_prediction_trade_plans"}
                else "prediction_id IN (SELECT id FROM predictions WHERE model_run_id = ANY(CAST(:ids AS INTEGER[])))"
            )
        )
        counts[table] = int(db.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {where}"), params).scalar() or 0)
    return counts


def _delete_prediction_outputs(db: Session, run_ids: list[int]) -> None:
    if not run_ids:
        return
    params = {"ids": run_ids}
    for table in (
        "cn_live_predictions",
        "hk_live_predictions",
        "live_predictions",
        "us_live_predictions",
    ):
        db.execute(
            text(
                f"DELETE FROM {table} "
                "WHERE model_run_id = ANY(CAST(:ids AS INTEGER[]))"
            ),
            params,
        )
    for table in (
        "cn_model_chart_signals",
        "model_chart_signals",
        "us_model_chart_signals",
        "hk_model_chart_signals",
    ):
        db.execute(
            text(
                f"DELETE FROM {table} "
                "WHERE model_run_id = ANY(CAST(:ids AS INTEGER[]))"
            ),
            params,
        )
    for table in ("cn_predictions", "hk_predictions", "us_predictions"):
        db.execute(
            text(
                f"DELETE FROM {table} "
                "WHERE model_run_id = ANY(CAST(:ids AS INTEGER[]))"
            ),
            params,
        )
    for table in ("prediction_explanations", "prediction_details", "prediction_trade_plans"):
        db.execute(
            text(
                f"DELETE FROM {table} "
                "WHERE prediction_id IN ("
                "SELECT id FROM predictions WHERE model_run_id = ANY(CAST(:ids AS INTEGER[]))"
                ")"
            ),
            params,
        )
    db.execute(text("DELETE FROM predictions WHERE model_run_id = ANY(CAST(:ids AS INTEGER[]))"), params)


def clean_failed_prediction_outputs(
    db: Session,
    *,
    markets: tuple[str, ...] | list[str] = ("CN", "US"),
    max_runs: int = 1,
    model_run_id: int | None = None,
    apply: bool = False,
    approval_token: str | None = None,
    evidence_manifest_path: Path | str | None = None,
    backup_receipt_path: Path | str | None = None,
    restore_receipt_path: Path | str | None = None,
    isolation_receipt_path: Path | str | None = None,
    hk_archive_receipt_path: Path | str | None = None,
) -> dict:
    """Remove invalid failed-run facts in small, independently verified batches."""

    target_markets = sorted(
        {
            str(market).strip().upper()
            for market in markets
            if str(market).strip().upper() in {"CN", "US"}
        }
    )
    if not target_markets:
        raise ValueError("Failed-output cleanup requires CN and/or US.")
    failed_run_ids = [
        int(value)
        for value in db.execute(
            text(
                "SELECT DISTINCT mr.id FROM model_runs mr "
                "JOIN predictions p ON p.model_run_id = mr.id "
                "WHERE mr.status = 'failed' "
                "AND mr.market = ANY(CAST(:markets AS TEXT[])) "
                "ORDER BY mr.id"
            ),
            {"markets": target_markets},
        ).scalars()
    ]
    if model_run_id is not None:
        selected_id = int(model_run_id)
        if selected_id not in failed_run_ids:
            raise RuntimeError(
                f"Model run {selected_id} is not a failed run with retained prediction output."
            )
        selected_ids = [selected_id]
    else:
        selected_ids = failed_run_ids[: max(1, int(max_runs))]
    before_counts = _related_row_counts(db, selected_ids)
    result = {
        "cleanup_version": "failed-prediction-output-retention-v1",
        "generated_at": app_now_iso(),
        "status": "pass",
        "mode": "apply" if apply else "dry_run",
        "markets": target_markets,
        "candidate_failed_runs": len(failed_run_ids),
        "batch_failed_run_ids": selected_ids,
        "remaining_failed_runs_after_batch": max(
            0, len(failed_run_ids) - len(selected_ids)
        ),
        "before_row_counts": before_counts,
        "database_mutated": False,
    }
    if not apply:
        return result
    if model_run_id is None:
        raise RuntimeError("Failed-output apply requires an explicit model_run_id.")
    if str(approval_token or "") != FAILED_OUTPUT_RETENTION_APPLY_TOKEN:
        raise RuntimeError("Failed-output cleanup requires its explicit approval token.")
    required = {
        "evidence_manifest": evidence_manifest_path,
        "backup_receipt": backup_receipt_path,
        "restore_receipt": restore_receipt_path,
        "isolation_receipt": isolation_receipt_path,
        "hk_archive_receipt": hk_archive_receipt_path,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise RuntimeError(
            "Failed-output cleanup is missing evidence: " + ", ".join(missing)
        )
    paths = {name: Path(value).resolve() for name, value in required.items()}
    absent = [name for name, path in paths.items() if not path.is_file()]
    if absent:
        raise RuntimeError(
            "Failed-output cleanup evidence does not exist: " + ", ".join(absent)
        )
    evidence = _load_json(paths["evidence_manifest"])
    registered = evidence.get("files") or {}
    for name in (
        "backup_receipt",
        "restore_receipt",
        "isolation_receipt",
        "hk_archive_receipt",
    ):
        path = paths[name]
        if str((registered.get(path.name) or {}).get("sha256") or "") != _sha256(
            path
        ):
            raise RuntimeError(
                f"Evidence manifest hash mismatch or missing registration: {path.name}"
            )
    backup = _load_json(paths["backup_receipt"])
    restore = _load_json(paths["restore_receipt"])
    isolation = _load_json(paths["isolation_receipt"])
    hk_archive = _load_json(paths["hk_archive_receipt"])
    backup_path = Path(str(backup.get("backup_path") or "")).resolve()
    if (
        backup.get("status") != "pass"
        or not backup_path.is_file()
        or _sha256(backup_path) != str(backup.get("backup_sha256") or "")
        or _same_device(backup_path, paths["evidence_manifest"])
    ):
        raise RuntimeError("Latest PostgreSQL backup did not pass failed-output cleanup gates.")
    if not (
        restore.get("status") == "pass"
        and (restore.get("critical_table_parity") or {}).get("exact_match") is True
        and restore.get("restore_database_dropped") is True
        and restore.get("source_database_mutated") is False
        and restore.get("backup_sha256") == backup.get("backup_sha256")
    ):
        raise RuntimeError("Latest isolated PostgreSQL restore did not pass.")
    if not (
        isolation.get("status") == "pass"
        and isolation.get("audit_version") == "market-table-isolation-contract-v2"
        and set(isolation.get("markets") or []) == {"CN", "HK", "US"}
    ):
        raise RuntimeError("CN/HK/US physical isolation evidence did not pass.")
    if not (
        hk_archive.get("status") == "pass"
        and hk_archive.get("archive_version") == "legacy-market-fact-archive-v1"
        and hk_archive.get("market") == "HK"
        and hk_archive.get("verification_status") == "success"
    ):
        raise RuntimeError("Legacy HK market archive evidence did not pass.")
    if not selected_ids:
        result["message"] = "No failed prediction outputs remain."
        return result

    _delete_prediction_outputs(db, selected_ids)
    after_counts = _related_row_counts(db, selected_ids)
    if any(after_counts.values()):
        db.rollback()
        raise RuntimeError(
            "Failed-output cleanup found residual rows; transaction rolled back."
        )
    db.commit()
    result.update(
        {
            "finished_at": app_now_iso(),
            "after_row_counts": after_counts,
            "database_mutated": True,
            "backup_sha256": backup["backup_sha256"],
            "message": "One bounded failed-output batch was deleted and verified.",
        }
    )
    return result


def clean_model_history(
    db: Session,
    *,
    keep_model_runs_per_market: int = 20,
    keep_workspace_snapshots_per_type: int = 10,
    markets: tuple[str, ...] | list[str] = ("CN",),
    purge_failed_outputs: bool = True,
    purge_workspace_snapshots: bool = False,
    max_verified_success_runs_per_batch: int = 5,
    max_failed_runs_per_batch: int = 20,
    max_workspace_snapshots_per_batch: int = 100,
    include_full_candidate_row_counts: bool = False,
    apply: bool = False,
    approval_token: str | None = None,
    evidence_manifest_path: Path | str | None = None,
    backup_receipt_path: Path | str | None = None,
    restore_receipt_path: Path | str | None = None,
    dual_write_receipt_path: Path | str | None = None,
    cutover_receipt_path: Path | str | None = None,
    physical_only_receipt_path: Path | str | None = None,
    user_authorized_override: bool = False,
    _prevalidated_acceptance_gate: dict | None = None,
) -> dict:
    """Preview or apply safe hot-storage retention.

    Successful history is never removed unless a verified cold artifact exists.
    Failed-run prediction rows are not valid model output and can be removed
    independently; model-run audit records themselves are retained.
    """

    keep_runs = max(1, int(keep_model_runs_per_market))
    keep_snapshots = max(1, int(keep_workspace_snapshots_per_type))
    success_batch_limit = max(1, int(max_verified_success_runs_per_batch))
    failed_batch_limit = max(1, int(max_failed_runs_per_batch))
    workspace_batch_limit = max(1, int(max_workspace_snapshots_per_batch))
    target_markets = sorted({str(market).strip().upper() for market in markets if str(market).strip().upper() in {"CN", "US"}})
    if not target_markets:
        raise ValueError("At least one retention market (CN or US) is required.")
    if apply and len(target_markets) != 1:
        raise ValueError("Apply mode requires exactly one market per retention transaction.")
    acceptance_gate = None
    if apply:
        if user_authorized_override:
            if _prevalidated_acceptance_gate is not None:
                prevalidated_market = str(
                    _prevalidated_acceptance_gate.get("market") or ""
                ).upper()
                if not (
                    _prevalidated_acceptance_gate.get("status") == "pass"
                    and _prevalidated_acceptance_gate.get("gate")
                    == "user_authorized_retention_override_v1"
                    and prevalidated_market == target_markets[0]
                ):
                    raise RuntimeError(
                        "Prevalidated retention override does not match the target market."
                    )
                acceptance_gate = dict(_prevalidated_acceptance_gate)
            else:
                acceptance_gate = validate_user_authorized_retention_override_gate(
                    markets=target_markets,
                    approval_token=approval_token,
                    evidence_manifest_path=evidence_manifest_path,
                    backup_receipt_path=backup_receipt_path,
                )
        else:
            acceptance_gate = validate_retention_acceptance_gate(
                markets=target_markets,
                approval_token=approval_token,
                evidence_manifest_path=evidence_manifest_path,
                backup_receipt_path=backup_receipt_path,
                restore_receipt_path=restore_receipt_path,
                dual_write_receipt_path=dual_write_receipt_path,
                cutover_receipt_path=cutover_receipt_path,
                physical_only_receipt_path=physical_only_receipt_path,
            )
            cutover_active = (
                cn_physical_only_cutover_active(db)
                if target_markets[0] == "CN"
                else physical_only_cutover_active(db, target_markets[0])
            )
            if not cutover_active:
                raise RuntimeError(
                    f"{target_markets[0]} physical-only cutover marker is not active in the target database."
                )
        # Retention scans and deletes historical rows from the largest shared
        # tables. They must not inherit the deliberately short request-traffic
        # timeout, while the lock timeout remains strict to avoid disrupting
        # live publication.
        db.execute(text("SET LOCAL statement_timeout = '15min'"))
        db.execute(text("SET LOCAL lock_timeout = '10s'"))
    successful_rows = [
        dict(row)
        for row in db.execute(
            text(
                """
                SELECT mr.id, mr.market, pa.status AS artifact_status
                FROM model_runs mr
                LEFT JOIN prediction_artifacts pa ON pa.model_run_id = mr.id
                WHERE mr.status = 'success' AND mr.market = ANY(CAST(:markets AS TEXT[]))
                  AND (
                    EXISTS (SELECT 1 FROM predictions p WHERE p.model_run_id = mr.id)
                    OR EXISTS (SELECT 1 FROM live_predictions lp WHERE lp.model_run_id = mr.id)
                    OR EXISTS (SELECT 1 FROM model_chart_signals mcs WHERE mcs.model_run_id = mr.id)
                    OR EXISTS (SELECT 1 FROM cn_predictions cp WHERE cp.model_run_id = mr.id)
                    OR EXISTS (SELECT 1 FROM cn_live_predictions clp WHERE clp.model_run_id = mr.id)
                    OR EXISTS (SELECT 1 FROM cn_model_chart_signals cmcs WHERE cmcs.model_run_id = mr.id)
                    OR EXISTS (SELECT 1 FROM us_predictions up WHERE up.model_run_id = mr.id)
                    OR EXISTS (SELECT 1 FROM us_live_predictions ulp WHERE ulp.model_run_id = mr.id)
                    OR EXISTS (SELECT 1 FROM us_model_chart_signals umcs WHERE umcs.model_run_id = mr.id)
                    OR EXISTS (SELECT 1 FROM hk_predictions hp WHERE hp.model_run_id = mr.id)
                    OR EXISTS (SELECT 1 FROM hk_live_predictions hlp WHERE hlp.model_run_id = mr.id)
                    OR EXISTS (SELECT 1 FROM hk_model_chart_signals hmcs WHERE hmcs.model_run_id = mr.id)
                  )
                ORDER BY mr.market, COALESCE(mr.finished_at, mr.created_at) DESC, mr.id DESC
                """
            ),
            {"markets": target_markets},
        ).mappings().all()
    ]
    purgeable_success_ids, artifact_blocked_ids = _split_successful_run_candidates(
        successful_rows,
        keep_runs_per_market=keep_runs,
    )
    failed_run_ids = (
        [
            int(run_id)
            for run_id in db.execute(
                text(
                    """
                    SELECT DISTINCT p.model_run_id
                    FROM predictions p
                    JOIN model_runs mr ON mr.id = p.model_run_id
                    WHERE mr.status = 'failed'
                      AND mr.market = ANY(CAST(:markets AS TEXT[]))
                    ORDER BY p.model_run_id
                    """
                ),
                {"markets": target_markets},
            ).scalars().all()
        ]
        if purge_failed_outputs
        else []
    )
    snapshot_rows = [
        dict(row)
        for row in db.execute(
            text(
                "SELECT id, snapshot_type, snapshot_date, created_at "
                "FROM workspace_snapshots ORDER BY snapshot_type, id DESC"
            )
        ).mappings().all()
    ]
    snapshot_retention = select_workspace_snapshot_retention(
        snapshot_rows,
        minimum_latest_per_type=keep_snapshots,
    )
    snapshot_ids = list(snapshot_retention["candidate_delete_ids"])
    selected_success_ids = _bounded_retention_ids(
        purgeable_success_ids,
        limit=success_batch_limit,
    )
    selected_failed_ids = _bounded_retention_ids(
        failed_run_ids,
        limit=failed_batch_limit,
    )
    selected_snapshot_ids = (
        _bounded_retention_ids(snapshot_ids, limit=workspace_batch_limit)
        if purge_workspace_snapshots
        else []
    )
    candidate_run_ids = sorted(set(purgeable_success_ids + failed_run_ids))
    selected_run_ids = sorted(set(selected_success_ids + selected_failed_ids))
    batch_counts = _related_row_counts(db, selected_run_ids)
    result = {
        "status": "success",
        "mode": "apply" if apply else "dry_run",
        "keep_model_runs_per_market": keep_runs,
        "keep_workspace_snapshots_per_type": keep_snapshots,
        "markets": target_markets,
        "purge_failed_outputs": bool(purge_failed_outputs),
        "purge_workspace_snapshots": bool(purge_workspace_snapshots),
        "user_authorized_override": bool(user_authorized_override),
        "batch_limits": {
            "verified_success_runs": success_batch_limit,
            "failed_runs": failed_batch_limit,
            "workspace_snapshots": workspace_batch_limit,
        },
        "candidate_model_runs": len(candidate_run_ids),
        "candidate_verified_success_runs": len(purgeable_success_ids),
        "candidate_failed_runs": len(failed_run_ids),
        "blocked_success_runs_without_verified_artifact": len(artifact_blocked_ids),
        "blocked_model_run_ids": artifact_blocked_ids,
        "candidate_workspace_snapshots": len(snapshot_ids),
        "batch_model_runs": len(selected_run_ids),
        "batch_verified_success_runs": len(selected_success_ids),
        "batch_failed_runs": len(selected_failed_ids),
        "batch_workspace_snapshots": len(selected_snapshot_ids),
        "batch_model_run_ids": selected_run_ids,
        "batch_verified_success_run_ids": selected_success_ids,
        "batch_failed_run_ids": selected_failed_ids,
        "batch_workspace_snapshot_ids": selected_snapshot_ids,
        "candidate_row_count_scan_enabled": bool(
            include_full_candidate_row_counts
        ),
        "remaining_verified_success_runs_after_batch": max(
            0, len(purgeable_success_ids) - len(selected_success_ids)
        ),
        "remaining_failed_runs_after_batch": max(
            0, len(failed_run_ids) - len(selected_failed_ids)
        ),
        "remaining_workspace_snapshots_after_batch": max(
            0, len(snapshot_ids) - len(selected_snapshot_ids)
        ),
        "workspace_snapshot_retention_policy": snapshot_retention["policy"],
        "batch_row_counts": batch_counts,
    }
    if include_full_candidate_row_counts:
        result["candidate_row_counts"] = _related_row_counts(
            db,
            candidate_run_ids,
        )
    if not apply:
        result["message"] = "Storage-retention preview completed; no data was deleted."
        return result

    batch_started_at = app_now_iso()
    artifact_rows = list(
        db.execute(
            text(
                "SELECT model_run_id, artifact_path FROM prediction_artifacts "
                "WHERE status = 'verified' AND model_run_id = ANY(CAST(:ids AS INTEGER[]))"
            ),
            {"ids": selected_success_ids},
        ).mappings().all()
    ) if selected_success_ids else []
    registered_ids = {int(row["model_run_id"]) for row in artifact_rows}
    if registered_ids != set(selected_success_ids):
        raise RuntimeError("A purge candidate lost its verified artifact registration before deletion.")
    failed_integrity = [
        int(row["model_run_id"])
        for row in artifact_rows
        if verify_prediction_artifact(row["artifact_path"])["status"] != "success"
    ]
    if failed_integrity:
        raise RuntimeError(f"Prediction artifact integrity failed before deletion: {failed_integrity}")

    _delete_prediction_outputs(db, selected_run_ids)
    if selected_snapshot_ids:
        db.execute(
            text("DELETE FROM workspace_snapshots WHERE id = ANY(CAST(:ids AS INTEGER[]))"),
            {"ids": selected_snapshot_ids},
        )
    post_delete_counts = _related_row_counts(db, selected_run_ids)
    if any(post_delete_counts.values()):
        db.rollback()
        raise RuntimeError(
            "Retention batch verification found prediction rows after DELETE; transaction rolled back."
        )
    if selected_snapshot_ids:
        remaining_selected_snapshots = int(
            db.execute(
                text(
                    "SELECT COUNT(*) FROM workspace_snapshots "
                    "WHERE id = ANY(CAST(:ids AS INTEGER[]))"
                ),
                {"ids": selected_snapshot_ids},
            ).scalar()
            or 0
        )
        if remaining_selected_snapshots:
            db.rollback()
            raise RuntimeError(
                "Retention batch verification found workspace rows after DELETE; transaction rolled back."
            )
    db.commit()
    result["acceptance_gate"] = acceptance_gate
    result["batch_started_at"] = batch_started_at
    result["batch_finished_at"] = app_now_iso()
    result["post_delete_row_counts"] = post_delete_counts
    result["post_delete_workspace_snapshots"] = 0
    result["message"] = (
        "Safe storage retention completed. Successful runs without a verified artifact were preserved; "
        "run VACUUM (ANALYZE) later to refresh statistics."
    )
    return result
