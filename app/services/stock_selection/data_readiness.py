from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True, slots=True)
class DataReadinessConfig:
    market: str
    required_history_sessions: int = 252
    minimum_eligible_symbols: int = 500
    minimum_history_coverage: float = 0.60
    minimum_metadata_coverage: float = 0.60
    minimum_industry_coverage: float = 0.60
    maximum_duplicate_conflict_rate: float = 0.001
    require_historical_universe_contract: bool = False

    def __post_init__(self) -> None:
        if str(self.market or "").strip().upper() not in {"CN", "US"}:
            raise ValueError("market must be CN or US")
        if self.required_history_sessions <= 0 or self.minimum_eligible_symbols <= 0:
            raise ValueError("readiness count thresholds must be positive")
        for name, value in (
            ("minimum_history_coverage", self.minimum_history_coverage),
            ("minimum_metadata_coverage", self.minimum_metadata_coverage),
            ("minimum_industry_coverage", self.minimum_industry_coverage),
            ("maximum_duplicate_conflict_rate", self.maximum_duplicate_conflict_rate),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class DataReadinessReport:
    market: str
    total_lake_symbols: int
    supported_security_symbols: int
    required_history_sessions: int
    history_eligible_symbols: int
    history_coverage: float
    metadata_coverage: float
    industry_coverage: float
    duplicate_conflict_symbols: int
    duplicate_conflict_rate: float
    history_counts: Mapping[int, int]
    historical_universe_contract_required: bool
    historical_security_master_verified: bool
    historical_membership_verified: bool
    delisting_history_verified: bool
    historical_industry_verified: bool
    universe_revision_history_verified: bool
    blockers: tuple[str, ...]
    scope: str

    @property
    def passed(self) -> bool:
        return not self.blockers


@dataclass(frozen=True, slots=True)
class DataReadinessEvidenceWriteResult:
    evidence_version: str
    artifact_dir: Path
    report_path: Path
    manifest_path: Path
    reused_existing: bool


def assess_data_readiness(
    metrics: Mapping[str, Mapping[str, object]],
    *,
    security_types: Mapping[str, str],
    metadata_present: Mapping[str, bool],
    industries: Mapping[str, str | None],
    config: DataReadinessConfig,
    historical_universe_contract: Mapping[str, bool] | None = None,
) -> DataReadinessReport:
    normalized_metrics = {
        str(ticker).strip().upper(): values
        for ticker, values in metrics.items()
        if str(ticker).strip()
    }
    supported = sorted(
        ticker
        for ticker in normalized_metrics
        if str(security_types.get(ticker) or "").lower() in {"equity", "common_stock", "common_equity"}
    )
    history_thresholds = tuple(sorted({60, 120, 180, 252, config.required_history_sessions}))
    history_counts = {
        threshold: sum(
            int(normalized_metrics[ticker].get("history_days") or 0) >= threshold
            for ticker in supported
        )
        for threshold in history_thresholds
    }
    eligible = [
        ticker
        for ticker in supported
        if int(normalized_metrics[ticker].get("history_days") or 0)
        >= config.required_history_sessions
    ]
    supported_count = len(supported)
    history_coverage = len(eligible) / supported_count if supported_count else 0.0
    metadata_coverage = (
        sum(bool(metadata_present.get(ticker)) for ticker in eligible) / len(eligible)
        if eligible
        else 0.0
    )
    industry_coverage = (
        sum(bool(str(industries.get(ticker) or "").strip()) for ticker in eligible) / len(eligible)
        if eligible
        else 0.0
    )
    duplicate_conflicts = sum(
        int(normalized_metrics[ticker].get("duplicate_conflict_days") or 0) > 0
        for ticker in eligible
    )
    duplicate_rate = duplicate_conflicts / len(eligible) if eligible else 0.0
    blockers: list[str] = []
    if len(eligible) < config.minimum_eligible_symbols:
        blockers.append("insufficient_history_eligible_symbols")
    if history_coverage < config.minimum_history_coverage:
        blockers.append("insufficient_history_coverage")
    if metadata_coverage < config.minimum_metadata_coverage:
        blockers.append("insufficient_metadata_coverage")
    if industry_coverage < config.minimum_industry_coverage:
        blockers.append("insufficient_industry_coverage")
    if duplicate_rate > config.maximum_duplicate_conflict_rate:
        blockers.append("duplicate_price_conflicts")
    historical_contract = historical_universe_contract or {}
    historical_security_master_verified = bool(
        historical_contract.get("historical_security_master_verified")
    )
    historical_membership_verified = bool(
        historical_contract.get("historical_membership_verified")
    )
    delisting_history_verified = bool(
        historical_contract.get("delisting_history_verified")
    )
    historical_industry_verified = bool(
        historical_contract.get("historical_industry_verified")
    )
    universe_revision_history_verified = bool(
        historical_contract.get("universe_revision_history_verified")
    )
    if config.require_historical_universe_contract:
        for verified, blocker in (
            (
                historical_security_master_verified,
                "historical_security_master_not_verified",
            ),
            (historical_membership_verified, "historical_membership_not_verified"),
            (delisting_history_verified, "delisting_history_not_verified"),
            (historical_industry_verified, "historical_industry_not_verified"),
            (
                universe_revision_history_verified,
                "universe_revision_history_not_verified",
            ),
        ):
            if not verified:
                blockers.append(blocker)
    return DataReadinessReport(
        market=config.market.strip().upper(),
        total_lake_symbols=len(normalized_metrics),
        supported_security_symbols=supported_count,
        required_history_sessions=config.required_history_sessions,
        history_eligible_symbols=len(eligible),
        history_coverage=history_coverage,
        metadata_coverage=metadata_coverage,
        industry_coverage=industry_coverage,
        duplicate_conflict_symbols=duplicate_conflicts,
        duplicate_conflict_rate=duplicate_rate,
        history_counts=history_counts,
        historical_universe_contract_required=config.require_historical_universe_contract,
        historical_security_master_verified=historical_security_master_verified,
        historical_membership_verified=historical_membership_verified,
        delisting_history_verified=delisting_history_verified,
        historical_industry_verified=historical_industry_verified,
        universe_revision_history_verified=universe_revision_history_verified,
        blockers=tuple(blockers),
        scope="formal_full_market" if not blockers else "engineering_only",
    )


def persist_data_readiness_report(
    report: DataReadinessReport,
    *,
    source_version: str,
    root: Path,
) -> DataReadinessEvidenceWriteResult:
    payload = {
        "schema_version": "stock_selection_data_readiness_v1",
        "source_version": source_version,
        "report": asdict(report),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    evidence_version = f"stock_selection_data_readiness_v1:{report.market}:{digest[:20]}"
    target_dir = root / evidence_version.replace(":", "_")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".readiness-", dir=root) as temporary_name:
        temporary_dir = Path(temporary_name)
        report_path = temporary_dir / "readiness.json"
        report_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
        manifest = {
            "schema_version": "stock_selection_data_readiness_manifest_v1",
            "evidence_version": evidence_version,
            "source_version": source_version,
            "report_file": "readiness.json",
            "report_sha256": report_sha256,
            "passed": report.passed,
            "scope": report.scope,
        }
        (temporary_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if target_dir.exists():
            manifest_path = target_dir / "manifest.json"
            if not manifest_path.exists():
                raise RuntimeError(f"immutable readiness evidence is missing manifest: {target_dir}")
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if existing.get("report_sha256") != report_sha256:
                raise RuntimeError(f"refusing to overwrite readiness evidence: {evidence_version}")
            return DataReadinessEvidenceWriteResult(
                evidence_version=evidence_version,
                artifact_dir=target_dir,
                report_path=target_dir / "readiness.json",
                manifest_path=manifest_path,
                reused_existing=True,
            )
        temporary_dir.replace(target_dir)
        return DataReadinessEvidenceWriteResult(
            evidence_version=evidence_version,
            artifact_dir=target_dir,
            report_path=target_dir / "readiness.json",
            manifest_path=target_dir / "manifest.json",
            reused_existing=False,
        )
