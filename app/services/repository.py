import json
import math
import time
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import case, delete, desc, func, insert, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.schema import SymbolCreate
from app.models.tables import (
    AppSetting,
    ConceptSnapshot,
    DataJob,
    FundamentalSnapshot,
    JobDefinition,
    JobRunAttempt,
    JobRunDependency,
    LivePrediction,
    MarketRefreshBatch,
    ModelEvaluation,
    ModelRun,
    ModelChartSignal,
    Prediction,
    PredictionArtifact,
    PredictionDetail,
    PredictionExplanation,
    PredictionTradePlan,
    PointInTimeFeatureSnapshot,
    PriceSyncState,
    StrategyDailyMetric,
    StrategyFill,
    StrategyOrder,
    StrategyPortfolioState,
    StrategyReject,
    StrategyRun,
    Symbol,
    TechnicalSnapshot,
    Watchlist,
    WatchlistItem,
    WorkspaceSnapshot,
)
from app.services.market_context import load_market_context_snapshot
from app.services.market_freshness import (
    classify_market_symbol_anomalies,
    summarize_market_freshness,
)
from app.services.market_storage_routing import (
    enabled_physical_markets,
    legacy_mirror_write_enabled,
    physical_fact_write_markets,
    physical_hot_prediction_models,
    physical_live_prediction_model,
    physical_model_chart_signal_model,
    physical_only_cutover_active,
    physical_prediction_trade_plan_model,
    physical_snapshot_models,
)
from app.services.json_payload_artifacts import (
    JsonPayloadArtifactStore,
    build_payload_envelope,
    canonical_json_bytes,
    resolve_payload_envelope,
    summarize_job_result,
)
from app.services.app_setting_storage import (
    decode_app_setting_value,
    encode_app_setting_value,
)
from app.services.prediction_artifacts import (
    read_prediction_artifact_rows,
    read_prediction_explanation_artifact_rows,
)
from app.services.market_lake import (
    count_lake_symbols_for_trade_date,
    get_latest_lake_trade_date,
    list_lake_symbols_for_trade_date,
)
from app.services.tradability_filter import evaluate_candidate_tradability
from app.services.time_utils import app_now, app_now_iso
# ---------------------------------------------------------------------------
# Backwards-compatible facade
#
# The repository implementations were split into the
# ``app/services/repositories/`` domain package.  This module stays the one
# stable import surface for the whole code base: every name that used to be
# reachable as an attribute of this module is re-exported explicitly below
# (no wildcard imports), so existing callers keep working unchanged.
#
# The imports in the original header above are retained on purpose: tests and
# tooling patch and introspect several of these module attributes, e.g.
# ``unittest.mock.patch("app.services.repository.get_settings")``.

from app.services.repositories.backtests import (
    BacktestRepository,
    StrategyRunRepository,
)
from app.services.repositories.jobs import (
    DataJobRepository,
    _declared_upstream_job_ids,
    _dependency_status,
    _job_category,
    _job_markets_from_params,
    _job_provider,
)
from app.services.repositories.market import (
    ConceptSnapshotRepository,
    MarketRefreshBatchRepository,
    PriceSyncStateRepository,
    TechnicalSnapshotRepository,
)
from app.services.repositories.predictions import (
    LivePredictionRepository,
    PredictionArtifactRepository,
    PredictionDetailRepository,
    PredictionExplanationRepository,
    PredictionRepository,
    PredictionTradePlanRepository,
    PredictionWriteRepository,
)
from app.services.repositories.research import (
    FundamentalSnapshotRepository,
    PointInTimeFeatureSnapshotRepository,
)
from app.services.repositories.shared import (
    DECOMMISSIONED_CN_REVIEW_JOB_TYPE,
    JOB_MESSAGE_MAX_CHARS,
    PRODUCTION_SIGNAL_MODEL_TYPES,
    _assert_legacy_prediction_write_allowed,
    _bounded_job_message,
    _is_database_locked_error,
    _job_duration_seconds,
    _legacy_snapshot_writes_enabled,
    _loads_json_list,
    _loads_json_object,
    _physical_date,
    _physical_datetime,
    _physical_snapshot_tables_for_market,
    _safe_parse_iso,
    _sleep_for_lock_retry,
    _symbol_market,
    chunked_ids,
    chunked_rows,
    market_sort_case,
    ticker_query_candidates,
    utc_now_iso,
)
from app.services.repositories.signals import (
    ModelChartSignalRepository,
    ModelRunRepository,
)
from app.services.repositories.symbols import SymbolRepository
from app.services.repositories.watchlist import WatchlistRepository
from app.services.repositories.workspace import (
    AppSettingRepository,
    DashboardReadRepository,
    WorkspaceSnapshotRepository,
)

__all__ = [
    "AppSettingRepository",
    "BacktestRepository",
    "ConceptSnapshotRepository",
    "DashboardReadRepository",
    "DataJobRepository",
    "FundamentalSnapshotRepository",
    "LivePredictionRepository",
    "MarketRefreshBatchRepository",
    "ModelChartSignalRepository",
    "ModelRunRepository",
    "PointInTimeFeatureSnapshotRepository",
    "PredictionArtifactRepository",
    "PredictionDetailRepository",
    "PredictionExplanationRepository",
    "PredictionRepository",
    "PredictionTradePlanRepository",
    "PredictionWriteRepository",
    "PriceSyncStateRepository",
    "StrategyRunRepository",
    "SymbolRepository",
    "TechnicalSnapshotRepository",
    "WatchlistRepository",
    "WorkspaceSnapshotRepository",
    "DECOMMISSIONED_CN_REVIEW_JOB_TYPE",
    "JOB_MESSAGE_MAX_CHARS",
    "PRODUCTION_SIGNAL_MODEL_TYPES",
    "chunked_ids",
    "chunked_rows",
    "market_sort_case",
    "ticker_query_candidates",
    "utc_now_iso",
    "_legacy_snapshot_writes_enabled",
    "_physical_snapshot_tables_for_market",
]


# ---------------------------------------------------------------------------
# Patch-compatibility shim.
#
# ``unittest.mock.patch("app.services.repository.<name>", ...)`` replaces
# attributes on *this* module.  After the domain split the repository classes
# resolve those names in their own module globals, so a write that only
# touched the facade would silently stop affecting behaviour.  Forwarding
# every attribute write into the domain modules that own the name preserves
# the pre-split patching semantics exactly (``patch`` also unwinds via
# ``setattr``, so restoring works symmetrically).
import sys
from types import ModuleType

_REPOSITORIES_PACKAGE_PREFIX = "app.services.repositories."


class _RepositoryCompatModule(ModuleType):
    """Facade module type that mirrors attribute writes into domain modules."""

    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        for module_name, module in list(sys.modules.items()):
            if module_name.startswith(_REPOSITORIES_PACKAGE_PREFIX) and name in vars(module):
                setattr(module, name, value)


sys.modules[__name__].__class__ = _RepositoryCompatModule
