import json
import csv
import threading
from collections.abc import Callable
from io import StringIO
from urllib.parse import urlencode
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.api.rendering import mini_trend_bars as _mini_trend_bars  # noqa: F401 - compatibility export
from app.api.rendering import compact_text as _compact_text  # noqa: F401 - compatibility export
from app.api.presentation.screener_components import (
    _build_screen_query,
    _evaluation_metric_row,  # noqa: F401 - compatibility export asserted by tests
    _lang_text,
    _model_cell,  # noqa: F401 - compatibility export
    _snapshot_pending_message,
    _sync_status_badge,
    _watchlist_summary,
)
from app.api.presentation.screener_receipt import render_screener_run_receipt
from app.api.presentation.factor_lab_pages import (
    render_factor_lab_page,
    render_factor_lab_run_detail_page,
    render_factor_lab_run_not_found,
)
from app.api.presentation.screener_tactical import (
    format_lightgbm_history_bias as _lightgbm_history_bias,  # noqa: F401 - compatibility export
    localize_lightgbm_tactical_results,
)
from app.api.presentation.screener_pages import (
    render_kronos_validation_pool_page,
    render_market_snapshot_page,
    render_quality_profile_status,
    render_screener_regression_discipline,
    render_selection_quality_page,
    render_today_focus_page,
)

from app.core.config import get_settings
from app.core.db import SessionLocal, get_db_session
from app.services.auth import is_authenticated, login_redirect
from app.services.market_intelligence import build_market_sentiment_snapshot
from app.services.market_context import load_market_context_snapshot
from app.models.schema import SymbolCreate
from app.services.market_sync import sync_market_data
from app.services.market_freshness import is_snapshot_as_of_current
from app.services.repository import AppSettingRepository, SymbolRepository, WatchlistRepository, WorkspaceSnapshotRepository
from app.services.runtime_cache import get_or_set
from app.services.ai_daily_report import load_ai_daily_report
from app.services.model_selection_guidance import (
    load_model_selection_guidance_snapshot,
    summarize_model_selection_guidance,
)
from app.services.recommendation_regression import (
    load_or_build_recommendation_regression,
    summarize_recommendation_regression,
)
from app.services.selection_quality import (
    load_or_build_selection_quality,
    save_selection_quality_snapshot,
)
from app.services.screener import MODEL_TEMPLATES, ScreenerService
from app.services.screener_snapshots import (
    _action_semantic_buckets,  # noqa: F401 - re-export identity asserted by tests/test_cleanup_shared_helpers.py
    _compact_snapshot_rows,
    _normalize_multi_model_templates,
    _snapshot_input_meta,
    _template_action_semantic_buckets,  # noqa: F401 - compatibility export
    build_base_precompute_params,
    exact_screener_snapshot_exists,
    load_exact_screener_snapshot,
    screener_snapshot_key,
    screener_snapshot_type,
)
from app.services.template_evaluation import (
    build_lightgbm_prediction_evaluation,
)
from app.services.focus_pool import add_to_today_focus_pool, enrich_focus_pool_with_symbols, load_today_focus_pool
from app.services.kronos_validation import annotate_rows_with_kronos, load_latest_kronos_validation
from app.services.factor_experiments import (
    get_factor_strategy,
    get_factor_experiment_run,
    list_factor_definitions,
    list_factor_experiment_runs,
    list_factor_strategies,
    refresh_factor_experiment_run,
    run_factor_experiment,
    save_factor_strategy,
)
from app.services.ui_lang import resolve_request_lang
from app.services.workspace_nav import render_workspace_nav_html
from app.services.workspace_snapshots import load_latest_workspace_snapshot
from app.services.workspace_snapshots import refresh_workspace_snapshots
from app.services.time_utils import app_now_iso
from app.services.stock_selection.p0_diagnostics import (
    build_p0_pipeline_diagnostics,
    load_latest_research_regime_coverage,
)
from app.services.stock_selection.selection_policy import (
    apply_quality_confluence_profile as _apply_quality_confluence_profile,  # noqa: F401 - compatibility export asserted by tests
    focus_pool_trade_candidates as _focus_pool_trade_candidates,
    kronos_sort_value as _kronos_sort_value,
    lightgbm_confluence_fit_score as _lightgbm_confluence_fit_score,  # noqa: F401 - compatibility export
    normalize_action_filter as _normalize_action_filter,  # noqa: F401 - compatibility export
    rerank_with_lightgbm_tactical_signal as _rerank_with_lightgbm_tactical_signal,
)
from app.services.stock_selection.lightgbm_tactical import annotate_lightgbm_tactical_context
from app.services.stock_selection.kronos_pool import prepare_kronos_validation_pool
from app.services.stock_selection.multi_model_confluence import (
    aggregate_multi_model_rows,  # noqa: F401 - compatibility export asserted by tests
)
from app.services.stock_selection.screener_query import (
    build_final_results as _build_final_results_service,
    filter_precomputed_rows as _filter_precomputed_rows_service,
    live_screen_rows as _live_screen_rows_service,
    load_precomputed_screener_rows as _load_precomputed_screener_rows_service,
    load_screen_rows_from_snapshot as _load_screen_rows_from_snapshot_service,
    normalize_screen_params as _normalize_screen_params,
    run_multi_screen as _run_multi_screen_service,
    run_screen as _run_screen_service,
    screen_snapshot_ready as _screen_snapshot_ready_service,
)
from app.services.stock_selection.screener_summary import summarize_screener_rows
from app.services.stock_selection.screener_receipt import build_screener_run_receipt
from app.services.stock_selection.market_snapshot_view import (
    build_market_snapshot_view,
    load_market_snapshot_history,
    market_snapshot_type,
    normalize_market_snapshot_filters,
)


router = APIRouter(prefix="/screeners", tags=["screeners"])


SCREENER_SNAPSHOT_TTL = timedelta(days=7)
SCREENERS_PRESETS_KEY = "screener_saved_presets"


@router.get("/p0-diagnostics")
def p0_pipeline_diagnostics(
    request: Request,
    market: str = Query("CN"),
    model_template: str = Query("technical_momentum"),
    universe: str = Query("full_market"),
    db: Session = Depends(get_db_session),
):
    if not is_authenticated(request):
        return login_redirect("/screeners/p0-diagnostics")
    market_code = str(market or "").strip().upper()
    if market_code not in {"CN", "US"}:
        market_code = "CN"
    params = build_base_precompute_params(
        model_template=model_template,
        universe=universe,
        market=market_code,
    )
    snapshot = load_exact_screener_snapshot(params, db=db) or {}
    research_coverage = load_latest_research_regime_coverage(
        artifact_root=get_settings().artifacts_dir / "stock_selection_research" / "evidence",
        market=market_code,
        horizon_days=5,
    )
    return build_p0_pipeline_diagnostics(
        market=market_code,
        screener_payload=snapshot.get("payload") if isinstance(snapshot, dict) else None,
        research_coverage=research_coverage,
        publication_report=load_ai_daily_report(db=db),
    )




def _annotate_lightgbm_results(items: list[dict], *, selected_market: str, lang: str, force_apply: bool = False) -> None:
    if not items:
        return
    history_eval = build_lightgbm_prediction_evaluation(market=selected_market, recent_runs=8, top_n=40)
    annotate_lightgbm_tactical_context(
        items,
        selected_market=selected_market,
        history_evaluation=history_eval,
        force_apply=force_apply,
    )
    localize_lightgbm_tactical_results(items, lang=lang)




def _load_saved_presets(db: Session) -> list[dict]:
    raw = AppSettingRepository(db).get(SCREENERS_PRESETS_KEY)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _save_saved_presets(db: Session, presets: list[dict]) -> None:
    AppSettingRepository(db).set(SCREENERS_PRESETS_KEY, json.dumps(presets, ensure_ascii=False))




def _redirect_with_message(message: str, lang: str = "en") -> RedirectResponse:
    return RedirectResponse(url=f"/screeners?{urlencode({'message': message, 'lang': lang})}", status_code=303)




def _current_params(
    *,
    model_template: str,
    universe: str,
    market: str,
    min_trend_score: int,
    action_filter: str,
    min_volume_ratio: float,
    min_listing_days: int,
    pe_min: float,
    pe_max: float,
    min_roe_avg_3y: float,
    min_net_profit_yoy: float,
    min_revenue_yoy: float,
    max_debt_to_assets: float,
    min_dividend_yield: float,
    exclude_bottom_market_cap_pct: float,
    recent_snapshot_runs: int,
    min_snapshot_hits: int,
    model_signal_filter: str,
    min_model_signal_strength: float,
    execution_tag_filter: str,
    exclude_execution_tag_filter: str,
    sort_by: str,
    sort_order: str,
    lang: str,
    multi_model_templates: list[str] | None = None,
    min_multi_model_hits: int = 2,
    confluence_action_filter: str = "ALL",
    strategy_profile: str = "",
    min_hit_probability: float | None = None,
    probability_calibration: str | None = None,
    model_reliability_weights: str | None = None,
) -> dict:
    params = {
        "model_template": model_template,
        "multi_model_templates": _normalize_multi_model_templates(multi_model_templates),
        "min_multi_model_hits": min_multi_model_hits,
        "confluence_action_filter": str(confluence_action_filter or "ALL"),
        "universe": universe,
        "market": market,
        "min_trend_score": min_trend_score,
        "action_filter": action_filter,
        "min_volume_ratio": min_volume_ratio,
        "min_listing_days": min_listing_days,
        "pe_min": pe_min,
        "pe_max": pe_max,
        "min_roe_avg_3y": min_roe_avg_3y,
        "min_net_profit_yoy": min_net_profit_yoy,
        "min_revenue_yoy": min_revenue_yoy,
        "max_debt_to_assets": max_debt_to_assets,
        "min_dividend_yield": min_dividend_yield,
        "exclude_bottom_market_cap_pct": exclude_bottom_market_cap_pct,
        "recent_snapshot_runs": recent_snapshot_runs,
        "min_snapshot_hits": min_snapshot_hits,
        "model_signal_filter": model_signal_filter,
        "min_model_signal_strength": min_model_signal_strength,
        "execution_tag_filter": execution_tag_filter,
        "exclude_execution_tag_filter": exclude_execution_tag_filter,
        "sort_by": sort_by,
        "sort_order": sort_order,
        "lang": lang,
    }
    if str(strategy_profile).strip() == "quality_confluence_v1":
        params["strategy_profile"] = "quality_confluence_v1"
    if min_hit_probability is not None:
        params["min_hit_probability"] = min_hit_probability
    if probability_calibration not in (None, ""):
        params["probability_calibration"] = probability_calibration
    if model_reliability_weights not in (None, ""):
        params["model_reliability_weights"] = model_reliability_weights
    return params



def _template_default_min_trend_score(template_key: str, fallback: int = 60) -> int:
    defaults = (MODEL_TEMPLATES.get(str(template_key) or "") or {}).get("defaults") or {}
    try:
        return max(0, int(defaults.get("min_trend_score", fallback)))
    except (TypeError, ValueError):
        return fallback


_LIVE_SCREEN_CACHE_NAMESPACE = "screener_results"


def _persist_live_screen_snapshot(params: dict, rows: list[dict]) -> None:
    """Store a live fallback result as this parameter set's exact snapshot.

    The payload mirrors the producer shape (``key``/``rows``/``input``), so the
    next request for the same parameters is served by the exact-snapshot loader
    instead of screening the lake again.  Persistence is an optimisation on top
    of an already successful screen, so a write failure must not turn a rendered
    page into an error: it is swallowed here and the caller still returns the
    live rows.
    """
    payload = {
        "key": screener_snapshot_key(params),
        "rows": _compact_snapshot_rows(rows, limit=int(params.get("limit", 500))),
        "updated_at": app_now_iso(),
        "model_template": params.get("model_template"),
        "market": params.get("market"),
        "universe": params.get("universe"),
        "input": _snapshot_input_meta(params),
        "source": "live_fallback",
    }
    try:
        with SessionLocal() as db:
            WorkspaceSnapshotRepository(db).create_snapshot(
                snapshot_type=screener_snapshot_type(params),
                snapshot_date=app_now_iso(),
                payload=payload,
            )
    except Exception:
        return


def _live_screen_rows_with_timeout(
    service: ScreenerService,
    params: dict,
    *,
    timeout_seconds: float,
) -> list[dict] | None:
    """Run the live screen, optionally abandoning a slow computation.

    ``timeout_seconds <= 0`` keeps the historic blocking behaviour.  With a
    positive budget the screen runs on a daemon worker thread: when it does not
    finish in time the caller treats the fallback as unavailable and the page
    keeps its snapshot-pending rendering instead of holding the request open.
    The abandoned worker's result is discarded, and the TTL memo bounds how
    often a slow parameter set can start a fresh attempt.
    """
    if timeout_seconds <= 0:
        return _live_screen_rows_service(service, params)
    outcome: dict[str, object] = {}

    def _run() -> None:
        try:
            outcome["rows"] = _live_screen_rows_service(service, params)
        except BaseException as exc:  # re-raised on the request thread
            outcome["error"] = exc

    worker = threading.Thread(target=_run, name="screener-live-fallback", daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    if worker.is_alive():
        return None
    error = outcome.get("error")
    if isinstance(error, BaseException):
        raise error
    rows = outcome.get("rows")
    return rows if isinstance(rows, list) else None


def _live_screen_fallback_loader() -> Callable[[ScreenerService, dict], list[dict] | None] | None:
    """Build the live-screener fallback used when both snapshots miss.

    ``None`` (``PQW_SCREENER_LIVE_FALLBACK_ENABLED=false``) restores the
    snapshot-only behaviour: an empty page plus the "snapshot still being
    prepared" notice.  When enabled, a miss runs ``ScreenerService.screen`` for
    this exact parameter set (one lake pass per parameter set, memoised for
    ``screener_live_fallback_ttl_seconds`` so concurrent readers share it) and
    persists the outcome as the exact snapshot, so later requests are served on
    the snapshot path again.
    """
    settings = get_settings()
    if not bool(settings.screener_live_fallback_enabled):
        return None
    ttl_seconds = max(0.0, float(settings.screener_live_fallback_ttl_seconds))
    timeout_seconds = max(0.0, float(settings.screener_live_fallback_timeout_seconds))

    def _load_live_rows(service: ScreenerService, params: dict) -> list[dict] | None:
        def _compute() -> list[dict] | None:
            computed = _live_screen_rows_with_timeout(
                service, params, timeout_seconds=timeout_seconds
            )
            if computed is None:
                return None
            # Persist once per cache miss (``get_or_set`` runs the loader on the
            # single-flight leader only), so repeating a request inside the TTL
            # window neither re-screens the lake nor writes a duplicate snapshot.
            _persist_live_screen_snapshot(params, computed)
            return computed

        rows = get_or_set(
            _LIVE_SCREEN_CACHE_NAMESPACE,
            json.dumps(params, sort_keys=True, ensure_ascii=False),
            ttl_seconds=ttl_seconds,
            loader=_compute,
        )
        if not isinstance(rows, list):
            return None
        # Hand every caller its own copy: the memoised value is shared.
        return [dict(row) for row in rows]

    return _load_live_rows


def _load_screen_rows_from_snapshot(service: ScreenerService, normalized: dict) -> tuple[list[dict] | None, bool]:
    return _load_screen_rows_from_snapshot_service(
        service,
        normalized,
        snapshot_loader=_load_screener_snapshot,
        live_screen_loader=_live_screen_fallback_loader(),
    )


def _run_screen(service: ScreenerService, params: dict) -> list[dict]:
    return _run_screen_service(
        service,
        params,
        snapshot_loader=_load_screener_snapshot,
        live_screen_loader=_live_screen_fallback_loader(),
    )


def _build_final_results(
    service: ScreenerService,
    params: dict,
    *,
    watchlist_state_map: dict | None = None,
) -> tuple[list[dict], dict]:
    return _build_final_results_service(
        service,
        params,
        snapshot_loader=_load_screener_snapshot,
        live_screen_loader=_live_screen_fallback_loader(),
        watchlist_state_map=watchlist_state_map,
    )


def _screen_snapshot_ready(service: ScreenerService, params: dict) -> bool:
    return _screen_snapshot_ready_service(
        service,
        params,
        snapshot_loader=_load_screener_snapshot,
        live_screen_loader=_live_screen_fallback_loader(),
    )


def _run_multi_screen(service: ScreenerService, params: dict) -> tuple[list[dict], bool, dict]:
    return _run_multi_screen_service(
        service,
        params,
        snapshot_loader=_load_screener_snapshot,
        screen_rows_loader=_load_screen_rows_from_snapshot,
    )




def _load_precomputed_screener_rows(service: ScreenerService, params: dict) -> list[dict] | None:
    return _load_precomputed_screener_rows_service(
        service,
        params,
        snapshot_loader=_load_screener_snapshot,
    )


def _filter_precomputed_rows(service: ScreenerService, rows: list[dict], params: dict) -> list[dict]:
    return _filter_precomputed_rows_service(service, rows, params)


def _should_execute_screen(request: Request) -> bool:
    if str(request.query_params.get("run") or "").strip() == "1":
        return True
    meaningful_keys = {
        "model_template",
        "universe",
        "market",
        "min_trend_score",
        "action_filter",
        "min_volume_ratio",
        "min_listing_days",
        "pe_min",
        "pe_max",
        "min_roe_avg_3y",
        "min_net_profit_yoy",
        "min_revenue_yoy",
        "max_debt_to_assets",
        "min_dividend_yield",
        "exclude_bottom_market_cap_pct",
        "recent_snapshot_runs",
        "min_snapshot_hits",
        "model_signal_filter",
        "min_model_signal_strength",
        "execution_tag_filter",
        "exclude_execution_tag_filter",
        "sort_by",
        "sort_order",
        "min_hit_probability",
        "probability_calibration",
        "model_reliability_weights",
    }
    return any(key in meaningful_keys for key in request.query_params.keys())


def _load_screener_snapshot(params: dict) -> list[dict] | None:
    with SessionLocal() as db:
        snapshot = WorkspaceSnapshotRepository(db).get_latest_snapshot(screener_snapshot_type(params))
    if not snapshot:
        return None
    payload = snapshot.get("payload") or {}
    if payload.get("key") != screener_snapshot_key(params):
        return None
    market = str(params.get("market") or payload.get("market") or "").strip().upper()
    if (payload.get("schema_version") or payload.get("input")) and not is_snapshot_as_of_current(snapshot.get("snapshot_date"), market):
        return None
    created_at = str(snapshot.get("created_at") or "")
    try:
        created = datetime.fromisoformat(created_at)
    except ValueError:
        return None
    if datetime.fromisoformat(app_now_iso()) - created > SCREENER_SNAPSHOT_TTL:
        return None
    rows = payload.get("rows")
    return rows if isinstance(rows, list) else None


def _load_screener_snapshot_record(params: dict) -> dict | None:
    with SessionLocal() as db:
        snapshot = WorkspaceSnapshotRepository(db).get_latest_snapshot(screener_snapshot_type(params))
    if not snapshot:
        return None
    payload = snapshot.get("payload") or {}
    if payload.get("key") != screener_snapshot_key(params):
        return None
    market = str(params.get("market") or payload.get("market") or "").strip().upper()
    if (payload.get("schema_version") or payload.get("input")) and not is_snapshot_as_of_current(snapshot.get("snapshot_date"), market):
        return None
    return snapshot



def _load_today_focus_items() -> list[dict]:
    return get_or_set(
        "today_focus_items",
        "latest",
        ttl_seconds=30.0,
        loader=lambda: enrich_focus_pool_with_symbols(load_today_focus_pool()),
    )


def _add_screen_results_to_watchlist(
    *,
    db: Session,
    params: dict,
    top_n: int = 0,
    auto_enable_sync: bool = False,
) -> tuple[int, int, int]:
    service = ScreenerService()
    symbol_repo = SymbolRepository(db)
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)
    results, _final_meta = _build_final_results(
        service,
        params,
        watchlist_state_map=watchlist_map,
    )
    if top_n > 0:
        results = results[:top_n]
    added = 0
    already_in_watchlist = 0
    sync_enabled_count = 0

    for item in results:
        ticker = item["ticker"]
        existing = watchlist_map.get(ticker)
        if existing:
            already_in_watchlist += 1
            if auto_enable_sync and not existing.get("sync_enabled"):
                updated = watchlist_repo.set_sync_enabled(existing["item_id"], True)
                if updated is not None:
                    sync_enabled_count += 1
                    existing["sync_enabled"] = 1
            continue
        symbol = symbol_repo.get_or_create_symbol(
            SymbolCreate(
                ticker=ticker,
                name=item.get("name"),
                market=item.get("market"),
            )
        )
        watchlist_item = watchlist_repo.add_symbol(watchlist.id, symbol.id)
        if auto_enable_sync:
            watchlist_repo.set_sync_enabled(watchlist_item.id, True)
            sync_enabled_count += 1
        watchlist_map[ticker] = {
            "item_id": watchlist_item.id,
            "symbol_id": symbol.id,
            "ticker": ticker,
            "name": item.get("name"),
            "market": item.get("market"),
            "sync_enabled": 1 if auto_enable_sync else 0,
        }
        added += 1

    return added, already_in_watchlist, sync_enabled_count


def _factor_lab_url(*, lang: str, message: str | None = None, strategy_id: str | None = None) -> str:
    params = {"lang": lang}
    if strategy_id:
        params["strategy_id"] = strategy_id
    if message:
        params["message"] = message
    return f"/screeners/factor-lab?{urlencode(params)}"


@router.get("/factor-lab", response_class=HTMLResponse)
def factor_lab_page(
    request: Request,
    lang: str = Query("en"),
    strategy_id: str | None = Query(None),
    message: str | None = Query(None),
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/screeners/factor-lab")
    lang = resolve_request_lang(request)
    strategies = list_factor_strategies(db)
    selected_strategy = get_factor_strategy(db, strategy_id) or (strategies[0] if strategies else {})
    factor_defs = list_factor_definitions()
    runs = list_factor_experiment_runs(db, limit=12)
    source_params = selected_strategy.get("source_params") or {}
    snapshot_ready = exact_screener_snapshot_exists(source_params, db=db) if source_params else False
    return render_factor_lab_page(
        lang=lang,
        strategies=strategies,
        selected_strategy=selected_strategy,
        factor_defs=factor_defs,
        runs=runs,
        snapshot_ready=snapshot_ready,
        message=message,
        nav_html=render_workspace_nav_html(lang=lang, active_key="screeners"),
    )


@router.get("/selection-quality", response_class=HTMLResponse)
def selection_quality_page(
    request: Request,
    lang: str = Query("en"),
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/screeners/selection-quality")
    lang = resolve_request_lang(request)
    try:
        payload = load_or_build_selection_quality(db=db)
    except Exception as exc:
        payload = {
            "sample_count": 0,
            "summary": {"all": {}, "by_source": []},
            "guidance": {
                "headline_zh": "命中率闭环暂时不可用：{}".format(exc),
                "headline_en": "Selection quality is temporarily unavailable: {}".format(exc),
                "rules_zh": [],
            },
            "recent_records": [],
        }
    return render_selection_quality_page(
        lang=lang,
        payload=payload,
        nav_html=render_workspace_nav_html(lang=lang, active_key="screeners"),
    )


@router.post("/selection-quality/refresh")
def refresh_selection_quality_snapshot(
    request: Request,
    lang: str = Form("en"),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners/selection-quality")
    lang = resolve_request_lang(request)
    save_selection_quality_snapshot(db=db)
    return RedirectResponse(f"/screeners/selection-quality?lang={lang}", status_code=303)


@router.post("/factor-lab/run")
def run_factor_lab_strategy(
    request: Request,
    lang: str = Form("en"),
    strategy_id: str = Form(...),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners/factor-lab")
    lang = resolve_request_lang(request)
    strategy = get_factor_strategy(db, strategy_id)
    if not strategy:
        return RedirectResponse(_factor_lab_url(lang=lang, message="策略不存在。"), status_code=303)
    source_params = strategy.get("source_params") or {}
    source_snapshot = load_exact_screener_snapshot(source_params, db=db)
    if source_snapshot is None:
        message = "没有找到对应的预计算快照，请先让 screener_precompute job 跑完。" if lang == "zh" else "No matching precomputed snapshot. Please run screener_precompute first."
        return RedirectResponse(_factor_lab_url(lang=lang, message=message, strategy_id=strategy_id), status_code=303)
    source_payload = source_snapshot.get("payload") or {}
    rows = [dict(row) for row in (source_payload.get("rows") or []) if isinstance(row, dict)]
    source_signal_date = str(source_snapshot.get("snapshot_date") or "")[:10]
    if source_signal_date:
        for row in rows:
            row.setdefault("factor_signal_trade_date", source_signal_date)
    try:
        result = run_factor_experiment(
            db,
            strategy_id=strategy_id,
            source_rows=rows,
            source_params=source_params,
            limit=80,
        )
        payload = result.get("payload") or {}
        metrics = payload.get("metrics") or {}
        message = (
            f"实验完成：源样本 {payload.get('source_count', 0)}，命中 {payload.get('matched_count', 0)}，1D 命中率 {_fmt_plain_metric(metrics.get('hit_rate_1d_pct'))}。"
            if lang == "zh"
            else f"Run complete: source {payload.get('source_count', 0)}, matched {payload.get('matched_count', 0)}, 1D hit {_fmt_plain_metric(metrics.get('hit_rate_1d_pct'))}."
        )
    except Exception as exc:
        message = f"实验运行失败：{exc}" if lang == "zh" else f"Experiment failed: {exc}"
    return RedirectResponse(_factor_lab_url(lang=lang, message=message, strategy_id=strategy_id), status_code=303)


def _fmt_plain_metric(value: object) -> str:
    if value in (None, ""):
        return "-"
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return str(value)


@router.get("/factor-lab/runs/{snapshot_id}", response_class=HTMLResponse)
def factor_lab_run_detail_page(
    snapshot_id: int,
    request: Request,
    lang: str = Query("en"),
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/screeners/factor-lab/runs/{}".format(snapshot_id))
    lang = resolve_request_lang(request)
    snapshot = get_factor_experiment_run(db, snapshot_id)
    if snapshot is None:
        return HTMLResponse(
            render_factor_lab_run_not_found(
                lang=lang,
                nav_html=render_workspace_nav_html(lang=lang, active_key="screeners"),
            ),
            status_code=404,
        )
    return render_factor_lab_run_detail_page(
        lang=lang,
        snapshot_id=snapshot_id,
        snapshot=snapshot,
        factor_defs=list_factor_definitions(),
        nav_html=render_workspace_nav_html(lang=lang, active_key="screeners"),
    )


@router.post("/factor-lab/runs/{snapshot_id}/refresh")
def refresh_factor_lab_run(
    snapshot_id: int,
    request: Request,
    lang: str = Form("en"),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect(f"/screeners/factor-lab/runs/{snapshot_id}")
    lang = resolve_request_lang(request)
    try:
        result = refresh_factor_experiment_run(db, snapshot_id)
        refreshed_id = result["snapshot"].id
        return RedirectResponse(
            f"/screeners/factor-lab/runs/{refreshed_id}?lang={lang}",
            status_code=303,
        )
    except Exception as exc:
        message = f"刷新失败：{exc}" if lang == "zh" else f"Refresh failed: {exc}"
        return RedirectResponse(_factor_lab_url(lang=lang, message=message), status_code=303)


@router.post("/factor-lab/save")
def save_factor_lab_strategy(
    request: Request,
    lang: str = Form("en"),
    base_strategy_id: str = Form(...),
    name: str = Form(...),
    min_readiness: float = Form(58.0),
    min_trend: float = Form(58.0),
    min_profit_yoy: float = Form(15.0),
    readiness_weight: float = Form(22.0),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners/factor-lab")
    lang = resolve_request_lang(request)
    base = get_factor_strategy(db, base_strategy_id)
    if not base:
        return RedirectResponse(_factor_lab_url(lang=lang, message="基础策略不存在。"), status_code=303)
    strategy = json.loads(json.dumps(base, ensure_ascii=False))
    strategy["id"] = ""
    strategy["name"] = name.strip() or str(base.get("name") or "Factor strategy")
    strategy["filters"] = [
        item for item in (strategy.get("filters") or [])
        if str(item.get("factor_key") or "") not in {"trend_score", "trade_readiness_score", "net_profit_yoy"}
    ]
    strategy["filters"].extend(
        [
            {"factor_key": "trend_score", "op": "gte", "value": float(min_trend), "required": True},
            {"factor_key": "trade_readiness_score", "op": "gte", "value": float(min_readiness), "required": True},
            {"factor_key": "net_profit_yoy", "op": "gte", "value": float(min_profit_yoy), "required": False},
        ]
    )
    weights = dict(strategy.get("weights") or {})
    weights["trade_readiness_score"] = float(readiness_weight)
    strategy["weights"] = weights
    saved = save_factor_strategy(db, strategy)
    message = "策略已保存，可以直接运行实验。" if lang == "zh" else "Strategy saved. You can run it now."
    return RedirectResponse(_factor_lab_url(lang=lang, message=message, strategy_id=str(saved.get("id") or "")), status_code=303)


@router.get("", response_class=HTMLResponse)
def screener_page(
    request: Request,
    message: str | None = Query(None),
    lang: str = Query("en"),
    model_template: str = Query("technical_momentum"),
    multi_model_templates: list[str] = Query([]),
    min_multi_model_hits: int = Query(2),
    confluence_action_filter: str = Query("ALL"),
    strategy_profile: str = Query(""),
    universe: str = Query("full_market"),
    market: str = Query("ALL"),
    min_trend_score: int = Query(60),
    action_filter: str = Query("ALL"),
    min_volume_ratio: float = Query(0.0),
    min_listing_days: int = Query(365),
    pe_min: float = Query(0.0),
    pe_max: float = Query(30.0),
    min_roe_avg_3y: float = Query(12.0),
    min_net_profit_yoy: float = Query(20.0),
    min_revenue_yoy: float = Query(0.0),
    max_debt_to_assets: float = Query(100.0),
    min_dividend_yield: float = Query(0.0),
    exclude_bottom_market_cap_pct: float = Query(10.0),
    recent_snapshot_runs: int = Query(0),
    min_snapshot_hits: int = Query(0),
    model_signal_filter: str = Query("ALL"),
    min_model_signal_strength: float = Query(0.0),
    execution_tag_filter: str = Query("ALL"),
    exclude_execution_tag_filter: str = Query("ALL"),
    sort_by: str = Query("default"),
    sort_order: str = Query("desc"),
    min_hit_probability: float | None = Query(None),
    probability_calibration: str | None = Query(None),
    model_reliability_weights: str | None = Query(None),
    show_evaluation: int = Query(0),
    show_details: int = Query(0),
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/screeners")
    lang = resolve_request_lang(request)

    service = ScreenerService()
    saved_presets = _load_saved_presets(db)
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)
    current_params = _current_params(
        lang=lang,
        model_template=model_template,
        multi_model_templates=multi_model_templates,
        min_multi_model_hits=min_multi_model_hits,
        confluence_action_filter=confluence_action_filter,
        strategy_profile=strategy_profile,
        universe=universe,
        market=market,
        min_trend_score=min_trend_score,
        action_filter=action_filter,
        min_volume_ratio=min_volume_ratio,
        min_listing_days=min_listing_days,
        pe_min=pe_min,
        pe_max=pe_max,
        min_roe_avg_3y=min_roe_avg_3y,
        min_net_profit_yoy=min_net_profit_yoy,
        min_revenue_yoy=min_revenue_yoy,
        max_debt_to_assets=max_debt_to_assets,
        min_dividend_yield=min_dividend_yield,
        exclude_bottom_market_cap_pct=exclude_bottom_market_cap_pct,
        recent_snapshot_runs=recent_snapshot_runs,
        min_snapshot_hits=min_snapshot_hits,
        model_signal_filter=model_signal_filter,
        min_model_signal_strength=min_model_signal_strength,
        execution_tag_filter=execution_tag_filter,
        exclude_execution_tag_filter=exclude_execution_tag_filter,
        sort_by=sort_by,
        sort_order=sort_order,
        min_hit_probability=min_hit_probability,
        probability_calibration=probability_calibration,
        model_reliability_weights=model_reliability_weights,
    )
    should_execute = _should_execute_screen(request)
    normalized_current_params = _normalize_screen_params(current_params)
    multi_templates_active = normalized_current_params.get("multi_model_templates") or []
    multi_screen_meta = {"available_templates": [], "missing_templates": []}
    if should_execute:
        results, final_meta = _build_final_results(
            service,
            current_params,
            watchlist_state_map=watchlist_map,
        )
        snapshot_ready = final_meta["snapshot_ready"]
        multi_screen_meta = final_meta["multi_screen_meta"]
    else:
        results = []
        snapshot_ready = True
        final_meta = {
            "snapshot_ready": True,
            "total_count": 0,
            "returned_count": 0,
            "limit": 500,
            "truncated": False,
            "multi_screen_meta": multi_screen_meta,
        }
    if results and (model_template == "lightgbm_top_picks" or "lightgbm_top_picks" in multi_templates_active):
        _annotate_lightgbm_results(
            results,
            selected_market=market,
            lang=lang,
            force_apply=(model_template == "lightgbm_top_picks"),
        )
        if len(multi_templates_active) >= 2 and sort_by in {"default", "confluence_rank"}:
            results = _rerank_with_lightgbm_tactical_signal(
                results,
                confluence_action_filter=confluence_action_filter,
            )
    if results:
        annotate_rows_with_kronos(results, db=db)
        if sort_by == "kronos_score":
            results = sorted(
                results,
                key=lambda item: _kronos_sort_value(item),
                reverse=(sort_order != "asc"),
            )
    total_results = final_meta["total_count"]
    results_truncated = bool(final_meta["truncated"])
    result_limit = int(final_meta["limit"])
    detail_rows_enabled = bool(int(show_details or 0))
    visible_results = results[:60]
    row_summary = summarize_screener_rows(visible_results)
    tagged_names = row_summary["tagged_names"]
    risk_examples = row_summary["risk_examples"]
    risk_top_tags = row_summary["risk_top_tags"]
    status_counts = row_summary["status_counts"]
    try:
        recommendation_regression = load_or_build_recommendation_regression(db=db)
    except Exception:
        recommendation_regression = {}
    regression_guidance = summarize_recommendation_regression(recommendation_regression, lang=lang)
    regression_discipline_html = render_screener_regression_discipline(
        lang=lang,
        recommendation_regression=recommendation_regression,
        regression_guidance=regression_guidance,
        status_counts=status_counts,
        visible_count=len(visible_results),
    )
    quality_profile_status_html = ""
    if str(normalized_current_params.get("strategy_profile") or "") == "quality_confluence_v1":
        market_context = load_market_context_snapshot(db, market="CN")
        quality_profile_status_html = render_quality_profile_status(
            lang=lang,
            regime=market_context.get("regime"),
            breadth=market_context.get("breadth_pct"),
            candidate_count=len(visible_results),
        )
    run_receipt_html = (
        render_screener_run_receipt(
            build_screener_run_receipt(
                params=current_params,
                result_count=len(results),
                snapshot_ready=snapshot_ready,
                multi_templates_active=multi_templates_active,
                multi_screen_meta=multi_screen_meta,
                snapshot_loader=_load_screener_snapshot_record,
            ),
            lang=lang,
        )
        if should_execute
        else ""
    )
    guidance_summary = summarize_model_selection_guidance(
        load_model_selection_guidance_snapshot(
            db,
            market=market if market in {"CN", "US", "ALL"} else "CN",
            allow_fallback=True,
        ),
        lang=lang,
    )
    from app.api.presentation.screener_main import render_main_screener_view

    return render_main_screener_view(
        action_filter=action_filter,
        confluence_action_filter=confluence_action_filter,
        current_params=current_params,
        detail_rows_enabled=detail_rows_enabled,
        exclude_bottom_market_cap_pct=exclude_bottom_market_cap_pct,
        exclude_execution_tag_filter=exclude_execution_tag_filter,
        execution_tag_filter=execution_tag_filter,
        guidance_summary=guidance_summary,
        lang=lang,
        market=market,
        max_debt_to_assets=max_debt_to_assets,
        message=message,
        min_dividend_yield=min_dividend_yield,
        min_listing_days=min_listing_days,
        min_model_signal_strength=min_model_signal_strength,
        min_multi_model_hits=min_multi_model_hits,
        min_net_profit_yoy=min_net_profit_yoy,
        min_revenue_yoy=min_revenue_yoy,
        min_roe_avg_3y=min_roe_avg_3y,
        min_snapshot_hits=min_snapshot_hits,
        min_trend_score=min_trend_score,
        min_volume_ratio=min_volume_ratio,
        model_signal_filter=model_signal_filter,
        model_template=model_template,
        multi_screen_meta=multi_screen_meta,
        multi_templates_active=multi_templates_active,
        pe_max=pe_max,
        pe_min=pe_min,
        quality_profile_status_html=quality_profile_status_html,
        recent_snapshot_runs=recent_snapshot_runs,
        regression_discipline_html=regression_discipline_html,
        results=results,
        results_truncated=results_truncated,
        result_limit=result_limit,
        risk_examples=risk_examples,
        risk_top_tags=risk_top_tags,
        run_receipt_html=run_receipt_html,
        saved_presets=saved_presets,
        should_execute=should_execute,
        show_evaluation=show_evaluation,
        snapshot_ready=snapshot_ready,
        sort_by=sort_by,
        sort_order=sort_order,
        tagged_names=tagged_names,
        total_results=total_results,
        universe=universe,
        visible_results=visible_results,
        watchlist_map=watchlist_map,
    )


@router.post("/save")
def save_screener_preset(
    request: Request,
    preset_name: str = Form(...),
    lang: str = Form("en"),
    model_template: str = Form("technical_momentum"),
    multi_model_templates: list[str] = Form([]),
    min_multi_model_hits: int = Form(2),
    confluence_action_filter: str = Form("ALL"),
    strategy_profile: str = Form(""),
    universe: str = Form("watchlist"),
    market: str = Form("ALL"),
    min_trend_score: int = Form(60),
    action_filter: str = Form("ALL"),
    min_volume_ratio: float = Form(0.0),
    min_listing_days: int = Form(365),
    pe_min: float = Form(0.0),
    pe_max: float = Form(30.0),
    min_roe_avg_3y: float = Form(12.0),
    min_net_profit_yoy: float = Form(20.0),
    min_revenue_yoy: float = Form(0.0),
    max_debt_to_assets: float = Form(100.0),
    min_dividend_yield: float = Form(0.0),
    exclude_bottom_market_cap_pct: float = Form(10.0),
    recent_snapshot_runs: int = Form(0),
    min_snapshot_hits: int = Form(0),
    model_signal_filter: str = Form("ALL"),
    min_model_signal_strength: float = Form(0.0),
    execution_tag_filter: str = Form("ALL"),
    exclude_execution_tag_filter: str = Form("ALL"),
    sort_by: str = Form("default"),
    sort_order: str = Form("desc"),
    min_hit_probability: float | None = Form(None),
    probability_calibration: str | None = Form(None),
    model_reliability_weights: str | None = Form(None),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners")
    params = _current_params(
        lang=lang,
        model_template=model_template,
        multi_model_templates=multi_model_templates,
        min_multi_model_hits=min_multi_model_hits,
        confluence_action_filter=confluence_action_filter,
        strategy_profile=strategy_profile,
        universe=universe,
        market=market,
        min_trend_score=min_trend_score,
        action_filter=action_filter,
        min_volume_ratio=min_volume_ratio,
        min_listing_days=min_listing_days,
        pe_min=pe_min,
        pe_max=pe_max,
        min_roe_avg_3y=min_roe_avg_3y,
        min_net_profit_yoy=min_net_profit_yoy,
        min_revenue_yoy=min_revenue_yoy,
        max_debt_to_assets=max_debt_to_assets,
        min_dividend_yield=min_dividend_yield,
        exclude_bottom_market_cap_pct=exclude_bottom_market_cap_pct,
        recent_snapshot_runs=recent_snapshot_runs,
        min_snapshot_hits=min_snapshot_hits,
        model_signal_filter=model_signal_filter,
        min_model_signal_strength=min_model_signal_strength,
        execution_tag_filter=execution_tag_filter,
        exclude_execution_tag_filter=exclude_execution_tag_filter,
        sort_by=sort_by,
        sort_order=sort_order,
        min_hit_probability=min_hit_probability,
        probability_calibration=probability_calibration,
        model_reliability_weights=model_reliability_weights,
    )
    presets = _load_saved_presets(db)
    clean_name = preset_name.strip()
    filtered = [preset for preset in presets if preset.get("name") != clean_name]
    filtered.insert(0, {"name": clean_name, "params": params, "updated_at": app_now_iso()})
    _save_saved_presets(db, filtered[:20])
    return RedirectResponse(
        url=f"{_build_screen_query(params)}&message={urlencode({'m': f'Saved strategy: {clean_name}'})[2:]}",
        status_code=303,
    )


@router.post("/rename")
def rename_screener_preset(
    request: Request,
    preset_name: str = Form(...),
    new_name: str = Form(...),
    lang: str = Form("en"),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners")
    old_name = preset_name.strip()
    clean_name = new_name.strip()
    if not old_name or not clean_name:
        return _redirect_with_message("Strategy name cannot be empty.", lang=lang)
    presets = _load_saved_presets(db)
    renamed: list[dict] = []
    found = False
    for preset in presets:
        current_name = str(preset.get("name") or "").strip()
        if current_name == old_name:
            renamed.append({**preset, "name": clean_name, "updated_at": app_now_iso()})
            found = True
            continue
        if current_name == clean_name:
            continue
        renamed.append(preset)
    if found:
        _save_saved_presets(db, renamed[:20])
        return _redirect_with_message(f"Renamed strategy: {clean_name}", lang=lang)
    return _redirect_with_message(f"Strategy not found: {old_name}", lang=lang)


@router.post("/delete")
def delete_screener_preset(
    request: Request,
    preset_name: str = Form(...),
    lang: str = Form("en"),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners")
    clean_name = preset_name.strip()
    presets = _load_saved_presets(db)
    filtered = [preset for preset in presets if preset.get("name") != clean_name]
    _save_saved_presets(db, filtered)
    return _redirect_with_message(f"Deleted strategy: {clean_name}", lang=lang)


@router.get("/export")
def export_screener_csv(
    request: Request,
    lang: str = Query("en"),
    model_template: str = Query("technical_momentum"),
    multi_model_templates: list[str] = Query([]),
    min_multi_model_hits: int = Query(2),
    confluence_action_filter: str = Query("ALL"),
    strategy_profile: str = Query(""),
    universe: str = Query("watchlist"),
    market: str = Query("ALL"),
    min_trend_score: int = Query(60),
    action_filter: str = Query("ALL"),
    min_volume_ratio: float = Query(0.0),
    min_listing_days: int = Query(365),
    pe_min: float = Query(0.0),
    pe_max: float = Query(30.0),
    min_roe_avg_3y: float = Query(12.0),
    min_net_profit_yoy: float = Query(20.0),
    min_revenue_yoy: float = Query(0.0),
    max_debt_to_assets: float = Query(100.0),
    min_dividend_yield: float = Query(0.0),
    exclude_bottom_market_cap_pct: float = Query(10.0),
    recent_snapshot_runs: int = Query(0),
    min_snapshot_hits: int = Query(0),
    model_signal_filter: str = Query("ALL"),
    min_model_signal_strength: float = Query(0.0),
    execution_tag_filter: str = Query("ALL"),
    exclude_execution_tag_filter: str = Query("ALL"),
    sort_by: str = Query("default"),
    sort_order: str = Query("desc"),
    min_hit_probability: float | None = Query(None),
    probability_calibration: str | None = Query(None),
    model_reliability_weights: str | None = Query(None),
    db: Session = Depends(get_db_session),
) -> Response:
    if not is_authenticated(request):
        return login_redirect("/screeners")
    params = _current_params(
        lang=lang,
        model_template=model_template,
        multi_model_templates=multi_model_templates,
        min_multi_model_hits=min_multi_model_hits,
        confluence_action_filter=confluence_action_filter,
        strategy_profile=strategy_profile,
        universe=universe,
        market=market,
        min_trend_score=min_trend_score,
        action_filter=action_filter,
        min_volume_ratio=min_volume_ratio,
        min_listing_days=min_listing_days,
        pe_min=pe_min,
        pe_max=pe_max,
        min_roe_avg_3y=min_roe_avg_3y,
        min_net_profit_yoy=min_net_profit_yoy,
        min_revenue_yoy=min_revenue_yoy,
        max_debt_to_assets=max_debt_to_assets,
        min_dividend_yield=min_dividend_yield,
        exclude_bottom_market_cap_pct=exclude_bottom_market_cap_pct,
        recent_snapshot_runs=recent_snapshot_runs,
        min_snapshot_hits=min_snapshot_hits,
        model_signal_filter=model_signal_filter,
        min_model_signal_strength=min_model_signal_strength,
        execution_tag_filter=execution_tag_filter,
        exclude_execution_tag_filter=exclude_execution_tag_filter,
        sort_by=sort_by,
        sort_order=sort_order,
        min_hit_probability=min_hit_probability,
        probability_calibration=probability_calibration,
        model_reliability_weights=model_reliability_weights,
    )
    if not _screen_snapshot_ready(ScreenerService(), params):
        return RedirectResponse(
            url=f"{_build_screen_query(params)}&message={urlencode({'m': _lang_text(lang, 'snapshot_pending_export')})[2:]}",
            status_code=303,
        )
    watchlist_repo = WatchlistRepository(db)
    watchlist_map = watchlist_repo.list_ticker_map(watchlist_repo.get_or_create_default().id)
    results, final_meta = _build_final_results(
        ScreenerService(),
        params,
        watchlist_state_map=watchlist_map,
    )
    buffer = StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=[
            "ticker",
            "name",
            "market",
            "model_hit_count",
            "matched_model_templates",
            "weighted_score",
            "weighted_score_normalized",
            "expected_hit_probability",
            "calibration_status",
            "weight_source",
            "trend_score",
            "action_label",
            "latest_close",
            "model_signal_label",
            "model_signal_strength",
            "model_conviction_bucket",
            "model_position_size_hint",
            "model_entry_style",
            "model_execution_tags",
            "model_percentile",
            "model_horizon_days",
            "model_reward_risk_ratio",
            "model_expected_drawdown_20d",
            "momentum_5",
            "momentum_20",
            "volume_ratio",
            "pe_ttm",
            "roe_avg_3y",
            "net_profit_yoy",
            "revenue_yoy",
            "dividend_yield",
            "debt_to_assets",
            "snapshot_hits",
            "selection_reason",
        ],
    )
    writer.writeheader()
    for item in results:
        row = {key: item.get(key) for key in writer.fieldnames}
        row["model_execution_tags"] = ";".join(item.get("model_execution_tags") or [])
        row["matched_model_templates"] = ";".join(item.get("matched_model_templates") or [])
        writer.writerow(row)
    filename = (
        f"multi_model_{len(_normalize_multi_model_templates(multi_model_templates))}_screener.csv"
        if len(_normalize_multi_model_templates(multi_model_templates)) >= 2
        else f"{model_template}_screener.csv"
    )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "X-Screener-Total-Count": str(final_meta["total_count"]),
            "X-Screener-Returned-Count": str(final_meta["returned_count"]),
            "X-Screener-Truncated": "true" if final_meta["truncated"] else "false",
        },
    )


@router.post("/add-to-watchlist")
def add_screener_result_to_watchlist(
    request: Request,
    ticker: str = Form(...),
    name: str | None = Form(None),
    symbol_market: str | None = Form(None),
    lang: str = Form("en"),
    model_template: str = Form("technical_momentum"),
    multi_model_templates: list[str] = Form([]),
    min_multi_model_hits: int = Form(2),
    confluence_action_filter: str = Form("ALL"),
    strategy_profile: str = Form(""),
    universe: str = Form("watchlist"),
    market: str = Form("ALL"),
    min_trend_score: int = Form(60),
    action_filter: str = Form("ALL"),
    min_volume_ratio: float = Form(0.0),
    min_listing_days: int = Form(365),
    pe_min: float = Form(0.0),
    pe_max: float = Form(30.0),
    min_roe_avg_3y: float = Form(12.0),
    min_net_profit_yoy: float = Form(20.0),
    min_revenue_yoy: float = Form(0.0),
    max_debt_to_assets: float = Form(100.0),
    min_dividend_yield: float = Form(0.0),
    exclude_bottom_market_cap_pct: float = Form(10.0),
    recent_snapshot_runs: int = Form(0),
    min_snapshot_hits: int = Form(0),
    model_signal_filter: str = Form("ALL"),
    min_model_signal_strength: float = Form(0.0),
    execution_tag_filter: str = Form("ALL"),
    exclude_execution_tag_filter: str = Form("ALL"),
    sort_by: str = Form("default"),
    sort_order: str = Form("desc"),
    min_hit_probability: float | None = Form(None),
    probability_calibration: str | None = Form(None),
    model_reliability_weights: str | None = Form(None),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners")
    symbol_repo = SymbolRepository(db)
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    params = _current_params(
        lang=lang,
        model_template=model_template,
        multi_model_templates=multi_model_templates,
        min_multi_model_hits=min_multi_model_hits,
        confluence_action_filter=confluence_action_filter,
        strategy_profile=strategy_profile,
        universe=universe,
        market=market,
        min_trend_score=min_trend_score,
        action_filter=action_filter,
        min_volume_ratio=min_volume_ratio,
        min_listing_days=min_listing_days,
        pe_min=pe_min,
        pe_max=pe_max,
        min_roe_avg_3y=min_roe_avg_3y,
        min_net_profit_yoy=min_net_profit_yoy,
        min_revenue_yoy=min_revenue_yoy,
        max_debt_to_assets=max_debt_to_assets,
        min_dividend_yield=min_dividend_yield,
        exclude_bottom_market_cap_pct=exclude_bottom_market_cap_pct,
        recent_snapshot_runs=recent_snapshot_runs,
        min_snapshot_hits=min_snapshot_hits,
        model_signal_filter=model_signal_filter,
        min_model_signal_strength=min_model_signal_strength,
        execution_tag_filter=execution_tag_filter,
        exclude_execution_tag_filter=exclude_execution_tag_filter,
        sort_by=sort_by,
        sort_order=sort_order,
        min_hit_probability=min_hit_probability,
        probability_calibration=probability_calibration,
        model_reliability_weights=model_reliability_weights,
    )
    normalized_ticker = str(ticker).strip().upper()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)
    results, final_meta = _build_final_results(
        ScreenerService(),
        params,
        watchlist_state_map=watchlist_map,
    )
    if final_meta["snapshot_ready"]:
        allowed = {str(item.get("ticker") or "").strip().upper() for item in results}
        # An empty result set carries no membership evidence: the page renders
        # no rows either, so refusing the explicit action would block a ticker
        # the user picked from another view.  This also keeps the behaviour of a
        # parameter set that has no prepared snapshot at all.
        if allowed and normalized_ticker not in allowed:
            return RedirectResponse(
                url=(
                    f"{_build_screen_query(params)}&message="
                    f"{urlencode({'m': f'{normalized_ticker} is not in the current screener results for these parameters'})[2:]}"
                ),
                status_code=303,
            )
    symbol = symbol_repo.get_or_create_symbol(
        SymbolCreate(
            ticker=ticker,
            name=name,
            market=symbol_market,
        )
    )
    watchlist_repo.add_symbol(watchlist.id, symbol.id)
    refresh_workspace_snapshots(db)
    return RedirectResponse(
        url=f"{_build_screen_query(params)}&message={urlencode({'m': f'Added {ticker} to watchlist · dashboard refreshed'})[2:]}",
        status_code=303,
    )


@router.post("/add-all-to-watchlist")
def add_all_screener_results_to_watchlist(
    request: Request,
    lang: str = Form("en"),
    model_template: str = Form("technical_momentum"),
    multi_model_templates: list[str] = Form([]),
    min_multi_model_hits: int = Form(2),
    confluence_action_filter: str = Form("ALL"),
    strategy_profile: str = Form(""),
    universe: str = Form("watchlist"),
    market: str = Form("ALL"),
    min_trend_score: int = Form(60),
    action_filter: str = Form("ALL"),
    min_volume_ratio: float = Form(0.0),
    min_listing_days: int = Form(365),
    pe_min: float = Form(0.0),
    pe_max: float = Form(30.0),
    min_roe_avg_3y: float = Form(12.0),
    min_net_profit_yoy: float = Form(20.0),
    min_revenue_yoy: float = Form(0.0),
    max_debt_to_assets: float = Form(100.0),
    min_dividend_yield: float = Form(0.0),
    exclude_bottom_market_cap_pct: float = Form(10.0),
    recent_snapshot_runs: int = Form(0),
    min_snapshot_hits: int = Form(0),
    model_signal_filter: str = Form("ALL"),
    min_model_signal_strength: float = Form(0.0),
    execution_tag_filter: str = Form("ALL"),
    exclude_execution_tag_filter: str = Form("ALL"),
    sort_by: str = Form("default"),
    sort_order: str = Form("desc"),
    min_hit_probability: float | None = Form(None),
    probability_calibration: str | None = Form(None),
    model_reliability_weights: str | None = Form(None),
    bulk_top_n: int = Form(0),
    auto_enable_sync: str | None = Form(None),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners")
    params = _current_params(
        lang=lang,
        model_template=model_template,
        multi_model_templates=multi_model_templates,
        min_multi_model_hits=min_multi_model_hits,
        confluence_action_filter=confluence_action_filter,
        strategy_profile=strategy_profile,
        universe=universe,
        market=market,
        min_trend_score=min_trend_score,
        action_filter=action_filter,
        min_volume_ratio=min_volume_ratio,
        min_listing_days=min_listing_days,
        pe_min=pe_min,
        pe_max=pe_max,
        min_roe_avg_3y=min_roe_avg_3y,
        min_net_profit_yoy=min_net_profit_yoy,
        min_revenue_yoy=min_revenue_yoy,
        max_debt_to_assets=max_debt_to_assets,
        min_dividend_yield=min_dividend_yield,
        exclude_bottom_market_cap_pct=exclude_bottom_market_cap_pct,
        recent_snapshot_runs=recent_snapshot_runs,
        min_snapshot_hits=min_snapshot_hits,
        model_signal_filter=model_signal_filter,
        min_model_signal_strength=min_model_signal_strength,
        execution_tag_filter=execution_tag_filter,
        exclude_execution_tag_filter=exclude_execution_tag_filter,
        sort_by=sort_by,
        sort_order=sort_order,
        min_hit_probability=min_hit_probability,
        probability_calibration=probability_calibration,
        model_reliability_weights=model_reliability_weights,
    )
    if not _screen_snapshot_ready(ScreenerService(), params):
        return RedirectResponse(
            url=f"{_build_screen_query(params)}&message={urlencode({'m': _snapshot_pending_message(lang)})[2:]}",
            status_code=303,
        )
    added, already_in_watchlist, sync_enabled_count = _add_screen_results_to_watchlist(
        db=db,
        params=params,
        top_n=bulk_top_n,
        auto_enable_sync=auto_enable_sync == "1",
    )
    refresh_workspace_snapshots(db)
    if added:
        message = f"Added {added} screener results to watchlist · dashboard refreshed"
    elif already_in_watchlist:
        message = "All matching stocks are already in your watchlist · dashboard refreshed"
    else:
        message = "No matching stocks to add"
    if sync_enabled_count:
        message += f" · Sync enabled for {sync_enabled_count}"
    return RedirectResponse(
        url=f"{_build_screen_query(params)}&message={urlencode({'m': message})[2:]}",
        status_code=303,
    )


@router.post("/sync-symbol")
def sync_screener_symbol(
    request: Request,
    ticker: str = Form(...),
    item_id: int | None = Form(None),
    lang: str = Form("en"),
    model_template: str = Form("technical_momentum"),
    multi_model_templates: list[str] = Form([]),
    min_multi_model_hits: int = Form(2),
    confluence_action_filter: str = Form("ALL"),
    strategy_profile: str = Form(""),
    universe: str = Form("watchlist"),
    market: str = Form("ALL"),
    min_trend_score: int = Form(60),
    action_filter: str = Form("ALL"),
    min_volume_ratio: float = Form(0.0),
    min_listing_days: int = Form(365),
    pe_min: float = Form(0.0),
    pe_max: float = Form(30.0),
    min_roe_avg_3y: float = Form(12.0),
    min_net_profit_yoy: float = Form(20.0),
    min_revenue_yoy: float = Form(0.0),
    max_debt_to_assets: float = Form(100.0),
    min_dividend_yield: float = Form(0.0),
    exclude_bottom_market_cap_pct: float = Form(10.0),
    recent_snapshot_runs: int = Form(0),
    min_snapshot_hits: int = Form(0),
    model_signal_filter: str = Form("ALL"),
    min_model_signal_strength: float = Form(0.0),
    execution_tag_filter: str = Form("ALL"),
    exclude_execution_tag_filter: str = Form("ALL"),
    sort_by: str = Form("default"),
    sort_order: str = Form("desc"),
    min_hit_probability: float | None = Form(None),
    probability_calibration: str | None = Form(None),
    model_reliability_weights: str | None = Form(None),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners")
    watchlist_repo = WatchlistRepository(db)
    if item_id is not None:
        watchlist_repo.set_sync_enabled(item_id, True)
    results = sync_market_data(tickers=[ticker], start_date="2025-01-01", provider="auto")
    result = results[0] if results else None
    if result and result["status"] == "success":
        message = f"Synced {ticker} with {result['rows']} rows"
    elif result:
        message = f"Sync failed for {ticker}: {result.get('message', 'Unknown error')}"
    else:
        message = f"Sync did not return a result for {ticker}"
    params = _current_params(
        lang=lang,
        model_template=model_template,
        multi_model_templates=multi_model_templates,
        min_multi_model_hits=min_multi_model_hits,
        confluence_action_filter=confluence_action_filter,
        strategy_profile=strategy_profile,
        universe=universe,
        market=market,
        min_trend_score=min_trend_score,
        action_filter=action_filter,
        min_volume_ratio=min_volume_ratio,
        min_listing_days=min_listing_days,
        pe_min=pe_min,
        pe_max=pe_max,
        min_roe_avg_3y=min_roe_avg_3y,
        min_net_profit_yoy=min_net_profit_yoy,
        min_revenue_yoy=min_revenue_yoy,
        max_debt_to_assets=max_debt_to_assets,
        min_dividend_yield=min_dividend_yield,
        exclude_bottom_market_cap_pct=exclude_bottom_market_cap_pct,
        recent_snapshot_runs=recent_snapshot_runs,
        min_snapshot_hits=min_snapshot_hits,
        model_signal_filter=model_signal_filter,
        min_model_signal_strength=min_model_signal_strength,
        execution_tag_filter=execution_tag_filter,
        exclude_execution_tag_filter=exclude_execution_tag_filter,
        sort_by=sort_by,
        sort_order=sort_order,
        min_hit_probability=min_hit_probability,
        probability_calibration=probability_calibration,
        model_reliability_weights=model_reliability_weights,
    )
    return RedirectResponse(
        url=f"{_build_screen_query(params)}&message={urlencode({'m': message})[2:]}",
        status_code=303,
    )


@router.post("/sync-top-results")
def sync_top_screener_results(
    request: Request,
    lang: str = Form("en"),
    model_template: str = Form("technical_momentum"),
    multi_model_templates: list[str] = Form([]),
    min_multi_model_hits: int = Form(2),
    confluence_action_filter: str = Form("ALL"),
    strategy_profile: str = Form(""),
    universe: str = Form("watchlist"),
    market: str = Form("ALL"),
    min_trend_score: int = Form(60),
    action_filter: str = Form("ALL"),
    min_volume_ratio: float = Form(0.0),
    min_listing_days: int = Form(365),
    pe_min: float = Form(0.0),
    pe_max: float = Form(30.0),
    min_roe_avg_3y: float = Form(12.0),
    min_net_profit_yoy: float = Form(20.0),
    min_revenue_yoy: float = Form(0.0),
    max_debt_to_assets: float = Form(100.0),
    min_dividend_yield: float = Form(0.0),
    exclude_bottom_market_cap_pct: float = Form(10.0),
    recent_snapshot_runs: int = Form(0),
    min_snapshot_hits: int = Form(0),
    model_signal_filter: str = Form("ALL"),
    min_model_signal_strength: float = Form(0.0),
    execution_tag_filter: str = Form("ALL"),
    exclude_execution_tag_filter: str = Form("ALL"),
    sort_by: str = Form("default"),
    sort_order: str = Form("desc"),
    min_hit_probability: float | None = Form(None),
    probability_calibration: str | None = Form(None),
    model_reliability_weights: str | None = Form(None),
    sync_top_n: int = Form(5),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners")
    params = _current_params(
        lang=lang,
        model_template=model_template,
        multi_model_templates=multi_model_templates,
        min_multi_model_hits=min_multi_model_hits,
        confluence_action_filter=confluence_action_filter,
        strategy_profile=strategy_profile,
        universe=universe,
        market=market,
        min_trend_score=min_trend_score,
        action_filter=action_filter,
        min_volume_ratio=min_volume_ratio,
        min_listing_days=min_listing_days,
        pe_min=pe_min,
        pe_max=pe_max,
        min_roe_avg_3y=min_roe_avg_3y,
        min_net_profit_yoy=min_net_profit_yoy,
        min_revenue_yoy=min_revenue_yoy,
        max_debt_to_assets=max_debt_to_assets,
        min_dividend_yield=min_dividend_yield,
        exclude_bottom_market_cap_pct=exclude_bottom_market_cap_pct,
        recent_snapshot_runs=recent_snapshot_runs,
        min_snapshot_hits=min_snapshot_hits,
        model_signal_filter=model_signal_filter,
        min_model_signal_strength=min_model_signal_strength,
        execution_tag_filter=execution_tag_filter,
        exclude_execution_tag_filter=exclude_execution_tag_filter,
        sort_by=sort_by,
        sort_order=sort_order,
        min_hit_probability=min_hit_probability,
        probability_calibration=probability_calibration,
        model_reliability_weights=model_reliability_weights,
    )
    if not _screen_snapshot_ready(ScreenerService(), params):
        return RedirectResponse(
            url=f"{_build_screen_query(params)}&message={urlencode({'m': _snapshot_pending_message(lang)})[2:]}",
            status_code=303,
        )
    watchlist_repo = WatchlistRepository(db)
    watchlist_map = watchlist_repo.list_ticker_map(watchlist_repo.get_or_create_default().id)
    results, _final_meta = _build_final_results(
        ScreenerService(),
        params,
        watchlist_state_map=watchlist_map,
    )
    if universe == "watchlist":
        results = [item for item in results if item["ticker"] in watchlist_map]
    if sync_top_n > 0:
        results = results[:sync_top_n]
    tickers = [item["ticker"] for item in results]
    if not tickers:
        return _redirect_with_message("No screener results available to sync.", lang=lang)
    sync_results = sync_market_data(tickers=tickers, start_date="2025-01-01", provider="auto")
    success_count = sum(1 for item in sync_results if item["status"] == "success")
    return RedirectResponse(
        url=f"{_build_screen_query(params)}&message={urlencode({'m': f'Synced {success_count}/{len(sync_results)} screener results'})[2:]}",
        status_code=303,
    )


@router.post("/add-to-focus")
def add_all_screener_results_to_focus(
    request: Request,
    lang: str = Form("en"),
    model_template: str = Form("technical_momentum"),
    multi_model_templates: list[str] = Form([]),
    min_multi_model_hits: int = Form(2),
    confluence_action_filter: str = Form("ALL"),
    strategy_profile: str = Form(""),
    universe: str = Form("watchlist"),
    market: str = Form("ALL"),
    min_trend_score: int = Form(60),
    action_filter: str = Form("ALL"),
    min_volume_ratio: float = Form(0.0),
    min_listing_days: int = Form(365),
    pe_min: float = Form(0.0),
    pe_max: float = Form(30.0),
    min_roe_avg_3y: float = Form(12.0),
    min_net_profit_yoy: float = Form(20.0),
    min_revenue_yoy: float = Form(0.0),
    max_debt_to_assets: float = Form(100.0),
    min_dividend_yield: float = Form(0.0),
    exclude_bottom_market_cap_pct: float = Form(10.0),
    recent_snapshot_runs: int = Form(0),
    min_snapshot_hits: int = Form(0),
    model_signal_filter: str = Form("ALL"),
    min_model_signal_strength: float = Form(0.0),
    execution_tag_filter: str = Form("ALL"),
    exclude_execution_tag_filter: str = Form("ALL"),
    sort_by: str = Form("default"),
    sort_order: str = Form("desc"),
    min_hit_probability: float | None = Form(None),
    probability_calibration: str | None = Form(None),
    model_reliability_weights: str | None = Form(None),
    focus_top_n: int = Form(10),
    db: Session = Depends(get_db_session),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners")
    params = _current_params(
        lang=lang,
        model_template=model_template,
        multi_model_templates=multi_model_templates,
        min_multi_model_hits=min_multi_model_hits,
        confluence_action_filter=confluence_action_filter,
        strategy_profile=strategy_profile,
        universe=universe,
        market=market,
        min_trend_score=min_trend_score,
        action_filter=action_filter,
        min_volume_ratio=min_volume_ratio,
        min_listing_days=min_listing_days,
        pe_min=pe_min,
        pe_max=pe_max,
        min_roe_avg_3y=min_roe_avg_3y,
        min_net_profit_yoy=min_net_profit_yoy,
        min_revenue_yoy=min_revenue_yoy,
        max_debt_to_assets=max_debt_to_assets,
        min_dividend_yield=min_dividend_yield,
        exclude_bottom_market_cap_pct=exclude_bottom_market_cap_pct,
        recent_snapshot_runs=recent_snapshot_runs,
        min_snapshot_hits=min_snapshot_hits,
        model_signal_filter=model_signal_filter,
        min_model_signal_strength=min_model_signal_strength,
        execution_tag_filter=execution_tag_filter,
        exclude_execution_tag_filter=exclude_execution_tag_filter,
        sort_by=sort_by,
        sort_order=sort_order,
        min_hit_probability=min_hit_probability,
        probability_calibration=probability_calibration,
        model_reliability_weights=model_reliability_weights,
    )
    if not _screen_snapshot_ready(ScreenerService(), params):
        return RedirectResponse(
            url=f"{_build_screen_query(params)}&message={urlencode({'m': _snapshot_pending_message(lang)})[2:]}",
            status_code=303,
        )
    watchlist_repo = WatchlistRepository(db)
    watchlist_map = watchlist_repo.list_ticker_map(watchlist_repo.get_or_create_default().id)
    raw_results, _final_meta = _build_final_results(
        ScreenerService(),
        params,
        watchlist_state_map=watchlist_map,
    )
    focus_candidates, skipped_count = _focus_pool_trade_candidates(raw_results)
    result = add_to_today_focus_pool(focus_candidates, top_n=focus_top_n)
    message = (
        f"Added {result['added']} stock(s) to today focus pool; skipped {skipped_count} blocked/low-readiness name(s)."
        if lang == "en"
        else f"已将 {result['added']} 只股票加入今日重点盯盘池；已跳过 {skipped_count} 只阻断/低就绪度候选"
    )
    return RedirectResponse(
        url=f"{_build_screen_query(params)}&message={urlencode({'m': message})[2:]}",
        status_code=303,
    )


@router.get("/kronos-validation", response_class=HTMLResponse)
def kronos_validation_pool_page(
    request: Request,
    lang: str = Query("zh"),
    market: str = Query("ALL"),
    status: str = Query("ALL"),
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/screeners/kronos-validation")
    lang = resolve_request_lang(request)
    snapshot = load_latest_kronos_validation(db) or {}
    payload = snapshot.get("payload") if isinstance(snapshot, dict) else {}
    pool = prepare_kronos_validation_pool(
        payload if isinstance(payload, dict) else {},
        market=market,
        status=status,
    )
    return render_kronos_validation_pool_page(
        lang=lang,
        pool=pool,
        snapshot_created_at=snapshot.get("created_at") if isinstance(snapshot, dict) else None,
        nav_html=render_workspace_nav_html(lang=lang, active_key="screeners"),
    )

@router.get("/focus/today", response_class=HTMLResponse)
def today_focus_pool_page(request: Request, lang: str = Query("en"), db: Session = Depends(get_db_session)) -> str:
    if not is_authenticated(request):
        return login_redirect("/screeners/focus/today")

    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)
    items = _load_today_focus_items()
    rows: list[dict] = []
    for item in items:
        ticker = str(item.get("ticker") or "").upper()
        existing = watchlist_map.get(ticker)
        patterns = " / ".join(item.get("matched_patterns") or []) or "-"
        rows.append(
            {
                "ticker": ticker,
                "name": item.get("name") or ticker,
                "market": item.get("market") or "-",
                "patterns": patterns,
                "model_signal_label": item.get("model_signal_label") or "-",
                "model_signal_strength": item.get("model_signal_strength") or "-",
                "watchlist_html": _watchlist_summary(existing, lang) if existing else "-",
                "sync_badge_html": _sync_status_badge(existing, lang),
            }
        )
    return render_today_focus_page(
        lang=lang,
        rows=rows,
        nav_html=render_workspace_nav_html(lang=lang, active_key="screeners"),
    )


@router.get("/market-snapshot", response_class=HTMLResponse)
def market_snapshot_page(
    request: Request,
    lang: str = Query("en"),
    mode: str = Query("monitor"),
    market_filter: str = Query("CN"),
    message: str | None = Query(None),
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/screeners/market-snapshot")
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)
    view_mode, market_filter = normalize_market_snapshot_filters(
        mode=mode,
        market_filter=market_filter,
    )
    snapshot_type = market_snapshot_type(view_mode)
    market_snapshot = load_latest_workspace_snapshot(db, snapshot_type)
    payload = (market_snapshot or {}).get("payload") if isinstance(market_snapshot, dict) else None
    history = load_market_snapshot_history(db, snapshot_type=snapshot_type, limit=6)
    view = build_market_snapshot_view(
        payload if isinstance(payload, dict) else {},
        mode=view_mode,
        market_filter=market_filter,
        history=history,
    )
    sentiment = get_or_set(
        "screener_market_sentiment",
        json.dumps({"mode": view_mode, "market_filter": market_filter}, sort_keys=True, ensure_ascii=False),
        ttl_seconds=45.0,
        loader=lambda: build_market_sentiment_snapshot(boards=view["boards"]),
    )
    return render_market_snapshot_page(
        lang=lang,
        view=view,
        sentiment=sentiment,
        watchlist_map=watchlist_map,
        message=message,
        nav_html=render_workspace_nav_html(lang=lang, active_key="screeners"),
    )


@router.post("/market-snapshot/add-to-focus")
def add_market_snapshot_ticker_to_focus(
    request: Request,
    lang: str = Form("en"),
    mode: str = Form("monitor"),
    market_filter: str = Form("CN"),
    ticker: str = Form(...),
    name: str = Form(""),
    market: str = Form("CN"),
    selection_reason: str = Form(""),
    matched_patterns: str = Form(""),
) -> RedirectResponse:
    if not is_authenticated(request):
        return login_redirect("/screeners/market-snapshot")

    patterns = [item.strip() for item in str(matched_patterns or "").split("/") if item.strip()]
    add_to_today_focus_pool(
        [
            {
                "ticker": str(ticker).strip().upper(),
                "name": name or str(ticker).strip().upper(),
                "market": market or "CN",
                "selection_reason": selection_reason or "",
                "matched_patterns": patterns,
            }
        ]
    )
    message = _lang_text(lang, "added_to_focus_message").format(ticker=str(ticker).strip().upper())
    return RedirectResponse(
        url=f"/screeners/market-snapshot?{urlencode({'lang': lang, 'mode': mode, 'market_filter': market_filter, 'message': message})}",
        status_code=303,
    )
