"""Append-only publication decisions; not delivery receipts or trade fills."""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
import hashlib
import json

from sqlalchemy import select

from app.models.tables import (
    CNSelectionDecision, USSelectionDecision, HKSelectionDecision,
    CNSelectionPublication, USSelectionPublication, HKSelectionPublication,
)
from app.services.json_payload_artifacts import payload_digest
from app.services.market_calendar import next_market_open_date
from app.services.stock_selection.data_contracts import validate_market_ticker

from app.services.json_payload_artifacts import JsonPayloadArtifactStore
from app.services.repository import WorkspaceSnapshotRepository


FINAL_DECISION_SNAPSHOT_TYPE = "stock_selection_final_decisions:daily_report:v1"
DECISION_MODELS = {"CN": CNSelectionDecision, "US": USSelectionDecision, "HK": HKSelectionDecision}
PUBLICATION_MODELS = {"CN": CNSelectionPublication, "US": USSelectionPublication, "HK": HKSelectionPublication}


def build_final_decision_payload(report: dict) -> dict:
    if not report.get("saved_at") or not report.get("report_date"):
        raise ValueError("Decision freeze requires report_date and saved_at")
    if not isinstance(report.get("market_recommendations"), list) and report.get("rows"):
        raise ValueError("Legacy report must be rebuilt with an explicit CN candidate list before publication")
    # Explicit candidate lists only: never substitute holdings or a later pool
    # for an empty actionable list. Keep original ordering and complete rows.
    markets = {}
    for market, actionable_key, watch_key, meta_key in (
        ("CN", "market_recommendations", "market_watch_recommendations", "market_recommendations_meta"),
        ("US", "us_model_recommendations", None, "us_model_recommendations_meta"),
        ("HK", "hk_model_recommendations", None, "hk_model_recommendations_meta"),
    ):
        if not any(key and key in report for key in (actionable_key, watch_key, meta_key)):
            continue
        candidates = report.get(actionable_key, [])
        watch = report.get(watch_key, []) if watch_key else []
        if not isinstance(candidates, list) or not isinstance(watch, list):
            raise ValueError(f"Invalid {market} candidate lists")
        meta = report.get(meta_key) or {}
        if str(meta.get("status") or "").lower() == "not_requested":
            if candidates or watch:
                raise ValueError(f"{market} not_requested market cannot contain candidates")
            continue
        seen: set[str] = set()
        normalized_candidates: list[dict] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise ValueError(f"Invalid {market} candidate")
            ticker = validate_market_ticker(market, candidate.get("ticker"))
            if ticker in seen:
                raise ValueError(f"Duplicate {market} candidate: {ticker}")
            seen.add(ticker)
            normalized_candidates.append({**deepcopy(candidate), "ticker": ticker})
        normalized_watch: list[dict] = []
        for item in watch:
            if not isinstance(item, dict):
                raise ValueError(f"Invalid {market} watch candidate")
            normalized_watch.append({**deepcopy(item), "ticker": validate_market_ticker(market, item.get("ticker"))})
        blocked = str(meta.get("status") or "").lower() in {"fallback", "not_ready", "observation_ready"}
        markets[market] = {
            "protocol_id": str(((report.get("model_qualification") or {}).get(market) or {}).get("protocol_id") or f"legacy_unapproved:{market}:daily_report_v1"),
            "input_market_date": (report.get("input_market_dates") or {}).get(market),
            "decision": "NOT_READY" if blocked else "ABSTAIN" if not candidates else "CANDIDATES",
            "candidate_semantics": "actionable_pool" if market == "CN" else "model_recommendations_not_execution_orders",
            "candidates": normalized_candidates,
            "push_top5_candidates": deepcopy(normalized_candidates[:5]) if market in {"CN", "US"} else [],
            "push_semantics": "renderable_top5_not_delivery_confirmation" if market in {"CN", "US"} else "not_supported",
            "watch_candidates": normalized_watch,
            "watch_published_in_push": False,
            "metadata": deepcopy(meta),
        }
    return {
        "schema_version": "final_stock_selection_decision_v1",
        "state": "prepared_for_publication",
        "frozen_at": report["saved_at"],
        "report_date": report["report_date"],
        "scope": report.get("scope"),
        "markets": markets,
        "strategy": deepcopy(report.get("strategy")),
        "recommendation_regression": deepcopy(report.get("recommendation_regression")),
        "semantics": "publication_snapshot_not_delivery_confirmation_or_filled_portfolio",
    }


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("decision timestamp must be timezone-aware")
    return parsed


def _market_decision_identity(payload: dict, market: str, details: dict, report: dict) -> tuple[str, str, str, str]:
    qualification = (report.get("model_qualification") or {}).get(market) or {}
    protocol_id = str(qualification.get("protocol_id") or f"legacy_unapproved:{market}:daily_report_v1")
    input_date = str(details.get("input_market_date") or payload["report_date"])[:10]
    date.fromisoformat(input_date)
    effective = next_market_open_date(market, input_date, include_self=False) if market in {"CN", "US"} else input_date
    economic = {
        "schema_version": "stock_selection_economic_decision_v1", "market": market,
        "protocol_id": protocol_id, "effective_trade_date": effective,
        "input_market_date": input_date, "decision": details["decision"],
        "candidates": details["candidates"], "watch_candidates": details["watch_candidates"],
    }
    regime_policy = (details.get("metadata") or {}).get("regime_policy")
    if isinstance(regime_policy, dict):
        economic["regime_policy"] = regime_policy
    digest = payload_digest(economic)
    return f"decision:{market}:{digest[:32]}", digest, protocol_id, effective


def freeze_final_decisions(
    report: dict, *, db, store=None, commit: bool = True,
    publication_messages: list[dict] | None = None,
    publication_channels: list[str] | None = None,
) -> dict:
    payload = build_final_decision_payload(report)
    artifact_store = store or JsonPayloadArtifactStore()
    reference = artifact_store.write(payload, namespace="stock_selection_final_decisions")
    # Verify cold data before committing its small, retained PostgreSQL pointer.
    artifact_store.read(reference)
    receipt = {
        "schema_version": payload["schema_version"],
        "state": payload["state"],
        "frozen_at": payload["frozen_at"],
        "artifact": reference,
        "market_summary": {
            market: {"decision": value["decision"], "candidate_count": len(value["candidates"]),
                     "watch_count": len(value["watch_candidates"])}
            for market, value in payload["markets"].items()
        },
    }
    decision_ids: dict[str, str] = {}
    publication_receipts: list[dict] = []
    if hasattr(db, "scalar") and hasattr(db, "add"):
        frozen_at = _aware_datetime(payload["frozen_at"])
        report_date = date.fromisoformat(str(payload["report_date"])[:10])
        for market, details in payload["markets"].items():
            model = DECISION_MODELS[market]
            decision_id, digest, protocol_id, effective = _market_decision_identity(payload, market, details, report)
            decision_ids[market] = decision_id
            row = db.scalar(select(model).where(model.decision_id == decision_id))
            if row is None:
                previous = db.scalar(
                    select(model).where(model.protocol_id == protocol_id,
                                        model.effective_trade_date == date.fromisoformat(effective))
                    .order_by(model.id.desc()).limit(1)
                )
                row = model(
                    decision_id=decision_id, market=market, protocol_id=protocol_id,
                    report_date=report_date, effective_trade_date=date.fromisoformat(effective),
                    cutoff_at=_aware_datetime(str(report["decision_cutoff_at"])) if report.get("decision_cutoff_at") else None,
                    frozen_at=frozen_at, decision_type=details["decision"],
                    state="BLOCKED" if market == "HK" or details["decision"] == "NOT_READY" else "VALIDATED",
                    candidate_count=len(details["candidates"]), payload_sha256=digest,
                    artifact_reference_json=json.dumps(reference, sort_keys=True),
                    supersedes_decision_id=previous.decision_id if previous else None,
                    created_at=frozen_at,
                )
                db.add(row)
                db.flush()
        for ordinal, message in enumerate(publication_messages or (), start=1):
            market = str(message.get("market") or "").upper()
            if market not in decision_ids:
                continue
            publication_payload = {
                "decision_id": decision_ids[market], "market": market,
                "title": str(message.get("title") or ""), "body": str(message.get("body") or ""),
            }
            message_reference = artifact_store.write(publication_payload, namespace="stock_selection_publications")
            if artifact_store.read(message_reference) != publication_payload:
                raise RuntimeError("publication artifact verification failed")
            message_hash = payload_digest(publication_payload)
            for channel in publication_channels or ():
                normalized_channel = str(channel).lower().strip()
                if not normalized_channel:
                    continue
                delivery_key = hashlib.sha256(
                    f"{decision_ids[market]}:{normalized_channel}:{message_hash}".encode("utf-8")
                ).hexdigest()
                pub_model = PUBLICATION_MODELS[market]
                publication = db.scalar(select(pub_model).where(pub_model.delivery_key == delivery_key))
                if publication is None:
                    publication = pub_model(
                        publication_id=f"publication:{market}:{delivery_key[:32]}",
                        decision_id=decision_ids[market], market=market,
                        channel=normalized_channel, delivery_key=delivery_key,
                        ordinal=ordinal, payload_sha256=message_hash,
                        artifact_reference_json=json.dumps(message_reference, sort_keys=True),
                        status="PENDING", attempt_count=0,
                        created_at=frozen_at, updated_at=frozen_at,
                    )
                    db.add(publication)
                    db.flush()
                publication_receipts.append({"market": market, "channel": normalized_channel,
                                             "delivery_key": delivery_key, "status": publication.status,
                                             "ordinal": ordinal})
    receipt["decision_ids"] = decision_ids
    receipt["publications"] = publication_receipts
    snapshot = WorkspaceSnapshotRepository(db).create_snapshot(
        snapshot_type=FINAL_DECISION_SNAPSHOT_TYPE,
        snapshot_date=str(report["report_date"])[:10],
        payload=receipt,
        commit=commit,
    )
    return {"snapshot_id": snapshot.id, **receipt, "decision_ids": decision_ids,
            "publications": publication_receipts}


def record_publication_result(*, db, market: str, delivery_key: str, result: dict, commit: bool = True) -> dict:
    model = PUBLICATION_MODELS.get(str(market).upper())
    if model is None:
        raise ValueError("unsupported publication market")
    row = db.scalar(select(model).where(model.delivery_key == delivery_key))
    if row is None:
        raise LookupError("publication delivery_key not found")
    if row.status == "SENT":
        return {"delivery_key": delivery_key, "status": row.status, "attempt_count": row.attempt_count}
    sent = str(row.channel) in set(result.get("sent") or [])
    errors = [item for item in (result.get("failed") or []) if item.get("channel") == row.channel]
    error = str(errors[0].get("message") or "") if errors else ""
    # An exception/timeout does not prove the provider rejected the message.
    # Fail closed until a provider receipt or operator reconciliation exists.
    was_claimed = row.status == "SENDING"
    row.status = "SENT" if sent else "UNKNOWN"
    if not was_claimed:
        row.attempt_count += 1
    row.last_error = error or None
    row.provider_message_id = str(result.get("provider_message_id") or "") or None
    row.updated_at = datetime.now().astimezone()
    if commit:
        db.commit()
    else:
        db.flush()
    return {"delivery_key": delivery_key, "status": row.status, "attempt_count": row.attempt_count}


def dispatch_publication_message(
    *, db, notifier, receipt: dict, ordinal: int, message: dict,
    event_type: str, channels: list[str], store=None,
) -> dict:
    """Send each stock-selection channel once; UNKNOWN needs reconciliation."""
    intents = [item for item in receipt.get("publications", ()) if item.get("ordinal") == ordinal]
    if not intents:
        if channels:
            raise RuntimeError("stock-selection message has no durable publication intent")
        return {"status": "skipped", "sent": [], "failed": [], "skipped": [], "delivery_records": []}
    sent: list[str] = []
    failed: list[dict] = []
    skipped: list[dict] = []
    delivery_records: list[dict] = []
    artifact_store = store or JsonPayloadArtifactStore()
    for intent in intents:
        market = str(intent.get("market") or "").upper()
        model = PUBLICATION_MODELS.get(market)
        if model is None:
            raise ValueError("unsupported publication market")
        # The returned receipt can be reused after a prior attempt. Its status
        # is only a snapshot; the durable row is authoritative for retry.
        publication = db.scalar(
            select(model).where(model.delivery_key == intent["delivery_key"]).with_for_update()
        )
        if publication is None or publication.decision_id != receipt.get("decision_ids", {}).get(market):
            raise LookupError("publication intent is missing or does not match the frozen decision")
        if publication.market != market or publication.channel != intent["channel"] or publication.ordinal != ordinal:
            raise RuntimeError("publication intent does not match its durable outbox row")
        if intent["channel"] not in channels:
            raise RuntimeError("publication channel is not in the requested delivery channels")
        frozen_message = artifact_store.read(json.loads(publication.artifact_reference_json))
        expected_message = {
            "decision_id": publication.decision_id,
            "market": market,
            "title": str(message.get("title") or ""),
            "body": str(message.get("body") or ""),
        }
        if (frozen_message != expected_message
                or payload_digest(frozen_message) != publication.payload_sha256):
            raise RuntimeError("publication message differs from the frozen outbox artifact")
        if publication.status in {"SENT", "UNKNOWN", "SENDING"}:
            skipped.append({"channel": intent["channel"], "status": publication.status})
            continue
        # Persist the claim before crossing the external side-effect boundary.
        # A crash may strand SENDING, which requires reconciliation; it must
        # never silently become a second provider call on restart.
        publication.status = "SENDING"
        publication.attempt_count += 1
        publication.updated_at = datetime.now().astimezone()
        db.commit()
        try:
            result = notifier.send_event(
                event_type=event_type, title=message["title"], body=message["body"],
                channels=[intent["channel"]],
            )
        except Exception as exc:
            result = {
                "status": "failed", "sent": [],
                "failed": [{"channel": intent["channel"],
                            "message": f"provider_call_exception:{type(exc).__name__}"}],
            }
        sent.extend(result.get("sent") or [])
        failed.extend(result.get("failed") or [])
        delivery_records.append(record_publication_result(
            db=db, market=market, delivery_key=intent["delivery_key"], result=result,
        ))
    return {
        "status": "success" if sent and not failed else "partial" if sent else "skipped" if skipped and not failed else "failed",
        "sent": sent, "failed": failed, "skipped": skipped,
        "delivery_records": delivery_records,
    }
