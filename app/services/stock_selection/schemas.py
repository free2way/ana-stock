from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Mapping


SCHEMA_VERSION = "stock_selection_contracts_v1"
LABELED_SAMPLE_SCHEMA_VERSION = "stock_selection_labeled_sample_v2"


class InsightDirection(StrEnum):
    UP = "up"
    DOWN = "down"
    FLAT = "flat"


class InsightStatus(StrEnum):
    ACTIVE = "active"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


def _require_non_empty(name: str, value: str) -> None:
    if not str(value or "").strip():
        raise ValueError(f"{name} must not be empty")


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ResearchProtocol:
    protocol_version: str
    market: str
    horizon_days: int
    purge_sessions: int
    entry_price_mode: str = "next_open"
    exit_price_mode: str = "close"
    round_trip_cost_bps: float = 20.0
    embargo_sessions: int = 0
    universe_version: str = "unversioned"
    label_version: str = "next_open_industry_excess_dd_v1"
    feature_set_version: str = "unversioned"
    training_protocol_version: str = "point_in_time_purged_v2"
    engine_version: str = "event_driven_daily_v2"
    reality_model_version: str = "unversioned"
    random_seed: int = 42
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_non_empty("protocol_version", self.protocol_version)
        _require_non_empty("market", self.market)
        if self.horizon_days <= 0:
            raise ValueError("horizon_days must be positive")
        if self.round_trip_cost_bps < 0:
            raise ValueError("round_trip_cost_bps must not be negative")
        if self.purge_sessions < self.horizon_days:
            raise ValueError("purge_sessions must be at least horizon_days")
        if self.embargo_sessions < 0:
            raise ValueError("embargo_sessions must not be negative")
        if self.entry_price_mode != "next_open":
            raise ValueError("v2 research protocol requires entry_price_mode=next_open")

    def content_hash(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class UniverseSnapshot:
    snapshot_id: str
    market: str
    trade_date: date
    ticker: str
    included: bool
    source_as_of: datetime
    universe_version: str
    exclusion_reason_codes: tuple[str, ...] = ()
    security_type: str = "equity"
    price: float | None = None
    adv20: float | None = None
    volume: float | None = None
    listing_age_sessions: int | None = None
    suspended: bool = False
    limit_status: str | None = None
    corporate_action_status: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name, value in (
            ("snapshot_id", self.snapshot_id),
            ("market", self.market),
            ("ticker", self.ticker),
            ("universe_version", self.universe_version),
        ):
            _require_non_empty(name, value)
        _require_aware("source_as_of", self.source_as_of)
        if self.included and self.exclusion_reason_codes:
            raise ValueError("included universe member cannot have exclusion reasons")
        if not self.included and not self.exclusion_reason_codes:
            raise ValueError("excluded universe member must record at least one reason")
        if self.listing_age_sessions is not None and self.listing_age_sessions < 0:
            raise ValueError("listing_age_sessions must not be negative")


@dataclass(frozen=True, slots=True)
class SelectionInsight:
    insight_id: str
    market: str
    ticker: str
    generated_at: datetime
    data_cutoff_at: datetime
    effective_from: datetime
    expires_at: datetime
    horizon_days: int
    direction: InsightDirection
    raw_score: float
    cross_sectional_rank: float
    source_model: str
    model_version: str
    universe_version: str
    expected_excess_return: float | None = None
    confidence: float | None = None
    status: InsightStatus = InsightStatus.ACTIVE
    explanation: Mapping[str, Any] = field(default_factory=dict)
    risk_tags: tuple[str, ...] = ()
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name, value in (
            ("insight_id", self.insight_id),
            ("market", self.market),
            ("ticker", self.ticker),
            ("source_model", self.source_model),
            ("model_version", self.model_version),
            ("universe_version", self.universe_version),
        ):
            _require_non_empty(name, value)
        for name, value in (
            ("generated_at", self.generated_at),
            ("data_cutoff_at", self.data_cutoff_at),
            ("effective_from", self.effective_from),
            ("expires_at", self.expires_at),
        ):
            _require_aware(name, value)
        if not (self.data_cutoff_at <= self.generated_at < self.effective_from <= self.expires_at):
            raise ValueError(
                "insight time order must satisfy data_cutoff_at <= generated_at "
                "< effective_from <= expires_at"
            )
        if self.horizon_days <= 0:
            raise ValueError("horizon_days must be positive")
        if not 0.0 <= self.cross_sectional_rank <= 1.0:
            raise ValueError("cross_sectional_rank must be in [0, 1]")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class PortfolioTarget:
    target_id: str
    insight_id: str
    market: str
    ticker: str
    generated_at: datetime
    target_weight: float
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_non_empty("target_id", self.target_id)
        _require_non_empty("insight_id", self.insight_id)
        _require_non_empty("market", self.market)
        _require_non_empty("ticker", self.ticker)
        _require_aware("generated_at", self.generated_at)
        if not -1.0 <= self.target_weight <= 1.0:
            raise ValueError("target_weight must be in [-1, 1]")


@dataclass(frozen=True, slots=True)
class RiskAdjustedTarget:
    target_id: str
    source_target_id: str
    insight_id: str
    market: str
    ticker: str
    generated_at: datetime
    original_weight: float
    adjusted_weight: float
    adjustment_reasons: tuple[str, ...] = ()
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_non_empty("target_id", self.target_id)
        _require_non_empty("source_target_id", self.source_target_id)
        _require_non_empty("insight_id", self.insight_id)
        _require_non_empty("market", self.market)
        _require_non_empty("ticker", self.ticker)
        _require_aware("generated_at", self.generated_at)
        if not -1.0 <= self.original_weight <= 1.0 or not -1.0 <= self.adjusted_weight <= 1.0:
            raise ValueError("target weights must be in [-1, 1]")


@dataclass(frozen=True, slots=True)
class OrderIntent:
    order_id: str
    source_target_id: str
    insight_id: str
    market: str
    ticker: str
    side: OrderSide
    quantity: float
    created_at: datetime
    effective_at: datetime
    order_type: str = "market_on_open"
    limit_price: float | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name, value in (
            ("order_id", self.order_id),
            ("source_target_id", self.source_target_id),
            ("insight_id", self.insight_id),
            ("market", self.market),
            ("ticker", self.ticker),
        ):
            _require_non_empty(name, value)
        _require_aware("created_at", self.created_at)
        _require_aware("effective_at", self.effective_at)
        if self.effective_at <= self.created_at:
            raise ValueError("order effective_at must be after created_at")
        if self.quantity <= 0:
            raise ValueError("order quantity must be positive")


@dataclass(frozen=True, slots=True)
class FillEvent:
    event_id: str
    order_id: str
    insight_id: str
    market: str
    ticker: str
    side: OrderSide
    event_time: datetime
    filled_quantity: float
    remaining_quantity: float
    fill_price: float
    fee: float
    slippage: float
    reality_model_version: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_non_empty("event_id", self.event_id)
        _require_non_empty("order_id", self.order_id)
        _require_non_empty("insight_id", self.insight_id)
        _require_non_empty("market", self.market)
        _require_non_empty("ticker", self.ticker)
        _require_non_empty("reality_model_version", self.reality_model_version)
        _require_aware("event_time", self.event_time)
        if self.filled_quantity <= 0 or self.remaining_quantity < 0:
            raise ValueError("fill quantities are invalid")
        if self.fill_price <= 0 or self.fee < 0 or self.slippage < 0:
            raise ValueError("fill price, fee, and slippage must be non-negative")


@dataclass(frozen=True, slots=True)
class RejectEvent:
    event_id: str
    order_id: str
    insight_id: str
    market: str
    ticker: str
    event_time: datetime
    reject_reason: str
    reality_model_version: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name, value in (
            ("event_id", self.event_id),
            ("order_id", self.order_id),
            ("insight_id", self.insight_id),
            ("market", self.market),
            ("ticker", self.ticker),
            ("reject_reason", self.reject_reason),
            ("reality_model_version", self.reality_model_version),
        ):
            _require_non_empty(name, value)
        _require_aware("event_time", self.event_time)


@dataclass(frozen=True, slots=True)
class CorporateActionEvent:
    event_id: str
    market: str
    ticker: str
    event_time: datetime
    action_type: str
    factor: float | None = None
    cash_amount: float | None = None
    replacement_ticker: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_non_empty("event_id", self.event_id)
        _require_non_empty("market", self.market)
        _require_non_empty("ticker", self.ticker)
        _require_non_empty("action_type", self.action_type)
        _require_aware("event_time", self.event_time)
        if self.factor is not None and self.factor <= 0:
            raise ValueError("corporate action factor must be positive")


@dataclass(frozen=True, slots=True)
class PortfolioState:
    state_id: str
    strategy_run_id: str
    event_time: datetime
    cash: float
    position_market_value: float
    nav: float
    gross_exposure: float
    net_exposure: float
    cumulative_fees: float = 0.0
    cumulative_slippage: float = 0.0
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_non_empty("state_id", self.state_id)
        _require_non_empty("strategy_run_id", self.strategy_run_id)
        _require_aware("event_time", self.event_time)
        if abs(self.nav - (self.cash + self.position_market_value)) > 1e-8:
            raise ValueError("portfolio accounting identity failed: nav != cash + position market value")
        if self.cumulative_fees < 0 or self.cumulative_slippage < 0:
            raise ValueError("cumulative costs must not be negative")


@dataclass(frozen=True, slots=True)
class LabeledSample:
    sample_id: str
    market: str
    ticker: str
    feature_date: date
    label_start_date: date
    label_end_date: date
    label_available_date: date
    horizon_days: int
    label_value: float | None
    features: Mapping[str, float] = field(default_factory=dict)
    label_components: Mapping[str, float] = field(default_factory=dict)
    tradable: bool = True
    exclusion_reason: str | None = None
    dataset_version: str = "unversioned"
    schema_version: str = LABELED_SAMPLE_SCHEMA_VERSION
    target_mode: str = "risk_adjusted_return"

    def __post_init__(self) -> None:
        _require_non_empty("sample_id", self.sample_id)
        _require_non_empty("market", self.market)
        _require_non_empty("ticker", self.ticker)
        if self.horizon_days <= 0:
            raise ValueError("horizon_days must be positive")
        if not (
            self.feature_date < self.label_start_date
            <= self.label_end_date
            <= self.label_available_date
        ):
            raise ValueError(
                "sample dates must satisfy feature_date < label_start_date "
                "<= label_end_date <= label_available_date"
            )
        if self.tradable and self.exclusion_reason:
            raise ValueError("tradable sample cannot have an exclusion reason")
        if not self.tradable and not self.exclusion_reason:
            raise ValueError("non-tradable sample must have an exclusion reason")
        if self.tradable and self.label_value is None:
            raise ValueError("tradable sample must have a label value")
        if not self.tradable and self.label_value is not None:
            raise ValueError("non-tradable sample must keep label value unknown")
        for name, value in self.label_components.items():
            if not str(name or "").strip():
                raise ValueError("label component name must not be empty")
            if not math.isfinite(float(value)):
                raise ValueError(f"label component {name!r} must be finite")
        if self.target_mode not in {"risk_adjusted_return", "net_return", "industry_excess_return"}:
            raise ValueError("unsupported sample target_mode")
        if self.label_value is not None and not math.isfinite(float(self.label_value)):
            raise ValueError("label_value must be finite")
        target = self.label_components.get(self.target_mode)
        if self.tradable and self.target_mode != "risk_adjusted_return" and target is None:
            raise ValueError("explicit target component required")
        if target is not None and self.label_value is not None and abs(float(target) - self.label_value) > 1e-12:
            raise ValueError(f"{self.target_mode} component must equal label_value")
