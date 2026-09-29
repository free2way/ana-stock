"""Unified, read-only diagnostics for stock-selection pipeline stage counts."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        while chunk := input_file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _unavailable_research(status: str, *, reason: str, manifest: dict | None = None) -> dict:
    payload = {
        "status": status,
        "evidence_complete": False,
        "reason": reason,
    }
    if manifest:
        payload.update({
            "market": manifest.get("market"),
            "horizon_days": manifest.get("horizon_days"),
            "evidence_version": manifest.get("evidence_version"),
        })
    return payload


def load_latest_research_regime_coverage(
    *,
    artifact_root: Path,
    market: str,
    horizon_days: int = 5,
) -> dict | None:
    """Load the newest immutable research regime receipt without running research.

    A newest matching manifest that is corrupt or predates regime evidence is
    returned as an explicit incomplete stage. It is never hidden by falling back
    to an older artifact.
    """
    market_code = str(market or "").strip().upper()
    if market_code not in {"CN", "US"}:
        raise ValueError("market must be CN or US")
    if horizon_days <= 0:
        raise ValueError("horizon_days must be positive")
    root = Path(artifact_root).resolve()
    if not root.is_dir():
        return None
    candidates: list[tuple[int, Path, dict]] = []
    for manifest_path in root.glob("*/manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            manifest.get("schema_version") == "stock_selection_research_evidence_v1"
            and str(manifest.get("market") or "").upper() == market_code
            and manifest.get("horizon_days") == horizon_days
        ):
            candidates.append((manifest_path.stat().st_mtime_ns, manifest_path, manifest))
    if not candidates:
        return None
    _, manifest_path, manifest = max(candidates, key=lambda item: (item[0], str(item[1])))
    file_sha256 = manifest.get("file_sha256")
    if not isinstance(file_sha256, dict) or not file_sha256:
        return _unavailable_research(
            "CORRUPT_EVIDENCE", reason="missing_file_checksums", manifest=manifest,
        )
    actual_hashes: dict[str, str] = {}
    for filename, expected_digest in sorted(file_sha256.items()):
        path = manifest_path.parent / str(filename)
        try:
            actual_digest = _sha256(path)
        except OSError:
            return _unavailable_research(
                "CORRUPT_EVIDENCE", reason=f"missing_artifact_file:{filename}", manifest=manifest,
            )
        if actual_digest != expected_digest:
            return _unavailable_research(
                "CORRUPT_EVIDENCE", reason=f"checksum_mismatch:{filename}", manifest=manifest,
            )
        actual_hashes[str(filename)] = actual_digest
    actual_bundle = hashlib.sha256(_canonical_json(actual_hashes).encode("utf-8")).hexdigest()
    if actual_bundle != manifest.get("bundle_sha256"):
        return _unavailable_research(
            "CORRUPT_EVIDENCE", reason="bundle_checksum_mismatch", manifest=manifest,
        )
    required = {"regime_policy.json", "regime_candidates.json"}
    if not required.issubset(actual_hashes):
        return _unavailable_research(
            "LEGACY_NO_REGIME_EVIDENCE", reason="regime_artifacts_missing", manifest=manifest,
        )
    try:
        policy_payload = json.loads((manifest_path.parent / "regime_policy.json").read_text(encoding="utf-8"))
        candidate_payload = json.loads((manifest_path.parent / "regime_candidates.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _unavailable_research(
            "CORRUPT_EVIDENCE", reason="regime_artifact_unreadable", manifest=manifest,
        )
    coverage = policy_payload.get("coverage") if isinstance(policy_payload, dict) else None
    policies = policy_payload.get("policies") if isinstance(policy_payload, dict) else None
    if not isinstance(coverage, dict) or not isinstance(policies, dict) or not isinstance(candidate_payload, dict):
        return _unavailable_research(
            "CORRUPT_EVIDENCE", reason="invalid_regime_artifact_schema", manifest=manifest,
        )
    if (
        policy_payload.get("mode") != manifest.get("regime_policy_mode")
        or coverage != manifest.get("regime_coverage")
        or policies != manifest.get("regime_policy_by_date")
        or candidate_payload != manifest.get("regime_candidate_sample_ids")
    ):
        return _unavailable_research(
            "CORRUPT_EVIDENCE", reason="manifest_regime_identity_mismatch", manifest=manifest,
        )
    evaluated_dates = manifest.get("common_evaluated_dates")
    evaluated_dates = evaluated_dates if isinstance(evaluated_dates, list) else []
    policy_dates = sorted(str(value) for value in policies)
    latest_policy_date = policy_dates[-1] if policy_dates else None
    latest_policy = policies.get(latest_policy_date) if latest_policy_date else None
    candidate_counts = {
        str(model_key): sum(len(values) for values in by_date.values() if isinstance(values, list))
        for model_key, by_date in candidate_payload.items()
        if isinstance(by_date, dict)
    }
    status = str(coverage.get("status") or "UNKNOWN")
    evidence_complete = status == "COMPLETE" and set(policy_dates) == set(evaluated_dates)
    return {
        **deepcopy(coverage),
        "status": status,
        "evidence_complete": evidence_complete,
        "integrity_status": "VERIFIED",
        "market": market_code,
        "horizon_days": horizon_days,
        "evidence_version": manifest.get("evidence_version"),
        "run_scope": manifest.get("run_scope"),
        "evaluation_sample_count": manifest.get("evaluation_sample_count"),
        "evaluated_date_count": len(evaluated_dates),
        "first_evaluated_date": min(evaluated_dates) if evaluated_dates else None,
        "last_evaluated_date": max(evaluated_dates) if evaluated_dates else None,
        "policy_date_count": len(policy_dates),
        "latest_policy_date": latest_policy_date,
        "latest_policy": deepcopy(latest_policy) if isinstance(latest_policy, dict) else None,
        "candidate_counts_by_model": candidate_counts,
        "bundle_sha256": actual_bundle,
        "manifest_modified_at": datetime.fromtimestamp(
            manifest_path.stat().st_mtime, tz=timezone.utc,
        ).isoformat(),
    }


def build_p0_pipeline_diagnostics(
    *,
    market: str,
    screener_payload: dict | None,
    research_coverage: dict | None,
    publication_report: dict | None,
) -> dict:
    market_code = str(market or "").strip().upper()
    if market_code not in {"CN", "US"}:
        raise ValueError("market must be CN or US")
    screener = screener_payload if isinstance(screener_payload, dict) else {}
    research = research_coverage if isinstance(research_coverage, dict) else {}
    report = publication_report if isinstance(publication_report, dict) else {}
    regime = screener.get("regime_diagnostics") if isinstance(screener.get("regime_diagnostics"), dict) else {}
    candidate_stats = screener.get("candidate_stats") if isinstance(screener.get("candidate_stats"), dict) else {}
    input_meta = screener.get("input") if isinstance(screener.get("input"), dict) else {}
    list_key = "market_recommendations" if market_code == "CN" else "us_model_recommendations"
    watch_key = "market_watch_recommendations" if market_code == "CN" else None
    meta_key = "market_recommendations_meta" if market_code == "CN" else "us_model_recommendations_meta"
    report_meta = report.get(meta_key) if isinstance(report.get(meta_key), dict) else {}
    candidates = report.get(list_key) if isinstance(report.get(list_key), list) else []
    watch = report.get(watch_key) if watch_key and isinstance(report.get(watch_key), list) else []
    screener_date = str(input_meta.get("input_as_of_date") or "") or None
    report_date = str((report.get("input_market_dates") or {}).get(market_code) or report_meta.get("target_snapshot_date") or "") or None
    blockers = []
    if not screener:
        blockers.append("missing_screener_snapshot")
    if not regime:
        blockers.append("missing_screener_regime_diagnostics")
    if not research:
        blockers.append("missing_research_regime_coverage")
    elif research.get("evidence_complete") is not True:
        blockers.append("incomplete_research_regime_evidence")
    if not report:
        blockers.append("missing_publication_report")
    if screener_date and report_date and screener_date != report_date:
        blockers.append("screener_publication_date_mismatch")
    report_policy = report_meta.get("regime_policy") if isinstance(report_meta.get("regime_policy"), dict) else {}
    screener_policy = regime.get("regime_policy") if isinstance(regime.get("regime_policy"), dict) else {}
    if screener_policy and report_policy and (
        screener_policy.get("policy_version") != report_policy.get("policy_version")
        or screener_policy.get("source_snapshot_sha256") != report_policy.get("source_snapshot_sha256")
    ):
        blockers.append("screener_publication_regime_identity_mismatch")
    research_policy = research.get("latest_policy") if isinstance(research.get("latest_policy"), dict) else {}
    research_policy_date = str(research.get("latest_policy_date") or "") or None
    if research_policy_date and research_policy_date == screener_date and screener_policy and research_policy and (
        screener_policy.get("policy_version") != research_policy.get("policy_version")
        or screener_policy.get("source_snapshot_sha256") != research_policy.get("source_snapshot_sha256")
    ):
        blockers.append("screener_research_regime_identity_mismatch")
    hard_block = any(reason.endswith("mismatch") for reason in blockers)
    status = "BLOCKED" if hard_block else "COMPLETE" if not blockers else "PARTIAL"
    return {
        "schema_version": "stock_selection_p0_pipeline_diagnostics_v1",
        "market": market_code,
        "status": status,
        "candidate_semantics": "diagnostic_counts_not_trade_authorization",
        "stages": {
            "screener": {
                "status": regime.get("status") or ("MISSING" if not screener else "NO_REGIME_DIAGNOSTICS"),
                "input_market_date": screener_date,
                "returned_count": candidate_stats.get("returned_count"),
                "persisted_count": candidate_stats.get("persisted_count"),
                "observation_count": regime.get("observation_count"),
                "tradability_ready_count": regime.get("tradability_ready_count"),
                "regime_shortlist_count": regime.get("regime_shortlist_count"),
                "formal_candidate_count": regime.get("formal_candidate_count"),
                "policy": deepcopy(screener_policy) if screener_policy else None,
            },
            "research": deepcopy(research) if research else {"status": "NOT_AVAILABLE"},
            "publication": {
                "status": report_meta.get("status") or ("MISSING" if not report else "UNKNOWN"),
                "input_market_date": report_date,
                "candidate_count": len(candidates),
                "watch_count": len(watch),
                "blocked_candidate_count": report_meta.get("blocked_candidate_count", 0),
                "policy": deepcopy(report_policy) if report_policy else None,
            },
        },
        "blockers": sorted(set(blockers)),
    }


__all__ = ["build_p0_pipeline_diagnostics", "load_latest_research_regime_coverage"]
