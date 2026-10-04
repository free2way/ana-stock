"""Dashboard routes package (split from the former dashboard.py)."""

import csv  # noqa: F401

import html  # noqa: F401

import json  # noqa: F401

import re  # noqa: F401

from collections import Counter  # noqa: F401

from io import StringIO  # noqa: F401

from urllib.parse import urlencode  # noqa: F401

from datetime import date, datetime, time, timedelta, timezone  # noqa: F401

from zoneinfo import ZoneInfo  # noqa: F401

from fastapi import APIRouter, Depends, Form, Request  # noqa: F401

from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response  # noqa: F401

from sqlalchemy import select  # noqa: F401

from sqlalchemy.orm import Session  # noqa: F401

from app.api.rendering import mini_trend_bars as _mini_trend_bars  # noqa: F401

from app.api.presentation.i18n import t  # noqa: F401

from app.api.presentation.styles_dashboard import (
    DASHBOARD_WORKSPACE_STYLE,
    DASHBOARD_DATA_SOURCES_STYLE,
    SIGNAL_KEY_STYLE,
    SORT_LINK_STYLE,
    MARKET_LABEL_STYLE,
    TEMPLATE_HREF_STYLE,
    BOARD_DETAIL_ROWS_STYLE,
    HEATMAP_METRIC_FOR_MARKET_LINK_STYLE,
    CONCEPT_TRACKER_SORT_LINK_STYLE,
    RISK_CARD_STYLE,
    LOAD_CN_SYNC_STATS_STYLE,
    DASHBOARD_OPS_MODELS_PAGE_STYLE,
    JOB_DETAIL_LINK_STYLE,
    DASHBOARD_OPS_HISTORY_PAGE_STYLE,
    DASHBOARD_OPS_JOB_DETAIL_STYLE,
    RESULT_SUMMARY_STYLE,
    DASHBOARD_WEEKLY_REVIEW_STYLE,
    PRICE_SOURCE_TEXT_STYLE,
    BUY_ZONE_TEXT_STYLE,
    AI_REPORT_NAME_CELL_STYLE,
    WINDOW_PILL_STYLE,
    REPORT_POOL_TABLE_ROWS_STYLE,
    DASHBOARD_AI_DAILY_REPORT_MESSAGE_STYLE,
)  # noqa: F401

from app.core.config import get_settings  # noqa: F401

from app.services.execution_tag_filters import (
    matches_execution_tag_filter as _matches_execution_tag_filter,
    excludes_execution_tag_filter as _excludes_execution_tag_filter,
)  # noqa: F401

from app.core.db import get_db_session  # noqa: F401

from app.models.tables import DataJob, Prediction, PredictionDetail, Symbol  # noqa: F401

from app.models.schema import SymbolCreate  # noqa: F401

from app.services.ai_daily_report import (
    _report_text_with_security_names,
    _report_ticker_labels,
    _security_name_is_code,
    build_trade_explain_text,
    build_close_review_action_feed,
    format_risk_flags,
    format_trade_gate_reason,
    format_trade_status,
    list_ai_daily_report_history,
    load_ai_daily_report,
    load_ai_daily_report_history_item,
    render_ai_daily_report_message,
)  # noqa: F401

from app.services.auth import is_authenticated, login_redirect  # noqa: F401

from app.services.focus_pool import enrich_focus_pool_with_symbols, load_today_focus_pool  # noqa: F401

from app.services.kronos_validation import load_latest_kronos_validation  # noqa: F401

from app.services.dashboard_summary import load_dashboard_summary, load_recent_jobs_summary  # noqa: F401

from app.services.market_intelligence import build_market_narrative_brief  # noqa: F401

from app.services.market_lake import (
    get_latest_lake_trade_date,
    load_lake_price_history,
    load_lake_rows,
)  # noqa: F401

from app.services.market_news import MarketNewsService  # noqa: F401

from app.services.market_risk import PORTFOLIO_RISK_ALERT_SNAPSHOT_TYPE, market_risk_snapshot_type  # noqa: F401

from app.services.market_sync import sync_market_data  # noqa: F401

from app.services.model_selection_guidance import (
    ACTION_BUCKET_LABELS,
    load_model_selection_guidance_snapshot,
    summarize_model_selection_guidance,
)  # noqa: F401

from app.services.model_signal_summary import build_model_state, build_signal_label, enrich_model_output, model_confidence  # noqa: F401

from app.services.model_evaluation import list_latest_model_evaluations  # noqa: F401

from app.services.nlp_snapshots import summarize_news_rows  # noqa: F401

from app.services.portfolio_book import (
    load_portfolio_positions,
    load_portfolio_trades,
    trade_reason_bucket,
    trade_reason_label,
)  # noqa: F401

from app.services.price_snapshot import load_latest_close  # noqa: F401

from app.services.realtime_quotes import load_cn_intraday_bars, load_us_intraday_bars, load_us_latest_trades  # noqa: F401

from app.services.repository import (
    ConceptSnapshotRepository,
    DataJobRepository,
    DECOMMISSIONED_CN_REVIEW_JOB_TYPE,
    FundamentalSnapshotRepository,
    ModelRunRepository,
    PredictionRepository,
    PredictionTradePlanRepository,
    PointInTimeFeatureSnapshotRepository,
    SymbolRepository,
    TechnicalSnapshotRepository,
    WatchlistRepository,
    WorkspaceSnapshotRepository,
)  # noqa: F401

from app.services.stock_selection.feature_availability import FUNDAMENTAL_FEATURE_NAMES  # noqa: F401

from app.services.stock_selection.forward_shadow import CN_FORWARD_SHADOW_SNAPSHOT_TYPE  # noqa: F401

from app.services.stock_selection.point_in_time_features import DEFAULT_MAX_AGE_DAYS  # noqa: F401

from app.services.runtime_cache import get_cached, get_or_set, set_cached  # noqa: F401

from app.services.screener import ScreenerService  # noqa: F401

from app.services.social_signals import social_signal_summary  # noqa: F401

from app.services.symbol_details import SymbolDataService  # noqa: F401

from app.services.template_evaluation import (
    aggregate_window_stats as _aggregate_window_stats,
    build_lightgbm_evaluation,
    build_lightgbm_prediction_evaluation,
    build_next_tesla_evaluation,
    build_technical_momentum_evaluation,
    lightgbm_bias,
    lightgbm_maturity,
    next_tesla_market_bias,
    next_tesla_maturity,
    resolve_template_group_label,
    technical_momentum_bias,
    technical_momentum_maturity,
)  # noqa: F401

from app.services.time_utils import app_today_iso, format_app_datetime, parse_app_datetime  # noqa: F401

from app.services.ui_lang import resolve_request_lang  # noqa: F401

from app.services.workspace_nav import WORKSPACE_COMPACT_STYLE, WORKSPACE_SIDEBAR_STYLE, render_workspace_nav_html  # noqa: F401

from app.services.workspace_snapshots import (
    SNAPSHOT_CONTINUOUS_LEADERS,
    SNAPSHOT_DASHBOARD_NLP,
    SNAPSHOT_HOME_PORTFOLIO,
    SNAPSHOT_HOME_WATCHLIST,
    SNAPSHOT_MARKET_HEATMAP_WORKSPACE,
    SNAPSHOT_MARKET_WORKSPACE,
    SNAPSHOT_MARKET_WORKSPACE_MONITOR,
    SNAPSHOT_MARKET_WORKSPACE_POSTMARKET,
    SNAPSHOT_MARKET_WORKSPACE_PREMARKET,
    SNAPSHOT_MODEL_CANDIDATES,
    SNAPSHOT_PIPELINE_STATUS,
    SNAPSHOT_WATCHLIST_NLP,
    load_latest_workspace_snapshot,
)  # noqa: F401

from app.api.routes.dashboard.concepts import (
    _add_concept_tickers_to_watchlist,
    _add_specific_tickers_to_watchlist,
    _comparison_sort_rank,
    _concept_sort_link,
    _concept_sort_rank,
    _enrich_heatmap_ticker_details,
    _first_not_none,
    _get_concept_for_detail,
    _get_concept_from_summary,
    _get_heatmap_concept_from_snapshot,
    _mini_signal_direction,
    _price_signal_sparkline_svg,
    _provider_strategy_view,
    dashboard_concept_add_to_watchlist,
    dashboard_concept_add_top_to_watchlist,
    dashboard_concept_detail,
    dashboard_concept_ticker_action,
    dashboard_continuous_leader_action,
    dashboard_continuous_leaders_add_top,
    dashboard_continuous_leaders_page,
    dashboard_data_sources,
    dashboard_summary,
)  # noqa: F401
from app.api.routes.dashboard.performance import (
    dashboard_continuous_leaders_export,
    dashboard_model_performance,
    dashboard_model_winner_traceback,
)  # noqa: F401
from app.api.routes.dashboard.market import (
    MARKET_PULSE_SOFT_RISK_TAGS,
    _breadth_chip,
    _concept_tracker_rows_from_heatmap_snapshot,
    _heatmap_metric_label,
    _load_concept_tracker_rows,
    _market_concept_sort_key,
    _recent_market_heat_history,
    _ticker_links_html,
    dashboard_market_concepts_export,
    dashboard_market_concepts_page,
    dashboard_market_heatmap_page,
    dashboard_market_page,
)  # noqa: F401
from app.api.routes.dashboard.ops import (
    SCREENER_PRECOMPUTE_STAGE_CONFIG,
    _build_screener_precompute_stage_rows,
    _compact_json_summary,
    _jobs_started_on_date,
    _latest_cn_refresh_summary,
    _render_screener_precompute_action_forms,
    _render_task_center_redesign,
    _task_action_form,
    _task_duration,
    _task_status_badge,
    _today_job_status_counts,
    dashboard_ops_history_page,
    dashboard_ops_job_detail,
    dashboard_ops_jobs_page,
    dashboard_ops_models_page,
    dashboard_ops_page,
    dashboard_ops_sync_page,
    dashboard_ops_today_page,
)  # noqa: F401
from app.api.routes.dashboard.home import (
    _dashboard_focus_items,
    _dashboard_home_panels,
    _dashboard_home_portfolio_rows,
    _dashboard_home_watchlist_rows,
    _dashboard_pseudo_strength_hint,
    _dashboard_signal_action_sets,
    _dashboard_trading_regime,
    _job_status_text,
    _payload_rows,
    _render_ai_daily_report_card,
    _render_dashboard_home_panels_fragment,
    _render_dashboard_top_fragment,
    _render_dashboard_workspace,
    _signal_status_tone,
    dashboard_home_panels_fragment,
    dashboard_page,
    dashboard_top_fragment,
)  # noqa: F401
from app.api.routes.dashboard.reports import (
    _build_realtime_monitor_rows,
    _monitor_float,
    _monitor_market_for_ticker,
    _monitor_source_label,
    _monitor_status,
    _premarket_plan_action_text,
    _premarket_plan_focus_text,
    _premarket_plan_rows,
    _render_ai_report_guidance_bridge,
    dashboard_ai_daily_report,
    dashboard_ai_daily_report_history,
    dashboard_ai_daily_report_history_detail,
    dashboard_ai_daily_report_message,
    dashboard_premarket_plan,
    dashboard_realtime_monitor,
    dashboard_realtime_monitor_intraday,
    dashboard_weekly_review,
)  # noqa: F401
from app.api.routes.dashboard._common import (
    CONCEPT_TEXT,
    DASHBOARD_TEXT,
    US_SIGNAL_TRAIN_JOB_TYPES,
    _build_market_context,
    _clamp_lookback_runs,
    _compact_job_type,
    _compact_label,
    _compact_run_name,
    _concept_price_strength,
    _concept_slug,
    _concept_ticker_watch_state,
    _concept_tr,
    _continuous_leader_sort_key,
    _dashboard_home_signal,
    _dashboard_model_badge,
    _dashboard_watchlist_map,
    _display_job_message,
    _dt,
    _find_latest_job_by_type,
    _hydrate_ai_report_names,
    _lightweight_market_context,
    _load_cached_ai_daily_report,
    _load_home_summary,
    _load_summary,
    _lookback_options,
    _lookback_pills,
    _percent_chip,
    _reason_screen_href,
    _reason_screen_link,
    _reason_screen_params,
    _score_sparkline_svg,
    _signal_pill,
    _sparkline_svg,
    _summarize_screener_precompute_job,
)  # noqa: F401
from app.services.dashboard_insights import (  # noqa: F401
    _audit_conclusion_for_trade,
    _build_model_run_performance_summary,
    _build_recommendation_validation_summary,
    _build_watchlist_post_add_summary,
    _build_weekly_review_summary,
    _display_time,
    _fmt_optional_float,
    _forward_return_from_history,
    _outcome_status_label,
    _report_market_rows,
    _report_outcome_rows,
    _report_outcome_summary,
    _return_since_history_start,
    _window_return_pct,
)
from app.api.routes.dashboard.concepts import router as concepts_router
from app.api.routes.dashboard.performance import router as performance_router
from app.api.routes.dashboard.market import router as market_router
from app.api.routes.dashboard.ops import router as ops_router
from app.api.routes.dashboard.home import router as home_router
from app.api.routes.dashboard.reports import router as reports_router


router = APIRouter()
router.include_router(concepts_router)
router.include_router(performance_router)
router.include_router(market_router)
router.include_router(ops_router)
router.include_router(home_router)
router.include_router(reports_router)
