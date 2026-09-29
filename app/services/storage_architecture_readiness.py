from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path

from app.services.prediction_dual_write_audit import dual_write_runtime_evidence_passed


DATABASE_SIZE_LIMIT_BYTES = 5 * 1024 * 1024 * 1024
HOT_PREDICTION_ROW_LIMIT = 3_000_000


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_acceptance_manifest(path: Path | str) -> dict:
    manifest_path = Path(path).resolve()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = payload.get("files") or {}
    if not isinstance(files, dict):
        raise RuntimeError("Acceptance manifest files must be an object.")
    root = manifest_path.parent
    missing: list[str] = []
    mismatched: list[str] = []
    for name, metadata in files.items():
        if not isinstance(metadata, dict):
            mismatched.append(str(name))
            continue
        candidate = (root / str(name)).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            mismatched.append(str(name))
            continue
        if not candidate.is_file():
            missing.append(str(name))
            continue
        if _sha256(candidate) != str(metadata.get("sha256") or ""):
            mismatched.append(str(name))
    return {
        "status": "pass" if not missing and not mismatched else "failed",
        "manifest_path": str(manifest_path),
        "registered_files": len(files),
        "missing_files": sorted(missing),
        "hash_mismatches": sorted(mismatched),
    }


def _registered_json_receipts(path: Path | str) -> list[tuple[str, dict]]:
    """Load only hash-verified JSON receipts registered by the manifest."""

    manifest_path = Path(path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = manifest_path.parent.resolve()
    receipts: list[tuple[str, dict]] = []
    for name, metadata in (manifest.get("files") or {}).items():
        if not isinstance(metadata, dict):
            continue
        candidate = (root / str(name)).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.suffix.lower() != ".json" or not candidate.is_file():
            continue
        if _sha256(candidate) != str((metadata or {}).get("sha256") or ""):
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(payload, dict):
            receipts.append((str(name), payload))
    return receipts


def audit_registered_hard_gate_evidence(path: Path | str) -> dict:
    """Evaluate frozen non-live hard gates from registered immutable receipts.

    Live facts, capacity, cutover state and database size are deliberately
    audited elsewhere on every run.  This profile covers the expensive or
    disruptive acceptance exercises that are represented by immutable
    evidence files.
    """

    receipts = _registered_json_receipts(path)

    def matching(predicate) -> list[tuple[str, dict]]:
        return [(name, payload) for name, payload in receipts if predicate(payload)]

    dual_read = matching(
        lambda payload: payload.get("status") == "success"
        and int(payload.get("verified_runs") or 0) >= 100
        and int(payload.get("artifact_integrity_failures") or 0) == 0
        and int(payload.get("table_parity_failures") or 0) == 0
    )
    recoveries = matching(
        lambda payload: payload.get("status") == "success"
        and payload.get("restore_target") == "isolated_sqlite"
        and payload.get("integrity_status") == "success"
        and (payload.get("top_k") or {}).get("exact_match") is True
        and int(payload.get("production_rows_changed") or 0) == 0
    )
    recovery_run_ids = sorted(
        {
            int(payload.get("model_run_id") or 0)
            for _, payload in recoveries
            if int(payload.get("model_run_id") or 0) > 0
        }
    )
    performance = matching(
        lambda payload: payload.get("benchmark_version")
        == "storage-architecture-read-v1"
        and payload.get("status") == "pass"
        and bool(payload.get("results"))
        and all(
            isinstance(item, dict) and item.get("status") == "pass"
            for item in (payload.get("results") or {}).values()
        )
    )
    rollback = matching(
        lambda payload: payload.get("drill_version")
        == "prediction-read-rollback-runtime-v1"
        and payload.get("status") == "pass"
        and (payload.get("latest_candidate_parity") or {}).get("exact_match")
        is True
        and int(payload.get("observed_completion_seconds_upper_bound") or 10**9)
        <= int(payload.get("hard_limit_seconds") or 0)
        and payload.get("database_mutated") is False
    )
    publication_guard = matching(
        lambda payload: payload.get("audit_version")
        == "prediction-publication-guard-v1"
        and payload.get("status") == "pass"
        and bool(payload.get("checks"))
        and all(value is True for value in (payload.get("checks") or {}).values())
    )
    type_partition = matching(
        lambda payload: payload.get("audit_version")
        == "market-storage-type-partition-governance-v1"
        and payload.get("status") == "pass"
        and bool(payload.get("checks"))
        and all(value is True for value in (payload.get("checks") or {}).values())
    )
    authenticated_pages = matching(
        lambda payload: payload.get("benchmark_version")
        == "authenticated-page-latency-v1"
        and payload.get("status") == "pass"
        and payload.get("read_only") is True
    )
    app_settings = matching(
        lambda payload: payload.get("audit_version") == "app-setting-storage-v1"
        and payload.get("status") == "pass"
        and int(payload.get("oversized_inline_count") or 0) == 0
        and int(payload.get("threshold_bytes") or 0) == 32 * 1024
    )
    checks = {
        "artifact_integrity_and_100_dual_reads": bool(dual_read),
        "at_least_three_isolated_cold_recoveries": len(recovery_run_ids) >= 3,
        "hot_and_cold_read_performance": bool(performance),
        "runtime_repository_rollback_under_30_minutes": bool(rollback),
        "publication_staging_and_explanation_guard": bool(publication_guard),
        "native_dates_partition_and_index_governance": bool(type_partition),
        "authenticated_business_pages": bool(authenticated_pages),
        "app_settings_inline_limit": bool(app_settings),
    }
    matched_receipts = {
        "dual_read": [name for name, _ in dual_read],
        "cold_recovery": [name for name, _ in recoveries],
        "performance": [name for name, _ in performance],
        "rollback": [name for name, _ in rollback],
        "publication_guard": [name for name, _ in publication_guard],
        "type_partition": [name for name, _ in type_partition],
        "authenticated_pages": [name for name, _ in authenticated_pages],
        "app_settings": [name for name, _ in app_settings],
    }
    return {
        "profile_version": "storage-architecture-frozen-hard-gates-v1",
        "status": "pass" if all(checks.values()) else "failed",
        "checks": checks,
        "blockers": [name for name, passed in checks.items() if not passed],
        "registered_json_receipts": len(receipts),
        "recovery_run_ids": recovery_run_ids,
        "matched_receipts": matched_receipts,
    }


def verify_us_independent_signoff(
    *,
    evidence_manifest_path: Path | str,
    receipt_path: Path | str | None,
) -> dict:
    """Verify an explicit US production signoff; absence always fails closed."""

    if receipt_path is None:
        return {"status": "pending", "passed": False, "reason": "receipt_missing"}
    manifest_path = Path(evidence_manifest_path).resolve()
    target = Path(receipt_path).resolve()
    if not target.is_file():
        return {"status": "failed", "passed": False, "reason": "receipt_missing"}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    registered = (manifest.get("files") or {}).get(target.name)
    registered_hash_ok = bool(
        isinstance(registered, dict)
        and str(registered.get("sha256") or "") == _sha256(target)
    )
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"status": "failed", "passed": False, "reason": "invalid_json"}
    checks = payload.get("checks") if isinstance(payload, dict) else None
    recovery_ids = payload.get("cold_recovery_run_ids") if isinstance(payload, dict) else None
    production_ids = payload.get("production_run_ids") if isinstance(payload, dict) else None
    passed = bool(
        isinstance(payload, dict)
        and payload.get("signoff_version") == "us-market-storage-signoff-v1"
        and payload.get("status") == "pass"
        and payload.get("market") == "US"
        and registered_hash_ok
        and isinstance(checks, dict)
        and bool(checks)
        and all(value is True for value in checks.values())
        and isinstance(recovery_ids, list)
        and len({int(value) for value in recovery_ids}) >= 3
        and isinstance(production_ids, list)
        and len({int(value) for value in production_ids}) >= 1
        and int(payload.get("wrong_market_rows") or 0) == 0
    )
    return {
        "status": "pass" if passed else "failed",
        "passed": passed,
        "receipt": str(target),
        "registered_hash_ok": registered_hash_ok,
        "cold_recovery_run_ids": list(recovery_ids or []),
        "production_run_ids": list(production_ids or []),
    }


def post_cutover_backup_restore_passed(
    *,
    evidence_manifest_path: Path | str,
    backup_receipt_path: Path | str | None,
    restore_receipt_path: Path | str | None,
    physical_only_generated_at: str | None,
) -> dict:
    if backup_receipt_path is None or restore_receipt_path is None:
        return {"status": "missing", "passed": False}
    backup_path = Path(backup_receipt_path).resolve()
    restore_path = Path(restore_receipt_path).resolve()
    if not backup_path.is_file() or not restore_path.is_file():
        return {"status": "missing", "passed": False}
    backup = json.loads(backup_path.read_text(encoding="utf-8"))
    restore = json.loads(restore_path.read_text(encoding="utf-8"))
    evidence_manifest = json.loads(
        Path(evidence_manifest_path).resolve().read_text(encoding="utf-8")
    )
    registered = evidence_manifest.get("files") or {}
    registered_receipts = all(
        str((registered.get(path.name) or {}).get("sha256") or "")
        == _sha256(path)
        for path in (backup_path, restore_path)
    )
    frozen_backup_path = Path(str(backup.get("backup_path") or "")).resolve()
    backup_file_verified = bool(
        frozen_backup_path.is_file()
        and _sha256(frozen_backup_path)
        == str(backup.get("backup_sha256") or "")
    )
    try:
        backup_time = datetime.fromisoformat(str(backup.get("generated_at") or ""))
        restore_time = datetime.fromisoformat(str(restore.get("generated_at") or ""))
        canary_time = datetime.fromisoformat(str(physical_only_generated_at or ""))
    except ValueError:
        return {"status": "failed", "passed": False, "reason": "invalid_timestamp"}
    exact_match = (restore.get("critical_table_parity") or {}).get("exact_match") is True
    passed = bool(
        backup.get("status") == "pass"
        and restore.get("status") == "pass"
        and registered_receipts
        and backup_file_verified
        and exact_match
        and restore.get("restore_database_dropped") is True
        and restore.get("source_database_mutated") is False
        and str(restore.get("backup_sha256") or "")
        == str(backup.get("backup_sha256") or "")
        and backup_time >= canary_time
        and restore_time >= backup_time
    )
    return {
        "status": "pass" if passed else "failed",
        "passed": passed,
        "backup_receipt": str(backup_path),
        "restore_receipt": str(restore_path),
        "backup_generated_at": backup_time.isoformat(),
        "restore_generated_at": restore_time.isoformat(),
        "critical_table_exact_match": exact_match,
        "receipts_registered": registered_receipts,
        "backup_file_verified": backup_file_verified,
    }


def summarize_storage_architecture_readiness(
    *,
    manifest: dict,
    dual_write: dict,
    capacity: dict,
    isolation: dict,
    retention_preview: dict,
    frozen_evidence: dict,
    failed_predictions: dict,
    hot_prediction_rows: int,
    cutover_active: bool,
    physical_only_canary_passed: bool,
    physical_only_generated_at: str | None,
    post_cutover_backup_restore: dict,
    database_bytes: int,
    us_signoff: dict,
    us_hold: bool = True,
) -> dict:
    dual_write_passed = bool(
        dual_write.get("status") == "pass"
        and dual_write_runtime_evidence_passed(dual_write)
        and int(dual_write.get("remaining_runs") or 0) == 0
        and len(dual_write.get("passed_runs") or []) >= 5
        and dual_write.get("consecutive_trade_dates") is True
    )
    archive_ready = bool(
        retention_preview.get("status") == "success"
        and int(
            retention_preview.get(
                "blocked_success_runs_without_verified_artifact"
            )
            or 0
        )
        == 0
    )
    size_passed = int(database_bytes) <= DATABASE_SIZE_LIMIT_BYTES
    capacity_passed = bool(
        capacity.get("status") == "pass"
        and capacity.get("acceptance_scope") == "post_cutover_under_size"
        and capacity.get("not_before_date")
        and int(capacity.get("maximum_database_bytes") or 0)
        == DATABASE_SIZE_LIMIT_BYTES
        and int(capacity.get("sample_count") or 0) >= 6
    )
    checks = {
        "evidence_manifest_integrity": manifest.get("status") == "pass",
        "registered_hard_gate_evidence": frozen_evidence.get("status") == "pass",
        "cn_us_physical_isolation": isolation.get("status") == "pass",
        "cn_failed_runs_online_rows_zero": failed_predictions.get("status") == "pass",
        "physical_hot_prediction_rows_at_most_3m": int(hot_prediction_rows)
        <= HOT_PREDICTION_ROW_LIMIT,
        "cn_archive_candidates_verified": archive_ready,
        "cn_five_consecutive_dual_writes": dual_write_passed,
        "cn_physical_only_cutover_active": bool(cutover_active),
        "cn_physical_only_production_canary": bool(physical_only_canary_passed),
        "post_cutover_backup_restore": bool(
            post_cutover_backup_restore.get("passed") is True
        ),
        "postgresql_size_at_most_5_gib": size_passed,
        "five_day_growth_at_most_20_mib": capacity_passed,
        "us_independent_production_signoff": bool(
            not us_hold and us_signoff.get("passed") is True
        ),
    }
    deletion_ready = all(
        checks[name]
        for name in (
            "evidence_manifest_integrity",
            "registered_hard_gate_evidence",
            "cn_us_physical_isolation",
            "cn_archive_candidates_verified",
            "cn_five_consecutive_dual_writes",
            "cn_physical_only_cutover_active",
            "cn_physical_only_production_canary",
            "post_cutover_backup_restore",
        )
    )
    cn_final_ready = bool(
        deletion_ready
        and size_passed
        and capacity_passed
        and checks["cn_failed_runs_online_rows_zero"]
        and checks["physical_hot_prediction_rows_at_most_3m"]
    )
    overall_ready = cn_final_ready and checks["us_independent_production_signoff"]
    foundation_checks = (
        "evidence_manifest_integrity",
        "registered_hard_gate_evidence",
        "cn_us_physical_isolation",
        "cn_archive_candidates_verified",
    )
    if not all(checks[name] for name in foundation_checks):
        stage = "pre_cutover_hard_gate_failed"
    elif not dual_write_passed:
        stage = "collecting_pre_cutover_evidence"
    elif not cutover_active:
        stage = "ready_for_cn_physical_only_cutover"
    elif not physical_only_canary_passed:
        stage = "awaiting_cn_physical_only_production_canary"
    elif not post_cutover_backup_restore.get("passed"):
        stage = "awaiting_post_cutover_backup_restore"
    elif not size_passed:
        stage = "ready_for_bounded_retention"
    elif not checks["cn_failed_runs_online_rows_zero"] or not checks[
        "physical_hot_prediction_rows_at_most_3m"
    ]:
        stage = "post_cleanup_data_limits_failed"
    elif not capacity_passed:
        stage = "collecting_post_cleanup_capacity_evidence"
    elif us_hold:
        stage = "cn_complete_us_hold"
    else:
        stage = "complete"
    blockers = [name for name, passed in checks.items() if not passed]
    return {
        "readiness_version": "storage-architecture-readiness-v2",
        "status": "pass" if overall_ready else "in_progress",
        "stage": stage,
        "checks": checks,
        "blockers": blockers,
        "cn_deletion_gate_ready": deletion_ready,
        "cn_final_acceptance_ready": cn_final_ready,
        "overall_acceptance_ready": overall_ready,
        "database_bytes": int(database_bytes),
        "database_size_limit_bytes": DATABASE_SIZE_LIMIT_BYTES,
        "hot_prediction_rows": int(hot_prediction_rows),
        "hot_prediction_row_limit": HOT_PREDICTION_ROW_LIMIT,
        "dual_write_progress": {
            "passed_runs": len(dual_write.get("passed_runs") or []),
            "required_runs": int(dual_write.get("required_runs") or 5),
            "remaining_runs": int(dual_write.get("remaining_runs") or 0),
            "trade_dates": list(dual_write.get("sequence_trade_dates") or []),
        },
        "capacity_progress": {
            "status": capacity.get("status"),
            "sample_count": int(capacity.get("sample_count") or 0),
            "required_samples": int(capacity.get("required_intervals") or 5) + 1,
            "sample_dates": list(capacity.get("sample_dates") or []),
            "average_daily_growth_bytes": capacity.get("average_daily_growth_bytes"),
            "limit_bytes": capacity.get("limit_bytes"),
            "acceptance_scope": capacity.get("acceptance_scope"),
            "not_before_date": capacity.get("not_before_date"),
            "maximum_database_bytes": capacity.get("maximum_database_bytes"),
            "excluded_before_window": int(
                capacity.get("excluded_before_window") or 0
            ),
            "excluded_above_maximum": int(
                capacity.get("excluded_above_maximum") or 0
            ),
        },
        "physical_only_generated_at": physical_only_generated_at,
        "us_status": "hold" if us_hold else "active",
        "us_signoff": us_signoff,
    }


def _five_consecutive_dual_writes_passed(dual_write: dict) -> bool:
    return bool(
        dual_write.get("status") == "pass"
        and dual_write_runtime_evidence_passed(dual_write)
        and int(dual_write.get("remaining_runs") or 0) == 0
        and len(dual_write.get("passed_runs") or []) >= 5
        and dual_write.get("consecutive_trade_dates") is True
    )


def _archive_candidates_verified(retention_preview: dict) -> bool:
    return bool(
        retention_preview.get("status") == "success"
        and int(
            retention_preview.get(
                "blocked_success_runs_without_verified_artifact"
            )
            or 0
        )
        == 0
    )


def _post_cutover_capacity_passed(capacity: dict) -> bool:
    return bool(
        capacity.get("status") == "pass"
        and capacity.get("acceptance_scope") == "post_cutover_under_size"
        and capacity.get("not_before_date")
        and int(capacity.get("maximum_database_bytes") or 0)
        == DATABASE_SIZE_LIMIT_BYTES
        and int(capacity.get("sample_count") or 0) >= 6
    )


def _market_readiness_stage(
    *,
    market: str,
    shared_foundation_passed: bool,
    scheduler_enabled: bool,
    archive_ready: bool,
    dual_write_passed: bool,
    failed_rows_zero: bool,
    cutover_active: bool,
    physical_only_canary_passed: bool,
    post_cutover_backup_restore_passed: bool,
    database_size_passed: bool,
    hot_prediction_limit_passed: bool,
    capacity_passed: bool,
    independent_signoff_passed: bool,
) -> str:
    if not scheduler_enabled:
        return "operational_scheduler_disabled"
    if not shared_foundation_passed:
        return "pre_cutover_hard_gate_failed"
    if not archive_ready:
        return "awaiting_rolling_archive"
    if not failed_rows_zero:
        return "failed_output_residue_detected"
    if not dual_write_passed:
        return "collecting_pre_cutover_evidence"
    if not cutover_active:
        return "ready_for_physical_only_cutover"
    if not physical_only_canary_passed:
        return "awaiting_physical_only_production_canary"
    if not post_cutover_backup_restore_passed:
        return "awaiting_post_cutover_backup_restore"
    if not database_size_passed:
        return "ready_for_bounded_retention"
    if not hot_prediction_limit_passed:
        return "post_cleanup_data_limits_failed"
    if not capacity_passed:
        return "collecting_post_cleanup_capacity_evidence"
    if market == "US" and not independent_signoff_passed:
        return "awaiting_independent_production_signoff"
    return "complete"


def summarize_multi_market_storage_architecture_readiness(
    *,
    manifest: dict,
    capacity: dict,
    isolation: dict,
    market_states: dict[str, dict],
    frozen_evidence: dict,
    hot_prediction_rows_by_market: dict[str, int],
    post_cutover_backup_restore: dict,
    database_bytes: int,
    us_signoff: dict,
) -> dict:
    """Summarize CN and US readiness without conflating operation and signoff.

    Each market owns its production, archive, cutover and canary gates.  Backup,
    database size and the post-cleanup capacity window are shared because both
    markets occupy the same PostgreSQL database.  Missing market state fails
    closed instead of silently falling back to the historical CN-only view.
    """

    required_markets = ("CN", "US")
    normalized_states = {
        market: dict(market_states.get(market) or {}) for market in required_markets
    }
    size_passed = int(database_bytes) <= DATABASE_SIZE_LIMIT_BYTES
    capacity_passed = _post_cutover_capacity_passed(capacity)
    hot_rows = {
        market: int(hot_prediction_rows_by_market.get(market) or 0)
        for market in required_markets
    }
    hot_total = sum(hot_rows.values())
    hot_limit_passed = hot_total <= HOT_PREDICTION_ROW_LIMIT
    shared_checks = {
        "evidence_manifest_integrity": manifest.get("status") == "pass",
        "registered_hard_gate_evidence": frozen_evidence.get("status") == "pass",
        "cn_us_physical_isolation": isolation.get("status") == "pass",
        "post_cutover_backup_restore": bool(
            post_cutover_backup_restore.get("passed") is True
        ),
        "postgresql_size_at_most_5_gib": size_passed,
        "five_day_growth_at_most_20_mib": capacity_passed,
        "physical_hot_prediction_rows_at_most_3m": hot_limit_passed,
    }
    foundation_passed = all(
        shared_checks[name]
        for name in (
            "evidence_manifest_integrity",
            "registered_hard_gate_evidence",
            "cn_us_physical_isolation",
        )
    )

    stage_next_actions = {
        "operational_scheduler_disabled": "enable_market_scheduler",
        "pre_cutover_hard_gate_failed": "repair_shared_foundation_evidence",
        "failed_output_residue_detected": "clean_failed_prediction_outputs",
        "awaiting_rolling_archive": "archive_unverified_rolling_window_runs",
        "collecting_pre_cutover_evidence": "continue_daily_dual_write_observation",
        "ready_for_physical_only_cutover": "activate_market_physical_only",
        "awaiting_physical_only_production_canary": "run_physical_only_production_canary",
        "awaiting_post_cutover_backup_restore": "create_and_restore_post_cutover_backup",
        "ready_for_bounded_retention": "apply_bounded_market_retention_batches",
        "post_cleanup_data_limits_failed": "repair_post_cleanup_data_limits",
        "collecting_post_cleanup_capacity_evidence": "collect_six_capacity_samples",
        "awaiting_independent_production_signoff": "create_us_independent_signoff",
        "complete": "none",
    }
    market_results: dict[str, dict] = {}
    flat_checks = dict(shared_checks)
    for market in required_markets:
        state = normalized_states[market]
        dual_write = dict(state.get("dual_write") or {})
        retention_preview = dict(state.get("retention_preview") or {})
        failed_predictions = dict(state.get("failed_predictions") or {})
        scheduler_enabled = state.get("scheduler_enabled") is True
        archive_ready = _archive_candidates_verified(retention_preview)
        dual_write_passed = _five_consecutive_dual_writes_passed(dual_write)
        failed_rows_zero = failed_predictions.get("status") == "pass"
        cutover_active = state.get("cutover_active") is True
        canary_passed = state.get("physical_only_canary_passed") is True
        signoff_passed = bool(
            market != "US" or us_signoff.get("passed") is True
        )
        prefix = market.lower()
        market_checks = {
            "operational_scheduler_enabled": scheduler_enabled,
            "archive_candidates_verified": archive_ready,
            "five_consecutive_dual_writes": dual_write_passed,
            "failed_runs_online_rows_zero": failed_rows_zero,
            "physical_only_cutover_active": cutover_active,
            "physical_only_production_canary": canary_passed,
        }
        if market == "US":
            market_checks["independent_production_signoff"] = signoff_passed
        for name, passed in market_checks.items():
            flat_checks[f"{prefix}_{name}"] = passed

        deletion_gate_ready = bool(
            foundation_passed
            and scheduler_enabled
            and archive_ready
            and dual_write_passed
            and failed_rows_zero
            and cutover_active
            and canary_passed
            and shared_checks["post_cutover_backup_restore"]
        )
        final_ready = bool(
            deletion_gate_ready
            and size_passed
            and capacity_passed
            and hot_limit_passed
            and signoff_passed
        )
        stage = _market_readiness_stage(
            market=market,
            shared_foundation_passed=foundation_passed,
            scheduler_enabled=scheduler_enabled,
            archive_ready=archive_ready,
            dual_write_passed=dual_write_passed,
            failed_rows_zero=failed_rows_zero,
            cutover_active=cutover_active,
            physical_only_canary_passed=canary_passed,
            post_cutover_backup_restore_passed=shared_checks[
                "post_cutover_backup_restore"
            ],
            database_size_passed=size_passed,
            hot_prediction_limit_passed=hot_limit_passed,
            capacity_passed=capacity_passed,
            independent_signoff_passed=signoff_passed,
        )
        market_results[market] = {
            "market": market,
            "operational_status": (
                "active" if scheduler_enabled else "disabled"
            ),
            "signoff_status": (
                "not_required"
                if market != "US"
                else "complete"
                if signoff_passed
                else "pending"
            ),
            "stage": stage,
            "next_action": stage_next_actions[stage],
            "checks": market_checks,
            "blockers": [
                name for name, passed in market_checks.items() if not passed
            ],
            "deletion_gate_ready": deletion_gate_ready,
            "final_acceptance_ready": final_ready,
            "dual_write_progress": {
                "passed_runs": len(dual_write.get("passed_runs") or []),
                "required_runs": int(dual_write.get("required_runs") or 5),
                "remaining_runs": int(dual_write.get("remaining_runs") or 0),
                "trade_dates": list(dual_write.get("sequence_trade_dates") or []),
            },
            "archive_progress": {
                "verified_candidate_runs": int(
                    retention_preview.get("candidate_verified_success_runs") or 0
                ),
                "blocked_unverified_runs": int(
                    retention_preview.get(
                        "blocked_success_runs_without_verified_artifact"
                    )
                    or 0
                ),
                "blocked_model_run_ids": [
                    int(value)
                    for value in (retention_preview.get("blocked_model_run_ids") or [])
                ],
                "next_bounded_batch_model_run_ids": [
                    int(value)
                    for value in (retention_preview.get("batch_model_run_ids") or [])
                ],
            },
            "failed_run_progress": {
                "status": failed_predictions.get("status"),
                "failed_run_count": int(
                    failed_predictions.get("failed_run_count") or 0
                ),
            },
            "hot_prediction_rows": hot_rows[market],
            "physical_only_generated_at": state.get(
                "physical_only_generated_at"
            ),
        }

    overall_ready = all(
        market_results[market]["final_acceptance_ready"]
        for market in required_markets
    )
    stage_order = (
        "operational_scheduler_disabled",
        "pre_cutover_hard_gate_failed",
        "failed_output_residue_detected",
        "awaiting_rolling_archive",
        "collecting_pre_cutover_evidence",
        "ready_for_physical_only_cutover",
        "awaiting_physical_only_production_canary",
        "awaiting_post_cutover_backup_restore",
        "ready_for_bounded_retention",
        "post_cleanup_data_limits_failed",
        "collecting_post_cleanup_capacity_evidence",
        "awaiting_independent_production_signoff",
        "complete",
    )
    market_stages = {item["stage"] for item in market_results.values()}
    stage = next(value for value in stage_order if value in market_stages)
    blockers = [name for name, passed in flat_checks.items() if not passed]

    return {
        "readiness_version": "storage-architecture-readiness-v3",
        "status": "pass" if overall_ready else "in_progress",
        "stage": stage,
        "next_action": stage_next_actions[stage],
        "checks": flat_checks,
        "blockers": blockers,
        "markets": market_results,
        "deletion_gate_ready_by_market": {
            market: market_results[market]["deletion_gate_ready"]
            for market in required_markets
        },
        "final_acceptance_ready_by_market": {
            market: market_results[market]["final_acceptance_ready"]
            for market in required_markets
        },
        "overall_acceptance_ready": overall_ready,
        "database_bytes": int(database_bytes),
        "database_size_limit_bytes": DATABASE_SIZE_LIMIT_BYTES,
        "hot_prediction_rows_by_market": hot_rows,
        "hot_prediction_rows": hot_total,
        "hot_prediction_row_limit": HOT_PREDICTION_ROW_LIMIT,
        "capacity_progress": {
            "status": capacity.get("status"),
            "sample_count": int(capacity.get("sample_count") or 0),
            "required_samples": int(capacity.get("required_intervals") or 5) + 1,
            "sample_dates": list(capacity.get("sample_dates") or []),
            "average_daily_growth_bytes": capacity.get("average_daily_growth_bytes"),
            "limit_bytes": capacity.get("limit_bytes"),
            "acceptance_scope": capacity.get("acceptance_scope"),
            "not_before_date": capacity.get("not_before_date"),
            "maximum_database_bytes": capacity.get("maximum_database_bytes"),
        },
        "us_operational_status": market_results["US"]["operational_status"],
        "us_signoff_status": market_results["US"]["signoff_status"],
        "us_signoff": us_signoff,
    }
