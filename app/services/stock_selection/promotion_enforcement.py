"""Serve-time enforcement of the unified promotion gate (v2).

``promotion_gate_v2`` *decides* whether a model run may be promoted, but the
decision is inert until the recommendation / production path has to honour it.
This module is the narrow bridge between that decision and the one place where
a model run's predictions become served recommendations: the production
signal-run selection in :mod:`app.services.repositories.predictions`.

Semantics (phase 4)
-------------------
* Every candidate run is evaluated with the same unified gate used at
  promotion time. Evidence the gate needs may be supplied explicitly, read
  from the run's persisted ``config_json`` or, for OOS coverage, read from the
  latest :class:`~app.models.tables.ModelEvaluation` row.
* A gate ``REJECT`` is an *explicit* failed check ("数据合同或回测风险检查不通过")
  and, under enforcement, the run is skipped: it can never be the serving
  champion / recommendation basis. Its artifact rows are still reachable from
  the research path (``list_predictions_for_run``), just labelled.
* ``OBSERVE`` (missing evidence) is not an explicit failure. By default it is
  *marked* non-promotable but is not intercepted, because blocking it would
  disable every legacy run whose promotion evidence was never persisted. Set
  ``PQW_PROMOTION_GATE_REQUIRE_COMPLETE_EVIDENCE=true`` to tighten the gate so
  an ``OBSERVE`` run is withheld too. See :func:`assess_run_for_serving`.
* ``PQW_PROMOTION_GATE_ENFORCE`` (default ``True``) controls interception.
  When disabled, no run is skipped, but every non-promotable run is still
  labelled and a WARNING is logged.

The assessment carries the audit fields the gate already exposes
(``promotion_schema_version`` / ``promotion_evidence_version`` /
``promotion_decision`` / ``promotable`` / ``non_promotable_reasons``) so a
served payload records *which* gate decision allowed it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping

from app.core.config import get_settings
from app.models.tables import ModelEvaluation
from app.services.stock_selection.promotion_gate_v2 import (
    DECISION_OBSERVE,
    DECISION_REJECT_V2,
    PromotionCandidate,
    PromotionGateV2Config,
    PromotionGateV2Report,
    evaluate_promotion_gate,
    resolve_code_version,
)

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _cached_code_version() -> tuple[tuple[str, Any], ...]:
    """Resolve the code version once per process.

    The serving path evaluates the gate on every read; shelling out to git each
    time would be wasteful, and the approval record only needs a stable commit
    identity for the process lifetime.
    """

    return tuple(sorted(resolve_code_version().items()))


PROMOTION_LABEL_ZH = "非晋级/研究口径"
PROMOTION_LABEL_EN = "Not promoted / research-only"

# Config keys that may carry persisted evidence on a model run. The trainer and
# backtest runner already persist ``prediction_price_basis_contract`` and the
# corporate-action audit; ``data_readiness`` / ``statistical_gate`` are optional
# and supplied by promotion tooling.
_DATA_READINESS_CONFIG_KEYS = ("data_readiness", "data_readiness_report")
_STATISTICAL_GATE_CONFIG_KEYS = (
    "statistical_gate",
    "statistical_gate_report",
    "promotion_gate_report",
)
_OOS_CONFIG_KEYS = ("oos_evaluation", "oos_metrics", "evaluation_summary")


@dataclass(frozen=True, slots=True)
class ServingPromotionDecision:
    """One run's serve-time gate assessment plus the enforcement outcome."""

    report: PromotionGateV2Report
    enforce: bool
    blocked: bool
    require_complete_evidence: bool = False
    warning: str | None = None

    @property
    def run_id(self) -> str | None:
        return self.report.run_id

    @property
    def promotable(self) -> bool:
        return self.report.promotable

    @property
    def marked_non_promotable(self) -> bool:
        return not self.report.promotable

    def status_fields(self) -> dict[str, Any]:
        """Audit + presentation fields merged into served payloads."""

        fields = dict(self.report.promotion_status_fields())
        fields.update(
            {
                "promotion_enforced": bool(self.enforce),
                "promotion_require_complete_evidence": bool(
                    self.require_complete_evidence
                ),
                "promotion_blocked_from_serving": bool(self.blocked),
                "promotion_label": (
                    PROMOTION_LABEL_ZH if self.marked_non_promotable else None
                ),
                "promotion_label_en": (
                    PROMOTION_LABEL_EN if self.marked_non_promotable else None
                ),
                "research_only": bool(self.marked_non_promotable),
            }
        )
        return fields


def promotion_enforce_enabled(settings: Any | None = None) -> bool:
    """Read the explicit enforcement switch (default True = fail-closed)."""

    resolved = settings if settings is not None else get_settings()
    return bool(getattr(resolved, "promotion_gate_enforce", True))


def promotion_require_complete_evidence_enabled(settings: Any | None = None) -> bool:
    """Read the "OBSERVE also blocks" switch (default False = observe-only).

    ``OBSERVE`` means the gate could not decide because evidence was missing.
    With this switch off (default, historical behaviour) such a run is marked
    non-promotable but still served. Turning it on tightens the gate to require
    complete evidence, so an OBSERVE run is withheld from serving just like a
    ``REJECT``. It never overrides ``promotion_gate_enforce``: interception only
    happens when enforcement itself is on.
    """

    resolved = settings if settings is not None else get_settings()
    return bool(getattr(resolved, "promotion_gate_require_complete_evidence", False))


def _run_config(run: object) -> dict[str, Any]:
    raw = getattr(run, "config_json", None)
    if isinstance(raw, Mapping):
        return dict(raw)
    if not raw:
        return {}
    try:
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _mapping_or_none(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _first_config_mapping(
    config: Mapping[str, Any], keys: tuple[str, ...]
) -> Mapping[str, Any] | None:
    for key in keys:
        value = config.get(key)
        if isinstance(value, Mapping):
            return value
    return None


def _oos_from_evaluation(db: Any, run: object) -> Mapping[str, Any] | None:
    """Best-effort OOS summary from the run's latest persisted evaluation.

    The gate looks for a risk-adjusted / excess mean, which the legacy
    evaluation schema does not persist. Coverage days are always available;
    the performance metric is only supplied when the summary already carries
    one of the gate's recognised keys, so missing evidence stays honest
    (``OBSERVE``) instead of being faked with a raw return.
    """

    run_id = getattr(run, "id", None)
    if db is None or run_id is None:
        return None
    try:
        evaluation = db.query(ModelEvaluation).filter(
            ModelEvaluation.model_run_id == int(run_id)
        ).order_by(ModelEvaluation.id.desc()).first()
    except Exception:  # pragma: no cover - defensive; a broken read must not serve
        return None
    if evaluation is None:
        return None
    summary: dict[str, Any] = {}
    raw_summary = getattr(evaluation, "summary_json", None)
    if raw_summary:
        try:
            parsed = json.loads(raw_summary)
            if isinstance(parsed, dict):
                summary = parsed
        except (TypeError, ValueError):
            summary = {}
    payload: dict[str, Any] = {
        "evaluated_date_count": getattr(evaluation, "oos_coverage_days", None),
    }
    for key in (
        "mean_risk_adjusted_return",
        "mean_calendar_risk_adjusted_return",
        "mean_excess_return",
    ):
        if summary.get(key) is not None:
            payload[key] = summary[key]
            break
    return payload


def build_promotion_candidate(
    run: object,
    *,
    db: Any | None = None,
    oos_evaluation: Mapping[str, Any] | None = None,
    data_readiness: Mapping[str, Any] | None = None,
    statistical_gate: Mapping[str, Any] | None = None,
) -> PromotionCandidate:
    """Adapt a persisted model run to the unified gate.

    Explicit arguments win; otherwise evidence is read from the run's
    ``config_json`` (and the latest evaluation row for OOS coverage). Nothing
    is written to the run.
    """

    config = _run_config(run)
    resolved_oos = oos_evaluation
    if resolved_oos is None:
        resolved_oos = _first_config_mapping(config, _OOS_CONFIG_KEYS)
    if resolved_oos is None:
        resolved_oos = _oos_from_evaluation(db, run) if db is not None else None
    return PromotionCandidate.from_model_run(
        run,
        oos_evaluation=resolved_oos,
        data_readiness=(
            data_readiness
            if data_readiness is not None
            else _first_config_mapping(config, _DATA_READINESS_CONFIG_KEYS)
        ),
        statistical_gate=(
            statistical_gate
            if statistical_gate is not None
            else _first_config_mapping(config, _STATISTICAL_GATE_CONFIG_KEYS)
        ),
    )


def assess_run_for_serving(
    run: object,
    *,
    db: Any | None = None,
    enforce: bool | None = None,
    require_complete_evidence: bool | None = None,
    settings: Any | None = None,
    config: PromotionGateV2Config | None = None,
    oos_evaluation: Mapping[str, Any] | None = None,
    data_readiness: Mapping[str, Any] | None = None,
    statistical_gate: Mapping[str, Any] | None = None,
    code_version: Mapping[str, Any] | None = None,
    log: logging.Logger | None = None,
    log_warning: bool = True,
) -> ServingPromotionDecision:
    """Evaluate a candidate run for the serving / recommendation path.

    ``blocked`` is True when enforcement is on and the gate returned either an
    explicit ``REJECT`` or, with
    ``PQW_PROMOTION_GATE_REQUIRE_COMPLETE_EVIDENCE=true``, an ``OBSERVE``
    (missing evidence). With that switch off (default) ``OBSERVE`` runs are
    marked non-promotable but not withheld, so persisted legacy runs keep
    serving while the marking exposes the missing evidence.
    """

    report = evaluate_promotion_gate(
        build_promotion_candidate(
            run,
            db=db,
            oos_evaluation=oos_evaluation,
            data_readiness=data_readiness,
            statistical_gate=statistical_gate,
        ),
        config=config,
        code_version=(
            dict(code_version)
            if code_version is not None
            else dict(_cached_code_version())
        ),
    )
    resolved_enforce = (
        promotion_enforce_enabled(settings) if enforce is None else bool(enforce)
    )
    resolved_complete_evidence = (
        promotion_require_complete_evidence_enabled(settings)
        if require_complete_evidence is None
        else bool(require_complete_evidence)
    )
    explicit_reject = report.decision == DECISION_REJECT_V2
    observed_missing_evidence = (
        resolved_complete_evidence and report.decision == DECISION_OBSERVE
    )
    blocked = resolved_enforce and (explicit_reject or observed_missing_evidence)

    warning: str | None = None
    active_log = log if log is not None else logger
    if blocked:
        warning = (
            f"promotion gate blocked model run {report.run_id or '<unknown>'} "
            f"from serving ({report.decision}): "
            + "; ".join(report.non_promotable_reasons)
        )
        if log_warning:
            active_log.warning(warning)
    elif report.promotable:
        if log_warning:
            active_log.debug(
                "promotion gate allowed model run %s for serving",
                report.run_id or "<unknown>",
            )
    elif not resolved_enforce:
        warning = (
            f"model run {report.run_id or '<unknown>'} is not promotable "
            f"({report.decision}); labelled {PROMOTION_LABEL_ZH} but still "
            "served (PQW_PROMOTION_GATE_ENFORCE=false)"
        )
        if log_warning:
            active_log.warning(warning)
    else:
        # Explicit REJECT is handled above; this is missing evidence (OBSERVE)
        # on a served legacy run, so keep it out of the WARNING channel.
        warning = (
            f"model run {report.run_id or '<unknown>'} is not promotable "
            f"({report.decision}); labelled {PROMOTION_LABEL_ZH}; "
            "insufficient evidence — marked, not withheld"
        )
        if log_warning:
            active_log.debug(warning)

    return ServingPromotionDecision(
        report=report,
        enforce=resolved_enforce,
        blocked=blocked,
        require_complete_evidence=resolved_complete_evidence,
        warning=warning,
    )


def annotate_rows_with_promotion(
    rows: list[dict[str, Any]], decision: ServingPromotionDecision
) -> list[dict[str, Any]]:
    """Merge the promotion audit / marking fields into served payloads."""

    fields = decision.status_fields()
    return [{**row, **fields} for row in rows]


__all__ = [
    "PROMOTION_LABEL_EN",
    "PROMOTION_LABEL_ZH",
    "ServingPromotionDecision",
    "annotate_rows_with_promotion",
    "assess_run_for_serving",
    "build_promotion_candidate",
    "promotion_enforce_enabled",
    "promotion_require_complete_evidence_enabled",
]
