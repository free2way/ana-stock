from sqlalchemy import Float, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class TimestampMixin:
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class Symbol(Base, TimestampMixin):
    __tablename__ = "symbols"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    market: Mapped[str | None] = mapped_column(Text, nullable=True)
    exchange: Mapped[str | None] = mapped_column(Text, nullable=True)
    sector: Mapped[str | None] = mapped_column(Text, nullable=True)
    industry: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class PriceSyncState(Base):
    __tablename__ = "price_sync_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id"), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    last_synced_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    symbol: Mapped[Symbol] = relationship()


class ModelRun(Base):
    __tablename__ = "model_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    model_type: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str | None] = mapped_column(Text, nullable=True)
    universe: Mapped[str | None] = mapped_column(Text, nullable=True)
    train_start: Mapped[str | None] = mapped_column(Text, nullable=True)
    train_end: Mapped[str | None] = mapped_column(Text, nullable=True)
    test_start: Mapped[str | None] = mapped_column(Text, nullable=True)
    test_end: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(Text, nullable=True)


class Prediction(Base):
    __tablename__ = "predictions"
    __table_args__ = (UniqueConstraint("model_run_id", "symbol_id", "trade_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_run_id: Mapped[int] = mapped_column(ForeignKey("model_runs.id"), nullable=False)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id"), nullable=False)
    trade_date: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    rank_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    model_run: Mapped[ModelRun] = relationship()
    symbol: Mapped[Symbol] = relationship()


class PredictionExplanation(Base):
    __tablename__ = "prediction_explanations"
    __table_args__ = (UniqueConstraint("prediction_id", "feature_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prediction_id: Mapped[int] = mapped_column(ForeignKey("predictions.id"), nullable=False)
    feature_name: Mapped[str] = mapped_column(Text, nullable=False)
    feature_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    contribution: Mapped[float | None] = mapped_column(Float, nullable=True)
    direction: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    prediction: Mapped[Prediction] = relationship()


class PredictionDetail(Base):
    __tablename__ = "prediction_details"
    __table_args__ = (UniqueConstraint("prediction_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prediction_id: Mapped[int] = mapped_column(ForeignKey("predictions.id"), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    bullish_prob: Mapped[float | None] = mapped_column(Float, nullable=True)
    bearish_prob: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_return_5d: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_return_20d: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_drawdown_20d: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_reward_risk_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_horizon_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    universe_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    percentile: Mapped[float | None] = mapped_column(Float, nullable=True)
    regime_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    conviction_bucket: Mapped[str | None] = mapped_column(Text, nullable=True)
    position_size_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry_style: Mapped[str | None] = mapped_column(Text, nullable=True)
    signal_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    signal_strength: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    prediction: Mapped[Prediction] = relationship()


class ModelChartSignal(Base):
    __tablename__ = "model_chart_signals"
    __table_args__ = (UniqueConstraint("model_run_id", "symbol_id", "trade_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_run_id: Mapped[int] = mapped_column(ForeignKey("model_runs.id"), nullable=False)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id"), nullable=False)
    trade_date: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    rank_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    signal_strength: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    model_run: Mapped[ModelRun] = relationship()
    symbol: Mapped[Symbol] = relationship()


class PredictionTradePlan(Base):
    __tablename__ = "prediction_trade_plans"
    __table_args__ = (UniqueConstraint("prediction_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prediction_id: Mapped[int] = mapped_column(ForeignKey("predictions.id"), nullable=False)
    entry_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    breakout_level: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_level: Mapped[float | None] = mapped_column(Float, nullable=True)
    support_level: Mapped[float | None] = mapped_column(Float, nullable=True)
    resistance_level: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    trailing_stop_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    invalidation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_tags_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    prediction: Mapped[Prediction] = relationship()


class StrategyRun(Base):
    __tablename__ = "strategy_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_run_id: Mapped[int | None] = mapped_column(ForeignKey("model_runs.id"), nullable=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    strategy_type: Mapped[str] = mapped_column(Text, nullable=False)
    start_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    end_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    model_run: Mapped[ModelRun | None] = relationship()


class StrategyDailyMetric(Base):
    __tablename__ = "strategy_daily_metrics"
    __table_args__ = (UniqueConstraint("strategy_run_id", "trade_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_run_id: Mapped[int] = mapped_column(ForeignKey("strategy_runs.id"), nullable=False)
    trade_date: Mapped[str] = mapped_column(Text, nullable=False)
    nav: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    benchmark_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    drawdown: Mapped[float | None] = mapped_column(Float, nullable=True)
    turnover: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    strategy_run: Mapped[StrategyRun] = relationship()


class Watchlist(Base, TimestampMixin):
    __tablename__ = "watchlists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (UniqueConstraint("watchlist_id", "symbol_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    watchlist_id: Mapped[int] = mapped_column(ForeignKey("watchlists.id"), nullable=False)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id"), nullable=False)
    sync_enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    watchlist: Mapped[Watchlist] = relationship()
    symbol: Mapped[Symbol] = relationship()


class DataJob(Base):
    __tablename__ = "data_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[str] = mapped_column(Text, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    params_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class WorkspaceSnapshot(Base):
    __tablename__ = "workspace_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_type: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_date: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_job_id: Mapped[int | None] = mapped_column(ForeignKey("data_jobs.id"), nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    source_job: Mapped[DataJob | None] = relationship()


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class FundamentalSnapshot(Base):
    __tablename__ = "fundamental_snapshots"
    __table_args__ = (UniqueConstraint("symbol_id", "report_date", "source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id"), nullable=False)
    report_date: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    listing_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    pe_ttm: Mapped[float | None] = mapped_column(Float, nullable=True)
    dividend_yield: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    roe_avg_3y: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_profit_yoy: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_yoy: Mapped[float | None] = mapped_column(Float, nullable=True)
    debt_to_assets: Mapped[float | None] = mapped_column(Float, nullable=True)
    data_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    symbol: Mapped[Symbol] = relationship()


class ConceptSnapshot(Base):
    __tablename__ = "concept_snapshots"
    __table_args__ = (UniqueConstraint("symbol_id", "concept_name", "as_of_date", "source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id"), nullable=False)
    concept_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    concept_name: Mapped[str] = mapped_column(Text, nullable=False)
    as_of_date: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    strength: Mapped[float | None] = mapped_column(Float, nullable=True)
    data_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    symbol: Mapped[Symbol] = relationship()


class TechnicalSnapshot(Base):
    __tablename__ = "technical_snapshots"
    __table_args__ = (UniqueConstraint("symbol_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id"), nullable=False)
    as_of_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    limit_up_yesterday: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    volume_breakout: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ma_cluster: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bullish_ma_stack: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    macd_underwater_cross: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    matched_patterns_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)

    symbol: Mapped[Symbol] = relationship()
