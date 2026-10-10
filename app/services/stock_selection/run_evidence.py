"""Produce + persist run-level promotion evidence *without* changing gate semantics.

Gap 4 (see ``acceptance/signoff/acceptance-debt-registry-zh.md``): the unified
promotion gate (``promotion_gate_v2``) reads ``data_readiness`` and
``statistical_gate`` from a run's persisted config, but no training/evaluation
runtime ever produced them, so both checks stayed ``NOT_ENOUGH_EVIDENCE`` for
every ship-able run. This module *wires* the existing producers into the run
lifecycle:

* ``data_readiness`` -> :func:`audit_market_research_readiness`
  (``production_research``), which already persists an immutable, digest-named
  readiness artifact.
* ``statistical_evidence`` -> the v1 statistical sub-gate
  (:func:`assess_candidate_promotion` over robustness evidence reports), which
  also persists a digest-named promotion-gate artifact.

Red line (owner decision, do not cross here)
-------------------------------------------
The produced evidence is recorded under *dedicated audit keys*
(:data:`DATA_READINESS_AUDIT_KEY` / :data:`STATISTICAL_GATE_AUDIT_KEY`) and the
gate's own config keys (``data_readiness`` / ``statistical_gate``) are
deliberately **not** written.  Wiring therefore does **not** change any run's
gate decision: the checks keep participating exactly as before
(``NOT_ENOUGH_EVIDENCE``).  Attaching the evidence to the gate -- or flipping
``PQW_PROMOTION_GATE_REQUIRE_COMPLETE_EVIDENCE`` -- tightens the gate and would
immediately withhold the CN serving surface (run 398 serves today as
``OBSERVE``/``blocked=False``); that is an explicit owner decision, never a side
effect of this wiring.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.core.config import get_settings

# Audit-only config keys.  These are intentionally *not* in
# ``promotion_enforcement._DATA_READINESS_CONFIG_KEYS`` /
# ``_STATISTICAL_GATE_CONFIG_KEYS`` -- the gate must not auto-consume them.
DATA_READINESS_AUDIT_KEY = "data_readiness_audit"
STATISTICAL_GATE_AUDIT_KEY = "statistical_gate_audit"
EVIDENCE_AUDIT_SCHEMA_VERSION = "stock_selection_run_evidence_audit_v1"

# Reason recorded (instead of a fabricated pass) when the runtime cannot reach
# a producer.  Kept short so it is safe to persist on a run config.
_NO_ROBUSTNESS_EVIDENCE_REASON = (
    "no robustness-evidence directories were supplied to the evaluation runtime; "
    "the statistical sub-gate needs base+stress robustness reports for the "
    "production multifactor model before it can return ELIGIBLE_FOR_MANUAL_REVIEW"
)


def produce_data_readiness_evidence(
    *,
    market: str,
    artifact_root: Path | None = None,
    required_history_sessions: int = 252,
    historical_universe_contract_path: Path | None = None,
) -> dict[str, Any]:
    """Run the readiness producer and return a compact, auditable envelope.

    The full immutable report is persisted to disk by the producer itself; this
    envelope is what the run config carries so the evidence can be located and
    read back.  Any failure is surfaced as ``produced=False`` with a reason --
    never swallowed into a fake pass.
    """

    # Imported lazily: the module pulls in DuckDB / DB repositories, and the
    # trainer path should not pay that import cost when readiness is not needed.
    from app.services.stock_selection.production_research import (
        audit_market_research_readiness,
    )

    try:
        result = audit_market_research_readiness(
            market=market,
            artifact_root=artifact_root,
            required_history_sessions=required_history_sessions,
            historical_universe_contract_path=historical_universe_contract_path,
        )
    except Exception as error:  # noqa: BLE001 - record, never block training
        return {
            "schema_version": EVIDENCE_AUDIT_SCHEMA_VERSION,
            "producer": "audit_market_research_readiness",
            "produced": False,
            "market": str(market or "").strip().upper(),
            "missing_reason": f"{type(error).__name__}: {error}"[:400],
        }
    report = result.report
    return {
        "schema_version": EVIDENCE_AUDIT_SCHEMA_VERSION,
        "producer": "audit_market_research_readiness",
        "produced": True,
        "market": report.market,
        "passed": bool(report.passed),
        "blockers": list(report.blockers),
        "scope": report.scope,
        "source_version": result.source_version,
        "evidence_version": result.evidence.evidence_version,
        "artifact_dir": str(result.evidence.artifact_dir),
        "reused_existing": bool(result.evidence.reused_existing),
    }


def produce_statistical_gate_evidence(
    *,
    robustness_evidence_dirs: "Sequence[str | Path] | None" = None,
    baseline_comparison_passed: bool | None = None,
    artifact_root: Path | None = None,
) -> dict[str, Any]:
    """Run the v1 statistical sub-gate over robustness evidence and envelope it.

    ``robustness_evidence_dirs`` is the set of directories holding persisted
    robustness reports (each carries its own round-trip cost in the report the
    v1 gate cross-checks).  When none is supplied the producer cannot run at
    all, so an explicit ``produced=False`` reason is recorded.
    """

    from app.services.stock_selection.promotion_gate import (
        assess_candidate_promotion,
        load_robustness_evidence,
        persist_promotion_gate_report,
    )

    if not robustness_evidence_dirs:
        return {
            "schema_version": EVIDENCE_AUDIT_SCHEMA_VERSION,
            "producer": "assess_candidate_promotion",
            "produced": False,
            "missing_reason": _NO_ROBUSTNESS_EVIDENCE_REASON,
        }
    loaded = [load_robustness_evidence(Path(path)) for path in robustness_evidence_dirs]
    reports = [item[0] for item in loaded]
    versions = [item[1] for item in loaded]
    try:
        report = assess_candidate_promotion(
            reports,
            source_evidence_versions=versions,
            baseline_comparison_passed=baseline_comparison_passed,
        )
    except Exception as error:  # noqa: BLE001 - record, never block training
        return {
            "schema_version": EVIDENCE_AUDIT_SCHEMA_VERSION,
            "producer": "assess_candidate_promotion",
            "produced": False,
            "missing_reason": f"{type(error).__name__}: {error}"[:400],
        }
    root = artifact_root or (
        get_settings().artifacts_dir / "stock_selection_research" / "promotion_gates"
    )
    write = persist_promotion_gate_report(report, root=root)
    return {
        "schema_version": EVIDENCE_AUDIT_SCHEMA_VERSION,
        "producer": "assess_candidate_promotion",
        "produced": True,
        "decision": report.decision,
        "failed_checks": [item.key for item in report.checks if item.status == "FAIL"],
        "missing_checks": [
            item.key for item in report.checks if item.status == "NOT_ENOUGH_EVIDENCE"
        ],
        "source_evidence_versions": list(report.source_evidence_versions),
        "evidence_version": write.evidence_version,
        "artifact_dir": str(write.artifact_dir),
        "reused_existing": bool(write.reused_existing),
    }


def discover_robustness_evidence_dirs(root: Path | None) -> list[Path]:
    """Best-effort discovery of persisted robustness report directories.

    The research pipeline writes ``robustness.json`` (plus a manifest) under
    digest-named subdirectories of ``.../stock_selection_research/robustness``.
    The training runtime cannot *generate* those reports (they need the
    selection-experiment dataset), but when they already exist it can consume
    them.  Returns ``[]`` when the root is missing -- the caller records the
    honest "no evidence" reason rather than fabricating a pass.
    """

    if root is None:
        return []
    base = Path(root) / "stock_selection_research" / "robustness"
    if not base.is_dir():
        return []
    return sorted(
        child for child in base.iterdir() if (child / "robustness.json").is_file()
    )


def build_evidence_audit_config(
    config: Mapping[str, Any] | None,
    *,
    data_readiness: Mapping[str, Any] | None = None,
    statistical_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return ``config`` with the produced evidence added under audit keys only.

    The gate-consumed keys (``data_readiness`` / ``statistical_gate``) are never
    written here -- that is the whole point of the audit-key indirection.
    """

    merged = dict(config or {})
    if data_readiness is not None:
        merged[DATA_READINESS_AUDIT_KEY] = dict(data_readiness)
    if statistical_evidence is not None:
        merged[STATISTICAL_GATE_AUDIT_KEY] = dict(statistical_evidence)
    return merged


def _run_config(run: object) -> dict[str, Any]:
    if isinstance(run, Mapping):
        return dict(run)
    raw = getattr(run, "config_json", None)
    if isinstance(raw, Mapping):
        return dict(raw)
    if not raw:
        return {}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def read_run_evidence_audit(run: object) -> dict[str, Any]:
    """Read side: fetch the produced evidence audit from a run (or its config).

    Accepts a :class:`~app.models.tables.ModelRun` row, a config mapping, or a
    JSON config string.  Returns the two envelopes (or ``None`` when the run
    predates the wiring) plus the raw schema version so an auditor can tell a
    missing producer from an empty one.
    """

    config = _run_config(run)
    readiness = config.get(DATA_READINESS_AUDIT_KEY)
    statistical = config.get(STATISTICAL_GATE_AUDIT_KEY)
    return {
        "schema_version": EVIDENCE_AUDIT_SCHEMA_VERSION,
        "data_readiness": dict(readiness) if isinstance(readiness, Mapping) else None,
        "statistical_evidence": (
            dict(statistical) if isinstance(statistical, Mapping) else None
        ),
    }


__all__ = [
    "DATA_READINESS_AUDIT_KEY",
    "EVIDENCE_AUDIT_SCHEMA_VERSION",
    "STATISTICAL_GATE_AUDIT_KEY",
    "build_evidence_audit_config",
    "discover_robustness_evidence_dirs",
    "produce_data_readiness_evidence",
    "produce_statistical_gate_evidence",
    "read_run_evidence_audit",
]
