"""Model-performance evaluation and winner-traceback routes."""

import json

from fastapi import APIRouter, Depends, Request

from fastapi.responses import HTMLResponse, Response

from sqlalchemy.orm import Session

from app.services.market_lake import load_lake_price_history  # noqa: F401 - patch target guarded by model-performance tests
from app.services.continuous_leaders import build_continuous_leader_view
from app.api.presentation.continuous_leaders_export import render_continuous_leaders_csv
from app.api.presentation.i18n import t
from app.api.presentation.dashboard_performance import render_model_performance_page, render_recent_run_rows
from app.api.presentation.dashboard_winner_traceback import render_winner_traceback_page
from app.services.model_winner_traceback import summarize_winner_traceback
from app.api.presentation.dashboard_performance_evaluations import (
    build_evaluation_fragments,
)
from app.api.presentation.dashboard_performance_guidance import (
    build_guidance_fragments,
)
from app.api.presentation.dashboard_performance_components import (
    render_aggregate_model_rows,
    render_detail_rows,
    render_grouped_return_rows,
    render_market_options,
    render_overview_cards,
    render_run_options,
    render_run_summary_cards,
    render_selected_run_identity,
    render_structured_evaluation_rows,
    render_training_diagnostic,
    render_watchlist_fragments,
)

from app.core.db import get_db_session

from app.services.ai_daily_report import (
    format_trade_gate_reason,
)

from app.services.auth import is_authenticated, login_redirect


from app.services.model_selection_guidance import (
    ACTION_BUCKET_LABELS,
    load_model_selection_guidance_snapshot,
    summarize_model_selection_guidance,
)

from app.services.model_evaluation import list_latest_model_evaluations

from app.services.repository import (
    ModelRunRepository,
    WatchlistRepository,
)


from app.services.template_evaluation import (
    build_lightgbm_evaluation,
    build_lightgbm_prediction_evaluation,
    build_next_tesla_evaluation,
    build_technical_momentum_evaluation,
    lightgbm_maturity,
    next_tesla_maturity,
    technical_momentum_maturity,
)

from app.services.ui_lang import resolve_request_lang

from app.services.workspace_nav import render_workspace_nav_html

from app.services.workspace_snapshots import (
    SNAPSHOT_CONTINUOUS_LEADERS,
    load_latest_workspace_snapshot,
)


from app.api.routes.dashboard._common import _clamp_lookback_runs, _load_home_summary, _reason_screen_link
from app.services.dashboard_insights import (
    _build_model_run_performance_summary,
    _build_recommendation_validation_summary,
    _build_watchlist_post_add_summary,
)
from app.services.model_performance_overview import (
    aggregate_model_run_performance,
    summarize_rows_by_dimension,
)
from app.services.model_performance_brief import build_model_performance_brief

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/model-performance", response_class=HTMLResponse)
def dashboard_model_performance(
    request: Request,
    run_id: int | None = None,
    market: str = "CN",
    top_n: int = 10,
    max_trade_dates: int = 20,
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/model-performance")
    lang = resolve_request_lang(request)
    nav_html = render_workspace_nav_html(lang=lang, active_key="model_eval")
    run_repo = ModelRunRepository(db)
    recent_runs = [item for item in run_repo.list_recent_runs(limit=8) if str(item.get("status") or "").lower() == "success"]
    aggregate_runs = recent_runs
    run_summaries: dict[int, dict | None] = {}

    def _load_run_summary(item: dict) -> dict | None:
        item_run_id = int(item["id"])
        if item_run_id not in run_summaries:
            run_summaries[item_run_id] = _build_model_run_performance_summary(
                db,
                run_id=item_run_id,
                top_n=max(1, int(top_n)),
                max_trade_dates=max(5, int(max_trade_dates)),
                market=market,
                allow_compute=False,
            )
        return run_summaries[item_run_id]

    aggregate_by_model = aggregate_model_run_performance(
        aggregate_runs,
        summary_loader=_load_run_summary,
    )
    selected_run_id = run_id or (recent_runs[0]["id"] if recent_runs else None)
    selected_run = None
    if selected_run_id is not None:
        selected_run = run_repo.get_run_by_id(int(selected_run_id))
    selected_summary = (
        _load_run_summary({"id": selected_run_id})
        if selected_run_id is not None
        else None
    )
    selected_run_config = {}
    selected_run_artifact = {}
    if selected_run is not None:
        if selected_run.config_json:
            try:
                selected_run_config = json.loads(selected_run.config_json)
            except json.JSONDecodeError:
                selected_run_config = {}
        artifact_path = str(selected_run.artifact_path or "").strip()
        if artifact_path:
            try:
                with open(artifact_path, "r", encoding="utf-8") as artifact_file:
                    selected_run_artifact = json.load(artifact_file)
            except (OSError, json.JSONDecodeError):
                selected_run_artifact = {}
    watchlist_summary = _build_watchlist_post_add_summary(
        db, market=market, allow_compute=False
    )
    # Template evaluations below use their own short-lived data sessions and
    # lake reads. Do not keep the request session in a transaction meanwhile.
    db.commit()
    next_tesla_eval = build_next_tesla_evaluation(
        market=market, lookback_snapshots=15, top_n=20, allow_compute=False
    )
    next_tesla_maturity_state = next_tesla_maturity(next_tesla_eval, lang=lang)
    technical_momentum_eval = build_technical_momentum_evaluation(
        market=market, lookback_snapshots=15, top_n=40, allow_compute=False
    )
    technical_momentum_maturity_state = technical_momentum_maturity(technical_momentum_eval, lang=lang)
    lightgbm_eval = build_lightgbm_evaluation(
        market=market, lookback_snapshots=15, top_n=40, allow_compute=False
    )
    lightgbm_maturity_state = lightgbm_maturity(lightgbm_eval, lang=lang)
    lightgbm_prediction_eval = build_lightgbm_prediction_evaluation(
        market=market, recent_runs=8, top_n=40, allow_compute=False
    )
    structured_evaluations = list_latest_model_evaluations(db, market=market, limit=12)
    db.commit()
    structured_evaluation_rows_html = render_structured_evaluation_rows(
        structured_evaluations, lang=lang
    )
    # Page rendering must consume the background snapshot; a missing/stale
    # snapshot is displayed as missing instead of being recomputed inline.
    selection_guidance = load_model_selection_guidance_snapshot(db, market=market, allow_fallback=False)
    db.commit()
    selection_guidance_summary = summarize_model_selection_guidance(selection_guidance, lang=lang)
    summary_cards_html = render_run_summary_cards(selected_summary, lang=lang)
    training_diagnostic_html = render_training_diagnostic(
        run_artifact=selected_run_artifact,
        run_config=selected_run_config,
        lang=lang,
    )
    run_options_html = render_run_options(
        recent_runs, selected_run_id=selected_run_id
    )
    market_options_html = render_market_options(market=market, lang=lang)
    reason_jump_html = "".join(
        _reason_screen_link(
            format_trade_gate_reason(reason, lang=lang),
            reason=reason,
            status=None,
            market=market,
            lang=lang,
            css_class="pill",
        )
        for reason in (
            "low_trade_readiness",
            "extended_after_sharp_move",
            "too_far_from_pullback_zone",
            "too_many_risk_flags",
            "missing_latest_price",
        )
    )
    validation_summary = _build_recommendation_validation_summary(
        db,
        market=market,
        lang=lang,
        selection_guidance=selection_guidance,
        selection_guidance_summary=selection_guidance_summary,
        report_limit=30,
        allow_compute=False,
    )
    guidance_ui = build_guidance_fragments(
        guidance=selection_guidance,
        guidance_summary=selection_guidance_summary,
        validation_summary=validation_summary,
        market=market,
        lang=lang,
    )
    recent_rows_html = render_recent_run_rows(
        [(item, _load_run_summary(item)) for item in recent_runs[:6]],
        lang=lang, market=market, top_n=top_n, max_trade_dates=max_trade_dates,
    )
    aggregate_rows_html = render_aggregate_model_rows(
        aggregate_by_model, lang=lang
    )
    evaluation_ui = build_evaluation_fragments(
        next_tesla=next_tesla_eval,
        next_tesla_maturity_state=next_tesla_maturity_state,
        technical=technical_momentum_eval,
        technical_maturity_state=technical_momentum_maturity_state,
        lightgbm=lightgbm_eval,
        lightgbm_maturity_state=lightgbm_maturity_state,
        lightgbm_prediction=lightgbm_prediction_eval,
        lang=lang,
    )
    overview_cards_html = render_overview_cards(
        build_model_performance_brief(
            next_tesla=next_tesla_eval,
            next_tesla_state=next_tesla_maturity_state,
            technical=technical_momentum_eval,
            technical_state=technical_momentum_maturity_state,
            lightgbm=lightgbm_eval,
            lightgbm_state=lightgbm_maturity_state,
            market=market,
            lang=lang,
        )
    )
    aggregate_rows_source = [
        row
        for item in aggregate_runs
        for row in ((_load_run_summary(item) or {}).get("rows") or [])
    ]
    aggregate_regime_rows_html = render_grouped_return_rows(
        summarize_rows_by_dimension(
            aggregate_rows_source,
            dimension="regime",
            missing_label=t(lang, "未标记", "Unlabeled"),
        ),
        lang=lang,
        empty_zh="最近成功 run 还没有足够的长期环境样本。",
        empty_en="Recent successful runs do not have enough long-horizon regime samples yet.",
    )
    aggregate_sector_rows_html = render_grouped_return_rows(
        summarize_rows_by_dimension(
            aggregate_rows_source,
            dimension="sector",
            missing_label=t(lang, "未分类", "Unclassified"),
            limit=20,
        ),
        lang=lang,
        empty_zh="最近成功 run 还没有足够的长期行业样本。",
        empty_en="Recent successful runs do not have enough long-horizon sector samples yet.",
    )
    detail_rows = (selected_summary or {}).get("rows") or []
    regime_rows_html = render_grouped_return_rows(
        summarize_rows_by_dimension(
            detail_rows,
            dimension="regime",
            missing_label=t(lang, "未标记", "Unlabeled"),
        ),
        lang=lang,
        empty_zh="当前 run 还没有可用的环境分层样本。",
        empty_en="No regime-sliced samples yet for this run.",
    )
    sector_rows_html = render_grouped_return_rows(
        summarize_rows_by_dimension(
            detail_rows,
            dimension="sector",
            missing_label=t(lang, "未分类", "Unclassified"),
            limit=20,
        ),
        lang=lang,
        empty_zh="当前 run 还没有可用的行业分层样本。",
        empty_en="No sector-sliced samples yet for this run.",
    )
    watchlist_current = (watchlist_summary or {}).get("current") or {}
    watchlist_cards_html, watchlist_rows_html = render_watchlist_fragments(
        watchlist_summary, lang=lang
    )
    detail_rows_html = render_detail_rows(selected_summary, lang=lang)
    selected_title, selected_subtitle = render_selected_run_identity(
        selected_summary, lang=lang
    )
    return render_model_performance_page(
        view={
            "lang": lang,
            "market": market,
            "top_n": top_n,
            "max_trade_dates": max_trade_dates,
            "nav_html": nav_html,
            "selected_title": selected_title,
            "selected_subtitle": selected_subtitle,
            "run_options_html": run_options_html,
            "market_options_html": market_options_html,
            "summary_cards_html": summary_cards_html,
            "training_diagnostic_html": training_diagnostic_html,
            "overview_cards_html": overview_cards_html,
            "reason_jump_html": reason_jump_html,
            "structured_evaluation_rows_html": structured_evaluation_rows_html,
            "guidance_ui": guidance_ui,
            "aggregate_rows_html": aggregate_rows_html,
            "evaluation_ui": evaluation_ui,
            "next_tesla_maturity_state": next_tesla_maturity_state,
            "technical_momentum_maturity_state": technical_momentum_maturity_state,
            "technical_momentum_eval": technical_momentum_eval,
            "lightgbm_maturity_state": lightgbm_maturity_state,
            "lightgbm_eval": lightgbm_eval,
            "aggregate_regime_rows_html": aggregate_regime_rows_html,
            "aggregate_sector_rows_html": aggregate_sector_rows_html,
            "regime_rows_html": regime_rows_html,
            "sector_rows_html": sector_rows_html,
            "watchlist_cards_html": watchlist_cards_html,
            "watchlist_current": watchlist_current,
            "watchlist_rows_html": watchlist_rows_html,
            "recent_rows_html": recent_rows_html,
            "detail_rows_html": detail_rows_html,
        }
    )


@router.get("/model-performance/winner-traceback", response_class=HTMLResponse)
def dashboard_model_winner_traceback(
    request: Request,
    market: str = "CN",
    min_hits: int = 0,
    db: Session = Depends(get_db_session),
) -> str:
    if not is_authenticated(request):
        return login_redirect("/dashboard/model-performance/winner-traceback")
    lang = resolve_request_lang(request)
    market_code = str(market or "CN").strip().upper()
    if market_code not in {"CN", "US", "ALL"}:
        market_code = "CN"
    min_hits = max(0, int(min_hits or 0))
    nav_html = render_workspace_nav_html(lang=lang, active_key="model_eval")
    guidance = load_model_selection_guidance_snapshot(db, market=market_code, allow_fallback=True)
    guidance_summary = summarize_model_selection_guidance(guidance, lang=lang)
    bucket_labels = {
        key: labels.get(lang, key) for key, labels in ACTION_BUCKET_LABELS.items()
    }
    summary = summarize_winner_traceback(
        list((guidance or {}).get("winner_attribution") or []),
        min_hits=min_hits,
        bucket_labels=bucket_labels,
    )
    return render_winner_traceback_page(
        summary=summary, market=market_code, lang=lang, min_hits=min_hits,
        nav_html=nav_html, snapshot_meta=guidance_summary.get("snapshot_meta") or {},
        bucket_labels=bucket_labels,
    )


@router.get("/continuous-leaders/export")
def dashboard_continuous_leaders_export(
    request: Request,
    lang: str = "en",
    lookback_runs: int = 5,
    continuous_sort_by: str = "hits",
    continuous_sort_order: str = "desc",
    continuous_market: str = "ALL",
    continuous_state: str = "ALL",
    continuous_signal: str = "ALL",
    min_signal_strength: int = 0,
    execution_tag_filter: str = "ALL",
    exclude_execution_tag_filter: str = "ALL",
    db: Session = Depends(get_db_session),
) -> Response:
    if not is_authenticated(request):
        return login_redirect("/dashboard/continuous-leaders")
    lookback_runs = _clamp_lookback_runs(lookback_runs)
    continuous_market = continuous_market.upper()
    continuous_state = continuous_state.upper()
    continuous_signal = continuous_signal.upper()
    execution_tag_filter = execution_tag_filter.strip()
    exclude_execution_tag_filter = exclude_execution_tag_filter.strip()
    summary = _load_home_summary(db, lookback_runs=lookback_runs)
    continuous_snapshot = load_latest_workspace_snapshot(db, SNAPSHOT_CONTINUOUS_LEADERS)
    continuous_rows_snapshot = ((continuous_snapshot or {}).get("payload") or {}).get("rows") if isinstance(continuous_snapshot, dict) else None
    watchlist_repo = WatchlistRepository(db)
    watchlist = watchlist_repo.get_or_create_default()
    watchlist_map = watchlist_repo.list_ticker_map(watchlist.id)
    view = build_continuous_leader_view(
        list(continuous_rows_snapshot or summary["market_context"].get("continuous_leaders", [])),
        watchlist_map,
        market=continuous_market, state=continuous_state, signal=continuous_signal,
        min_signal_strength=min_signal_strength,
        include_tags=execution_tag_filter, exclude_tags=exclude_execution_tag_filter,
        sort_by=continuous_sort_by, sort_order=continuous_sort_order,
    )
    filename = f"continuous_leaders_{lookback_runs}runs.csv"
    return Response(
        content=render_continuous_leaders_csv(view["rows"]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
