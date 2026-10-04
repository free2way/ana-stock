from __future__ import annotations

from collections.abc import Callable

from app.services.screener import MODEL_TEMPLATES, ScreenerService
from app.services.screener_snapshots import build_base_precompute_params
from app.services.stock_selection.multi_model_confluence import (
    aggregate_multi_model_rows,
    normalize_multi_model_templates,
)
from app.services.stock_selection.selection_policy import (
    apply_quality_confluence_profile,
    normalize_action_filter,
)


SnapshotLoader = Callable[[dict], list[dict] | None]
ScreenRowsLoader = Callable[[ScreenerService, dict], tuple[list[dict] | None, bool]]


def watchlist_state_rank(existing: dict | None) -> int:
    """Rank a row by its watchlist sync state (shared by page and bulk actions)."""
    if not existing:
        return 0
    if existing.get("sync_enabled") and existing.get("sync_status") == "success":
        return 3
    if existing.get("sync_enabled"):
        return 2
    return 1


def normalize_screen_params(params: dict) -> dict:
    model_template = str(params.get("model_template", "technical_momentum"))
    template = MODEL_TEMPLATES.get(model_template, MODEL_TEMPLATES["technical_momentum"])
    requested_market = str(params.get("market", "ALL"))
    effective_market = str(template.get("market") or requested_market)
    if effective_market != "ALL":
        requested_market = effective_market
    normalized = {
        "model_template": model_template,
        "multi_model_templates": normalize_multi_model_templates(params.get("multi_model_templates")),
        "min_multi_model_hits": max(1, int(float(params.get("min_multi_model_hits", 2)))),
        "confluence_action_filter": str(params.get("confluence_action_filter", "ALL")),
        "lang": str(params.get("lang", "en")),
        "universe": str(params.get("universe", "full_market")),
        "market": requested_market,
        "min_trend_score": int(float(params.get("min_trend_score", 60))),
        "action_filter": str(params.get("action_filter", "ALL")),
        "min_volume_ratio": float(params.get("min_volume_ratio", 0.0)),
        "min_listing_days": int(float(params.get("min_listing_days", 365))),
        "pe_min": float(params.get("pe_min", 0.0)),
        "pe_max": float(params.get("pe_max", 30.0)),
        "min_roe_avg_3y": float(params.get("min_roe_avg_3y", 12.0)),
        "min_net_profit_yoy": float(params.get("min_net_profit_yoy", 20.0)),
        "min_revenue_yoy": float(params.get("min_revenue_yoy", 0.0)),
        "max_debt_to_assets": float(params.get("max_debt_to_assets", 100.0)),
        "min_dividend_yield": float(params.get("min_dividend_yield", 0.0)),
        "exclude_bottom_market_cap_pct": float(params.get("exclude_bottom_market_cap_pct", 10.0)),
        "recent_snapshot_runs": int(float(params.get("recent_snapshot_runs", 0))),
        "min_snapshot_hits": int(float(params.get("min_snapshot_hits", 0))),
        "model_signal_filter": str(params.get("model_signal_filter", "ALL")),
        "min_model_signal_strength": float(params.get("min_model_signal_strength", 0.0)),
        "execution_tag_filter": str(params.get("execution_tag_filter", "ALL")),
        "exclude_execution_tag_filter": str(params.get("exclude_execution_tag_filter", "ALL")),
        "sort_by": str(params.get("sort_by", "default")),
        "sort_order": str(params.get("sort_order", "desc")),
        "limit": 500,
    }
    if str(params.get("strategy_profile") or "").strip() == "quality_confluence_v1":
        normalized["strategy_profile"] = "quality_confluence_v1"
    return normalized


def _rank_precomputed_rows(service: ScreenerService, rows: list[dict], params: dict) -> list[dict]:
    """Apply every query-time filter, profile gate and sort without truncating.

    Keeping truncation out of this helper lets callers expose the true match
    count (S-8) and sort on the full result set before cutting it (S-2).
    """
    min_trend_score = int(params.get("min_trend_score", 60))
    action_filter = str(params.get("action_filter", "ALL"))
    min_volume_ratio = float(params.get("min_volume_ratio", 0.0))
    min_listing_days = int(params.get("min_listing_days", 365))
    pe_min = float(params.get("pe_min", 0.0))
    pe_max = float(params.get("pe_max", 30.0))
    min_roe_avg_3y = float(params.get("min_roe_avg_3y", 12.0))
    min_net_profit_yoy = float(params.get("min_net_profit_yoy", 20.0))
    min_revenue_yoy = float(params.get("min_revenue_yoy", 0.0))
    max_debt_to_assets = float(params.get("max_debt_to_assets", 100.0))
    min_dividend_yield = float(params.get("min_dividend_yield", 0.0))
    recent_snapshot_runs = int(params.get("recent_snapshot_runs", 0))
    min_snapshot_hits = int(params.get("min_snapshot_hits", 0))
    normalized_action_filter = normalize_action_filter(action_filter)
    filtered: list[dict] = []

    for row in rows:
        trend_score = row.get("trend_score")
        if trend_score is not None and float(trend_score or 0.0) < min_trend_score:
            continue
        if normalized_action_filter not in {"", "all"}:
            if normalize_action_filter(row.get("action_label")) != normalized_action_filter:
                continue
        if float(row.get("volume_ratio") or 0.0) < min_volume_ratio:
            continue
        listing_days = row.get("listing_days")
        if listing_days is not None and int(listing_days or 0) < min_listing_days:
            continue
        pe_ttm = row.get("pe_ttm")
        if pe_ttm is not None and not (pe_min <= float(pe_ttm) <= pe_max):
            continue
        roe_avg_3y = row.get("roe_avg_3y")
        if roe_avg_3y is not None and float(roe_avg_3y) < min_roe_avg_3y:
            continue
        net_profit_yoy = row.get("net_profit_yoy")
        if net_profit_yoy is not None and float(net_profit_yoy) < min_net_profit_yoy:
            continue
        revenue_yoy = row.get("revenue_yoy")
        if revenue_yoy is not None and float(revenue_yoy) < min_revenue_yoy:
            continue
        debt_to_assets = row.get("debt_to_assets")
        if debt_to_assets is not None and float(debt_to_assets) > max_debt_to_assets:
            continue
        dividend_yield = row.get("dividend_yield")
        if dividend_yield is not None and float(dividend_yield) < min_dividend_yield:
            continue
        filtered.append(dict(row))

    filtered = service._apply_snapshot_persistence_filter(
        filtered,
        recent_snapshot_runs=recent_snapshot_runs,
        min_snapshot_hits=min_snapshot_hits,
    )
    filtered = service._apply_model_signal_filter(
        filtered,
        model_signal_filter=str(params.get("model_signal_filter", "ALL")),
        min_model_signal_strength=float(params.get("min_model_signal_strength", 0.0)),
    )
    filtered = service._apply_execution_tag_filter(
        filtered,
        execution_tag_filter=str(params.get("execution_tag_filter", "ALL")),
        exclude_execution_tag_filter=str(params.get("exclude_execution_tag_filter", "ALL")),
    )
    filtered = apply_quality_confluence_profile(
        filtered,
        profile=str(params.get("strategy_profile") or ""),
    )
    return service._sort_results(
        filtered,
        sort_by=str(params.get("sort_by", "default")),
        sort_order=str(params.get("sort_order", "desc")),
    )


def filter_precomputed_rows(service: ScreenerService, rows: list[dict], params: dict) -> list[dict]:
    ranked = _rank_precomputed_rows(service, rows, params)
    return ranked[: int(params.get("limit", 500))]


def load_precomputed_screener_rows(
    service: ScreenerService,
    params: dict,
    *,
    snapshot_loader: SnapshotLoader,
    apply_limit: bool = True,
) -> list[dict] | None:
    base_params = build_base_precompute_params(
        model_template=str(params.get("model_template") or "technical_momentum"),
        universe=str(params.get("universe") or "full_market"),
        market=str(params.get("market") or "ALL"),
    )
    snapshot_rows = snapshot_loader(base_params)
    if snapshot_rows is None:
        return None
    ranked = _rank_precomputed_rows(service, snapshot_rows, params)
    if apply_limit:
        ranked = ranked[: int(params.get("limit", 500))]
    return ranked


def load_screen_rows_from_snapshot(
    service: ScreenerService,
    normalized: dict,
    *,
    snapshot_loader: SnapshotLoader,
    apply_limit: bool = True,
) -> tuple[list[dict] | None, bool]:
    snapshot_rows = load_precomputed_screener_rows(
        service,
        normalized,
        snapshot_loader=snapshot_loader,
        apply_limit=apply_limit,
    )
    if snapshot_rows is not None:
        return snapshot_rows, True
    snapshot_rows = snapshot_loader(normalized)
    if snapshot_rows is not None:
        profiled = apply_quality_confluence_profile(
            [dict(row) for row in snapshot_rows],
            profile=str(normalized.get("strategy_profile") or ""),
        )
        return profiled, True
    return None, False


def run_multi_screen(
    service: ScreenerService,
    params: dict,
    *,
    snapshot_loader: SnapshotLoader,
    screen_rows_loader: ScreenRowsLoader | None = None,
    apply_limit: bool = True,
) -> tuple[list[dict], bool, dict]:
    combo_snapshot_rows = snapshot_loader(params)
    if combo_snapshot_rows is not None:
        return combo_snapshot_rows, True, {
            "available_templates": normalize_multi_model_templates(params.get("multi_model_templates")),
            "missing_templates": [],
            "precomputed_combo": True,
        }
    template_keys = normalize_multi_model_templates(params.get("multi_model_templates"))
    if len(template_keys) < 2:
        return [], False, {"available_templates": [], "missing_templates": []}
    template_rows: dict[str, list[dict]] = {}
    for template_key in template_keys:
        local_params = dict(params)
        local_params["model_template"] = template_key
        local_params["multi_model_templates"] = []
        local_params["min_multi_model_hits"] = 1
        normalized_local_params = normalize_screen_params(local_params)
        if screen_rows_loader is not None:
            rows, ready = screen_rows_loader(service, normalized_local_params)
        else:
            rows, ready = load_screen_rows_from_snapshot(
                service,
                normalized_local_params,
                snapshot_loader=snapshot_loader,
                apply_limit=apply_limit,
            )
        if ready and rows is not None:
            template_rows[template_key] = rows
    results, meta = aggregate_multi_model_rows(
        template_rows,
        template_keys=template_keys,
        params=params,
        apply_limit=apply_limit,
    )
    return results, bool(meta["available_templates"]), meta


def run_screen(
    service: ScreenerService,
    params: dict,
    *,
    snapshot_loader: SnapshotLoader,
) -> list[dict]:
    normalized = normalize_screen_params(params)
    if len(normalized.get("multi_model_templates") or []) >= 2:
        rows, _ready, _meta = run_multi_screen(
            service,
            normalized,
            snapshot_loader=snapshot_loader,
        )
        return rows
    rows, ready = load_screen_rows_from_snapshot(
        service,
        normalized,
        snapshot_loader=snapshot_loader,
    )
    if ready:
        return rows or []
    return []


def build_final_results(
    service: ScreenerService,
    params: dict,
    *,
    snapshot_loader: SnapshotLoader,
    watchlist_state_map: dict | None = None,
) -> tuple[list[dict], dict]:
    """Build the one canonical result set shared by page, export and bulk actions.

    The pipeline is normalize -> snapshot -> filters -> profile -> sort -> limit.
    It returns the truncated rows plus metadata carrying the pre-truncation
    total so callers can render an honest truncation notice (S-8) and sort the
    full set before cutting it (S-2).
    """
    normalized = normalize_screen_params(params)
    multi_screen_meta: dict = {"available_templates": [], "missing_templates": []}
    if len(normalized.get("multi_model_templates") or []) >= 2:
        rows, ready, multi_screen_meta = run_multi_screen(
            service,
            normalized,
            snapshot_loader=snapshot_loader,
            apply_limit=False,
        )
    else:
        collected, ready = load_screen_rows_from_snapshot(
            service,
            normalized,
            snapshot_loader=snapshot_loader,
            apply_limit=False,
        )
        rows = collected or []

    if str(normalized.get("sort_by") or "") == "watchlist_state":
        mapping = watchlist_state_map or {}
        rows = sorted(
            rows,
            key=lambda item: (
                watchlist_state_rank(mapping.get(str(item.get("ticker") or ""))),
                str(item.get("ticker") or ""),
            ),
            reverse=str(normalized.get("sort_order") or "desc") != "asc",
        )

    limit = int(normalized.get("limit", 500))
    total_count = len(rows)
    return rows[:limit], {
        "snapshot_ready": bool(ready),
        "total_count": total_count,
        "returned_count": min(total_count, limit),
        "limit": limit,
        "truncated": total_count > limit,
        "multi_screen_meta": multi_screen_meta,
    }


def screen_snapshot_ready(
    service: ScreenerService,
    params: dict,
    *,
    snapshot_loader: SnapshotLoader,
) -> bool:
    normalized = normalize_screen_params(params)
    if len(normalized.get("multi_model_templates") or []) >= 2:
        _rows, ready, _meta = run_multi_screen(
            service,
            normalized,
            snapshot_loader=snapshot_loader,
        )
        return ready
    _, ready = load_screen_rows_from_snapshot(
        service,
        normalized,
        snapshot_loader=snapshot_loader,
    )
    return ready
