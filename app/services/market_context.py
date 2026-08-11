from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session


MARKET_WORKSPACE_POSTMARKET_SNAPSHOT_TYPE = "market_workspace:postmarket"
MARKET_HEATMAP_SNAPSHOT_TYPE = "market_heatmap_workspace"


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _entry_style_value(row: dict) -> str:
    for key in ("entry_style", "model_entry_style", "action_label", "setup_label"):
        value = str(row.get(key) or "").strip().lower()
        if value:
            return value
    return ""


def summarize_market_context_snapshot(
    *,
    market: str,
    workspace_payload: dict | None = None,
    heatmap_payload: dict | None = None,
    snapshot_date: str | None = None,
    heatmap_snapshot_date: str | None = None,
) -> dict:
    market_code = str(market or "").strip().upper()
    boards = list((workspace_payload or {}).get("boards") or [])
    market_boards = [board for board in boards if str(board.get("market") or "").upper() == market_code]
    rows = [row for board in market_boards for row in (board.get("rows") or []) if isinstance(row, dict)]

    row_count = len(rows)
    avg_snapshot_score = (
        round(sum(float(row.get("snapshot_score") or 0.0) for row in rows) / row_count, 1)
        if row_count
        else None
    )
    avg_trend_score = (
        round(sum(float(row.get("trend_score") or 0.0) for row in rows) / row_count, 1)
        if row_count
        else None
    )
    breakout_candidates = sum(
        1
        for row in rows
        if _entry_style_value(row) in {"breakout", "momentum", "wait_for_breakout", "breakout_ready"}
    )
    breakout_share = round((breakout_candidates / row_count) * 100.0, 1) if row_count else None

    heatmap_rows = [
        item
        for item in (heatmap_payload or {}).get("sector_heatmap") or []
        if str(item.get("market") or "").upper() == market_code
    ]
    tracked_signal_count = int((heatmap_payload or {}).get("tracked_signal_count") or 0)
    weighted_breadth_numerator = 0.0
    weighted_breadth_denominator = 0.0
    top_sector_share = None
    if heatmap_rows and tracked_signal_count > 0:
        top_hits = max(int(item.get("hits") or 0) for item in heatmap_rows)
        top_sector_share = round((top_hits / tracked_signal_count) * 100.0, 1)
    for item in heatmap_rows:
        breadth_pct = _safe_float(item.get("breadth_pct"))
        hits = max(1, int(item.get("hits") or 0))
        if breadth_pct is None:
            continue
        weighted_breadth_numerator += breadth_pct * hits
        weighted_breadth_denominator += hits
    breadth_pct = (
        round(weighted_breadth_numerator / weighted_breadth_denominator, 1)
        if weighted_breadth_denominator > 0
        else None
    )
    resonance_score = _safe_float((heatmap_payload or {}).get("resonance_score"))

    if row_count == 0:
        regime = "unknown"
    elif (
        (avg_snapshot_score or 0.0) >= 72.0
        and (avg_trend_score or 0.0) >= 68.0
        and (breadth_pct or 0.0) >= 58.0
    ):
        regime = "risk_on"
    elif (
        (avg_snapshot_score is not None and avg_snapshot_score < 56.0)
        or (breadth_pct is not None and breadth_pct < 45.0)
        or row_count < 6
    ):
        regime = "defensive"
    else:
        regime = "watchful"

    crowded_theme = bool(top_sector_share is not None and top_sector_share >= 34.0 and (breadth_pct or 0.0) < 62.0)
    breakout_tailwind = bool(regime == "risk_on" and (breadth_pct or 0.0) >= 60.0)

    return {
        "market": market_code,
        "regime": regime,
        "row_count": row_count,
        "board_count": len(market_boards),
        "avg_snapshot_score": avg_snapshot_score,
        "avg_trend_score": avg_trend_score,
        "breakout_share": breakout_share,
        "breadth_pct": breadth_pct,
        "resonance_score": resonance_score,
        "top_sector_share": top_sector_share,
        "crowded_theme": crowded_theme,
        "breakout_tailwind": breakout_tailwind,
        "snapshot_date": str(snapshot_date or "")[:10] or None,
        "heatmap_snapshot_date": str(heatmap_snapshot_date or "")[:10] or None,
    }


def load_market_context_snapshot(db: Session, *, market: str) -> dict:
    from app.services.repository import WorkspaceSnapshotRepository
    from app.services.market_risk import market_risk_snapshot_type

    repo = WorkspaceSnapshotRepository(db)
    workspace_snapshot = repo.get_latest_snapshot(MARKET_WORKSPACE_POSTMARKET_SNAPSHOT_TYPE) or {}
    heatmap_snapshot = repo.get_latest_snapshot(MARKET_HEATMAP_SNAPSHOT_TYPE) or {}
    market_code = str(market or "").strip().upper()
    risk_snapshot = repo.get_latest_snapshot(market_risk_snapshot_type(market_code)) or {}
    risk_payload = risk_snapshot.get("payload") if isinstance(risk_snapshot, dict) else None
    summary = summarize_market_context_snapshot(
        market=market_code,
        workspace_payload=(workspace_snapshot.get("payload") if isinstance(workspace_snapshot, dict) else None) or {},
        heatmap_payload=(heatmap_snapshot.get("payload") if isinstance(heatmap_snapshot, dict) else None) or {},
        snapshot_date=workspace_snapshot.get("snapshot_date") if isinstance(workspace_snapshot, dict) else None,
        heatmap_snapshot_date=heatmap_snapshot.get("snapshot_date") if isinstance(heatmap_snapshot, dict) else None,
    )
    if isinstance(risk_payload, dict):
        risk_regime = str(risk_payload.get("risk_regime") or "").strip()
        buy_gate = str(risk_payload.get("buy_gate") or "").strip().upper()
        risk_regime_for_trade = str(risk_payload.get("regime") or "").strip().lower()
        if risk_regime_for_trade in {"defensive", "watchful", "risk_on"}:
            summary["regime"] = risk_regime_for_trade
        if buy_gate == "BLOCK":
            summary["regime"] = "defensive"
        elif buy_gate == "REVIEW" and summary.get("regime") == "risk_on":
            summary["regime"] = "watchful"
        latest = risk_payload.get("latest") if isinstance(risk_payload.get("latest"), dict) else {}
        if latest.get("up_pct") is not None:
            summary["breadth_pct"] = latest.get("up_pct")
        summary.update(
            {
                "risk_regime": risk_regime or None,
                "buy_gate": buy_gate or None,
                "risk_level": risk_payload.get("risk_level"),
                "max_position_scale": risk_payload.get("max_position_scale"),
                "risk_headline": risk_payload.get("headline"),
                "risk_playbook": risk_payload.get("playbook"),
                "risk_flags": risk_payload.get("flags") or [],
                "market_risk_snapshot_date": risk_payload.get("snapshot_date"),
            }
        )
    summary["workspace_snapshot"] = workspace_snapshot
    summary["heatmap_snapshot"] = heatmap_snapshot
    summary["risk_snapshot"] = risk_snapshot
    return summary
