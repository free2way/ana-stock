from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import polars as pl

from app.services.stock_selection.research_runner import WalkForwardComparisonResult
from app.services.stock_selection.factor_diagnostics import FactorDiagnosticReport


@dataclass(frozen=True, slots=True)
class ResearchEvidenceWriteResult:
    evidence_version: str
    artifact_dir: Path
    manifest_path: Path
    file_sha256: dict[str, str]
    reused_existing: bool


def persist_factor_diagnostic_evidence(
    report: FactorDiagnosticReport,
    *,
    root: Path,
    market: str,
    source_version: str,
    universe_version: str,
    dataset_version: str,
    run_scope: str,
) -> ResearchEvidenceWriteResult:
    market_code = str(market or "").strip().upper()
    if market_code not in {"CN", "US"}:
        raise ValueError("market must be CN or US")
    report_payload = asdict(report)
    identity_payload = {
        "schema_version": "stock_selection_factor_diagnostic_evidence_v1",
        "market": market_code,
        "horizon_days": report.horizon_days,
        "target_name": report.target_name,
        "source_version": source_version,
        "universe_version": universe_version,
        "dataset_version": dataset_version,
        "run_scope": run_scope,
        "report": report_payload,
    }
    digest = hashlib.sha256(_canonical_json(identity_payload).encode("utf-8")).hexdigest()[:20]
    evidence_version = (
        "stock_selection_factor_diagnostic_v1:"
        f"{market_code}:{report.horizon_days}d:{report.target_name}:{digest}"
    )
    root.mkdir(parents=True, exist_ok=True)
    target_dir = root / evidence_version.replace(":", "_")
    with tempfile.TemporaryDirectory(prefix=".factor-diagnostic-", dir=root) as temporary_name:
        temporary_dir = Path(temporary_name)
        report_path = temporary_dir / "diagnostics.json"
        _write_json(report_path, report_payload)
        file_sha256 = {"diagnostics.json": _sha256(report_path)}
        bundle_sha256 = hashlib.sha256(_canonical_json(file_sha256).encode("utf-8")).hexdigest()
        manifest = {
            **{key: value for key, value in identity_payload.items() if key != "report"},
            "evidence_version": evidence_version,
            "file_sha256": file_sha256,
            "bundle_sha256": bundle_sha256,
        }
        _write_json(temporary_dir / "manifest.json", manifest)
        if target_dir.exists():
            manifest_path = target_dir / "manifest.json"
            if not manifest_path.exists():
                raise RuntimeError(f"immutable evidence directory is missing manifest: {target_dir}")
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if existing.get("bundle_sha256") != bundle_sha256:
                raise RuntimeError(
                    f"refusing to overwrite immutable factor diagnostic evidence: {evidence_version}"
                )
            return ResearchEvidenceWriteResult(
                evidence_version=evidence_version,
                artifact_dir=target_dir,
                manifest_path=manifest_path,
                file_sha256=file_sha256,
                reused_existing=True,
            )
        temporary_dir.replace(target_dir)
        return ResearchEvidenceWriteResult(
            evidence_version=evidence_version,
            artifact_dir=target_dir,
            manifest_path=target_dir / "manifest.json",
            file_sha256=file_sha256,
            reused_existing=False,
        )


def _json_default(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        default=_json_default,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(
            payload,
            default=_json_default,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        while chunk := input_file.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def persist_walk_forward_evidence(
    result: WalkForwardComparisonResult,
    *,
    root: Path,
    market: str,
    source_version: str,
    universe_version: str,
    dataset_version: str,
    run_scope: str = "challenger_research_only",
    factor_set_key: str | None = None,
    factor_set_version: str | None = None,
) -> ResearchEvidenceWriteResult:
    market_code = str(market or "").strip().upper()
    if market_code not in {"CN", "US"}:
        raise ValueError("market must be CN or US")
    report_payload = {
        model_key: asdict(report)
        for model_key, report in sorted(result.reports.items())
    }
    fold_payload = [asdict(item) for item in result.fold_audits]
    regime_policy_payload = {
        feature_date.isoformat(): dict(policy)
        for feature_date, policy in sorted(result.regime_policy_by_date.items())
    }
    regime_candidate_payload = {
        model_key: {
            feature_date.isoformat(): list(sample_ids)
            for feature_date, sample_ids in sorted(by_date.items())
        }
        for model_key, by_date in sorted(result.regime_candidate_sample_ids.items())
    }
    identity_payload = {
        "schema_version": "stock_selection_research_evidence_v1",
        "market": market_code,
        "horizon_days": result.horizon_days,
        "source_version": source_version,
        "universe_version": universe_version,
        "dataset_version": dataset_version,
        "run_scope": run_scope,
        "common_evaluated_dates": result.common_evaluated_dates,
        "evaluation_sample_count": result.evaluation_sample_count,
        "training_policy": {
            "purge_sessions": result.purge_sessions,
            "embargo_sessions": result.embargo_sessions,
            "preprocessing_scope": result.preprocessing_scope,
            "label_availability_rule": "strictly_before_prediction_date",
            "sampling_mode": result.training_sampling_mode,
            "sampling_config_version": result.training_sampling_config_version,
        },
        "reports": report_payload,
        "fold_audits": fold_payload,
        "regime_policy_mode": result.regime_policy_mode,
        "regime_coverage": dict(result.regime_coverage),
        "regime_policy_by_date": regime_policy_payload,
        "regime_candidate_sample_ids": regime_candidate_payload,
    }
    if factor_set_key is not None or factor_set_version is not None:
        if not factor_set_key or not factor_set_version:
            raise ValueError("factor_set_key and factor_set_version must be provided together")
        identity_payload["factor_set_key"] = factor_set_key
        identity_payload["factor_set_version"] = factor_set_version
    digest = hashlib.sha256(_canonical_json(identity_payload).encode("utf-8")).hexdigest()[:20]
    evidence_version = f"stock_selection_research_evidence_v1:{market_code}:{result.horizon_days}d:{digest}"
    root.mkdir(parents=True, exist_ok=True)
    target_dir = root / evidence_version.replace(":", "_")

    with tempfile.TemporaryDirectory(prefix=".research-evidence-", dir=root) as temporary_name:
        temporary_dir = Path(temporary_name)
        _write_json(temporary_dir / "reports.json", report_payload)
        _write_json(temporary_dir / "fold_audits.json", fold_payload)
        _write_json(temporary_dir / "regime_policy.json", {
            "mode": result.regime_policy_mode,
            "coverage": dict(result.regime_coverage),
            "policies": regime_policy_payload,
        })
        _write_json(temporary_dir / "regime_candidates.json", regime_candidate_payload)
        prediction_records = [
            {
                "model_key": model_key,
                "sample_id": item.sample_id,
                "ticker": item.ticker,
                "feature_date": item.feature_date.isoformat(),
                "horizon_days": item.horizon_days,
                "raw_score": item.raw_score,
                "cross_sectional_rank": item.cross_sectional_rank,
                "model_version": item.model_version,
            }
            for model_key, predictions in sorted(result.predictions.items())
            for item in predictions
        ]
        pl.DataFrame(prediction_records).write_parquet(
            temporary_dir / "predictions.parquet",
            compression="zstd",
        )
        filenames = (
            "reports.json", "fold_audits.json", "predictions.parquet",
            "regime_policy.json", "regime_candidates.json",
        )
        file_sha256 = {name: _sha256(temporary_dir / name) for name in filenames}
        bundle_sha256 = hashlib.sha256(_canonical_json(file_sha256).encode("utf-8")).hexdigest()
        manifest = {
            **{key: value for key, value in identity_payload.items() if key not in {"reports", "fold_audits"}},
            "evidence_version": evidence_version,
            "file_sha256": file_sha256,
            "bundle_sha256": bundle_sha256,
        }
        _write_json(temporary_dir / "manifest.json", manifest)

        if target_dir.exists():
            manifest_path = target_dir / "manifest.json"
            if not manifest_path.exists():
                raise RuntimeError(f"immutable evidence directory is missing manifest: {target_dir}")
            try:
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"immutable evidence manifest is unreadable: {target_dir}") from exc
            if existing.get("bundle_sha256") != bundle_sha256:
                raise RuntimeError(
                    f"refusing to overwrite immutable research evidence: {evidence_version}"
                )
            return ResearchEvidenceWriteResult(
                evidence_version=evidence_version,
                artifact_dir=target_dir,
                manifest_path=manifest_path,
                file_sha256=file_sha256,
                reused_existing=True,
            )

        temporary_dir.replace(target_dir)
        return ResearchEvidenceWriteResult(
            evidence_version=evidence_version,
            artifact_dir=target_dir,
            manifest_path=target_dir / "manifest.json",
            file_sha256=file_sha256,
            reused_existing=False,
        )
