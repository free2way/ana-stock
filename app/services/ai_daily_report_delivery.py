from __future__ import annotations

from app.services.ai_daily_report import (
    build_ai_daily_report,
    render_ai_daily_report_push_messages,
    save_ai_daily_report,
)
from app.services.push_notifications import PushNotificationService
from app.core.db import SessionLocal
from app.services.stock_selection.decision_ledger import dispatch_publication_message
from app.services.stock_selection.publication_guard import (
    load_trusted_model_qualifications, load_trusted_regime_snapshots, prepare_report_for_publication,
)
from app.services.time_utils import app_now_iso


def deliver_cn_ai_daily_report_to_feishu(*, limit: int = 8) -> dict:
    """Build the latest A-share report and deliver it only to Feishu.

    When the candidate gate is not ready, send a readiness notice instead of
    publishing fallback stock picks as an actionable daily report.
    """

    report = build_ai_daily_report(limit=limit, markets=["CN"])
    report.setdefault("decision_cutoff_at", app_now_iso())
    report.update(prepare_report_for_publication(
        report, approved_qualifications=load_trusted_model_qualifications(),
        trusted_regime_snapshots=load_trusted_regime_snapshots(),
    ))
    report_date = str(report.get("report_date") or "").strip() or None
    market_meta = report.get("market_recommendations_meta") or {}
    market_status = str(market_meta.get("status") or "").strip().lower()

    if market_status in {"fallback", "not_ready"}:
        message = next(item for item in render_ai_daily_report_push_messages(report)
                       if item.get("market") == "CN" and not str(item.get("title") or "").endswith("持仓股总结"))
        decision_receipt = save_ai_daily_report(
            report, publication_messages=[message], publication_channels=["feishu"],
        )
        notifier = PushNotificationService()
        with SessionLocal() as db:
            result = dispatch_publication_message(
                db=db, notifier=notifier, receipt=decision_receipt, ordinal=1,
                message=message, event_type="ai_report", channels=["feishu"],
            )
        return {
            **result,
            "report_date": report_date,
            "market_status": market_status,
            "delivery_kind": "readiness_notice",
            "messages": 1,
            "final_decision_receipt": decision_receipt,
        }

    sent: list[str] = []
    failed: list[dict] = []
    message_results: list[dict] = []
    push_messages = render_ai_daily_report_push_messages(report)
    decision_receipt = save_ai_daily_report(
        report, publication_messages=push_messages, publication_channels=["feishu"],
    )
    notifier = PushNotificationService()
    with SessionLocal() as db:
        for ordinal, message_item in enumerate(push_messages, start=1):
            result = dispatch_publication_message(
                db=db, notifier=notifier, receipt=decision_receipt,
                ordinal=ordinal, message=message_item,
                event_type="stock_recommendation", channels=["feishu"],
            )
            message_results.append({"title": message_item["title"], **result})
            sent.extend(item for item in (result.get("sent") or []) if item not in sent)
            failed.extend(result.get("failed") or [])
    status = "success" if sent and not failed else "partial" if sent else "failed"
    return {
        "status": status,
        "sent": sent,
        "failed": failed,
        "report_date": report_date,
        "market_status": market_status or "ready",
        "delivery_kind": "observation_report" if market_status == "observation_ready" else "daily_report",
        "messages": len(push_messages),
        "message_results": message_results,
        "final_decision_receipt": decision_receipt,
    }
