"""Domain-split repository package.

Re-exports the complete repository surface (classes, constants and
helpers) so that the legacy facade ``app.services.repository`` and any
direct domain-module import keep working with identical objects.
"""

from app.services.repositories.shared import (
    DECOMMISSIONED_CN_REVIEW_JOB_TYPE,
    PRODUCTION_SIGNAL_MODEL_TYPES,
    JOB_MESSAGE_MAX_CHARS,
    utc_now_iso,
    ticker_query_candidates,
    chunked_ids,
    chunked_rows,
    market_sort_case,
    _bounded_job_message,
    _physical_snapshot_tables_for_market,
    _legacy_snapshot_writes_enabled,
    _symbol_market,
    _assert_legacy_prediction_write_allowed,
    _physical_date,
    _physical_datetime,
    _loads_json_object,
    _loads_json_list,
    _safe_parse_iso,
    _job_duration_seconds,
    _is_database_locked_error,
    _sleep_for_lock_retry,
)
from app.services.repositories.symbols import SymbolRepository
from app.services.repositories.predictions import (
    PredictionRepository,
    PredictionExplanationRepository,
    PredictionDetailRepository,
    PredictionTradePlanRepository,
    PredictionArtifactRepository,
    LivePredictionRepository,
    PredictionWriteRepository,
)
from app.services.repositories.signals import (
    ModelChartSignalRepository,
    ModelRunRepository,
)
from app.services.repositories.backtests import (
    BacktestRepository,
    StrategyRunRepository,
)
from app.services.repositories.market import (
    MarketRefreshBatchRepository,
    PriceSyncStateRepository,
    ConceptSnapshotRepository,
    TechnicalSnapshotRepository,
)
from app.services.repositories.jobs import (
    _job_markets_from_params,
    _job_category,
    _job_provider,
    _declared_upstream_job_ids,
    _dependency_status,
    DataJobRepository,
)
from app.services.repositories.workspace import (
    WorkspaceSnapshotRepository,
    DashboardReadRepository,
    AppSettingRepository,
)
from app.services.repositories.research import (
    FundamentalSnapshotRepository,
    PointInTimeFeatureSnapshotRepository,
)
from app.services.repositories.watchlist import WatchlistRepository

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
