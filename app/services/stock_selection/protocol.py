"""Immutable, content-addressed protocol for executable stock selection."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

from app.services.json_payload_artifacts import JsonPayloadArtifactStore


@dataclass(frozen=True, slots=True)
class ExecutableSelectionProtocol:
    market: str
    horizon_days: int = 5
    entry_price_mode: str = "next_session_open"
    exit_price_mode: str = "holding_session_close"
    top_n: int = 5
    minimum_expected_net_return: float = 0.0
    decision_cutoff_local_time: str = "16:00"
    initial_cash: float = 1_000_000.0
    max_position_weight: float = 0.10
    max_sector_weight: float = 0.30
    max_gross_exposure: float = 0.60
    max_participation_rate: float = 0.01
    commission_bps_one_way: float = 8.0
    slippage_bps_one_way: float = 12.0
    universe_version: str = "unversioned"
    data_manifest_hash: str = "unversioned"
    feature_set_version: str = "unversioned"
    label_version: str = "next_open_fixed_horizon_net_profit_v1"
    calendar_version: str = "market_calendar_2026_v1"
    market_rules_version: str = "daily_market_rules_v2"
    adjustment_version: str = "raw_prices_with_actions_v1"
    cost_model_version: str = "one_way_commission_slippage_v1"
    risk_model_version: str = "portfolio_limits_v1"
    calibration_version: str = "unversioned"
    model_bundle_hash: str = "unversioned"
    source_tree_hash: str = "unversioned"
    dependency_lock_hash: str = "unversioned"
    engine_version: str = "event_driven_daily_v3"
    baseline_id: str = "tradable_equal_weight_v1"
    primary_metric: str = "net_excess_return"
    train_end_date: str | None = None
    calibration_start_date: str | None = None
    calibration_end_date: str | None = None
    validation_start_date: str | None = None
    experiment_budget: int = 24
    random_seed: int = 20260912
    approved: bool = False
    approved_by: str | None = None
    approved_at: str | None = None
    schema_version: str = "executable_selection_protocol_v1"

    def __post_init__(self) -> None:
        market = self.market.strip().upper()
        if market not in {"CN", "US", "HK"}:
            raise ValueError("market must be CN, US, or HK")
        if self.horizon_days <= 0 or self.top_n <= 0:
            raise ValueError("horizon_days and top_n must be positive")
        if self.entry_price_mode != "next_session_open" or self.exit_price_mode != "holding_session_close":
            raise ValueError("v1 executable protocol requires next-session open and fixed-horizon close")
        try:
            hour, minute = (int(part) for part in self.decision_cutoff_local_time.split(":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("decision_cutoff_local_time must be HH:MM") from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("decision_cutoff_local_time must be HH:MM")
        for name in ("universe_version", "feature_set_version", "label_version", "calendar_version",
                     "market_rules_version", "engine_version", "baseline_id", "data_manifest_hash",
                     "adjustment_version", "cost_model_version", "risk_model_version",
                     "calibration_version", "model_bundle_hash", "source_tree_hash",
                     "dependency_lock_hash", "primary_metric"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} must not be empty")
        for name in ("max_position_weight", "max_sector_weight", "max_gross_exposure", "max_participation_rate"):
            value = float(getattr(self, name))
            if not 0 < value <= 1:
                raise ValueError(f"{name} must be in (0, 1]")
        if self.max_position_weight > self.max_gross_exposure or self.max_sector_weight > self.max_gross_exposure:
            raise ValueError("position and sector limits must not exceed gross exposure")
        if min(self.initial_cash, self.commission_bps_one_way, self.slippage_bps_one_way) < 0 or self.initial_cash == 0:
            raise ValueError("cash must be positive and costs non-negative")
        if not -1.0 <= self.minimum_expected_net_return <= 1.0:
            raise ValueError("minimum_expected_net_return must be a decimal return in [-1, 1]")
        if self.experiment_budget <= 0:
            raise ValueError("experiment_budget must be positive")
        ordered = [value for value in (
            self.train_end_date, self.calibration_start_date, self.calibration_end_date,
            self.validation_start_date,
        ) if value]
        parsed = [date.fromisoformat(value[:10]) for value in ordered]
        if parsed != sorted(parsed):
            raise ValueError("training, calibration, and validation dates must be ordered")
        if self.approved and not self.approved_by:
            raise ValueError("approved protocol requires approved_by")
        if self.approved and not self.approved_at:
            raise ValueError("approved protocol requires approved_at")
        if self.approved_at:
            approved_at = datetime.fromisoformat(self.approved_at.replace("Z", "+00:00"))
            if approved_at.tzinfo is None or approved_at.utcoffset() is None:
                raise ValueError("approved_at must include a timezone")
        if not self.approved and (self.approved_by or self.approved_at):
            raise ValueError("unapproved protocol cannot carry approval identity")

    def canonical_payload(self) -> dict:
        payload = asdict(self)
        payload["market"] = self.market.strip().upper()
        return payload

    def semantic_payload(self) -> dict:
        payload = self.canonical_payload()
        for key in ("approved", "approved_by", "approved_at"):
            payload.pop(key, None)
        return payload

    @property
    def protocol_hash(self) -> str:
        content = json.dumps(self.semantic_payload(), ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(content).hexdigest()

    @property
    def protocol_id(self) -> str:
        return f"{self.schema_version}:{self.market.strip().upper()}:{self.protocol_hash[:20]}"

    def require_approved(self) -> None:
        if not self.approved:
            raise PermissionError(f"protocol is not approved: {self.protocol_id}")


def persist_protocol(protocol: ExecutableSelectionProtocol, *, artifact_root: Path | None = None) -> dict:
    store = JsonPayloadArtifactStore(artifact_root)
    payload = {"protocol_id": protocol.protocol_id, **protocol.canonical_payload()}
    reference = store.write(payload, namespace="stock_selection_protocols")
    if store.read(reference) != payload:
        raise RuntimeError("protocol artifact verification failed")
    return {"protocol_id": protocol.protocol_id, "protocol_hash": protocol.protocol_hash,
            "approved": protocol.approved, "artifact": reference}


def protocol_change_classification(before: ExecutableSelectionProtocol, after: ExecutableSelectionProtocol) -> dict:
    left, right = before.canonical_payload(), after.canonical_payload()
    changed = sorted(key for key in left if left[key] != right[key])
    invalidates_evidence = any(key not in {"approved", "approved_by", "approved_at"} for key in changed)
    return {"changed_fields": changed, "new_protocol_required": before.protocol_id != after.protocol_id,
            "invalidates_prior_effectiveness_evidence": invalidates_evidence}


__all__ = ["ExecutableSelectionProtocol", "persist_protocol", "protocol_change_classification"]
