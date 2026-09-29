from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class TimestampMixin:
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class Symbol(Base, TimestampMixin):
    __tablename__ = "symbols"
    __table_args__ = (
        UniqueConstraint("id", "market", name="uq_symbols_id_market"),
    )

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


class PredictionArtifact(Base):
    __tablename__ = "prediction_artifacts"
    __table_args__ = (UniqueConstraint("model_run_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_run_id: Mapped[int] = mapped_column(ForeignKey("model_runs.id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_path: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    prediction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    explanation_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    min_trade_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_trade_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    verified_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    model_run: Mapped[ModelRun] = relationship()


class LivePrediction(Base):
    """Small online-serving snapshot; full history remains in Parquet/compact PG."""

    __tablename__ = "live_predictions"
    __table_args__ = (
        UniqueConstraint("model_run_id", "symbol_id", "trade_date"),
        Index("ix_live_predictions_market_run_score", "market", "model_run_id", "score"),
        Index("ix_live_predictions_symbol_date", "symbol_id", "trade_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_run_id: Mapped[int] = mapped_column(
        ForeignKey("model_runs.id", ondelete="CASCADE"), nullable=False
    )
    symbol_id: Mapped[int] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE"), nullable=False
    )
    market: Mapped[str] = mapped_column(Text, nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    rank_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    signal_strength: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_return_20d: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_drawdown_20d: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_reward_risk_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    conviction_bucket: Mapped[str | None] = mapped_column(Text, nullable=True)
    position_size_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry_style: Mapped[str | None] = mapped_column(Text, nullable=True)
    percentile: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    model_run: Mapped[ModelRun] = relationship()
    symbol: Mapped[Symbol] = relationship()


class _PhysicalLivePredictionMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_run_id: Mapped[int] = mapped_column(
        ForeignKey("model_runs.id", ondelete="CASCADE"), nullable=False
    )
    symbol_id: Mapped[int] = mapped_column(Integer, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    rank_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    signal_strength: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_return_20d: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_drawdown_20d: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_reward_risk_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    conviction_bucket: Mapped[str | None] = mapped_column(Text, nullable=True)
    position_size_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry_style: Mapped[str | None] = mapped_column(Text, nullable=True)
    percentile: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CNLivePrediction(_PhysicalLivePredictionMixin, Base):
    __tablename__ = "cn_live_predictions"
    __table_args__ = (
        UniqueConstraint("model_run_id", "symbol_id", "trade_date"),
        CheckConstraint("market = 'CN'", name="ck_cn_live_predictions_market"),
        ForeignKeyConstraint(
            ["symbol_id", "market"],
            ["symbols.id", "symbols.market"],
            name="fk_cn_live_predictions_symbol_market",
            ondelete="CASCADE",
        ),
        Index("ix_cn_live_predictions_run_score", "model_run_id", "score"),
        Index("ix_cn_live_predictions_symbol_date", "symbol_id", "trade_date"),
    )


class USLivePrediction(_PhysicalLivePredictionMixin, Base):
    __tablename__ = "us_live_predictions"
    __table_args__ = (
        UniqueConstraint("model_run_id", "symbol_id", "trade_date"),
        CheckConstraint("market = 'US'", name="ck_us_live_predictions_market"),
        ForeignKeyConstraint(
            ["symbol_id", "market"],
            ["symbols.id", "symbols.market"],
            name="fk_us_live_predictions_symbol_market",
            ondelete="CASCADE",
        ),
        Index("ix_us_live_predictions_run_score", "model_run_id", "score"),
        Index("ix_us_live_predictions_symbol_date", "symbol_id", "trade_date"),
    )


class HKLivePrediction(_PhysicalLivePredictionMixin, Base):
    __tablename__ = "hk_live_predictions"
    __table_args__ = (
        UniqueConstraint("model_run_id", "symbol_id", "trade_date"),
        CheckConstraint("market = 'HK'", name="ck_hk_live_predictions_market"),
        ForeignKeyConstraint(
            ["symbol_id", "market"],
            ["symbols.id", "symbols.market"],
            name="fk_hk_live_predictions_symbol_market",
            ondelete="CASCADE",
        ),
        Index("ix_hk_live_predictions_run_score", "model_run_id", "score"),
        Index("ix_hk_live_predictions_symbol_date", "symbol_id", "trade_date"),
    )


class _PhysicalPredictionMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_run_id: Mapped[int] = mapped_column(
        ForeignKey("model_runs.id", ondelete="CASCADE"), nullable=False
    )
    symbol_id: Mapped[int] = mapped_column(Integer, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    rank_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CNPrediction(_PhysicalPredictionMixin, Base):
    __tablename__ = "cn_predictions"
    __table_args__ = (
        UniqueConstraint("model_run_id", "symbol_id", "trade_date"),
        CheckConstraint("market = 'CN'", name="ck_cn_predictions_market"),
        ForeignKeyConstraint(
            ["symbol_id", "market"],
            ["symbols.id", "symbols.market"],
            name="fk_cn_predictions_symbol_market",
            ondelete="CASCADE",
        ),
        Index("ix_cn_predictions_run_date_score", "model_run_id", "trade_date", "score"),
        Index("ix_cn_predictions_symbol_date", "symbol_id", "trade_date"),
    )


class USPrediction(_PhysicalPredictionMixin, Base):
    __tablename__ = "us_predictions"
    __table_args__ = (
        UniqueConstraint("model_run_id", "symbol_id", "trade_date"),
        CheckConstraint("market = 'US'", name="ck_us_predictions_market"),
        ForeignKeyConstraint(
            ["symbol_id", "market"],
            ["symbols.id", "symbols.market"],
            name="fk_us_predictions_symbol_market",
            ondelete="CASCADE",
        ),
        Index("ix_us_predictions_run_date_score", "model_run_id", "trade_date", "score"),
        Index("ix_us_predictions_symbol_date", "symbol_id", "trade_date"),
    )


class HKPrediction(_PhysicalPredictionMixin, Base):
    __tablename__ = "hk_predictions"
    __table_args__ = (
        UniqueConstraint("model_run_id", "symbol_id", "trade_date"),
        CheckConstraint("market = 'HK'", name="ck_hk_predictions_market"),
        ForeignKeyConstraint(
            ["symbol_id", "market"],
            ["symbols.id", "symbols.market"],
            name="fk_hk_predictions_symbol_market",
            ondelete="CASCADE",
        ),
        Index("ix_hk_predictions_run_date_score", "model_run_id", "trade_date", "score"),
        Index("ix_hk_predictions_symbol_date", "symbol_id", "trade_date"),
    )


class _PhysicalPredictionDetailMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CNPredictionDetail(_PhysicalPredictionDetailMixin, Base):
    __tablename__ = "cn_prediction_details"
    __table_args__ = (UniqueConstraint("prediction_id"),)
    prediction_id: Mapped[int] = mapped_column(
        ForeignKey("cn_predictions.id", ondelete="CASCADE"), nullable=False
    )


class USPredictionDetail(_PhysicalPredictionDetailMixin, Base):
    __tablename__ = "us_prediction_details"
    __table_args__ = (UniqueConstraint("prediction_id"),)
    prediction_id: Mapped[int] = mapped_column(
        ForeignKey("us_predictions.id", ondelete="CASCADE"), nullable=False
    )


class HKPredictionDetail(_PhysicalPredictionDetailMixin, Base):
    __tablename__ = "hk_prediction_details"
    __table_args__ = (UniqueConstraint("prediction_id"),)
    prediction_id: Mapped[int] = mapped_column(
        ForeignKey("hk_predictions.id", ondelete="CASCADE"), nullable=False
    )


class _PhysicalPredictionExplanationMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    feature_name: Mapped[str] = mapped_column(Text, nullable=False)
    feature_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    contribution: Mapped[float | None] = mapped_column(Float, nullable=True)
    direction: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CNPredictionExplanation(_PhysicalPredictionExplanationMixin, Base):
    __tablename__ = "cn_prediction_explanations"
    __table_args__ = (UniqueConstraint("prediction_id", "feature_name"),)
    prediction_id: Mapped[int] = mapped_column(
        ForeignKey("cn_predictions.id", ondelete="CASCADE"), nullable=False
    )


class USPredictionExplanation(_PhysicalPredictionExplanationMixin, Base):
    __tablename__ = "us_prediction_explanations"
    __table_args__ = (UniqueConstraint("prediction_id", "feature_name"),)
    prediction_id: Mapped[int] = mapped_column(
        ForeignKey("us_predictions.id", ondelete="CASCADE"), nullable=False
    )


class HKPredictionExplanation(_PhysicalPredictionExplanationMixin, Base):
    __tablename__ = "hk_prediction_explanations"
    __table_args__ = (UniqueConstraint("prediction_id", "feature_name"),)
    prediction_id: Mapped[int] = mapped_column(
        ForeignKey("hk_predictions.id", ondelete="CASCADE"), nullable=False
    )


class _PhysicalModelChartSignalMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_run_id: Mapped[int] = mapped_column(
        ForeignKey("model_runs.id", ondelete="CASCADE"), nullable=False
    )
    symbol_id: Mapped[int] = mapped_column(Integer, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    rank_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_label: Mapped[str | None] = mapped_column(Text, nullable=True)
    signal_strength: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CNModelChartSignal(_PhysicalModelChartSignalMixin, Base):
    __tablename__ = "cn_model_chart_signals"
    __table_args__ = (
        UniqueConstraint("model_run_id", "symbol_id", "trade_date"),
        CheckConstraint("market = 'CN'", name="ck_cn_model_chart_signals_market"),
        ForeignKeyConstraint(
            ["symbol_id", "market"],
            ["symbols.id", "symbols.market"],
            name="fk_cn_model_chart_signals_symbol_market",
            ondelete="CASCADE",
        ),
        Index(
            "ix_cn_model_chart_signals_symbol_date",
            "symbol_id",
            "trade_date",
        ),
    )


class USModelChartSignal(_PhysicalModelChartSignalMixin, Base):
    __tablename__ = "us_model_chart_signals"
    __table_args__ = (
        UniqueConstraint("model_run_id", "symbol_id", "trade_date"),
        CheckConstraint("market = 'US'", name="ck_us_model_chart_signals_market"),
        ForeignKeyConstraint(
            ["symbol_id", "market"],
            ["symbols.id", "symbols.market"],
            name="fk_us_model_chart_signals_symbol_market",
            ondelete="CASCADE",
        ),
        Index(
            "ix_us_model_chart_signals_symbol_date",
            "symbol_id",
            "trade_date",
        ),
    )


class HKModelChartSignal(_PhysicalModelChartSignalMixin, Base):
    __tablename__ = "hk_model_chart_signals"
    __table_args__ = (
        UniqueConstraint("model_run_id", "symbol_id", "trade_date"),
        CheckConstraint("market = 'HK'", name="ck_hk_model_chart_signals_market"),
        ForeignKeyConstraint(
            ["symbol_id", "market"],
            ["symbols.id", "symbols.market"],
            name="fk_hk_model_chart_signals_symbol_market",
            ondelete="CASCADE",
        ),
        Index("ix_hk_model_chart_signals_symbol_date", "symbol_id", "trade_date"),
    )


class _PhysicalPredictionTradePlanMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CNPredictionTradePlan(_PhysicalPredictionTradePlanMixin, Base):
    __tablename__ = "cn_prediction_trade_plans"
    __table_args__ = (UniqueConstraint("prediction_id"),)
    prediction_id: Mapped[int] = mapped_column(
        ForeignKey("cn_predictions.id", ondelete="CASCADE"), nullable=False
    )


class USPredictionTradePlan(_PhysicalPredictionTradePlanMixin, Base):
    __tablename__ = "us_prediction_trade_plans"
    __table_args__ = (UniqueConstraint("prediction_id"),)
    prediction_id: Mapped[int] = mapped_column(
        ForeignKey("us_predictions.id", ondelete="CASCADE"), nullable=False
    )


class HKPredictionTradePlan(_PhysicalPredictionTradePlanMixin, Base):
    __tablename__ = "hk_prediction_trade_plans"
    __table_args__ = (UniqueConstraint("prediction_id"),)
    prediction_id: Mapped[int] = mapped_column(
        ForeignKey("hk_predictions.id", ondelete="CASCADE"), nullable=False
    )


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


class StrategyOrder(Base):
    __tablename__ = "strategy_orders"
    __table_args__ = (UniqueConstraint("strategy_run_id", "order_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_run_id: Mapped[int] = mapped_column(ForeignKey("strategy_runs.id"), nullable=False)
    order_id: Mapped[str] = mapped_column(Text, nullable=False)
    insight_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    ticker: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    signal_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective_date: Mapped[str] = mapped_column(Text, nullable=False)
    order_type: Mapped[str] = mapped_column(Text, nullable=False)
    exit_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class StrategyFill(Base):
    __tablename__ = "strategy_fills"
    __table_args__ = (UniqueConstraint("strategy_run_id", "order_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_run_id: Mapped[int] = mapped_column(ForeignKey("strategy_runs.id"), nullable=False)
    order_id: Mapped[str] = mapped_column(Text, nullable=False)
    insight_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    ticker: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    fill_date: Mapped[str] = mapped_column(Text, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    reference_price: Mapped[float] = mapped_column(Float, nullable=False)
    fill_price: Mapped[float] = mapped_column(Float, nullable=False)
    fee: Mapped[float] = mapped_column(Float, nullable=False)
    slippage: Mapped[float] = mapped_column(Float, nullable=False)
    notional: Mapped[float] = mapped_column(Float, nullable=False)
    lot_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    entry_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class StrategyReject(Base):
    __tablename__ = "strategy_rejects"
    __table_args__ = (UniqueConstraint("strategy_run_id", "order_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_run_id: Mapped[int] = mapped_column(ForeignKey("strategy_runs.id"), nullable=False)
    order_id: Mapped[str] = mapped_column(Text, nullable=False)
    ticker: Mapped[str] = mapped_column(Text, nullable=False)
    side: Mapped[str] = mapped_column(Text, nullable=False)
    effective_date: Mapped[str] = mapped_column(Text, nullable=False)
    reject_reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class StrategyPortfolioState(Base):
    __tablename__ = "strategy_portfolio_states"
    __table_args__ = (UniqueConstraint("strategy_run_id", "trade_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_run_id: Mapped[int] = mapped_column(ForeignKey("strategy_runs.id"), nullable=False)
    trade_date: Mapped[str] = mapped_column(Text, nullable=False)
    cash: Mapped[float] = mapped_column(Float, nullable=False)
    position_market_value: Mapped[float] = mapped_column(Float, nullable=False)
    nav: Mapped[float] = mapped_column(Float, nullable=False)
    gross_exposure: Mapped[float] = mapped_column(Float, nullable=False)
    net_exposure: Mapped[float] = mapped_column(Float, nullable=False)
    cumulative_fees: Mapped[float] = mapped_column(Float, nullable=False)
    cumulative_slippage: Mapped[float] = mapped_column(Float, nullable=False)
    open_lots: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class _SelectionDecisionMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    protocol_id: Mapped[str] = mapped_column(Text, nullable=False)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    effective_trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    cutoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision_type: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_reference_json: Mapped[str] = mapped_column(Text, nullable=False)
    supersedes_decision_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CNSelectionDecision(_SelectionDecisionMixin, Base):
    __tablename__ = "cn_selection_decisions"
    __table_args__ = (
        CheckConstraint("market = 'CN'", name="ck_cn_selection_decisions_market"),
        UniqueConstraint("protocol_id", "effective_trade_date", "payload_sha256", name="uq_cn_selection_economic_decision"),
        Index("ix_cn_selection_decision_date", "effective_trade_date", "protocol_id"),
    )


class USSelectionDecision(_SelectionDecisionMixin, Base):
    __tablename__ = "us_selection_decisions"
    __table_args__ = (
        CheckConstraint("market = 'US'", name="ck_us_selection_decisions_market"),
        UniqueConstraint("protocol_id", "effective_trade_date", "payload_sha256", name="uq_us_selection_economic_decision"),
        Index("ix_us_selection_decision_date", "effective_trade_date", "protocol_id"),
    )


class HKSelectionDecision(_SelectionDecisionMixin, Base):
    __tablename__ = "hk_selection_decisions"
    __table_args__ = (
        CheckConstraint("market = 'HK'", name="ck_hk_selection_decisions_market"),
        UniqueConstraint("protocol_id", "effective_trade_date", "payload_sha256", name="uq_hk_selection_economic_decision"),
        Index("ix_hk_selection_decision_date", "effective_trade_date", "protocol_id"),
    )


class _SelectionPublicationMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    publication_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    decision_id: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    delivery_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_reference_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_message_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CNSelectionPublication(_SelectionPublicationMixin, Base):
    __tablename__ = "cn_selection_publications"
    __table_args__ = (CheckConstraint("market = 'CN'", name="ck_cn_selection_publications_market"),
                      Index("ix_cn_selection_publication_decision", "decision_id", "status"))


class USSelectionPublication(_SelectionPublicationMixin, Base):
    __tablename__ = "us_selection_publications"
    __table_args__ = (CheckConstraint("market = 'US'", name="ck_us_selection_publications_market"),
                      Index("ix_us_selection_publication_decision", "decision_id", "status"))


class HKSelectionPublication(_SelectionPublicationMixin, Base):
    __tablename__ = "hk_selection_publications"
    __table_args__ = (CheckConstraint("market = 'HK'", name="ck_hk_selection_publications_market"),
                      Index("ix_hk_selection_publication_decision", "decision_id", "status"))


class _PaperSimulationMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    simulation_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    decision_id: Mapped[str] = mapped_column(Text, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    protocol_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_reference_json: Mapped[str] = mapped_column(Text, nullable=False)
    summary_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CNPaperSimulation(_PaperSimulationMixin, Base):
    __tablename__ = "cn_paper_simulations"
    __table_args__ = (CheckConstraint("market = 'CN'", name="ck_cn_paper_simulations_market"),
                      Index("ix_cn_paper_sim_decision", "decision_id", "start_date"))


class USPaperSimulation(_PaperSimulationMixin, Base):
    __tablename__ = "us_paper_simulations"
    __table_args__ = (CheckConstraint("market = 'US'", name="ck_us_paper_simulations_market"),
                      Index("ix_us_paper_sim_decision", "decision_id", "start_date"))


class HKPaperSimulation(_PaperSimulationMixin, Base):
    __tablename__ = "hk_paper_simulations"
    __table_args__ = (CheckConstraint("market = 'HK'", name="ck_hk_paper_simulations_market"),
                      Index("ix_hk_paper_sim_decision", "decision_id", "start_date"))


class ModelEvaluation(Base):
    """A reproducible, out-of-sample evaluation of one persisted model run."""

    __tablename__ = "model_evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_run_id: Mapped[int] = mapped_column(ForeignKey("model_runs.id"), nullable=False)
    source_job_id: Mapped[int | None] = mapped_column(ForeignKey("data_jobs.id"), nullable=True)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    evaluation_type: Mapped[str] = mapped_column(Text, nullable=False, default="prediction_forward_return")
    input_as_of_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    sample_start_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    sample_end_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_out_of_sample: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    oos_sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    oos_coverage_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    purge_gap_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    benchmark_avg_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    universe_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    activation_status: Mapped[str] = mapped_column(Text, nullable=False, default="observation")
    includes_costs: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    round_trip_cost_bps: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    model_run: Mapped[ModelRun] = relationship()


class ModelEvaluationMetric(Base):
    """One holding-period and market-state slice within a model evaluation."""

    __tablename__ = "model_evaluation_metrics"
    __table_args__ = (UniqueConstraint("model_evaluation_id", "horizon_days", "metric_scope"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_evaluation_id: Mapped[int] = mapped_column(ForeignKey("model_evaluations.id"), nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    metric_scope: Mapped[str] = mapped_column(Text, nullable=False, default="overall")
    market_regime: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_regime: Mapped[str | None] = mapped_column(Text, nullable=True)
    buy_gate: Mapped[str | None] = mapped_column(Text, nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hit_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    median_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    gross_avg_return: Mapped[float | None] = mapped_column(Float, nullable=True)
    avg_drawdown: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_drawdown: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit_loss_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    turnover: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    metrics_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    evaluation: Mapped[ModelEvaluation] = relationship()


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


class JobDefinition(Base):
    """Stable task metadata; ``DataJob`` remains the per-execution run record."""

    __tablename__ = "job_definitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_type: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(Text, nullable=False, default="maintenance")
    markets_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    schedule_rule: Mapped[str | None] = mapped_column(Text, nullable=True)
    timeout_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class JobRunDependency(Base):
    __tablename__ = "job_run_dependencies"
    __table_args__ = (UniqueConstraint("job_id", "upstream_job_id", "dependency_type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("data_jobs.id"), nullable=False)
    upstream_job_id: Mapped[int] = mapped_column(ForeignKey("data_jobs.id"), nullable=False)
    dependency_type: Mapped[str] = mapped_column(Text, nullable=False, default="source_job")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="waiting")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    required_as_of_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    actual_as_of_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class JobRunAttempt(Base):
    __tablename__ = "job_run_attempts"
    __table_args__ = (UniqueConstraint("job_id", "attempt_no"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("data_jobs.id"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[str] = mapped_column(Text, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class MarketRefreshBatch(Base):
    __tablename__ = "market_refresh_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_job_id: Mapped[int | None] = mapped_column(ForeignKey("data_jobs.id"), nullable=True)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    requested_as_of_date: Mapped[str] = mapped_column(Text, nullable=False)
    actual_as_of_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    universe_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    no_trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inactive_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    partial_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    summary_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[str] = mapped_column(Text, nullable=False)
    finished_at: Mapped[str | None] = mapped_column(Text, nullable=True)

    source_job: Mapped[DataJob | None] = relationship()


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


class StorageCapacitySample(Base):
    """One idempotent PostgreSQL capacity/maintenance sample per calendar day."""

    __tablename__ = "storage_capacity_samples"
    __table_args__ = (
        UniqueConstraint("sample_date", name="uq_storage_capacity_samples_date"),
        Index("ix_storage_capacity_samples_date", "sample_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sample_date: Mapped[date] = mapped_column(Date, nullable=False)
    database_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    physical_hot_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    legacy_prediction_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    workspace_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    job_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    table_metrics_json: Mapped[str] = mapped_column(Text, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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


class _PhysicalFundamentalSnapshotMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol_id: Mapped[int] = mapped_column(Integer, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    report_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    listing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    pe_ttm: Mapped[float | None] = mapped_column(Float, nullable=True)
    dividend_yield: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    roe_avg_3y: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_profit_yoy: Mapped[float | None] = mapped_column(Float, nullable=True)
    revenue_yoy: Mapped[float | None] = mapped_column(Float, nullable=True)
    debt_to_assets: Mapped[float | None] = mapped_column(Float, nullable=True)
    data_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CNFundamentalSnapshot(_PhysicalFundamentalSnapshotMixin, Base):
    __tablename__ = "cn_fundamental_snapshots"
    __table_args__ = (
        UniqueConstraint("symbol_id", "report_date", "source"),
        CheckConstraint("market = 'CN'", name="ck_cn_fundamental_snapshots_market"),
        ForeignKeyConstraint(
            ["symbol_id", "market"],
            ["symbols.id", "symbols.market"],
            name="fk_cn_fundamental_snapshots_symbol_market",
            ondelete="CASCADE",
        ),
        Index("ix_cn_fundamental_symbol_report", "symbol_id", "report_date"),
    )


class USFundamentalSnapshot(_PhysicalFundamentalSnapshotMixin, Base):
    __tablename__ = "us_fundamental_snapshots"
    __table_args__ = (
        UniqueConstraint("symbol_id", "report_date", "source"),
        CheckConstraint("market = 'US'", name="ck_us_fundamental_snapshots_market"),
        ForeignKeyConstraint(
            ["symbol_id", "market"],
            ["symbols.id", "symbols.market"],
            name="fk_us_fundamental_snapshots_symbol_market",
            ondelete="CASCADE",
        ),
        Index("ix_us_fundamental_symbol_report", "symbol_id", "report_date"),
    )


class HKFundamentalSnapshot(_PhysicalFundamentalSnapshotMixin, Base):
    __tablename__ = "hk_fundamental_snapshots"
    __table_args__ = (
        UniqueConstraint("symbol_id", "report_date", "source"),
        CheckConstraint("market = 'HK'", name="ck_hk_fundamental_snapshots_market"),
        ForeignKeyConstraint(
            ["symbol_id", "market"],
            ["symbols.id", "symbols.market"],
            name="fk_hk_fundamental_snapshots_symbol_market",
            ondelete="CASCADE",
        ),
        Index("ix_hk_fundamental_symbol_report", "symbol_id", "report_date"),
    )


class PointInTimeFeatureSnapshot(Base):
    """Append-only feature observation with explicit knowledge timestamps."""

    __tablename__ = "point_in_time_feature_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "symbol_id",
            "feature_name",
            "source",
            "source_record_id",
            "revision_id",
        ),
        Index(
            "ix_pit_features_symbol_name_available",
            "symbol_id",
            "feature_name",
            "available_time",
        ),
        Index(
            "ix_pit_features_source_record_revision",
            "source",
            "source_record_id",
            "revision_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol_id: Mapped[int] = mapped_column(ForeignKey("symbols.id"), nullable=False)
    feature_name: Mapped[str] = mapped_column(Text, nullable=False)
    feature_value: Mapped[float] = mapped_column(Float, nullable=False)
    event_time: Mapped[str] = mapped_column(Text, nullable=False)
    available_time: Mapped[str] = mapped_column(Text, nullable=False)
    ingested_time: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_record_id: Mapped[str] = mapped_column(Text, nullable=False)
    revision_id: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)

    symbol: Mapped[Symbol] = relationship()


class _PhysicalPointInTimeFeatureMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol_id: Mapped[int] = mapped_column(Integer, nullable=False)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    feature_name: Mapped[str] = mapped_column(Text, nullable=False)
    feature_value: Mapped[float] = mapped_column(Float, nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    available_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_record_id: Mapped[str] = mapped_column(Text, nullable=False)
    revision_id: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CNPointInTimeFeature(_PhysicalPointInTimeFeatureMixin, Base):
    __tablename__ = "cn_point_in_time_features"
    __table_args__ = (
        UniqueConstraint(
            "symbol_id",
            "feature_name",
            "source",
            "source_record_id",
            "revision_id",
        ),
        CheckConstraint("market = 'CN'", name="ck_cn_point_in_time_features_market"),
        ForeignKeyConstraint(
            ["symbol_id", "market"],
            ["symbols.id", "symbols.market"],
            name="fk_cn_point_in_time_features_symbol_market",
            ondelete="CASCADE",
        ),
        Index(
            "ix_cn_pit_features_symbol_name_available",
            "symbol_id",
            "feature_name",
            "available_time",
        ),
        Index(
            "ix_cn_pit_features_source_record_revision",
            "source",
            "source_record_id",
            "revision_id",
        ),
    )


class USPointInTimeFeature(_PhysicalPointInTimeFeatureMixin, Base):
    __tablename__ = "us_point_in_time_features"
    __table_args__ = (
        UniqueConstraint(
            "symbol_id",
            "feature_name",
            "source",
            "source_record_id",
            "revision_id",
        ),
        CheckConstraint("market = 'US'", name="ck_us_point_in_time_features_market"),
        ForeignKeyConstraint(
            ["symbol_id", "market"],
            ["symbols.id", "symbols.market"],
            name="fk_us_point_in_time_features_symbol_market",
            ondelete="CASCADE",
        ),
        Index(
            "ix_us_pit_features_symbol_name_available",
            "symbol_id",
            "feature_name",
            "available_time",
        ),
        Index(
            "ix_us_pit_features_source_record_revision",
            "source",
            "source_record_id",
            "revision_id",
        ),
    )


class HKPointInTimeFeature(_PhysicalPointInTimeFeatureMixin, Base):
    __tablename__ = "hk_point_in_time_features"
    __table_args__ = (
        UniqueConstraint(
            "symbol_id",
            "feature_name",
            "source",
            "source_record_id",
            "revision_id",
        ),
        CheckConstraint("market = 'HK'", name="ck_hk_point_in_time_features_market"),
        ForeignKeyConstraint(
            ["symbol_id", "market"],
            ["symbols.id", "symbols.market"],
            name="fk_hk_point_in_time_features_symbol_market",
            ondelete="CASCADE",
        ),
        Index(
            "ix_hk_pit_features_symbol_name_available",
            "symbol_id",
            "feature_name",
            "available_time",
        ),
        Index(
            "ix_hk_pit_features_source_record_revision",
            "source",
            "source_record_id",
            "revision_id",
        ),
    )


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


class _PhysicalTechnicalSnapshotMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol_id: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    market: Mapped[str] = mapped_column(Text, nullable=False)
    as_of_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    limit_up_yesterday: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    volume_breakout: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ma_cluster: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bullish_ma_stack: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    macd_underwater_cross: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    matched_patterns_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CNTechnicalSnapshot(_PhysicalTechnicalSnapshotMixin, Base):
    __tablename__ = "cn_technical_snapshots"
    __table_args__ = (
        CheckConstraint("market = 'CN'", name="ck_cn_technical_snapshots_market"),
        ForeignKeyConstraint(
            ["symbol_id", "market"],
            ["symbols.id", "symbols.market"],
            name="fk_cn_technical_snapshots_symbol_market",
            ondelete="CASCADE",
        ),
        Index("ix_cn_technical_as_of_date", "as_of_date"),
    )


class USTechnicalSnapshot(_PhysicalTechnicalSnapshotMixin, Base):
    __tablename__ = "us_technical_snapshots"
    __table_args__ = (
        CheckConstraint("market = 'US'", name="ck_us_technical_snapshots_market"),
        ForeignKeyConstraint(
            ["symbol_id", "market"],
            ["symbols.id", "symbols.market"],
            name="fk_us_technical_snapshots_symbol_market",
            ondelete="CASCADE",
        ),
        Index("ix_us_technical_as_of_date", "as_of_date"),
    )


class HKTechnicalSnapshot(_PhysicalTechnicalSnapshotMixin, Base):
    __tablename__ = "hk_technical_snapshots"
    __table_args__ = (
        CheckConstraint("market = 'HK'", name="ck_hk_technical_snapshots_market"),
        ForeignKeyConstraint(
            ["symbol_id", "market"],
            ["symbols.id", "symbols.market"],
            name="fk_hk_technical_snapshots_symbol_market",
            ondelete="CASCADE",
        ),
        Index("ix_hk_technical_as_of_date", "as_of_date"),
    )
