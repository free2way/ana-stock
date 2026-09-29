"""P1 research-only price/volume factors with point-in-time construction rules."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
import math
import statistics
from typing import Mapping, Sequence

from app.services.stock_selection.labels import PriceBar
from app.services.stock_selection.schemas import UniverseSnapshot


@dataclass(frozen=True, slots=True)
class P1FactorDefinition:
    name: str
    information_family: str
    unit: str
    window_sessions: int
    available_at: str
    missing_policy: str
    source_requirement: str
    engineering_status: str = "READY_RESEARCH_ONLY"


@dataclass(frozen=True, slots=True)
class P1PriceVolumeFeatureConfig:
    notional_zscore_sessions: int = 20
    illiquidity_sessions: int = 20
    minimum_industry_peers: int = 2
    schema_version: str = "p1_price_volume_feature_config_v1"

    def __post_init__(self) -> None:
        if self.notional_zscore_sessions < 2 or self.illiquidity_sessions < 2:
            raise ValueError("P1 feature windows must be at least two sessions")
        if self.minimum_industry_peers < 1:
            raise ValueError("minimum_industry_peers must be positive")

    def version(self) -> str:
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return f"p1_price_volume_features_v1:{hashlib.sha256(encoded.encode()).hexdigest()[:16]}"


@dataclass(frozen=True, slots=True)
class P1FeatureBuildResult:
    feature_set_version: str
    feature_names: tuple[str, ...]
    features_by_key: Mapping[tuple[str, date], Mapping[str, float]]
    eligible_key_count: int
    coverage_by_factor: Mapping[str, int]
    missing_industry_peer_count: int


P1_PRICE_VOLUME_FACTOR_NAMES = (
    "residual_momentum_20d",
    "industry_relative_momentum_20d",
    "notional_volume_zscore_20d",
    "amihud_illiquidity_20d_per_million",
    "opening_gap_1d",
    "intraday_return_1d",
)
P1_FACTOR_CATALOG_SCHEMA = "p1_factor_catalog_v1"


def p1_factor_definitions() -> dict[str, P1FactorDefinition]:
    definitions = (
        P1FactorDefinition(
            name="residual_momentum_20d",
            information_family="residual_momentum",
            unit="fractional_return_minus_same_date_universe_mean",
            window_sessions=20,
            available_at="official_daily_bar_close",
            missing_policy="omit_factor_value",
            source_requirement="point_in_time_daily_ohlcv_and_dated_universe",
        ),
        P1FactorDefinition(
            name="industry_relative_momentum_20d",
            information_family="industry_relative_strength",
            unit="fractional_return_minus_same_date_peer_mean",
            window_sessions=20,
            available_at="official_daily_bar_close",
            missing_policy="omit_when_dated_industry_or_minimum_peers_missing",
            source_requirement="point_in_time_daily_ohlcv_dated_universe_and_industry",
        ),
        P1FactorDefinition(
            name="notional_volume_zscore_20d",
            information_family="liquidity_crowding",
            unit="zscore_of_close_times_volume",
            window_sessions=20,
            available_at="official_daily_bar_close",
            missing_policy="omit_factor_value",
            source_requirement="point_in_time_daily_ohlcv",
        ),
        P1FactorDefinition(
            name="amihud_illiquidity_20d_per_million",
            information_family="liquidity_crowding",
            unit="mean_abs_return_per_million_close_times_volume",
            window_sessions=20,
            available_at="official_daily_bar_close",
            missing_policy="omit_when_notional_is_non_positive",
            source_requirement="point_in_time_daily_ohlcv",
        ),
        P1FactorDefinition(
            name="opening_gap_1d",
            information_family="opening_gap_intraday_structure",
            unit="fractional_return_from_previous_close_to_open",
            window_sessions=1,
            available_at="official_daily_bar_close",
            missing_policy="omit_factor_value",
            source_requirement="point_in_time_daily_ohlcv",
        ),
        P1FactorDefinition(
            name="intraday_return_1d",
            information_family="opening_gap_intraday_structure",
            unit="fractional_return_from_open_to_close",
            window_sessions=1,
            available_at="official_daily_bar_close",
            missing_policy="omit_factor_value",
            source_requirement="point_in_time_daily_ohlcv",
        ),
        P1FactorDefinition(
            name="northbound_net_inflow_5d",
            information_family="fund_flow",
            unit="source_defined_currency_amount",
            window_sessions=5,
            available_at="source_publication_time",
            missing_policy="blocked_never_zero_fill",
            source_requirement="historical_point_in_time_fund_flow",
            engineering_status="BLOCKED_PENDING_HISTORICAL_COVERAGE_AUDIT",
        ),
        P1FactorDefinition(
            name="main_fund_net_amount_5d",
            information_family="fund_flow",
            unit="source_defined_currency_amount",
            window_sessions=5,
            available_at="source_publication_time",
            missing_policy="blocked_never_zero_fill",
            source_requirement="historical_point_in_time_fund_flow",
            engineering_status="BLOCKED_PENDING_HISTORICAL_COVERAGE_AUDIT",
        ),
    )
    return {item.name: item for item in definitions}


def p1_factor_catalog_version() -> str:
    payload = {
        "schema_version": P1_FACTOR_CATALOG_SCHEMA,
        "definitions": {
            name: asdict(definition)
            for name, definition in sorted(p1_factor_definitions().items())
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"{P1_FACTOR_CATALOG_SCHEMA}:{hashlib.sha256(encoded.encode()).hexdigest()[:16]}"


def _industry_for(
    industries: Mapping[str | tuple[str, date], str | None],
    ticker: str,
    feature_date: date,
) -> str | None:
    # P1 industry-relative research is intentionally stricter than legacy
    # benchmark fallbacks: a current/static industry cannot be borrowed for a
    # historical date.
    value = industries.get((ticker, feature_date))
    text = str(value or "").strip()
    return text or None


def _return(current: float, previous: float) -> float | None:
    if current <= 0 or previous <= 0:
        return None
    return (current / previous) - 1.0


def build_p1_price_volume_features(
    *,
    base_features_by_key: Mapping[tuple[str, date], Mapping[str, float]],
    bars_by_ticker: Mapping[str, Sequence[PriceBar]],
    universe_snapshots: Sequence[UniverseSnapshot],
    industries: Mapping[str | tuple[str, date], str | None],
    config: P1PriceVolumeFeatureConfig | None = None,
) -> P1FeatureBuildResult:
    settings = config or P1PriceVolumeFeatureConfig()
    included = {
        (item.ticker.upper(), item.trade_date)
        for item in universe_snapshots
        if item.included
    }
    time_series: dict[tuple[str, date], dict[str, float]] = {}
    for raw_ticker in sorted(bars_by_ticker):
        ticker = str(raw_ticker).strip().upper()
        bars = sorted(bars_by_ticker[raw_ticker], key=lambda item: item.trade_date)
        minimum_index = max(settings.notional_zscore_sessions, settings.illiquidity_sessions)
        for index in range(minimum_index, len(bars)):
            bar = bars[index]
            key = (ticker, bar.trade_date)
            if key not in included or key not in base_features_by_key:
                continue
            previous = bars[index - 1]
            gap = _return(bar.open, previous.close)
            intraday = _return(bar.close, bar.open)
            notional_history = [
                item.close * item.volume
                for item in bars[index - settings.notional_zscore_sessions : index]
                if item.close > 0 and item.volume >= 0
            ]
            current_notional = bar.close * bar.volume
            values: dict[str, float] = {}
            if gap is not None:
                values["opening_gap_1d"] = gap
            if intraday is not None:
                values["intraday_return_1d"] = intraday
            if len(notional_history) == settings.notional_zscore_sessions and current_notional >= 0:
                mean_notional = statistics.fmean(notional_history)
                std_notional = statistics.pstdev(notional_history)
                values["notional_volume_zscore_20d"] = (
                    (current_notional - mean_notional) / std_notional
                    if std_notional > 1e-15
                    else 0.0
                )
            illiquidity_values = []
            start = index - settings.illiquidity_sessions + 1
            for position in range(start, index + 1):
                current = bars[position]
                prior = bars[position - 1]
                daily_return = _return(current.close, prior.close)
                notional = current.close * current.volume
                if daily_return is not None and notional > 0:
                    illiquidity_values.append(abs(daily_return) / (notional / 1_000_000.0))
            if len(illiquidity_values) == settings.illiquidity_sessions:
                values["amihud_illiquidity_20d_per_million"] = statistics.fmean(illiquidity_values)
            time_series[key] = values

    by_date: dict[date, list[tuple[str, dict[str, float]]]] = defaultdict(list)
    for (ticker, feature_date), values in time_series.items():
        momentum = base_features_by_key[(ticker, feature_date)].get("momentum_20d")
        try:
            parsed = float(momentum)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed):
            by_date[feature_date].append((ticker, values))

    missing_industry_peer_count = 0
    for feature_date, rows in by_date.items():
        momentum_by_ticker = {
            ticker: float(base_features_by_key[(ticker, feature_date)]["momentum_20d"])
            for ticker, _ in rows
        }
        market_mean = statistics.fmean(momentum_by_ticker.values())
        industry_groups: dict[str, list[str]] = defaultdict(list)
        for ticker in momentum_by_ticker:
            industry = _industry_for(industries, ticker, feature_date)
            if industry:
                industry_groups[industry].append(ticker)
        for ticker, values in rows:
            momentum = momentum_by_ticker[ticker]
            values["residual_momentum_20d"] = momentum - market_mean
            industry = _industry_for(industries, ticker, feature_date)
            peers = [item for item in industry_groups.get(industry or "", []) if item != ticker]
            if len(peers) >= settings.minimum_industry_peers:
                values["industry_relative_momentum_20d"] = momentum - statistics.fmean(
                    momentum_by_ticker[item] for item in peers
                )
            else:
                missing_industry_peer_count += 1

    coverage = {
        factor_name: sum(factor_name in values for values in time_series.values())
        for factor_name in P1_PRICE_VOLUME_FACTOR_NAMES
    }
    return P1FeatureBuildResult(
        feature_set_version=settings.version(),
        feature_names=P1_PRICE_VOLUME_FACTOR_NAMES,
        features_by_key={key: dict(values) for key, values in sorted(time_series.items())},
        eligible_key_count=len(time_series),
        coverage_by_factor=coverage,
        missing_industry_peer_count=missing_industry_peer_count,
    )


__all__ = [
    "P1FactorDefinition",
    "P1FeatureBuildResult",
    "P1PriceVolumeFeatureConfig",
    "P1_PRICE_VOLUME_FACTOR_NAMES",
    "build_p1_price_volume_features",
    "p1_factor_catalog_version",
    "p1_factor_definitions",
]
