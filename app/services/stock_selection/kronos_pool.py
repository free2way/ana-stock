from __future__ import annotations


def _safe_number(value: object, default: float = -999999.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def kronos_decision_tone(decision: object) -> str:
    text = str(decision or "").strip()
    lowered = text.lower()
    rejected = "不支持" in text or "avoid" in lowered or "reject" in lowered
    supported = ("支持" in text and not rejected) or ("support" in lowered and not rejected)
    return "support" if supported else "avoid" if rejected else "neutral"


def prepare_kronos_validation_pool(
    payload: dict | None,
    *,
    market: str = "ALL",
    status: str = "ALL",
) -> dict:
    """Build a language-neutral, non-mutating Kronos validation view model."""
    source = payload if isinstance(payload, dict) else {}
    selected_market = str(market or "ALL").strip().upper()
    selected_status = str(status or "ALL").strip().upper()
    all_rows = [dict(item) for item in (source.get("rows") or []) if isinstance(item, dict)]

    status_counts: dict[str, int] = {}
    market_counts: dict[str, int] = {}
    for item in all_rows:
        row_status = str(item.get("kronos_status") or "UNKNOWN").strip().upper() or "UNKNOWN"
        row_market = str(item.get("market") or "UNKNOWN").strip().upper() or "UNKNOWN"
        status_counts[row_status] = status_counts.get(row_status, 0) + 1
        market_counts[row_market] = market_counts.get(row_market, 0) + 1

    rows = all_rows
    if selected_market in {"CN", "US"}:
        rows = [row for row in rows if str(row.get("market") or "").strip().upper() == selected_market]
    if selected_status != "ALL":
        rows = [row for row in rows if str(row.get("kronos_status") or "").strip().upper() == selected_status]
    rows.sort(
        key=lambda item: (
            2
            if kronos_decision_tone(item.get("kronos_decision")) == "support"
            else 1
            if str(item.get("kronos_status") or "").upper() == "READY"
            else 0,
            _safe_number(item.get("kronos_score")),
            _safe_number((item.get("path_precheck") or {}).get("score")),
            _safe_number(item.get("trade_readiness_score")),
        ),
        reverse=True,
    )
    return {
        "selected_market": selected_market,
        "selected_status": selected_status,
        "rows": rows,
        "status_counts": status_counts,
        "market_counts": market_counts,
        "updated_at": source.get("updated_at"),
        "status": source.get("status") or "-",
        "message": source.get("message") or "-",
        "model_name": source.get("model_name") or "-",
        "candidate_count": int(source.get("candidate_count") or 0),
        "validated_count": int(source.get("validated_count") or 0),
        "refresh_markets": selected_market if selected_market in {"CN", "US"} else "CN,US",
    }
