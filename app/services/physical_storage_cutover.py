from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.services.market_storage_routing import (
    MARKET_PHYSICAL_CUTOVER_VERSION,
    cn_physical_only_cutover_active,
    normalize_fact_market,
    physical_cutover_setting_key,
    physical_only_cutover_active,
)
from app.services.repository import AppSettingRepository
from app.services.storage_retention import (
    retention_apply_token,
    validate_retention_foundation_gate,
)
from app.services.time_utils import app_now_iso


CUTOVER_APPLY_TOKEN = "ACTIVATE_VERIFIED_CN_PHYSICAL_ONLY"
CUTOVER_ROLLBACK_TOKEN = "ROLLBACK_CN_PHYSICAL_ONLY"


def cutover_apply_token(market: str | None) -> str:
    return f"ACTIVATE_VERIFIED_{normalize_fact_market(market)}_PHYSICAL_ONLY"


def cutover_rollback_token(market: str | None) -> str:
    return f"ROLLBACK_{normalize_fact_market(market)}_PHYSICAL_ONLY"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"Cutover evidence is not a JSON object: {path}")
    return payload


def _registered_evidence(manifest: dict, *, label: str, path: Path) -> dict:
    registered = ((manifest.get("files") or {}).get(path.name) or {}).get("sha256")
    actual = _sha256(path)
    if str(registered or "") != actual:
        raise RuntimeError(
            f"Cutover evidence manifest hash mismatch or missing registration: {path.name}"
        )
    payload = _load_json(path)
    if payload.get("status") != "pass":
        raise RuntimeError(f"{label} receipt has not passed.")
    return payload


def _required_checks(payload: dict, names: tuple[str, ...], *, label: str) -> None:
    checks = payload.get("checks") or {}
    if not all(checks.get(name) is True for name in names):
        missing = [name for name in names if checks.get(name) is not True]
        raise RuntimeError(f"{label} is missing hard checks: {', '.join(missing)}")


def validate_market_physical_cutover_gate(
    *,
    market: str,
    approval_token: str | None,
    evidence_manifest_path: Path | str,
    backup_receipt_path: Path | str,
    restore_receipt_path: Path | str,
    dual_write_receipt_path: Path | str,
    live_audit_receipt_path: Path | str,
    hot_audit_receipt_path: Path | str,
    snapshot_audit_receipt_path: Path | str,
    isolation_audit_receipt_path: Path | str,
) -> dict:
    """Validate one market before disabling its shared-table compatibility mirror."""

    normalized = normalize_fact_market(market)
    if str(approval_token or "") != cutover_apply_token(normalized):
        raise RuntimeError(
            f"{normalized} physical cutover requires its explicit activation token."
        )
    base_gate = validate_retention_foundation_gate(
        markets=[normalized],
        approval_token=retention_apply_token(normalized),
        evidence_manifest_path=evidence_manifest_path,
        backup_receipt_path=backup_receipt_path,
        restore_receipt_path=restore_receipt_path,
        dual_write_receipt_path=dual_write_receipt_path,
    )
    paths = {
        "evidence_manifest": Path(evidence_manifest_path).resolve(),
        "dual_write": Path(dual_write_receipt_path).resolve(),
        "live": Path(live_audit_receipt_path).resolve(),
        "hot": Path(hot_audit_receipt_path).resolve(),
        "snapshots": Path(snapshot_audit_receipt_path).resolve(),
        "isolation": Path(isolation_audit_receipt_path).resolve(),
    }
    absent = [name for name, path in paths.items() if not path.is_file()]
    if absent:
        raise RuntimeError(
            f"{normalized} physical cutover evidence files do not exist: "
            + ", ".join(absent)
        )
    manifest = _load_json(paths["evidence_manifest"])
    dual_write = _load_json(paths["dual_write"])
    sequence_runs = [int(value) for value in dual_write.get("sequence_runs") or []]
    if dual_write.get("market") != normalized or len(sequence_runs) < 5:
        raise RuntimeError(
            f"{normalized} physical cutover requires five market-scoped sequence runs."
        )
    latest_run_id = int(sequence_runs[0])

    live = _registered_evidence(
        manifest,
        label=f"{normalized} physical live",
        path=paths["live"],
    )
    hot = _registered_evidence(
        manifest,
        label=f"{normalized} physical hot",
        path=paths["hot"],
    )
    snapshots = _registered_evidence(
        manifest,
        label=f"{normalized} physical snapshots",
        path=paths["snapshots"],
    )
    isolation = _registered_evidence(
        manifest,
        label="CN/HK/US twenty-seven-table isolation",
        path=paths["isolation"],
    )
    for label, payload, version in (
        ("live audit", live, "market-physical-live-storage-v2"),
        ("hot audit", hot, "market-physical-hot-storage-v2"),
        ("snapshot audit", snapshots, "market-physical-snapshots-v2"),
    ):
        if payload.get("audit_version") != version or payload.get("market") != normalized:
            raise RuntimeError(f"{normalized} {label} has the wrong market or version.")
    if int(live.get("model_run_id") or 0) != latest_run_id:
        raise RuntimeError(f"{normalized} live audit does not cover the latest accepted run.")
    if int(hot.get("model_run_id") or 0) != latest_run_id:
        raise RuntimeError(f"{normalized} hot audit does not cover the latest accepted run.")
    _required_checks(
        live,
        (
            "physical_tables_exist",
            "legacy_exact_match",
            "wrong_market_rows_zero",
            "symbol_market_mismatch_zero",
            "model_run_market_mismatch_zero",
            "constraint_rejects_wrong_market",
            "all_market_constraints_present",
            "repository_reads_market_physical_table",
            "composite_symbol_market_fks_present",
        ),
        label=f"{normalized} physical live audit",
    )
    _required_checks(
        hot,
        (
            "physical_tables_exist",
            "source_exact_match",
            "wrong_market_rows_zero",
            "symbol_market_mismatch_zero",
            "model_run_market_mismatch_zero",
            "child_orphans_zero",
            "constraint_rejects_wrong_market",
            "all_market_constraints_present",
            "child_foreign_keys_cascade",
            "repository_reads_market_physical_table",
            "composite_symbol_market_fks_present",
        ),
        label=f"{normalized} physical hot audit",
    )
    _required_checks(
        snapshots,
        (
            "physical_tables_exist",
            "source_exact_match",
            "wrong_market_rows_zero",
            "symbol_market_mismatch_zero",
            "all_market_constraints_present",
            "repositories_read_market_physical_tables",
            "explain_is_market_local",
            "composite_symbol_market_fks_present",
        ),
        label=f"{normalized} physical snapshot audit",
    )
    if (
        isolation.get("audit_version") != "market-table-isolation-contract-v2"
        or set(isolation.get("markets") or []) != {"CN", "HK", "US"}
    ):
        raise RuntimeError("CN/HK/US isolation receipt has the wrong contract version.")
    _required_checks(
        isolation,
        (
            "twenty_seven_physical_tables_exist",
            "market_table_sets_pairwise_disjoint",
            "market_table_prefixes",
            "required_physical_write_markets_cn_hk_us",
            "read_rollout_defaults_include_cn_hk_us",
            "parent_market_check_constraints_present",
            "parent_wrong_market_rows_zero",
            "parent_symbol_market_fks_compliant",
            "child_tables_reference_same_market_parent",
        ),
        label="CN/HK/US isolation audit",
    )
    table_contract = isolation.get("table_contract") or {}
    if set(table_contract) != {"CN", "HK", "US"} or any(
        len((table_contract.get(item) or {})) != 9 for item in ("CN", "HK", "US")
    ):
        raise RuntimeError("Isolation audit does not contain the required 9+9+9 table contract.")
    return {
        "status": "pass",
        "market": normalized,
        "latest_run_id": latest_run_id,
        "dual_write_run_ids": sequence_runs,
        "dual_write_trade_dates": base_gate["dual_write_trade_dates"],
        "backup_sha256": base_gate["backup_sha256"],
        "evidence_sha256": {
            name: _sha256(path)
            for name, path in paths.items()
            if name != "evidence_manifest"
        },
        "table_contract": "CN/HK/US-9+9+9",
    }


def activate_market_physical_only(
    db: Session,
    *,
    market: str,
    gate: dict,
    approval_token: str | None,
) -> dict:
    normalized = normalize_fact_market(market)
    if str(approval_token or "") != cutover_apply_token(normalized):
        raise RuntimeError(
            f"{normalized} physical cutover requires its explicit activation token."
        )
    if gate.get("status") != "pass" or gate.get("market") != normalized:
        raise RuntimeError(f"{normalized} physical cutover gate has not passed.")
    marker = {
        "cutover_version": MARKET_PHYSICAL_CUTOVER_VERSION,
        "status": "active",
        "market": normalized,
        "activated_at": app_now_iso(),
        "latest_run_id": int(gate["latest_run_id"]),
        "dual_write_run_ids": [int(value) for value in gate["dual_write_run_ids"]],
        "dual_write_trade_dates": list(gate["dual_write_trade_dates"]),
        "backup_sha256": str(gate["backup_sha256"]),
        "evidence_sha256": dict(gate["evidence_sha256"]),
    }
    AppSettingRepository(db).set(
        physical_cutover_setting_key(normalized),
        json.dumps(marker, ensure_ascii=False, sort_keys=True),
    )
    marker_active = (
        cn_physical_only_cutover_active(db)
        if normalized == "CN"
        else physical_only_cutover_active(db, normalized)
    )
    if not marker_active:
        raise RuntimeError(f"{normalized} physical cutover marker could not be read back.")
    return {"status": "success", "action": "activated", "marker": marker}


def rollback_market_physical_only(
    db: Session,
    *,
    market: str,
    approval_token: str | None,
) -> dict:
    normalized = normalize_fact_market(market)
    if str(approval_token or "") != cutover_rollback_token(normalized):
        raise RuntimeError(
            f"{normalized} physical cutover rollback requires its explicit rollback token."
        )
    marker = {
        "cutover_version": MARKET_PHYSICAL_CUTOVER_VERSION,
        "status": "rolled_back",
        "market": normalized,
        "rolled_back_at": app_now_iso(),
    }
    AppSettingRepository(db).set(
        physical_cutover_setting_key(normalized),
        json.dumps(marker, ensure_ascii=False, sort_keys=True),
    )
    if physical_only_cutover_active(db, normalized):
        raise RuntimeError(f"{normalized} physical cutover rollback did not take effect.")
    return {"status": "success", "action": "rolled_back", "marker": marker}


def validate_cn_physical_cutover_gate(**kwargs) -> dict:
    return validate_market_physical_cutover_gate(market="CN", **kwargs)


def activate_cn_physical_only(
    db: Session,
    *,
    gate: dict,
    approval_token: str | None,
) -> dict:
    return activate_market_physical_only(
        db,
        market="CN",
        gate=gate,
        approval_token=approval_token,
    )


def rollback_cn_physical_only(db: Session, *, approval_token: str | None) -> dict:
    return rollback_market_physical_only(
        db,
        market="CN",
        approval_token=approval_token,
    )
