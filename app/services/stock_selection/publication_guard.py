"""Last, model-independent gate before any report or notification is rendered."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import logging
import re
from typing import Mapping

from app.services.stock_selection.data_contracts import candidate_eligibility, validate_market_ticker
from app.services.stock_selection.regime_policy import evaluate_regime_policy


logger = logging.getLogger(__name__)

_LIST_KEYS = {"CN": "market_recommendations", "US": "us_model_recommendations", "HK": "hk_model_recommendations"}
_META_KEYS = {"CN": "market_recommendations_meta", "US": "us_model_recommendations_meta", "HK": "hk_model_recommendations_meta"}
_WATCH_KEYS = {"CN": "market_watch_recommendations", "US": None, "HK": None}
APPROVAL_REGISTRY_KEY = "stock_selection_model_qualification_registry_v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Gate decisions that must never reach publication. ``REJECT`` is an explicit
# failure; ``OBSERVE`` (missing evidence) is only refused when the operator has
# opted into complete-evidence enforcement.
_REFUSED_DECISIONS = {"REJECT"}
# Gate decisions that are known to be non-promotable even when not refused.
# With ``PQW_PROMOTION_GATE_ENFORCE=false`` they are published but must be
# labelled "非晋级/研究口径" and logged as a WARNING.
_NON_PROMOTABLE_DECISIONS = {"REJECT", "OBSERVE"}
# Report key carrying the publication-level promotion audit / label fields.
_PROMOTION_STATUS_KEY = "promotion_status"
# Report keys read only when the caller does not pass explicit evidence.
_EVIDENCE_DECISION_KEY = "promotion_decision"
_EVIDENCE_RUN_KEY = "model_run_id"
# Provenance marker written when a report's source run cannot be confirmed. The
# report is marked non-promotable / research-only, and no run-specific evidence
# (schema / evidence version / digest) is attached: omission beats misattribution.
PROVENANCE_UNRESOLVED = "unresolved"
# Run-specific evidence fields a report may already embed, at top level or under
# ``promotion_status``. When a full gate report is supplied, any embedded value
# must match it, otherwise the supplied evidence belongs to a different run.
_EMBEDDED_EVIDENCE_KEYS = (
    "promotion_schema_version",
    "promotion_evidence_version",
    "promotion_decision",
    "content_digest",
)


def load_trusted_model_qualifications(*, db=None) -> dict[str, dict]:
    """Read independently approved model identities, never from AI report text."""
    if db is None:
        from app.core.db import SessionLocal

        with SessionLocal() as own_db:
            return load_trusted_model_qualifications(db=own_db)
    from app.services.repository import AppSettingRepository

    raw = AppSettingRepository(db).get(APPROVAL_REGISTRY_KEY)
    if not isinstance(raw, str):
        return {}
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    approved = {}
    for market in _LIST_KEYS:
        record = decoded.get(market)
        if not isinstance(record, dict):
            continue
        timestamp = str(record.get("approved_at") or "")
        try:
            approved_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if (
            approved_at.tzinfo is None or approved_at.utcoffset() is None
            or str(record.get("status") or "").upper() not in {"QUALIFIED", "PROMOTED"}
            or record.get("protocol_approved") is not True
            or not str(record.get("approved_by") or "").strip()
            or not str(record.get("protocol_id") or "").strip()
            or not _SHA256_RE.fullmatch(str(record.get("model_artifact_sha256") or ""))
        ):
            continue
        approved[market] = deepcopy(record)
    return approved


def load_trusted_regime_snapshots(*, db=None) -> dict[str, dict]:
    """Live publication only. Historical research must supply its as-of snapshot.

    A newer or stale stored snapshot is not substituted for the report's date:
    the pure guard below rejects the mismatch. Never use candidate/AI fields.
    """
    if db is None:
        from app.core.db import SessionLocal
        with SessionLocal() as own_db:
            return load_trusted_regime_snapshots(db=own_db)
    from app.services.repository import WorkspaceSnapshotRepository
    try:
        stored = WorkspaceSnapshotRepository(db).get_latest_snapshot("market_regime_snapshot:CN") or {}
        payload = stored.get("payload")
        return {"CN": deepcopy(payload)} if isinstance(payload, dict) else {}
    except (ValueError, OSError, RuntimeError):
        # Missing/corrupt cold snapshot is a blocked publication, not an ALLOW.
        return {}


def _resolve_promotion_evidence(
    report: dict, promotion_decision: str | None, model_run_id: object
) -> tuple[str, object]:
    """Minimal promotion-evidence contract for a report.

    Explicit arguments win; otherwise the report's ``promotion_decision`` /
    ``model_run_id`` keys are used. Only ``promotion_decision`` is authoritative:
    a run id without a decision is treated as missing evidence, never guessed.
    """

    decision = promotion_decision
    if decision is None:
        decision = report.get(_EVIDENCE_DECISION_KEY)
    run_id = model_run_id
    if run_id is None:
        run_id = report.get(_EVIDENCE_RUN_KEY)
    normalized = str(decision).strip().upper() if decision is not None else ""
    return normalized, run_id


def _enforce_promotion_evidence(
    decision: str, run_id: object, *, require_complete_evidence: bool
) -> None:
    """Refuse publication when the supplied gate decision forbids it.

    ``REJECT`` is always refused; ``OBSERVE`` only when complete evidence is
    required. The caller only invokes this when
    ``PQW_PROMOTION_GATE_ENFORCE`` is on.
    """

    run_label = run_id if run_id not in (None, "") else "<unknown>"
    if decision in _REFUSED_DECISIONS:
        raise ValueError(
            "publication refused: promotion gate rejected model run "
            f"{run_label} ({decision})"
        )
    if decision == "OBSERVE" and require_complete_evidence:
        raise ValueError(
            "publication refused: promotion gate returned OBSERVE (incomplete "
            f"evidence) for model run {run_label} "
            "while PQW_PROMOTION_GATE_REQUIRE_COMPLETE_EVIDENCE=true"
        )


def _publication_promotion_fields(
    decision: str, *, enforce: bool, require_complete_evidence: bool, promotable: bool = False,
    promotion_schema_version: str | None = None,
    promotion_evidence_version: str | None = None,
    non_promotable_reasons: list[str] | None = None,
    content_digest: str | None = None,
) -> dict:
    """Publication-level audit / marking fields for a published report.

    Mirrors :meth:`ServingPromotionDecision.status_fields` so a report records
    *which* gate decision allowed it. ``promotion_blocked_from_serving`` is
    ``False`` by construction: this branch only runs when the report is being
    published, i.e. the gate did not withhold it.

    The schema / evidence / reasons arguments carry the *real* gate-report
    fields when a full report was supplied; the minimal string-evidence path
    leaves them unset and uses the synthesised ``promotion_gate_decision:<X>``
    reason instead.
    """

    from app.services.stock_selection.promotion_enforcement import (
        PROMOTION_LABEL_EN,
        PROMOTION_LABEL_ZH,
    )

    marked_non_promotable = not promotable
    fields = {
        "promotion_decision": decision,
        "promotable": bool(promotable),
        "non_promotable_reasons": (
            list(non_promotable_reasons)
            if non_promotable_reasons is not None
            else [f"promotion_gate_decision:{decision}"]
        ),
        "promotion_enforced": bool(enforce),
        "promotion_require_complete_evidence": bool(require_complete_evidence),
        "promotion_blocked_from_serving": False,
        "promotion_label": PROMOTION_LABEL_ZH if marked_non_promotable else None,
        "promotion_label_en": PROMOTION_LABEL_EN if marked_non_promotable else None,
        "research_only": bool(marked_non_promotable),
    }
    if promotion_schema_version:
        fields["promotion_schema_version"] = promotion_schema_version
    if promotion_evidence_version:
        fields["promotion_evidence_version"] = promotion_evidence_version
    if content_digest:
        fields["content_digest"] = content_digest
    return fields


def _normalize_promotion_report(promotion_report: object) -> dict:
    """Read the real gate-report fields from a ``PromotionGateV2Report``.

    Accepts the dataclass itself or its ``as_dict()`` / persisted mapping. Only
    the fields the publication guard needs are read; nothing is inferred from
    report text. ``evidence_version`` is recomputed with the gate's own formula
    when a plain mapping is passed, because it is a derived property and is
    therefore absent from ``as_dict()``.
    """

    if isinstance(promotion_report, Mapping):
        def _get(key: str):
            return promotion_report.get(key)
    else:
        def _get(key: str):
            return getattr(promotion_report, key, None)

    schema_version = str(_get("schema_version") or "").strip()
    decision = str(_get("decision") or "").strip().upper()
    raw_run_id = _get("run_id")
    run_id = raw_run_id if raw_run_id not in (None, "") else None
    market = str(_get("market") or "").strip().upper()
    raw_promotable = _get("promotable")
    promotable = (
        bool(raw_promotable)
        if raw_promotable is not None
        else decision not in _NON_PROMOTABLE_DECISIONS
    )
    raw_reasons = _get("non_promotable_reasons") or ()
    if isinstance(raw_reasons, str):
        raw_reasons = [raw_reasons]
    reasons = [str(item) for item in raw_reasons]
    content_digest = str(_get("content_digest") or "").strip()
    evidence_version = str(_get("evidence_version") or "").strip()
    if not evidence_version and schema_version and content_digest:
        evidence_version = (
            f"{schema_version}:{market or 'NA'}:{run_id or 'unknown'}:"
            f"{content_digest[:20]}"
        )
    return {
        "decision": decision,
        "run_id": run_id,
        "promotable": promotable,
        "promotion_schema_version": schema_version or None,
        "promotion_evidence_version": evidence_version or None,
        "non_promotable_reasons": reasons,
        "content_digest": content_digest or None,
    }


def _report_embedded_evidence(report: Mapping) -> dict:
    """Run-specific evidence fields a report already carries, if any.

    Only values that are actually present are returned; a report without
    embedded evidence is not treated as inconsistent, just as unverified.
    """

    embedded: dict = {}
    sources = [report]
    status = report.get(_PROMOTION_STATUS_KEY)
    if isinstance(status, Mapping):
        sources.append(status)
    for source in sources:
        for key in _EMBEDDED_EVIDENCE_KEYS:
            value = source.get(key)
            if value not in (None, "") and key not in embedded:
                embedded[key] = value
    return embedded


def _confirm_promotion_provenance(
    declared_run_id: object, report: Mapping, report_fields: dict
) -> tuple[bool, str | None]:
    """Decide whether a supplied gate report truly belongs to this report.

    Confirmation requires both:

    * the report declares the same source run id as the gate report
      (``model_run_id`` argument, else the report's own ``model_run_id``), and
    * any evidence the report already embeds (``promotion_schema_version`` /
      ``promotion_evidence_version`` / ``promotion_decision`` / ``content_digest``,
      at top level or under ``promotion_status``) matches that gate report.

    A run id declared without an existing run, or evidence that conflicts with
    the run, is not resolvable: the caller must mark provenance unresolved rather
    than attach another run's evidence.
    """

    actual_run_id = report_fields.get("run_id")
    if declared_run_id in (None, "") or actual_run_id in (None, ""):
        return False, "source run id is not declared on the report"
    if str(declared_run_id) != str(actual_run_id):
        return False, (
            f"declared run {declared_run_id} does not match gate report run "
            f"{actual_run_id}"
        )
    expected = {
        "promotion_schema_version": report_fields.get("promotion_schema_version"),
        "promotion_evidence_version": report_fields.get("promotion_evidence_version"),
        "promotion_decision": report_fields.get("decision"),
        "content_digest": report_fields.get("content_digest"),
    }
    mismatched_keys = [
        key
        for key, embedded in _report_embedded_evidence(report).items()
        if embedded != expected.get(key)
    ]
    if mismatched_keys:
        return False, (
            "embedded evidence mismatches the run: " + ", ".join(mismatched_keys)
        )
    return True, None


def _unresolved_promotion_fields(
    *, enforce: bool, require_complete_evidence: bool, reason: str
) -> dict:
    """Publication marking when a report's promotion-evidence provenance fails.

    Deliberately carries *no* run-specific evidence (no schema / evidence
    version, digest or decision): those belong to whichever run produced them and
    must never be attributed to a report whose source run is unconfirmed.
    """

    from app.services.stock_selection.promotion_enforcement import (
        PROMOTION_LABEL_EN,
        PROMOTION_LABEL_ZH,
    )

    return {
        "promotion_evidence_provenance": PROVENANCE_UNRESOLVED,
        "promotion_unresolved_reason": reason,
        "promotable": False,
        "non_promotable_reasons": ["promotion_evidence_provenance_unresolved"],
        "promotion_enforced": bool(enforce),
        "promotion_require_complete_evidence": bool(require_complete_evidence),
        "promotion_blocked_from_serving": False,
        "promotion_label": PROMOTION_LABEL_ZH,
        "promotion_label_en": PROMOTION_LABEL_EN,
        "research_only": True,
    }


def prepare_report_for_publication(report: dict, *, approved_qualifications: dict | None = None,
                                   trusted_regime_snapshots: dict | None = None,
                                   promotion_decision: str | None = None,
                                   model_run_id: object = None,
                                   promotion_report: object | None = None,
                                   promotion_evidence_provenance: str | None = None) -> dict:
    """Preserve raw research elsewhere, but never publish unqualified stock picks.

    Promotion-evidence contract
    ---------------------------
    The preferred form is ``promotion_report``: the run's unified gate report
    (:class:`~app.services.stock_selection.promotion_gate_v2.PromotionGateV2Report`)
    passed either as the dataclass or its ``as_dict()`` / persisted mapping.
    When supplied it is authoritative and its *real* fields populate
    ``promotion_status`` -- ``promotion_schema_version`` /
    ``promotion_evidence_version`` / ``non_promotable_reasons`` /
    ``content_digest`` -- matching
    :meth:`ServingPromotionDecision.status_fields`.

    The transitional minimal contract remains for callers that only have the
    string verdict: ``promotion_decision`` (``REJECT`` / ``OBSERVE`` /
    ``ELIGIBLE_FOR_MANUAL_REVIEW``) and ``model_run_id``, or the report's own
    ``promotion_decision`` / ``model_run_id`` keys. When the verdict is
    ``REJECT`` -- or ``OBSERVE`` with
    ``PQW_PROMOTION_GATE_REQUIRE_COMPLETE_EVIDENCE=true`` -- publication is
    refused with :class:`ValueError`, *provided*
    ``PQW_PROMOTION_GATE_ENFORCE`` is on (the fail-closed default). With
    enforcement disabled the report is still published, but it is labelled
    "非晋级/研究口径" via ``promotion_status`` and a WARNING is logged; the same
    marking applies to every non-promotable decision under that switch.

    Provenance contract
    -------------------
    A supplied ``promotion_report`` is only honoured when it is *confirmed* to
    belong to this report: the report must declare the same source run
    (``model_run_id`` argument or the report's own ``model_run_id``) and any
    evidence it already embeds must match the gate report. Otherwise the evidence
    is unattributable and the report is marked with
    ``promotion_status.promotion_evidence_provenance == "unresolved"`` plus
    ``promotion_label == "非晋级/研究口径"`` and a WARNING -- never another run's
    fields. Callers that know evidence was expected but could not be confirmed
    (e.g. the daily-report wiring without a declared run) signal the same marking
    explicitly with ``promotion_evidence_provenance="unresolved"``.

    Supplying the evidence is currently optional: when it is missing the report
    is processed exactly as before, but a WARNING is logged so the gap is
    visible. This is a deliberate transition step; once every producer of
    reports carries the gate verdict, the evidence should become a required
    argument (or a hard precondition) and the WARNING fallback removed.
    """
    from app.services.stock_selection.promotion_enforcement import (
        promotion_enforce_enabled,
        promotion_require_complete_evidence_enabled,
    )

    enforce = promotion_enforce_enabled()
    require_complete_evidence = promotion_require_complete_evidence_enabled()
    promotion_status: dict | None = None
    report_fields = (
        _normalize_promotion_report(promotion_report)
        if promotion_report is not None
        else None
    )
    declared_run_id = (
        model_run_id if model_run_id not in (None, "") else report.get(_EVIDENCE_RUN_KEY)
    )
    provenance_unresolved = (
        str(promotion_evidence_provenance or "").strip().lower()
        == PROVENANCE_UNRESOLVED
    )
    unresolved_reason: str | None = None
    if report_fields is not None and report_fields["decision"]:
        confirmed, mismatch_reason = _confirm_promotion_provenance(
            declared_run_id, report, report_fields
        )
        if not confirmed:
            provenance_unresolved = True
            unresolved_reason = mismatch_reason
            logger.warning(
                "promotion evidence for report is unresolved (%s); not attaching "
                "run-specific fields from model run %s",
                mismatch_reason,
                report_fields["run_id"] if report_fields["run_id"] not in (None, "") else "<unknown>",
            )
        else:
            # Full gate report confirmed against the report's declared run: its
            # fields are authoritative, aligned with serving.
            decision = report_fields["decision"]
            run_id = report_fields["run_id"]
            if enforce:
                # Refusal is unchanged when enforcement is on: REJECT always, and
                # OBSERVE only under complete-evidence enforcement.
                _enforce_promotion_evidence(
                    decision,
                    run_id,
                    require_complete_evidence=require_complete_evidence,
                )
            if not report_fields["promotable"]:
                promotion_status = _publication_promotion_fields(
                    decision,
                    enforce=enforce,
                    require_complete_evidence=require_complete_evidence,
                    promotable=False,
                    promotion_schema_version=report_fields["promotion_schema_version"],
                    promotion_evidence_version=report_fields["promotion_evidence_version"],
                    non_promotable_reasons=report_fields["non_promotable_reasons"],
                    content_digest=report_fields["content_digest"],
                )
                logger.warning(
                    "model run %s is not promotable (%s); labelled %s but still "
                    "published (%s)",
                    run_id if run_id not in (None, "") else "<unknown>",
                    decision,
                    promotion_status["promotion_label"],
                    "PQW_PROMOTION_GATE_ENFORCE=false"
                    if not enforce
                    else "observe-only policy: marked, not withheld",
                )
    if promotion_status is None and provenance_unresolved:
        promotion_status = _unresolved_promotion_fields(
            enforce=enforce,
            require_complete_evidence=require_complete_evidence,
            reason=unresolved_reason or "source run not declared or not resolvable",
        )
        logger.warning(
            "report promotion evidence provenance is unresolved "
            "(model_run_id=%s); labelled %s, no run-specific evidence attached",
            declared_run_id if declared_run_id not in (None, "") else "<missing>",
            promotion_status["promotion_label"],
        )
    elif promotion_status is None and not (report_fields is not None and report_fields["decision"]):
        decision, run_id = _resolve_promotion_evidence(report, promotion_decision, model_run_id)
        if decision:
            if enforce:
                # Refusal is unchanged when enforcement is on: REJECT always, and
                # OBSERVE only under complete-evidence enforcement.
                _enforce_promotion_evidence(
                    decision,
                    run_id,
                    require_complete_evidence=require_complete_evidence,
                )
            elif decision in _NON_PROMOTABLE_DECISIONS:
                # Enforcement disabled: never refuse, but label and warn.
                promotion_status = _publication_promotion_fields(
                    decision,
                    enforce=enforce,
                    require_complete_evidence=require_complete_evidence,
                )
                logger.warning(
                    "model run %s is not promotable (%s); labelled %s but still "
                    "published (PQW_PROMOTION_GATE_ENFORCE=false)",
                    run_id if run_id not in (None, "") else "<unknown>",
                    decision,
                    promotion_status["promotion_label"],
                )
        else:
            logger.warning(
                "publication proceeding without promotion evidence "
                "(model_run_id=%s); this fallback is transitional and should "
                "become mandatory",
                run_id if run_id not in (None, "") else "<missing>",
            )

    output = deepcopy(report)
    if promotion_status is not None:
        output[_PROMOTION_STATUS_KEY] = promotion_status
    claimed = output.get("model_qualification") or {}
    trusted = approved_qualifications or {}
    qualification = {}
    if isinstance(claimed, dict) and isinstance(trusted, dict):
        for market in _LIST_KEYS:
            claim = claimed.get(market)
            approval = trusted.get(market)
            if not isinstance(claim, dict) or not isinstance(approval, dict):
                continue
            if (str(claim.get("protocol_id") or "") == str(approval.get("protocol_id") or "")
                and str(claim.get("model_artifact_sha256") or "") == str(approval.get("model_artifact_sha256") or "")):
                qualification[market] = deepcopy(approval)
    output["model_qualification"] = qualification
    blocked: dict[str, list[dict]] = deepcopy(output.get("publication_blocked_candidates") or {})
    for market, list_key in _LIST_KEYS.items():
        meta_key = _META_KEYS[market]
        if list_key not in output and meta_key not in output:
            continue
        if list_key not in output and str((output.get(meta_key) or {}).get("status") or "").lower() == "not_requested":
            continue
        rows = output.setdefault(list_key, [])
        if not isinstance(rows, list):
            raise ValueError(f"{market} recommendation list must be explicit")
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"{market} recommendation must be an object")
            ticker = validate_market_ticker(market, row.get("ticker"))
            row["ticker"] = ticker
            if ticker in seen:
                raise ValueError(f"duplicate {market} recommendation ticker: {ticker}")
            seen.add(ticker)
        gate = qualification.get(market) if isinstance(qualification, dict) else None
        gate = gate if isinstance(gate, dict) else {}
        configured_top_n = gate.get("top_n", 5)
        if type(configured_top_n) is not int or configured_top_n <= 0:
            raise ValueError("publication top_n must be positive")
        top_n = min(5, configured_top_n)
        frozen_rows = rows[:top_n]
        qualified = (
            str(gate.get("status") or "").upper() in {"QUALIFIED", "PROMOTED"}
            and gate.get("protocol_approved") is True
            and bool(str(gate.get("protocol_id") or "").strip())
        )
        meta = deepcopy(output.get(meta_key) or {})
        watch_key = _WATCH_KEYS[market]
        watch_rows = output.get(watch_key) if watch_key else []
        has_research_observations = isinstance(watch_rows, list) and bool(watch_rows)
        if market == "CN":
            input_date = str((output.get("input_market_dates") or {}).get("CN") or
                             meta.get("target_snapshot_date") or "")
            source = (trusted_regime_snapshots or {}).get("CN")
            if meta.get("target_snapshot_date") and meta["target_snapshot_date"] != input_date:
                source = None  # Conflicting lineage cannot be resolved by guessing.
            policy = evaluate_regime_policy(source, market="CN", expected_market_date=input_date,
                decision_cutoff_at=output.get("decision_cutoff_at") or output.get("saved_at") or "",
                max_new_candidates=top_n)
            meta["regime_policy"] = policy
            meta["regime_position_hint"] = policy["regime_position_hint"]
            output[meta_key] = meta
            if qualified and policy["buy_gate"] == "BLOCK":
                if rows:
                    blocked[market] = [{**row, "publication_block_reason": "market_regime_blocked"} for row in rows]
                output[list_key] = []
                meta["status"] = "observation_ready" if has_research_observations else "not_ready"
                meta["publication_blocker"] = "market_regime_blocked"
                meta["note"] = "市场体制或其数据证据未通过校验，暂停新增买入名单；" + policy["regime_position_hint"]
                meta["blocked_candidate_count"] = len(blocked.get(market, []))
                continue
        if not rows:
            if not qualified and str(meta.get("status") or "").lower() != "not_requested":
                meta["status"] = "observation_ready" if has_research_observations else "not_ready"
                meta["note"] = str(meta.get("note") or (
                    "全市场扫描和研究候选已就绪；模型尚未按冻结协议通过资格验收，今日不发布可执行股票名单。"
                    if has_research_observations
                    else "模型尚未按冻结协议通过资格验收；今日不发布可执行股票名单。"
                ))
                meta["qualification_blocker"] = "model_not_qualified"
                output[meta_key] = meta
            continue
        if not qualified:
            blocked[market] = rows
            output[list_key] = []
            meta["status"] = "observation_ready" if has_research_observations else "not_ready"
            meta["note"] = "模型尚未按冻结协议通过资格验收；原始名单仅供研究，不构成可执行买入建议。"
            meta["qualification_blocker"] = "model_not_qualified"
            output[meta_key] = meta
            continue
        ready: list[dict] = []
        for row in frozen_rows:
            status = row.get("symbol_status") or row.get("tradability_status") or "unknown"
            result = candidate_eligibility(
                market=market, ticker=row["ticker"],
                market_health=str(meta.get("market_health") or meta.get("status") or "unknown"),
                symbol_status=str(status), source_status=str(row.get("source_status") or "ready"),
            )
            if result["eligible"]:
                ready.append(row)
            else:
                blocked.setdefault(market, []).append({**row, "publication_block_reason": result["reason"]})
        if len(rows) > top_n:
            blocked.setdefault(market, []).extend(
                {**row, "publication_block_reason": "outside_frozen_top_n"}
                for row in rows[top_n:]
            )
        output[list_key] = ready
        if blocked.get(market):
            meta["blocked_candidate_count"] = len(blocked[market])
            meta["blocked_candidate_reason"] = "security_level_gate"
            output[meta_key] = meta
    if blocked:
        output["publication_blocked_candidates"] = blocked
    return output


__all__ = ["prepare_report_for_publication", "load_trusted_model_qualifications", "load_trusted_regime_snapshots", "APPROVAL_REGISTRY_KEY"]
