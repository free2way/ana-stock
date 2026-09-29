"""Immutable P2 candidate freeze and forward-observation accounting.

This module deliberately stops at research governance.  A completed observation
window is eligible for independent review; it never approves or deploys a model.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
from typing import Iterable, Mapping

from app.services.json_payload_artifacts import JsonPayloadArtifactStore
from app.services.market_calendar import (
    is_market_open_date,
    market_timezone,
    next_market_open_date,
)
from app.services.stock_selection.protocol import ExecutableSelectionProtocol


EXPERIMENT_STATUSES = frozenset({"SUCCESS", "FAIL", "INTERRUPTED", "BLOCKED"})


def _canonical_digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class P2ExperimentRecord:
    experiment_id: str
    candidate_id: str
    status: str
    config_version: str

    def __post_init__(self) -> None:
        if not all(str(value or "").strip() for value in (
            self.experiment_id, self.candidate_id, self.config_version,
        )):
            raise ValueError("experiment identity fields must not be empty")
        if self.status not in EXPERIMENT_STATUSES:
            raise ValueError("invalid experiment status")


@dataclass(frozen=True, slots=True)
class P2CandidateSpec:
    candidate_id: str
    model_version: str
    factor_set_version: str
    sampling_config_version: str
    portfolio_risk_version: str
    decision_policy_version: str
    calibration_version: str
    evidence_versions: tuple[str, ...]
    experiments: tuple[P2ExperimentRecord, ...]
    multiple_comparison_control: str = "single_frozen_candidate_no_adjustment"
    minimum_forward_dates: int = 60
    minimum_active_dates: int = 30
    minimum_closed_lots: int = 200
    schema_version: str = "p2_forward_candidate_spec_v1"

    def __post_init__(self) -> None:
        for name in (
            "candidate_id", "model_version", "factor_set_version",
            "sampling_config_version", "portfolio_risk_version",
            "decision_policy_version", "calibration_version",
            "multiple_comparison_control",
        ):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} must not be empty")
        if not self.evidence_versions or any(not str(value or "").strip() for value in self.evidence_versions):
            raise ValueError("at least one non-empty evidence version is required")
        experiment_ids = [item.experiment_id for item in self.experiments]
        if len(experiment_ids) != len(set(experiment_ids)):
            raise ValueError("experiment ids must be unique")
        if any(item.candidate_id != self.candidate_id for item in self.experiments):
            raise ValueError("experiment candidate_id must match the frozen candidate")
        if min(self.minimum_forward_dates, self.minimum_active_dates, self.minimum_closed_lots) <= 0:
            raise ValueError("forward evidence thresholds must be positive")
        if self.minimum_active_dates > self.minimum_forward_dates:
            raise ValueError("minimum active dates cannot exceed minimum forward dates")

    def semantic_payload(self) -> dict:
        return {
            **asdict(self),
            "evidence_versions": sorted(set(self.evidence_versions)),
            "experiments": [
                asdict(item) for item in sorted(self.experiments, key=lambda value: value.experiment_id)
            ],
        }

    def version(self) -> str:
        digest = _canonical_digest(self.semantic_payload())
        return f"{self.schema_version}:{digest[:20]}"


@dataclass(frozen=True, slots=True)
class P2ForwardObservation:
    observation_date: date
    freeze_version: str
    protocol_id: str
    candidate_config_version: str
    active: bool
    closed_lots: int
    result_verifiable: bool = True
    matured: bool = True

    def __post_init__(self) -> None:
        if not all(str(value or "").strip() for value in (
            self.freeze_version, self.protocol_id, self.candidate_config_version,
        )):
            raise ValueError("observation identity fields must not be empty")
        if isinstance(self.closed_lots, bool) or self.closed_lots < 0:
            raise ValueError("closed_lots must be a non-negative integer")
        if not isinstance(self.closed_lots, int):
            raise ValueError("closed_lots must be an integer")


def freeze_p2_forward_candidate(
    protocol: ExecutableSelectionProtocol,
    spec: P2CandidateSpec,
    *,
    frozen_at: datetime,
) -> dict:
    """Freeze a research candidate without granting production qualification."""

    if frozen_at.tzinfo is None or frozen_at.utcoffset() is None:
        raise ValueError("frozen_at must be timezone-aware")
    if protocol.approved:
        raise ValueError("engineering freeze must not reuse a production-approved protocol")
    if len(spec.experiments) > 24 or len(spec.experiments) > protocol.experiment_budget:
        raise ValueError("experiment registry exceeds the frozen protocol budget")
    market = protocol.market.strip().upper()
    local_frozen_at = frozen_at.astimezone(market_timezone(market))
    forward_start_date = next_market_open_date(
        market, local_frozen_at.date(), include_self=False,
    )
    semantic = {
        "schema_version": "p2_forward_candidate_freeze_v1",
        "market": market,
        "protocol_id": protocol.protocol_id,
        "protocol_hash": protocol.protocol_hash,
        "protocol_approved": False,
        "candidate_config_version": spec.version(),
        "candidate_spec": spec.semantic_payload(),
        "frozen_at": local_frozen_at.isoformat(),
        "forward_start_date": forward_start_date,
    }
    freeze_version = f"p2_forward_candidate_freeze_v1:{market}:{_canonical_digest(semantic)[:20]}"
    return {
        **semantic,
        "freeze_version": freeze_version,
        "status": "FROZEN_RESEARCH_ONLY",
        "promotion_status": "BLOCKED",
        "production_deployed": False,
        "promotion_blockers": [
            f"minimum_{spec.minimum_forward_dates}_new_forward_dates_not_met",
            f"minimum_{spec.minimum_active_dates}_active_dates_not_met",
            f"minimum_{spec.minimum_closed_lots}_closed_lots_not_met",
            "independent_review_not_completed",
            "production_approval_not_granted",
        ],
    }


def assess_p2_forward_progress(
    freeze: Mapping[str, object],
    observations: Iterable[P2ForwardObservation],
    *,
    economic_logic_changed: bool = False,
) -> dict:
    """Count only new, market-open, mature and verifiable frozen observations."""

    spec = dict(freeze.get("candidate_spec") or {})
    required_keys = {
        "freeze_version", "protocol_id", "candidate_config_version",
        "forward_start_date", "market", "protocol_approved", "production_deployed",
    }
    if not required_keys.issubset(freeze):
        raise ValueError("invalid candidate freeze payload")
    if freeze.get("protocol_approved") is not False or freeze.get("production_deployed") is not False:
        raise ValueError("P2 research freeze must remain unapproved and undeployed")
    start_date = date.fromisoformat(str(freeze["forward_start_date"])[:10])
    market = str(freeze["market"]).upper()
    rows = sorted(observations, key=lambda value: value.observation_date)
    dates = [item.observation_date for item in rows]
    if len(dates) != len(set(dates)):
        raise ValueError("forward observation dates must be unique")
    for item in rows:
        if item.observation_date < start_date:
            raise ValueError("historical or pre-freeze observations cannot count as new forward evidence")
        if not is_market_open_date(market, item.observation_date):
            raise ValueError("forward observations must be market-open dates")
        if item.freeze_version != freeze["freeze_version"]:
            raise ValueError("observation freeze version mismatch")
        if item.protocol_id != freeze["protocol_id"]:
            raise ValueError("observation protocol id mismatch")
        if item.candidate_config_version != freeze["candidate_config_version"]:
            raise ValueError("observation candidate config version mismatch")

    minimum_forward_dates = int(spec.get("minimum_forward_dates") or 60)
    minimum_active_dates = int(spec.get("minimum_active_dates") or 30)
    minimum_closed_lots = int(spec.get("minimum_closed_lots") or 200)
    verified = [item for item in rows if item.result_verifiable and item.matured]
    active_dates = sum(bool(item.active) for item in verified)
    closed_lots = sum(item.closed_lots for item in verified)
    unmet = []
    if len(verified) < minimum_forward_dates:
        unmet.append(f"minimum_{minimum_forward_dates}_new_forward_dates_not_met")
    if active_dates < minimum_active_dates:
        unmet.append(f"minimum_{minimum_active_dates}_active_dates_not_met")
    if closed_lots < minimum_closed_lots:
        unmet.append(f"minimum_{minimum_closed_lots}_closed_lots_not_met")
    if any(not item.result_verifiable for item in rows):
        unmet.append("unverifiable_forward_results_present")
    if any(not item.matured for item in rows):
        unmet.append("unmatured_forward_results_present")

    if economic_logic_changed:
        status = "RESTART_REQUIRED"
        unmet = ["economic_decision_logic_changed_new_freeze_required"]
    elif unmet:
        status = "COLLECTING"
    else:
        status = "READY_FOR_INDEPENDENT_REVIEW"
    return {
        "schema_version": "p2_forward_progress_v1",
        "freeze_version": freeze["freeze_version"],
        "protocol_id": freeze["protocol_id"],
        "candidate_config_version": freeze["candidate_config_version"],
        "market": market,
        "forward_start_date": start_date.isoformat(),
        "status": status,
        "promotion_status": "BLOCKED",
        "protocol_approved": False,
        "production_deployed": False,
        "recorded_date_count": len(rows),
        "verified_mature_date_count": len(verified),
        "active_date_count": active_dates,
        "closed_lot_count": closed_lots,
        "minimum_forward_dates": minimum_forward_dates,
        "minimum_active_dates": minimum_active_dates,
        "minimum_closed_lots": minimum_closed_lots,
        "blockers": sorted(set(unmet + ["independent_review_not_completed"])),
    }


def persist_p2_candidate_freeze(
    freeze: Mapping[str, object], *, artifact_root: Path | None = None,
) -> dict:
    payload = dict(freeze)
    store = JsonPayloadArtifactStore(artifact_root)
    reference = store.write(payload, namespace="stock_selection_p2_candidate_freezes")
    if store.read(reference) != payload:
        raise RuntimeError("P2 candidate-freeze artifact verification failed")
    return {
        "freeze_version": payload.get("freeze_version"),
        "artifact": reference,
        "status": payload.get("status"),
    }


__all__ = [
    "P2CandidateSpec",
    "P2ExperimentRecord",
    "P2ForwardObservation",
    "assess_p2_forward_progress",
    "freeze_p2_forward_candidate",
    "persist_p2_candidate_freeze",
]
