from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.services.screener_snapshots import (
    build_base_precompute_params,
    screener_snapshot_type,
)
from app.services.stock_selection.screener_query import normalize_screen_params


SnapshotLoader = Callable[[dict[str, Any]], dict[str, Any] | None]


def build_screener_run_receipt(
    *,
    params: dict[str, Any],
    result_count: int,
    snapshot_ready: bool,
    multi_templates_active: list[str],
    multi_screen_meta: dict[str, Any],
    snapshot_loader: SnapshotLoader,
) -> dict[str, Any]:
    """Resolve the data lineage for one screener run without rendering HTML.

    Snapshot access stays injected so the service is deterministic in tests and
    independent from the API/database session layer.
    """
    normalized = normalize_screen_params(params)
    source_params = normalized
    source_kind = "multi" if len(multi_templates_active) >= 2 else "exact"
    snapshot = snapshot_loader(source_params)

    if snapshot is None and len(multi_templates_active) < 2:
        source_params = build_base_precompute_params(
            model_template=str(normalized.get("model_template") or "technical_momentum"),
            universe=str(normalized.get("universe") or "full_market"),
            market=str(normalized.get("market") or "ALL"),
        )
        snapshot = snapshot_loader(source_params)
        if snapshot is not None:
            source_kind = "base_precompute"

    if snapshot is None:
        source_kind = "page_result"

    payload = (snapshot or {}).get("payload") if isinstance(snapshot, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    candidate_stats = payload.get("candidate_stats")
    if not isinstance(candidate_stats, dict):
        candidate_stats = {}
    regime_diagnostics = payload.get("regime_diagnostics")
    if not isinstance(regime_diagnostics, dict):
        regime_diagnostics = {}

    return {
        "normalized_params": normalized,
        "source_kind": source_kind,
        "snapshot": snapshot or {},
        "snapshot_payload": payload,
        "snapshot_ready": bool(snapshot_ready),
        "result_count": int(result_count),
        "param_digest": screener_snapshot_type(source_params).split(":", 1)[-1],
        "available_templates": list(multi_screen_meta.get("available_templates") or []),
        "missing_templates": list(multi_screen_meta.get("missing_templates") or []),
        "multi_model": len(multi_templates_active) >= 2,
        "candidate_stats": candidate_stats,
        "regime_diagnostics": regime_diagnostics,
        "empty_reason": payload.get("empty_reason"),
    }
