"""Unified promotion gate (v2): one decision across every promotion precondition.

Why a new module instead of extending :mod:`promotion_gate`
-----------------------------------------------------------
``promotion_gate.py`` (E-9) is the *statistical* sub-gate: it consumes aligned
robustness / selective evaluation reports and answers "is the evidence strong
enough". Its inputs are already-derived evaluation reports, it is referenced by
existing scripts/tests, and it deliberately says nothing about data quality,
price basis, corporate-action coverage, point-in-time readiness or research
scope.

This module is the *top-level* gate. Its input is a candidate model run plus a
market, and it composes:

* the price-basis contract (``price_basis_contract.decide_price_basis``);
* the corporate-action coverage audit recorded by the backtest runner;
* the ``data_readiness`` audit (blockers, incl. the point-in-time contract);
* training-sample size, OOS evaluation coverage/performance and the
  purge/embargo protocol fields already persisted by the trainer;
* the statistical sub-gate report from :mod:`promotion_gate` when supplied.

Keeping it separate means the E-9 behaviour and tests stay byte-for-byte
unchanged, the two gates can be versioned independently
(``stock_selection_promotion_approval_v2``), and v2 can import v1 without the
reverse dependency. The alternative (growing ``promotion_gate.py`` with a second
input contract and a persistence table) was rejected because it would couple two
very different evidence shapes and risk the existing E-9 guarantees.

Non-promotable runs
-------------------
Research-scope runs (pilot / ``explicit_tickers`` / ``engineering_*`` /
``*not_for_model_selection``) are still evaluated and their evidence is
retained, but the report carries ``promotable=False`` plus explicit reasons and
never reaches the recommendation/production path. Callers on that path must
guard with :func:`assert_promotable` (or read ``promotion_status_fields``).

Traceability
------------
:func:`persist_promotion_approval_record` writes a content-addressed, immutable
JSON approval record (market / run_id / data version hashes / code version /
params / evaluation summary / decision / decided_by / decided_at / reasons) under
the existing artifacts root. A JSON artifact was chosen over the DB-backed
``decision_ledger`` because the gate runs *before* publication, must not require
a database session, and its evidence is naturally content-addressed like the
existing readiness / robustness / promotion-gate artifacts. The write is atomic
and idempotent: re-persisting identical evidence reuses the directory and never
overwrites.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from app.core.config import get_settings
from app.services.price_basis_contract import (
    DECISION_ALLOW_ADJUSTED,
    DECISION_ALLOW_RAW_WITH_AUTHORIZATION,
    DECISION_REJECT,
    ENTRY_INFERENCE,
    FALLBACK_EXPLICIT_RAW,
    FALLBACK_NOT_APPLICABLE,
    AdjustedViewProbe,
    PriceBasisDecision,
    PriceBasisRequirements,
    decide_price_basis,
    probe_adjusted_view,
)
from app.services.stock_selection.data_readiness import DataReadinessReport
from app.services.stock_selection.promotion_gate import (
    PromotionGateCheck,
    PromotionGateReport,
)

SCHEMA_VERSION = "stock_selection_promotion_approval_v2"
DECISION_REJECT_V2 = "REJECT"
DECISION_OBSERVE = "OBSERVE"
DECISION_ELIGIBLE = "ELIGIBLE_FOR_MANUAL_REVIEW"

COMPLETED_RUN_STATUSES = {"success", "succeeded", "completed", "done"}

# Explicit research-scope markers. A scope containing any of these is retained
# for study but is never promotable. Scopes without a marker stay eligible so a
# plain production run is not blocked merely for not carrying a label.
RESEARCH_SCOPE_MARKERS = (
    "engineering",
    "pilot",
    "explicit_tickers",
    "not_for_model_selection",
    "survivor_bias",
    "research",
    "experiment",
)
RESEARCH_SELECTION_MODES = {
    "explicit_tickers",
    "latest_liquidity_pilot_not_point_in_time",
}

_OOS_PERFORMANCE_KEYS = (
    "mean_risk_adjusted_return",
    "mean_calendar_risk_adjusted_return",
    "mean_excess_return",
)


class PromotionNotAuthorized(RuntimeError):
    """Raised when a non-promotable run is pushed toward production."""


# Threshold field names persisted on a trainer's ``oos_evaluation`` evidence.
WINDOW_CAPABLE_DATES_KEY = "window_capable_dates"

# How the effective OOS threshold was derived, recorded so a lower-than-
# configured threshold is never silent.
OOS_THRESHOLD_SOURCE_CONFIGURED = "configured_minimum"
OOS_THRESHOLD_SOURCE_WINDOW_CAP = "window_capable_dates"


def _default_minimum_training_samples() -> int:
    return int(get_settings().promotion_gate_minimum_training_samples)


def _default_minimum_oos_dates() -> int:
    return int(get_settings().promotion_gate_minimum_oos_dates)


@dataclass(frozen=True, slots=True)
class PromotionGateV2Config:
    """Thresholds for the non-statistical parts of the unified gate.

    Defaults are read from settings at construction time
    (``PQW_PROMOTION_GATE_MINIMUM_OOS_DATES`` / ``..._TRAINING_SAMPLES``) so the
    gate is aligned with what the production trainer can actually emit: a
    60-session walk-forward prediction window matures at most ~54 OOS dates.
    The formal promotion protocol may raise the minimum; lowering it requires
    approval. Explicit constructor arguments still win over the settings.

    ``minimum_training_samples`` additionally reflects the trainer's own hard
    floor (LightGBM refuses fewer than 1000 labeled samples), so the gate never
    invents a looser requirement than the trainer already enforces.
    """

    minimum_training_samples: int = field(default_factory=_default_minimum_training_samples)
    minimum_oos_dates: int = field(default_factory=_default_minimum_oos_dates)
    minimum_purge_sessions: int = 1

    def __post_init__(self) -> None:
        if self.minimum_training_samples <= 0:
            raise ValueError("minimum_training_samples must be positive")
        if self.minimum_oos_dates <= 0:
            raise ValueError("minimum_oos_dates must be positive")
        if self.minimum_purge_sessions < 0:
            raise ValueError("minimum_purge_sessions must be non-negative")


@dataclass(frozen=True, slots=True)
class PromotionCandidate:
    """Normalised candidate-run evidence consumed by the unified gate.

    Build one from a ``ModelRun`` ORM row via :meth:`from_model_run`, or
    construct directly in tests / replay tooling. ``price_basis`` may be a
    :class:`PriceBasisDecision` produced by the shared contract or its
    ``audit_fields()`` mapping already persisted on the run.
    """

    market: str
    model_key: str
    run_id: str | None = None
    status: str | None = None
    scope: str | None = None
    scope_hints: Mapping[str, Any] = field(default_factory=dict)
    params: Mapping[str, Any] = field(default_factory=dict)
    data_version: Mapping[str, Any] = field(default_factory=dict)
    training_sample_count: int | None = None
    oos_evaluation: Mapping[str, Any] | None = None
    purge: Mapping[str, Any] | None = None
    price_basis: PriceBasisDecision | Mapping[str, Any] | None = None
    corporate_actions: Mapping[str, Any] | None = None
    data_readiness: DataReadinessReport | Mapping[str, Any] | None = None
    statistical_gate: PromotionGateReport | Mapping[str, Any] | None = None
    artifact_references: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_model_run(
        cls,
        model_run: object,
        *,
        scope: str | None = None,
        training_sample_count: int | None = None,
        oos_evaluation: Mapping[str, Any] | None = None,
        purge: Mapping[str, Any] | None = None,
        price_basis: PriceBasisDecision | Mapping[str, Any] | None = None,
        corporate_actions: Mapping[str, Any] | None = None,
        data_readiness: DataReadinessReport | Mapping[str, Any] | None = None,
        statistical_gate: PromotionGateReport | Mapping[str, Any] | None = None,
        artifact_references: Mapping[str, str] | None = None,
    ) -> "PromotionCandidate":
        """Adapt a :class:`app.models.tables.ModelRun` row (or stub) to the gate.

        Explicit keyword arguments win; otherwise fields are read from the run's
        persisted ``config_json``. Nothing is written back to the run.
        """

        config = cls._parse_config(getattr(model_run, "config_json", None))
        resolved_scope = (
            scope
            or _first_str(config, "run_scope", "scope")
            or _first_str(config, "selection_mode")
        )
        sample_count = training_sample_count
        if sample_count is None:
            sample_count = _training_sample_count(config)
        resolved_oos = oos_evaluation if oos_evaluation is not None else _oos_from_config(config)
        resolved_purge = purge if purge is not None else {
            "purge_sessions": _first_number(config, "purge_gap_days", "purge_sessions"),
            "embargo_sessions": config.get("embargo_sessions"),
            "evaluation_protocol": config.get("evaluation_protocol"),
            "training_protocol": config.get("training_protocol"),
        }
        resolved_price_basis = price_basis
        if resolved_price_basis is None:
            resolved_price_basis = (
                config.get("prediction_price_basis_contract")
                or config.get("price_basis_contract")
            )
        resolved_corporate_actions = corporate_actions
        if resolved_corporate_actions is None and (
            "unmodeled_corporate_actions" in config or "unmodeled_opt_in" in config
        ):
            resolved_corporate_actions = {
                "unmodeled_corporate_actions": config.get("unmodeled_corporate_actions"),
                "unmodeled_opt_in": config.get("unmodeled_opt_in"),
            }
        return cls(
            market=str(getattr(model_run, "market", "") or "").strip().upper(),
            model_key=str(getattr(model_run, "name", "") or "").strip() or "unknown_model",
            run_id=None if getattr(model_run, "id", None) is None else str(getattr(model_run, "id")),
            status=_first_str(config, "status") or _attr_str(model_run, "status"),
            scope=resolved_scope,
            scope_hints={
                "selection_mode": config.get("selection_mode"),
                "universe": _attr_str(model_run, "universe"),
                "universe_version": config.get("universe_version"),
            },
            params={
                "model_type": config.get("model_type") or _attr_str(model_run, "model_type"),
                "lookback_days": config.get("lookback_days"),
                "prediction_horizon_days": config.get("prediction_horizon_days"),
                "target_profile": config.get("target_profile"),
                "prediction_dates": config.get("prediction_dates"),
                "evaluation_protocol": config.get("evaluation_protocol"),
                "training_protocol": config.get("training_protocol"),
                "train_start": _attr_str(model_run, "train_start"),
                "train_end": _attr_str(model_run, "train_end"),
                "test_start": _attr_str(model_run, "test_start"),
                "test_end": _attr_str(model_run, "test_end"),
            },
            data_version={
                "adjusted_view_sha256": config.get("adjusted_view_sha256"),
                "actions_snapshot_sha256": config.get("actions_snapshot_sha256"),
                "adjustment_version": config.get("adjustment_version"),
                "label_price_basis": config.get("label_price_basis"),
                "adjusted_view_state": config.get("adjusted_view_state"),
                "input_market_date": config.get("input_market_date"),
            },
            training_sample_count=sample_count,
            oos_evaluation=resolved_oos,
            purge={key: value for key, value in resolved_purge.items() if value is not None}
            or None,
            price_basis=resolved_price_basis,
            corporate_actions=resolved_corporate_actions,
            data_readiness=data_readiness,
            statistical_gate=statistical_gate,
            artifact_references=dict(artifact_references or {}),
        )

    @staticmethod
    def _parse_config(raw: object) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}


@dataclass(frozen=True, slots=True)
class PromotionGateV2Report:
    schema_version: str
    market: str
    model_key: str
    run_id: str | None
    scope: str | None
    decision: str
    promotable: bool
    non_promotable_reasons: tuple[str, ...]
    reasons: tuple[str, ...]
    checks: tuple[PromotionGateCheck, ...]
    data_version: Mapping[str, Any]
    code_version: Mapping[str, Any]
    params: Mapping[str, Any]
    evaluation: Mapping[str, Any]
    decided_by: str
    decided_at: str
    content_digest: str

    @property
    def evidence_version(self) -> str:
        return _approval_evidence_version(self.market, self.run_id, self.content_digest)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def promotion_status_fields(self) -> dict[str, Any]:
        """Fields a product should merge so a non-promotable run is explicit."""

        return {
            "promotion_schema_version": self.schema_version,
            "promotion_evidence_version": self.evidence_version,
            "promotion_decision": self.decision,
            "promotable": self.promotable,
            "non_promotable_reasons": list(self.non_promotable_reasons),
        }

    def failed_checks(self) -> tuple[PromotionGateCheck, ...]:
        return tuple(item for item in self.checks if item.status != "PASS")


@dataclass(frozen=True, slots=True)
class PromotionApprovalWriteResult:
    evidence_version: str
    artifact_dir: Path
    record_path: Path
    manifest_path: Path
    reused_existing: bool


def _first_str(mapping: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _first_number(mapping: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = mapping.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _attr_str(obj: object, name: str) -> str | None:
    value = getattr(obj, name, None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _training_sample_count(config: Mapping[str, Any]) -> int | None:
    for key in ("sample_count", "training_sample_count", "train_window_sample_count"):
        value = config.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
    audits = config.get("training_window_audits")
    if isinstance(audits, list):
        for entry in reversed(audits):
            if isinstance(entry, Mapping) and not isinstance(entry.get("sample_count"), bool):
                if isinstance(entry.get("sample_count"), (int, float)):
                    return int(entry["sample_count"])
    profile = config.get("train_window_target_profile")
    if isinstance(profile, Mapping) and isinstance(profile.get("sample_count"), (int, float)):
        return int(profile["sample_count"])
    return None


def _oos_from_config(config: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for key in ("oos_evaluation", "oos_metrics", "evaluation_summary"):
        value = config.get(key)
        if isinstance(value, Mapping):
            return value
    dates = config.get("oos_date_count")
    metric = None
    for key in _OOS_PERFORMANCE_KEYS:
        if config.get(key) is not None:
            metric = config.get(key)
            break
    if dates is None and metric is None:
        return None
    return {
        "evaluated_date_count": dates,
        "mean_risk_adjusted_return": metric,
    }


def _check(
    key: str,
    status: str,
    observed: float | bool | None,
    threshold: str,
    detail: str,
) -> PromotionGateCheck:
    return PromotionGateCheck(
        key=key, status=status, observed=observed, threshold=threshold, detail=detail
    )


def classify_run_scope(
    scope: str | None, scope_hints: Mapping[str, Any] | None = None
) -> tuple[bool, str]:
    """Return ``(promotable, reason)`` for a run/selection scope.

    Only *explicit* research markers make a run non-promotable. A plain run with
    no scope label stays eligible: the gate must not invent a blocking scope out
    of missing metadata, but it must never let a labelled research run through.
    """

    text = str(scope or "").strip()
    lowered = text.lower()
    if lowered:
        for marker in RESEARCH_SCOPE_MARKERS:
            if marker in lowered:
                return False, f"research_scope_not_promotable:{text}"
    hints = scope_hints or {}
    selection_mode = str(hints.get("selection_mode") or "").strip().lower()
    if selection_mode in RESEARCH_SELECTION_MODES:
        return False, f"research_selection_mode_not_promotable:{selection_mode}"
    return True, ""


def _resolution_price_basis(
    value: PriceBasisDecision | Mapping[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(value, PriceBasisDecision):
        return value.audit_fields()
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _price_basis_check(value: PriceBasisDecision | Mapping[str, Any] | None) -> PromotionGateCheck:
    fields = _resolution_price_basis(value)
    if not fields:
        return _check(
            "price_basis_contract",
            "NOT_ENOUGH_EVIDENCE",
            None,
            "shared price-basis decision present, not reject, authorized if raw",
            "No price-basis contract decision was supplied for this run.",
        )
    decision = str(fields.get("decision") or "")
    fallback = str(fields.get("fallback_policy") or "")
    authorized_by = fields.get("authorized_by")
    version_match = fields.get("version_match")
    reasons = ", ".join(str(item) for item in (fields.get("reasons") or ())) or "none"
    if decision == DECISION_REJECT:
        return _check(
            "price_basis_contract",
            "FAIL",
            False,
            "shared price-basis decision present, not reject, authorized if raw",
            f"The shared price-basis contract rejected the run (reasons: {reasons}).",
        )
    if version_match is False:
        return _check(
            "price_basis_contract",
            "FAIL",
            False,
            "recorded view hash equals current view hash",
            "Recorded adjusted-view hash no longer matches the current view "
            "(version mismatch).",
        )
    if fallback == FALLBACK_EXPLICIT_RAW and not authorized_by:
        return _check(
            "price_basis_contract",
            "FAIL",
            False,
            "raw fallback only with a recorded authorization",
            "The run fell back to raw prices without a recorded authorization.",
        )
    if decision not in {DECISION_ALLOW_ADJUSTED, DECISION_ALLOW_RAW_WITH_AUTHORIZATION}:
        return _check(
            "price_basis_contract",
            "FAIL",
            False,
            "one of the enumerated price-basis decisions",
            f"Unknown price-basis decision: {decision or '<missing>'}.",
        )
    raw_basis = decision == DECISION_ALLOW_RAW_WITH_AUTHORIZATION and fallback != FALLBACK_NOT_APPLICABLE
    detail = (
        f"Raw basis was explicitly authorized by {authorized_by} "
        f"(label_price_basis={fields.get('label_price_basis')})."
        if raw_basis
        else f"Adjusted basis allowed (view_state={fields.get('view_state')})."
    )
    return _check(
        "price_basis_contract",
        "PASS",
        True,
        "shared price-basis decision present, not reject, authorized if raw",
        detail,
    )


def _corporate_action_check(value: Mapping[str, Any] | None) -> PromotionGateCheck:
    if value is None:
        return _check(
            "corporate_action_coverage",
            "NOT_ENOUGH_EVIDENCE",
            None,
            "unmodeled event detail empty, or explicit opt-in with recorded audit",
            "No corporate-action coverage audit was supplied.",
        )
    if "unmodeled_corporate_actions" not in value or "unmodeled_opt_in" not in value:
        return _check(
            "corporate_action_coverage",
            "NOT_ENOUGH_EVIDENCE",
            None,
            "unmodeled event detail empty, or explicit opt-in with recorded audit",
            "Corporate-action audit is missing the event detail or the opt-in flag.",
        )
    details = value.get("unmodeled_corporate_actions") or []
    if not isinstance(details, (list, tuple)):
        return _check(
            "corporate_action_coverage",
            "FAIL",
            False,
            "unmodeled event detail empty, or explicit opt-in with recorded audit",
            "Corporate-action detail must be a list of unmodeled events.",
        )
    if not details:
        return _check(
            "corporate_action_coverage",
            "PASS",
            True,
            "unmodeled event detail empty, or explicit opt-in with recorded audit",
            "No unmodeled corporate-action events fell inside the traded window.",
        )
    if value.get("unmodeled_opt_in") is True:
        return _check(
            "corporate_action_coverage",
            "PASS",
            True,
            "unmodeled event detail empty, or explicit opt-in with recorded audit",
            f"{len(details)} unmodeled event(s) accepted under explicit opt-in; "
            "the detail list is recorded in the run audit.",
        )
    return _check(
        "corporate_action_coverage",
        "FAIL",
        False,
        "unmodeled event detail empty, or explicit opt-in with recorded audit",
        f"{len(details)} unmodeled corporate-action event(s) inside the traded "
        "window without an explicit opt-in.",
    )


def _data_readiness_check(value: DataReadinessReport | Mapping[str, Any] | None) -> PromotionGateCheck:
    if value is None:
        return _check(
            "data_readiness",
            "NOT_ENOUGH_EVIDENCE",
            None,
            "readiness blockers empty",
            "No data-readiness audit was supplied.",
        )
    if isinstance(value, DataReadinessReport):
        blockers = tuple(value.blockers)
    else:
        blockers = tuple(str(item) for item in (value.get("blockers") or ()))
    if blockers:
        return _check(
            "data_readiness",
            "FAIL",
            False,
            "readiness blockers empty",
            "Data readiness blockers: " + ", ".join(blockers),
        )
    return _check(
        "data_readiness",
        "PASS",
        True,
        "readiness blockers empty",
        "Data-readiness audit reports no blockers.",
    )


def _training_sample_check(count: int | None, minimum: int) -> PromotionGateCheck:
    if count is None:
        return _check(
            "training_sample_size",
            "NOT_ENOUGH_EVIDENCE",
            None,
            f">={minimum}",
            "The candidate run does not report a training sample count.",
        )
    return _check(
        "training_sample_size",
        "PASS" if int(count) >= minimum else "FAIL",
        float(count),
        f">={minimum}",
        "Labeled training samples available to the candidate.",
    )


def _oos_threshold(
    value: Mapping[str, Any] | None, configured_minimum: int
) -> tuple[int, str, float | None]:
    """Resolve the effective OOS date threshold and its provenance.

    By default the threshold is the configured minimum. When the evidence
    *declares* the maximum number of matured evaluation dates its evaluation
    window can physically produce (``window_capable_dates``), the threshold is
    lowered to that declared cap -- but only when it is smaller, and never
    silently: the source is returned and recorded in the report audit fields so
    a reviewer can see exactly why a lower bar was applied. Runs that do not
    declare the cap keep the configured threshold unchanged.
    """

    capable = _first_number(value, WINDOW_CAPABLE_DATES_KEY) if value else None
    if capable is not None and capable > 0:
        if capable < configured_minimum:
            # The declared window cannot reach the configured bar; apply the
            # physical ceiling and record where the lower threshold came from.
            return int(capable), OOS_THRESHOLD_SOURCE_WINDOW_CAP, capable
        return int(configured_minimum), OOS_THRESHOLD_SOURCE_CONFIGURED, capable
    return int(configured_minimum), OOS_THRESHOLD_SOURCE_CONFIGURED, None


def _oos_evaluation_check(
    value: Mapping[str, Any] | None,
    minimum_dates: int,
    *,
    threshold_source: str = OOS_THRESHOLD_SOURCE_CONFIGURED,
) -> PromotionGateCheck:
    requirement = f"evaluated_date_count>={minimum_dates} and mean performance>0"
    cap_note = (
        " (threshold lowered to the window's declared maximum capable dates)"
        if threshold_source == OOS_THRESHOLD_SOURCE_WINDOW_CAP
        else ""
    )
    if not value:
        return _check(
            "oos_evaluation",
            "NOT_ENOUGH_EVIDENCE",
            None,
            requirement,
            "No OOS evaluation summary was supplied.",
        )
    dates = _first_number(value, "evaluated_date_count", "oos_date_count", "date_count")
    metric = _first_number(value, *_OOS_PERFORMANCE_KEYS)
    if dates is None or metric is None:
        return _check(
            "oos_evaluation",
            "NOT_ENOUGH_EVIDENCE",
            None,
            requirement,
            "OOS summary must contain an evaluated date count and a mean performance metric.",
        )
    if dates < minimum_dates:
        return _check(
            "oos_evaluation",
            "FAIL",
            float(dates),
            requirement,
            "OOS evaluation window is shorter than the promotion requirement."
            + cap_note,
        )
    if metric <= 0:
        return _check(
            "oos_evaluation",
            "FAIL",
            float(metric),
            requirement,
            "Mean OOS performance is not positive." + cap_note,
        )
    return _check(
        "oos_evaluation",
        "PASS",
        float(metric),
        requirement,
        f"{int(dates)} OOS dates evaluated." + cap_note,
    )


def _purge_embargo_check(
    value: Mapping[str, Any] | None, minimum_purge: int
) -> PromotionGateCheck:
    if not value:
        return _check(
            "purge_embargo_audit",
            "NOT_ENOUGH_EVIDENCE",
            None,
            f"purge_sessions>={minimum_purge} and embargo_sessions>=0",
            "No purge/embargo protocol audit was supplied.",
        )
    purge = _first_number(value, "purge_sessions", "purge_gap_days")
    embargo = _first_number(value, "embargo_sessions")
    if purge is None or embargo is None:
        return _check(
            "purge_embargo_audit",
            "NOT_ENOUGH_EVIDENCE",
            None,
            f"purge_sessions>={minimum_purge} and embargo_sessions>=0",
            "Purge/embargo audit must record both purge and embargo session counts.",
        )
    if value.get("purged") is False:
        return _check(
            "purge_embargo_audit",
            "FAIL",
            False,
            f"purge_sessions>={minimum_purge} and embargo_sessions>=0",
            "The run audit explicitly records that purge was not applied.",
        )
    if purge < minimum_purge or embargo < 0:
        return _check(
            "purge_embargo_audit",
            "FAIL",
            float(purge),
            f"purge_sessions>={minimum_purge} and embargo_sessions>=0",
            f"purge_sessions={purge:g}, embargo_sessions={embargo:g}.",
        )
    return _check(
        "purge_embargo_audit",
        "PASS",
        float(purge),
        f"purge_sessions>={minimum_purge} and embargo_sessions>=0",
        "Point-in-time purge/embargo protocol is recorded.",
    )


def _statistical_gate_check(
    value: PromotionGateReport | Mapping[str, Any] | None,
) -> PromotionGateCheck:
    if value is None:
        return _check(
            "statistical_evidence",
            "NOT_ENOUGH_EVIDENCE",
            None,
            "statistical sub-gate decision is ELIGIBLE_FOR_MANUAL_REVIEW",
            "No statistical promotion-gate report was supplied.",
        )
    if isinstance(value, PromotionGateReport):
        decision = value.decision
        failed = tuple(item.key for item in value.checks if item.status == "FAIL")
    else:
        decision = str(value.get("decision") or "")
        failed = tuple(
            str(item.get("key"))
            for item in (value.get("checks") or ())
            if isinstance(item, Mapping) and item.get("status") == "FAIL"
        )
    if decision == DECISION_ELIGIBLE:
        return _check(
            "statistical_evidence",
            "PASS",
            True,
            "statistical sub-gate decision is ELIGIBLE_FOR_MANUAL_REVIEW",
            "Statistical sub-gate (v1) passed all evidence checks.",
        )
    if decision == DECISION_OBSERVE:
        return _check(
            "statistical_evidence",
            "NOT_ENOUGH_EVIDENCE",
            None,
            "statistical sub-gate decision is ELIGIBLE_FOR_MANUAL_REVIEW",
            "Statistical sub-gate returned OBSERVE (missing evidence).",
        )
    detail = "Statistical sub-gate rejected the candidate"
    if failed:
        detail += " (failed: " + ", ".join(failed) + ")"
    return _check(
        "statistical_evidence",
        "FAIL",
        False,
        "statistical sub-gate decision is ELIGIBLE_FOR_MANUAL_REVIEW",
        detail + ".",
    )


def _status_check(status: str | None) -> PromotionGateCheck:
    if status is None:
        return _check(
            "run_status_completed",
            "NOT_ENOUGH_EVIDENCE",
            None,
            "one of: " + ", ".join(sorted(COMPLETED_RUN_STATUSES)),
            "Candidate run status was not supplied.",
        )
    normalized = status.strip().lower()
    return _check(
        "run_status_completed",
        "PASS" if normalized in COMPLETED_RUN_STATUSES else "FAIL",
        normalized in COMPLETED_RUN_STATUSES,
        "one of: " + ", ".join(sorted(COMPLETED_RUN_STATUSES)),
        f"Candidate run status is {status!r}.",
    )


def resolve_code_version(
    *,
    frozen_dir: Path | str | None = None,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve the code version for the approval record.

    Order: explicit env override, then ``git rev-parse HEAD`` plus a bounded
    dirty-worktree flag, then ``None``. ``frozen_dir`` lets a caller cite the
    frozen source directory instead of a live checkout.
    """

    resolved_env = dict(os.environ if env is None else env)
    commit = (
        resolved_env.get("DIM_CODE_COMMIT")
        or resolved_env.get("GIT_COMMIT")
        or resolved_env.get("CODE_VERSION")
    )
    source = "env" if commit else None
    dirty: bool | None = None
    workdir = str(cwd) if cwd is not None else None
    if not commit:
        try:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            candidate = head.stdout.strip()
            if head.returncode == 0 and candidate:
                commit = candidate
                source = "git"
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if status.returncode == 0:
                dirty = bool(status.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            commit = commit or None
    return {
        "commit": commit,
        "worktree_dirty": dirty,
        "resolved_from": source,
        "frozen_dir": str(frozen_dir) if frozen_dir is not None else None,
    }


def evaluate_promotion_gate(
    candidate: PromotionCandidate,
    *,
    config: PromotionGateV2Config | None = None,
    code_version: Mapping[str, Any] | None = None,
    frozen_dir: Path | str | None = None,
    decided_by: str = "automated:promotion_gate_v2",
    decided_at: str | None = None,
) -> PromotionGateV2Report:
    """Evaluate every promotion precondition and return one unified decision.

    ``promotable`` is True only when every check passes. That still means
    "eligible for manual promotion", never "already published" — consistent
    with the v1 sub-gate. Any FAIL makes the decision ``REJECT``; a missing
    evidence item makes it ``OBSERVE``. Either way ``promotable`` is False.
    """

    resolved = config or PromotionGateV2Config()
    scope_promotable, scope_reason = classify_run_scope(candidate.scope, candidate.scope_hints)
    oos_threshold_effective, oos_threshold_source, oos_window_capable = _oos_threshold(
        candidate.oos_evaluation, resolved.minimum_oos_dates
    )
    checks = [
        _status_check(candidate.status),
        _check(
            "run_scope_promotable",
            "PASS" if scope_promotable else "FAIL",
            scope_promotable,
            "scope carries no research-only marker",
            scope_reason
            or (
                "Run scope carries no research-only marker."
                if candidate.scope
                else "Run scope is unlabelled; no research-only marker applies."
            ),
        ),
        _price_basis_check(candidate.price_basis),
        _corporate_action_check(candidate.corporate_actions),
        _data_readiness_check(candidate.data_readiness),
        _training_sample_check(candidate.training_sample_count, resolved.minimum_training_samples),
        _oos_evaluation_check(
            candidate.oos_evaluation,
            oos_threshold_effective,
            threshold_source=oos_threshold_source,
        ),
        _purge_embargo_check(candidate.purge, resolved.minimum_purge_sessions),
        _statistical_gate_check(candidate.statistical_gate),
    ]

    if any(item.status == "FAIL" for item in checks):
        decision = DECISION_REJECT_V2
    elif any(item.status == "NOT_ENOUGH_EVIDENCE" for item in checks):
        decision = DECISION_OBSERVE
    else:
        decision = DECISION_ELIGIBLE
    promotable = decision == DECISION_ELIGIBLE

    non_promotable_reasons = tuple(
        f"{item.key}: {item.detail}" for item in checks if item.status in {"FAIL", "NOT_ENOUGH_EVIDENCE"}
    )
    reasons = tuple(
        sorted(
            {
                item.detail
                for item in checks
                if item.status in {"FAIL", "NOT_ENOUGH_EVIDENCE"}
            }
        )
    )
    resolved_code_version = dict(code_version) if code_version is not None else resolve_code_version(
        frozen_dir=frozen_dir
    )
    evaluation = {
        "training_sample_count": candidate.training_sample_count,
        "oos_evaluation": dict(candidate.oos_evaluation or {}),
        "oos_threshold_configured": int(resolved.minimum_oos_dates),
        "oos_threshold_effective": int(oos_threshold_effective),
        "oos_threshold_source": oos_threshold_source,
        "oos_window_capable_dates": (
            None if oos_window_capable is None else int(oos_window_capable)
        ),
        "purge": dict(candidate.purge or {}),
        "statistical_gate_decision": _gate_decision(candidate.statistical_gate),
        "statistical_gate_evidence_versions": _gate_evidence_versions(candidate.statistical_gate),
        "check_statuses": {item.key: item.status for item in checks},
    }
    report = PromotionGateV2Report(
        schema_version=SCHEMA_VERSION,
        market=str(candidate.market or "").strip().upper(),
        model_key=candidate.model_key,
        run_id=candidate.run_id,
        scope=candidate.scope,
        decision=decision,
        promotable=promotable,
        non_promotable_reasons=non_promotable_reasons,
        reasons=reasons,
        checks=tuple(checks),
        data_version=dict(candidate.data_version),
        code_version=resolved_code_version,
        params=dict(candidate.params),
        evaluation=evaluation,
        decided_by=decided_by,
        decided_at=decided_at or datetime.now(timezone.utc).isoformat(),
        content_digest="",
    )
    digest = _content_digest(report)
    return _replace_digest(report, digest)


def _gate_decision(value: PromotionGateReport | Mapping[str, Any] | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, PromotionGateReport):
        return value.decision
    return value.get("decision")


def _gate_evidence_versions(value: PromotionGateReport | Mapping[str, Any] | None) -> list[str]:
    if value is None:
        return []
    versions = getattr(value, "source_evidence_versions", None)
    if versions is None and isinstance(value, Mapping):
        versions = value.get("source_evidence_versions")
    return [str(item) for item in (versions or ())]


def _content_digest(report: PromotionGateV2Report) -> str:
    payload = asdict(report)
    payload.pop("content_digest", None)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _replace_digest(report: PromotionGateV2Report, digest: str) -> PromotionGateV2Report:
    payload = asdict(report)
    payload["content_digest"] = digest
    payload["checks"] = tuple(report.checks)
    return PromotionGateV2Report(**payload)


def _approval_evidence_version(market: str, run_id: str | None, digest: str) -> str:
    return f"{SCHEMA_VERSION}:{market or 'NA'}:{run_id or 'unknown'}:{digest[:20]}"


def persist_promotion_approval_record(
    report: PromotionGateV2Report,
    *,
    root: Path,
) -> PromotionApprovalWriteResult:
    """Write the immutable, content-addressed approval record.

    Idempotent: identical evidence reuses the directory. Non-overwrite: a
    different record that hashes to an existing directory raises instead of
    silently replacing a prior approval decision.
    """

    payload = asdict(report)
    evidence_version = report.evidence_version
    root.mkdir(parents=True, exist_ok=True)
    target_dir = root / evidence_version.replace(":", "_")
    with tempfile.TemporaryDirectory(prefix=".promotion-approval-", dir=root) as temporary_name:
        temporary_dir = Path(temporary_name)
        record_path = temporary_dir / "approval.json"
        record_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        written_sha256 = hashlib.sha256(record_path.read_bytes()).hexdigest()
        manifest = {
            "schema_version": f"{SCHEMA_VERSION}_manifest_v1",
            "evidence_version": evidence_version,
            "market": report.market,
            "run_id": report.run_id,
            "model_key": report.model_key,
            "decision": report.decision,
            "promotable": report.promotable,
            "non_promotable_reasons": list(report.non_promotable_reasons),
            "record_file": record_path.name,
            "record_sha256": written_sha256,
            "content_digest": report.content_digest,
            "code_version": dict(report.code_version),
            "data_version": dict(report.data_version),
            "decided_by": report.decided_by,
            "decided_at": report.decided_at,
        }
        (temporary_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if target_dir.exists():
            manifest_path = target_dir / "manifest.json"
            if not manifest_path.exists():
                raise RuntimeError(f"immutable approval record is missing manifest: {target_dir}")
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if existing.get("record_sha256") != written_sha256:
                raise RuntimeError(f"refusing to overwrite promotion approval record: {evidence_version}")
            return PromotionApprovalWriteResult(
                evidence_version=evidence_version,
                artifact_dir=target_dir,
                record_path=target_dir / record_path.name,
                manifest_path=manifest_path,
                reused_existing=True,
            )
        temporary_dir.replace(target_dir)
    return PromotionApprovalWriteResult(
        evidence_version=evidence_version,
        artifact_dir=target_dir,
        record_path=target_dir / "approval.json",
        manifest_path=target_dir / "manifest.json",
        reused_existing=False,
    )


def resolve_candidate_price_basis(
    market: str,
    run_config: Mapping[str, Any] | None,
    *,
    entry_point: str = ENTRY_INFERENCE,
    symbols: set[str] | None = None,
    allow_raw_fallback: bool | None = None,
    root: Path | None = None,
) -> tuple[PriceBasisDecision, AdjustedViewProbe]:
    """Run the shared price-basis contract for a candidate run.

    Mirrors the backtest runner's resolution so the gate reaches the *same*
    decision the entry points did: the run's recorded view hash is enforced,
    coverage is gated only when the run declared it, and raw fallback is only
    allowed when the run (or an explicit argument) authorised it.
    """

    config = dict(run_config or {})
    expected_view_sha256 = config.get("adjusted_view_sha256")
    require_full_coverage = bool(config.get("require_full_adjusted_coverage", False))
    if allow_raw_fallback is None:
        allow_raw_fallback = bool(config.get("raw_fallback_allowed", False))
    probe = probe_adjusted_view(
        market, symbols=symbols, read_bars=require_full_coverage, root=root
    )
    decision = decide_price_basis(
        entry_point,
        probe,
        PriceBasisRequirements(
            entry_point=entry_point,
            requires_adjusted_prices=True,
            require_full_coverage=require_full_coverage,
            allow_raw_fallback=bool(allow_raw_fallback),
            expected_view_sha256=expected_view_sha256,
            gates_coverage=require_full_coverage,
        ),
    )
    return decision, probe


def assert_promotable(report: PromotionGateV2Report) -> None:
    """Fail closed at the recommendation/production boundary.

    Any product path that turns a run into recommendations or production output
    must call this (or require ``report.promotable``) before proceeding.
    """

    if not report.promotable:
        raise PromotionNotAuthorized(
            f"run {report.run_id or '<unknown>'} is not promotable "
            f"({report.decision}): " + "; ".join(report.non_promotable_reasons)
        )
