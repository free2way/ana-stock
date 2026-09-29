"""Read-only delivery reconciliation queue for durable stock-selection outboxes."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from app.services.json_payload_artifacts import JsonPayloadArtifactStore, payload_digest
from app.services.stock_selection.decision_ledger import PUBLICATION_MODELS


def audit_stock_selection_publications(*, db, store=None, now: datetime | None = None,
                                       pending_grace_seconds: int = 300) -> dict:
    """Expose uncertain delivery without resending or changing any outbox row."""
    artifact_store = store or JsonPayloadArtifactStore()
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("publication audit time must be timezone-aware")
    grace = max(0, int(pending_grace_seconds))
    counts: dict[str, int] = {}
    review: list[dict] = []
    checked = 0
    for market, model in PUBLICATION_MODELS.items():
        for row in db.scalars(select(model).order_by(model.id)).all():
            checked += 1
            counts[row.status] = counts.get(row.status, 0) + 1
            reasons: list[str] = []
            try:
                frozen = artifact_store.read(json.loads(row.artifact_reference_json))
                if (payload_digest(frozen) != row.payload_sha256
                        or frozen.get("decision_id") != row.decision_id
                        or frozen.get("market") != market):
                    reasons.append("frozen_payload_mismatch")
            except (OSError, ValueError, RuntimeError, TypeError, json.JSONDecodeError):
                reasons.append("frozen_payload_unreadable")
            updated_at = row.updated_at
            if updated_at.tzinfo is None or updated_at.utcoffset() is None:
                reasons.append("updated_at_timezone_missing")
                age_seconds = 0
            else:
                age_seconds = max(0, int((observed_at - updated_at).total_seconds()))
            if row.status in {"UNKNOWN", "SENDING"}:
                reasons.append("provider_outcome_uncertain")
            elif row.status == "SENT" and not row.provider_message_id:
                reasons.append("sent_without_provider_message_id")
            elif row.status == "PENDING" and age_seconds >= grace:
                reasons.append("pending_dispatch_over_grace")
            elif row.status not in {"PENDING", "SENDING", "SENT", "UNKNOWN"}:
                reasons.append("unexpected_status")
            if reasons:
                review.append({
                    "market": market,
                    "channel": row.channel,
                    "decision_id": row.decision_id,
                    "delivery_key": row.delivery_key,
                    "status": row.status,
                    "attempt_count": row.attempt_count,
                    "provider_message_id_present": bool(row.provider_message_id),
                    "payload_sha256": row.payload_sha256,
                    "age_seconds": age_seconds,
                    "reasons": reasons,
                })
    return {
        "status": "review_required" if review else "pass",
        "observed_at": observed_at.isoformat(),
        "publications_checked": checked,
        "status_counts": counts,
        "review_required": review,
        "provider_send_calls_made": 0,
        "database_rows_changed": 0,
    }


__all__ = ["audit_stock_selection_publications"]


def find_feishu_provider_receipt_candidates(*, db, provider, store=None,
                                            window_seconds: int = 600) -> dict:
    """Match exact bot/chat/title/body in read-only history; never certify delivery."""
    artifact_store = store or JsonPayloadArtifactStore()
    settings = provider.settings
    if settings.feishu_webhook_url or not provider._has_feishu_app_config():
        raise RuntimeError("Feishu app mode is required for provider-history reconciliation")
    window = min(max(int(window_seconds), 60), 3600)
    from app.services.push_notifications import PushNotificationService

    expected_events = ("stock_recommendation", "ai_report")
    results: list[dict] = []
    for market, model in PUBLICATION_MODELS.items():
        rows = db.scalars(select(model).where(
            model.channel == "feishu",
            model.status.in_(("SENDING", "UNKNOWN", "SENT")),
        ).order_by(model.id)).all()
        for row in rows:
            if row.status == "SENT" and row.provider_message_id:
                continue
            entry = {"market": market, "delivery_key": row.delivery_key,
                     "status": row.status, "matches": [], "history_checked": False}
            try:
                frozen = artifact_store.read(json.loads(row.artifact_reference_json))
                if (payload_digest(frozen) != row.payload_sha256
                        or frozen.get("decision_id") != row.decision_id
                        or frozen.get("market") != market):
                    raise RuntimeError("frozen publication payload mismatch")
                updated_at = row.updated_at
                if updated_at.tzinfo is None or updated_at.utcoffset() is None:
                    raise ValueError("outbox timestamp has no timezone")
                center = int(updated_at.timestamp())
                history = provider.list_feishu_chat_messages(
                    start_time=center - window, end_time=center + window,
                )
                entry["history_checked"] = True
                for item in history:
                    sender = item.get("sender") or {}
                    if (str(item.get("chat_id") or "") != settings.feishu_chat_id
                            or str(sender.get("id") or "") != settings.feishu_app_id
                            or str(sender.get("sender_type") or "") != "app"
                            or item.get("deleted") is True):
                        continue
                    body = item.get("body") or {}
                    try:
                        content = json.loads(str(body.get("content") or ""))
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(content, dict):
                        continue
                    nested_post = content.get("post") if isinstance(content.get("post"), dict) else {}
                    post = content.get("zh_cn") or nested_post.get("zh_cn") or {}
                    if not isinstance(post, dict):
                        continue
                    actual_title = str(post.get("title") or "")
                    actual_body = "".join(
                        str(part.get("text") or "")
                        for line in (post.get("content") or []) if isinstance(line, list)
                        for part in line if isinstance(part, dict) and part.get("tag") == "text"
                    )
                    for event_type in expected_events:
                        label = PushNotificationService.EVENT_LABELS[event_type]
                        if (str(item.get("message_id") or "")
                                and actual_title == f"【{label}】{frozen['title']}"
                                and actual_body == f"通知类型：{label}\n\n{frozen['body']}"):
                            entry["matches"].append({
                                "provider_message_id": str(item.get("message_id") or ""),
                                "create_time": str(item.get("create_time") or ""),
                                "event_type": event_type,
                            })
                            break
            except (OSError, RuntimeError, ValueError, TypeError, KeyError, httpx.HTTPError) as exc:
                entry["error_type"] = type(exc).__name__
                if str(exc).startswith("Feishu chat history HTTP"):
                    entry["error_detail"] = str(exc)
            results.append(entry)
    return {"status": "review_required", "entries": results,
            "provider_calls_are_read_only": True, "database_rows_changed": 0,
            "automatic_status_changes": 0}


__all__.append("find_feishu_provider_receipt_candidates")
