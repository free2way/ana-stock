from __future__ import annotations

from app.services.repository import WorkspaceSnapshotRepository
from app.services.workspace_snapshots import (
    SNAPSHOT_MARKET_WORKSPACE_MONITOR,
    SNAPSHOT_MARKET_WORKSPACE_POSTMARKET,
    SNAPSHOT_MARKET_WORKSPACE_PREMARKET,
)


_SNAPSHOT_TYPES = {
    "premarket": SNAPSHOT_MARKET_WORKSPACE_PREMARKET,
    "monitor": SNAPSHOT_MARKET_WORKSPACE_MONITOR,
    "postmarket": SNAPSHOT_MARKET_WORKSPACE_POSTMARKET,
}


def normalize_market_snapshot_filters(*, mode: object, market_filter: object) -> tuple[str, str]:
    view_mode = str(mode or "monitor").strip().lower()
    if view_mode not in _SNAPSHOT_TYPES:
        view_mode = "monitor"
    selected_market = str(market_filter or "CN").strip().upper()
    if selected_market not in {"ALL", "CN", "US"}:
        selected_market = "CN"
    return view_mode, selected_market


def market_snapshot_type(mode: str) -> str:
    return _SNAPSHOT_TYPES.get(str(mode or "").strip().lower(), SNAPSHOT_MARKET_WORKSPACE_MONITOR)


def load_market_snapshot_history(db: object, *, snapshot_type: str, limit: int = 6) -> dict[str, list[int]]:
    snapshots = WorkspaceSnapshotRepository(db).list_snapshots(snapshot_type, limit=max(limit * 3, limit))
    points: list[dict[str, int]] = []
    for snapshot in reversed(snapshots):
        payload = snapshot.get("payload") or {}
        boards = payload.get("boards") or []
        if not isinstance(boards, list) or not boards:
            continue
        point: dict[str, int] = {}
        for board in boards:
            market = str(board.get("market") or "").upper()
            if market:
                point[market] = point.get(market, 0) + len(board.get("rows") or [])
        if point:
            points.append(point)
    points = points[-limit:]
    return {market: [int(point.get(market, 0)) for point in points] for market in ("CN", "US")}


def build_market_snapshot_view(
    payload: dict | None,
    *,
    mode: object,
    market_filter: object,
    history: dict[str, list[int]] | None = None,
) -> dict:
    view_mode, selected_market = normalize_market_snapshot_filters(
        mode=mode,
        market_filter=market_filter,
    )
    source = payload if isinstance(payload, dict) else {}
    raw_boards = source.get("boards")
    snapshot_ready = isinstance(raw_boards, list) and bool(raw_boards)
    all_boards = [dict(board) for board in (raw_boards or []) if isinstance(board, dict)]
    boards = [
        board
        for board in all_boards
        if selected_market == "ALL" or str(board.get("market") or "").upper() == selected_market
    ]
    history = history or {}
    market_summaries = []
    for market in ("CN", "US"):
        scoped_boards = [board for board in all_boards if str(board.get("market") or "").upper() == market]
        rows = [row for board in scoped_boards for row in (board.get("rows") or []) if isinstance(row, dict)]
        heat = round(sum(float(row.get("trend_score") or 0.0) for row in rows) / max(len(rows), 1), 1) if rows else 0.0
        highest_trend = max((float(row.get("trend_score") or 0.0) for row in rows), default=0.0)
        history_values = [int(value) for value in (history.get(market) or [])]
        delta = history_values[-1] - history_values[0] if len(history_values) >= 2 else 0
        market_summaries.append(
            {
                "market": market,
                "board_count": len(scoped_boards),
                "candidate_count": len(rows),
                "heat": heat,
                "highest_trend": highest_trend,
                "history": history_values,
                "delta": delta,
                "delta_tone": "up" if delta > 0 else "down" if delta < 0 else "flat",
            }
        )
    return {
        "mode": view_mode,
        "selected_market": selected_market,
        "snapshot_ready": snapshot_ready,
        "boards": boards,
        "market_summaries": market_summaries,
    }
