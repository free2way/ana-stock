from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import date
from typing import Iterable, Mapping, Sequence

from app.services.stock_selection.factor_pipeline import FactorSpec
from app.services.stock_selection.factor_sets import original_price_factor_specs
from app.services.stock_selection.feature_availability import PointInTimeFeatureRecord
from app.services.stock_selection.labels import PriceBar
from app.services.stock_selection.execution_evidence import ResearchExecutionEvidence
from app.services.stock_selection.point_in_time_features import (
    PointInTimeFeatureJoinConfig,
    PointInTimeFeatureJoinResult,
    build_point_in_time_feature_join,
    merge_point_in_time_features,
)
from app.services.stock_selection.p1_factors import (
    P1FeatureBuildResult,
    P1PriceVolumeFeatureConfig,
    build_p1_price_volume_features,
)
from app.services.stock_selection.sample_builder import (
    SampleBuildConfig,
    SampleBuildResult,
    build_training_samples,
)
from app.services.stock_selection.universe import (
    SecurityMetadata,
    UniverseBuildResult,
    UniverseRuleConfig,
    build_point_in_time_universe,
)


@dataclass(frozen=True, slots=True)
class PriceFeatureConfig:
    minimum_history_sessions: int = 61
    momentum_short_sessions: int = 5
    momentum_medium_sessions: int = 20
    momentum_long_sessions: int = 60
    volatility_sessions: int = 20
    efficiency_ratio_sessions: int = 20
    volume_sessions: int = 20
    drawdown_sessions: int = 60
    schema_version: str = "price_factor_set_v1"

    def __post_init__(self) -> None:
        windows = (
            self.minimum_history_sessions,
            self.momentum_short_sessions,
            self.momentum_medium_sessions,
            self.momentum_long_sessions,
            self.volatility_sessions,
            self.efficiency_ratio_sessions,
            self.volume_sessions,
            self.drawdown_sessions,
        )
        if any(value <= 0 for value in windows):
            raise ValueError("price feature windows must be positive")
        # ma20_slope_5d also reads a fixed 25-session window. Smaller custom
        # settings must not permit negative slices that can touch future rows.
        required = max(25, max(windows[1:]) + 1)
        if self.minimum_history_sessions < required:
            raise ValueError("minimum_history_sessions is shorter than a configured feature window")

    def version(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return f"price_factor_set_v1:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"


@dataclass(frozen=True, slots=True)
class PriceFeatureBuildResult:
    feature_set_version: str
    feature_names: tuple[str, ...]
    features_by_key: Mapping[tuple[str, date], Mapping[str, float]]
    row_count: int
    ticker_count: int


@dataclass(frozen=True, slots=True)
class RelativeReturnBuildResult:
    market_returns: Mapping[tuple[date, int], float]
    industry_returns: Mapping[tuple[str, date, int], float]
    industry_peer_count: int
    market_fallback_count: int


@dataclass(frozen=True, slots=True)
class ProductionResearchDataset:
    market: str
    source_version: str
    trading_dates: tuple[date, ...]
    bars_by_ticker: Mapping[str, tuple[PriceBar, ...]]
    feature_result: PriceFeatureBuildResult
    universe_result: UniverseBuildResult
    benchmark_result: RelativeReturnBuildResult
    sample_result: SampleBuildResult
    invalid_row_count: int
    point_in_time_feature_result: PointInTimeFeatureJoinResult | None
    p1_feature_result: P1FeatureBuildResult | None = None


def default_price_factor_specs() -> tuple[FactorSpec, ...]:
    return original_price_factor_specs()


def _parse_date(value: object) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError as exc:
        raise ValueError(f"invalid price row date: {value!r}") from exc


def _float(value: object) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid numeric market value: {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError(f"non-finite numeric market value: {value!r}")
    return result


def normalize_price_rows(
    rows: Iterable[Mapping[str, object]],
) -> tuple[dict[str, tuple[PriceBar, ...]], int]:
    bars: dict[str, dict[date, PriceBar]] = defaultdict(dict)
    invalid_count = 0
    for row in rows:
        ticker = str(row.get("symbol") or row.get("ticker") or "").strip().upper()
        if not ticker:
            invalid_count += 1
            continue
        try:
            bar = PriceBar(
                trade_date=_parse_date(row.get("date") or row.get("trade_date")),
                open=_float(row.get("open")),
                high=_float(row.get("high")),
                low=_float(row.get("low")),
                close=_float(row.get("close")),
                volume=_float(row.get("volume") or 0.0),
            )
        except (ValueError, TypeError):
            invalid_count += 1
            continue
        if bar.trade_date in bars[ticker]:
            raise ValueError(f"duplicate market row for {ticker} on {bar.trade_date.isoformat()}")
        bars[ticker][bar.trade_date] = bar
    return {
        ticker: tuple(sorted(items.values(), key=lambda item: item.trade_date))
        for ticker, items in sorted(bars.items())
    }, invalid_count


def _return(current: float, previous: float) -> float:
    return (current / previous) - 1.0 if current > 0 and previous > 0 else 0.0


def build_price_features(
    bars_by_ticker: Mapping[str, Sequence[PriceBar]],
    *,
    config: PriceFeatureConfig | None = None,
) -> PriceFeatureBuildResult:
    settings = config or PriceFeatureConfig()
    output: dict[tuple[str, date], Mapping[str, float]] = {}
    for ticker in sorted(bars_by_ticker):
        bars = sorted(bars_by_ticker[ticker], key=lambda item: item.trade_date)
        closes = [item.close for item in bars]
        volumes = [item.volume for item in bars]
        for index, bar in enumerate(bars):
            if index + 1 < settings.minimum_history_sessions:
                continue
            short_anchor = closes[index - settings.momentum_short_sessions]
            medium_anchor = closes[index - settings.momentum_medium_sessions]
            long_anchor = closes[index - settings.momentum_long_sessions]
            ma20 = statistics.fmean(closes[index - 19 : index + 1])
            previous_ma20 = statistics.fmean(closes[index - 24 : index - 4])
            return_window = [
                _return(closes[position], closes[position - 1])
                for position in range(index - settings.volatility_sessions + 1, index + 1)
            ]
            efficiency_start = index - settings.efficiency_ratio_sessions
            direction = abs(closes[index] - closes[efficiency_start])
            path = sum(
                abs(closes[position] - closes[position - 1])
                for position in range(efficiency_start + 1, index + 1)
            )
            volume_history = volumes[index - settings.volume_sessions : index]
            average_volume = statistics.fmean(volume_history) if volume_history else 0.0
            drawdown_start = index - settings.drawdown_sessions + 1
            drawdown_bars = bars[drawdown_start : index + 1]
            drawdown_high = max(item.high for item in drawdown_bars)
            location_bars = bars[index - settings.momentum_medium_sessions + 1 : index + 1]
            location_high = max(item.high for item in location_bars)
            location_low = min(item.low for item in location_bars)
            intraday_bars = bars[max(0, index - 4) : index + 1]
            output[(ticker, bar.trade_date)] = {
                "momentum_5d": _return(bar.close, short_anchor),
                "momentum_20d": _return(bar.close, medium_anchor),
                "momentum_60d": _return(bar.close, long_anchor),
                "price_vs_ma20": _return(bar.close, ma20),
                "ma20_slope_5d": _return(ma20, previous_ma20),
                "efficiency_ratio_20d": direction / path if path > 1e-15 else 0.0,
                "close_location_20d": (
                    ((bar.close - location_low) / (location_high - location_low)) - 0.5
                    if location_high > location_low
                    else 0.0
                ),
                "volume_ratio_20d": (bar.volume / average_volume) - 1.0 if average_volume > 0 else 0.0,
                "dollar_volume_log": math.log10(max(0.0, bar.close * bar.volume) + 1.0),
                "volatility_20d": statistics.pstdev(return_window),
                "drawdown_from_60d_high": (drawdown_high / bar.close) - 1.0,
                "intraday_range_5d": statistics.fmean(
                    (item.high - item.low) / item.close for item in intraday_bars
                ),
            }
    feature_names = tuple(item.name for item in default_price_factor_specs())
    return PriceFeatureBuildResult(
        feature_set_version=settings.version(),
        feature_names=feature_names,
        features_by_key=output,
        row_count=len(output),
        ticker_count=len({key[0] for key in output}),
    )


def build_relative_return_benchmarks(
    *,
    trading_dates: Sequence[date],
    bars_by_ticker: Mapping[str, Sequence[PriceBar]],
    universe_result: UniverseBuildResult,
    industries: Mapping[str | tuple[str, date], str | None],
    horizons: Sequence[int],
    minimum_industry_peers: int = 2,
) -> RelativeReturnBuildResult:
    if minimum_industry_peers < 1:
        raise ValueError("minimum_industry_peers must be positive")
    dates = list(trading_dates)
    date_index = {item: index for index, item in enumerate(dates)}
    bars_by_key = {
        (ticker, bar.trade_date): bar
        for ticker, bars in bars_by_ticker.items()
        for bar in bars
    }
    included_by_date: dict[date, list[str]] = defaultdict(list)
    for snapshot in universe_result.snapshots:
        if snapshot.included:
            included_by_date[snapshot.trade_date].append(snapshot.ticker)

    market_returns: dict[tuple[date, int], float] = {}
    industry_returns: dict[tuple[str, date, int], float] = {}
    industry_peer_count = 0
    market_fallback_count = 0
    for signal_date in dates:
        signal_index = date_index[signal_date]
        tickers = included_by_date.get(signal_date, [])
        for horizon in horizons:
            exit_index = signal_index + horizon
            if exit_index >= len(dates):
                continue
            entry_date = dates[signal_index + 1]
            exit_date = dates[exit_index]
            returns: dict[str, float] = {}
            for ticker in tickers:
                entry = bars_by_key.get((ticker, entry_date))
                exit_bar = bars_by_key.get((ticker, exit_date))
                if entry is not None and exit_bar is not None and entry.open > 0:
                    returns[ticker] = (exit_bar.close / entry.open) - 1.0
            if not returns:
                continue
            market_return = statistics.fmean(returns.values())
            market_returns[(signal_date, int(horizon))] = market_return
            grouped: dict[str, list[str]] = defaultdict(list)
            for ticker in returns:
                industry = str(
                    industries.get((ticker, signal_date), industries.get(ticker)) or ""
                ).strip()
                if industry:
                    grouped[industry].append(ticker)
            for ticker in returns:
                industry = str(
                    industries.get((ticker, signal_date), industries.get(ticker)) or ""
                ).strip()
                peers = [item for item in grouped.get(industry, []) if item != ticker]
                if len(peers) >= minimum_industry_peers:
                    industry_returns[(ticker, signal_date, int(horizon))] = statistics.fmean(
                        returns[item] for item in peers
                    )
                    industry_peer_count += 1
                else:
                    industry_returns[(ticker, signal_date, int(horizon))] = market_return
                    market_fallback_count += 1
    return RelativeReturnBuildResult(
        market_returns=market_returns,
        industry_returns=industry_returns,
        industry_peer_count=industry_peer_count,
        market_fallback_count=market_fallback_count,
    )


def build_production_research_dataset(
    rows: Iterable[Mapping[str, object]],
    *,
    market: str,
    metadata: Mapping[str, SecurityMetadata],
    industries: Mapping[str | tuple[str, date], str | None],
    universe_rules: UniverseRuleConfig,
    sample_config: SampleBuildConfig,
    source_version: str,
    feature_config: PriceFeatureConfig | None = None,
    point_in_time_feature_records: Iterable[PointInTimeFeatureRecord] | None = None,
    point_in_time_feature_config: PointInTimeFeatureJoinConfig | None = None,
    point_in_time_source_version: str | None = None,
    execution_evidence: ResearchExecutionEvidence | None = None,
    p1_feature_config: P1PriceVolumeFeatureConfig | None = None,
) -> ProductionResearchDataset:
    market_code = str(market or "").strip().upper()
    if market_code not in {"CN", "US"}:
        raise ValueError("market must be CN or US")
    if universe_rules.market.upper() != market_code or sample_config.market.upper() != market_code:
        raise ValueError("dataset, universe and sample market values must match")
    bars_by_ticker, invalid_count = normalize_price_rows(rows)
    if not bars_by_ticker:
        raise ValueError("production research dataset has no valid price rows")
    trading_dates = sorted({bar.trade_date for bars in bars_by_ticker.values() for bar in bars})
    if sample_config.fill_cost_model:
        if execution_evidence is None:
            raise ValueError("fill-cost dataset requires explicit execution evidence and calendar")
        execution_evidence.validate_prices(bars_by_ticker, market=market_code)
        trading_dates = list(execution_evidence.trading_dates)
    normalized_rows = [
        {
            "symbol": ticker,
            "date": bar.trade_date.isoformat(),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        }
        for ticker, bars in bars_by_ticker.items()
        for bar in bars
    ]
    universe_result = build_point_in_time_universe(
        normalized_rows,
        trading_dates=trading_dates,
        metadata=metadata,
        rules=universe_rules,
        source_version=source_version,
    )
    normalized_industries: dict[str | tuple[str, date], str | None] = {}
    for key, value in industries.items():
        if isinstance(key, tuple) and len(key) == 2:
            ticker, effective_date = key
            normalized_industries[(str(ticker).strip().upper(), _parse_date(effective_date))] = value
        else:
            normalized_industries[str(key).strip().upper()] = value
    feature_result = build_price_features(bars_by_ticker, config=feature_config)
    p1_feature_result: P1FeatureBuildResult | None = None
    if p1_feature_config is not None:
        p1_feature_result = build_p1_price_volume_features(
            base_features_by_key=feature_result.features_by_key,
            bars_by_ticker=bars_by_ticker,
            universe_snapshots=universe_result.snapshots,
            industries=normalized_industries,
            config=p1_feature_config,
        )
        merged_features = {
            key: {
                **dict(values),
                **dict(p1_feature_result.features_by_key.get(key, {})),
            }
            for key, values in feature_result.features_by_key.items()
        }
        combined_payload = (
            feature_result.feature_set_version
            + ":"
            + p1_feature_result.feature_set_version
        )
        combined_digest = hashlib.sha256(combined_payload.encode("utf-8")).hexdigest()[:16]
        feature_result = PriceFeatureBuildResult(
            feature_set_version=f"price_plus_p1_v1:{combined_digest}",
            feature_names=tuple(
                dict.fromkeys(feature_result.feature_names + p1_feature_result.feature_names)
            ),
            features_by_key=merged_features,
            row_count=len(merged_features),
            ticker_count=len({key[0] for key in merged_features}),
        )
    point_in_time_result: PointInTimeFeatureJoinResult | None = None
    if point_in_time_feature_records is not None:
        if point_in_time_feature_config is None or not point_in_time_source_version:
            raise ValueError(
                "point-in-time records require feature config and point_in_time_source_version"
            )
        if point_in_time_feature_config.market.strip().upper() != market_code:
            raise ValueError("point-in-time feature market must match dataset market")
        universe_by_date: dict[date, list[str]] = defaultdict(list)
        price_feature_keys = set(feature_result.features_by_key)
        for snapshot in universe_result.snapshots:
            key = (snapshot.ticker, snapshot.trade_date)
            if snapshot.included and key in price_feature_keys:
                universe_by_date[snapshot.trade_date].append(snapshot.ticker)
        point_in_time_result = build_point_in_time_feature_join(
            point_in_time_feature_records,
            universe_by_date=universe_by_date,
            source_version=point_in_time_source_version,
            config=point_in_time_feature_config,
        )
        merged_features = merge_point_in_time_features(
            feature_result.features_by_key,
            point_in_time_result,
        )
        combined_payload = (
            feature_result.feature_set_version
            + ":"
            + point_in_time_result.feature_set_version
        )
        combined_digest = hashlib.sha256(combined_payload.encode("utf-8")).hexdigest()[:16]
        feature_result = PriceFeatureBuildResult(
            feature_set_version=f"price_plus_pit_fundamentals_v1:{combined_digest}",
            feature_names=tuple(
                dict.fromkeys(feature_result.feature_names + point_in_time_result.feature_names)
            ),
            features_by_key=merged_features,
            row_count=len(merged_features),
            ticker_count=len({key[0] for key in merged_features}),
        )
    benchmark_result = build_relative_return_benchmarks(
        trading_dates=trading_dates,
        bars_by_ticker=bars_by_ticker,
        universe_result=universe_result,
        industries=normalized_industries,
        horizons=sample_config.horizons,
    )
    entry_exclusions: dict[tuple[str, date], str] = {}
    for ticker, bars in bars_by_ticker.items():
        for bar in bars:
            if bar.volume <= 0:
                entry_exclusions[(ticker, bar.trade_date)] = "zero_volume_at_entry"
    sample_result = build_training_samples(
        trading_dates=trading_dates,
        bars_by_ticker=bars_by_ticker,
        universe_snapshots=universe_result.snapshots,
        features_by_key=feature_result.features_by_key,
        market_returns=benchmark_result.market_returns,
        industry_returns=benchmark_result.industry_returns,
        entry_exclusions=entry_exclusions,
        config=replace(sample_config, feature_set_version=feature_result.feature_set_version),
        execution_evidence=execution_evidence,
    )
    return ProductionResearchDataset(
        market=market_code,
        source_version=source_version,
        trading_dates=tuple(trading_dates),
        bars_by_ticker={key: tuple(value) for key, value in bars_by_ticker.items()},
        feature_result=feature_result,
        universe_result=universe_result,
        benchmark_result=benchmark_result,
        sample_result=sample_result,
        invalid_row_count=invalid_count,
        point_in_time_feature_result=point_in_time_result,
        p1_feature_result=p1_feature_result,
    )
