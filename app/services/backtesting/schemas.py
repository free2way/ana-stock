from __future__ import annotations

from dataclasses import dataclass, field
from app.services.execution_costs import FillCostModel


@dataclass(frozen=True, slots=True)
class DailyBar:
    ticker: str
    trade_date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_st: bool = False
    previous_close: float | None = None
    adv20: float = 0.0
    price_basis: str = "raw"

    def __post_init__(self) -> None:
        if not self.ticker or not self.trade_date:
            raise ValueError("bar ticker and trade_date are required")
        if min(self.open, self.high, self.low, self.close, self.volume) < 0:
            raise ValueError("bar prices and volume must be non-negative")
        if self.price_basis not in {"raw", "adjusted"}:
            raise ValueError("price_basis must be raw or adjusted")


@dataclass(frozen=True, slots=True)
class SignalCandidate:
    signal_date: str
    ticker: str
    score: float
    rank_value: float | None = None
    insight_id: str | None = None
    ordinal: int | None = None
    sector: str | None = None
    qualified: bool = True


@dataclass(frozen=True, slots=True)
class EngineConfig:
    market: str
    top_n: int = 5
    holding_days: int = 5
    initial_cash: float = 1_000_000.0
    commission_bps: float = 8.0
    slippage_bps: float = 12.0
    max_position_weight: float = 0.10
    max_sector_weight: float = 1.0
    max_gross_exposure: float = 1.0
    max_participation_rate: float = 1.0
    min_signal_score: float = 0.0
    min_adv: float = 0.0
    max_gap_pct: float = 1.0
    liquidate_at_end: bool = False
    calendar_version: str = "market_calendar_2026_v1"
    engine_version: str = "event_driven_daily_v3"
    reality_model_version: str = "daily_ohlcv_basic_v2"

    def __post_init__(self) -> None:
        if str(self.market or "").upper() not in {"CN", "US"}:
            raise ValueError("market must be CN or US")
        FillCostModel(self.commission_bps, self.slippage_bps)
        if self.top_n <= 0 or self.holding_days <= 0:
            raise ValueError("top_n and holding_days must be positive")
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if min(self.commission_bps, self.slippage_bps, self.min_adv, self.max_gap_pct) < 0:
            raise ValueError("cost and gate settings must be non-negative")
        for name, value in (
            ("max_position_weight", self.max_position_weight),
            ("max_sector_weight", self.max_sector_weight),
            ("max_gross_exposure", self.max_gross_exposure),
            ("max_participation_rate", self.max_participation_rate),
        ):
            if not 0 < value <= 1:
                raise ValueError(f"{name} must be in (0, 1]")
        if self.max_position_weight > self.max_gross_exposure:
            raise ValueError("max_position_weight must not exceed max_gross_exposure")


@dataclass(frozen=True, slots=True)
class MarketCorporateAction:
    ticker: str
    effective_date: str
    action_type: str
    factor: float | None = None
    cash_amount: float | None = None

    def __post_init__(self) -> None:
        if not self.ticker or not self.effective_date:
            raise ValueError("corporate action ticker and effective_date are required")
        if self.action_type not in {"split", "cash_dividend"}:
            raise ValueError("unsupported corporate action type")
        if self.action_type == "split" and (self.factor is None or self.factor <= 0):
            raise ValueError("split factor must be positive")
        if self.action_type == "cash_dividend" and (self.cash_amount is None or self.cash_amount < 0):
            raise ValueError("cash dividend must be non-negative")


@dataclass(slots=True)
class PositionLot:
    lot_id: str
    ticker: str
    quantity: float
    entry_price: float
    entry_date: str
    exit_date: str
    insight_id: str
    sector: str | None = None
    entry_notional: float = 0.0
    entry_fee: float = 0.0
    cash_distributions: float = 0.0


@dataclass(frozen=True, slots=True)
class EngineResult:
    metrics: tuple[dict, ...]
    orders: tuple[dict, ...]
    fills: tuple[dict, ...]
    rejects: tuple[dict, ...]
    portfolio_states: tuple[dict, ...]
    initial_cash: float
    end_nav: float
    cumulative_fees: float
    cumulative_slippage: float
    open_position_count: int
    gate_stats: dict[str, int] = field(default_factory=dict)
    outcomes: tuple[dict, ...] = ()
    corporate_action_events: tuple[dict, ...] = ()
    cost_model_metadata: dict = field(default_factory=dict)
