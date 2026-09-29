from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Mapping, Sequence

from app.services.stock_selection.labels import PriceBar, build_executable_label
from app.services.stock_selection.schemas import LabeledSample, UniverseSnapshot
from app.services.execution_costs import FillCostModel
from app.services.stock_selection.executable_outcomes import confirmed_outcome, ExecutionEligibility, FILL_COST_OUTCOME_VERSION
from app.services.stock_selection.execution_evidence import ResearchExecutionEvidence


@dataclass(frozen=True, slots=True)
class SampleBuildConfig:
    market: str
    horizons: tuple[int, ...] = (1, 3, 5)
    round_trip_cost_bps: float = 20.0
    drawdown_penalty: float = 0.25
    require_relative_returns: bool = True
    target_mode: str = "risk_adjusted_return"
    label_version: str = "next_open_industry_excess_dd_v1"
    feature_set_version: str = "unversioned"
    schema_version: str = "stock_selection_sample_builder_v2"
    fill_cost_model: FillCostModel | None = None

    def __post_init__(self) -> None:
        if str(self.market or "").upper() not in {"CN", "US"}:
            raise ValueError("market must be CN or US")
        if not self.horizons or any(item <= 0 for item in self.horizons):
            raise ValueError("horizons must contain positive values")
        if len(set(self.horizons)) != len(self.horizons):
            raise ValueError("horizons must not contain duplicates")
        if self.round_trip_cost_bps < 0 or self.drawdown_penalty < 0:
            raise ValueError("cost and drawdown penalty must not be negative")
        if self.target_mode not in {"risk_adjusted_return", "net_return", "industry_excess_return"}:
            raise ValueError("unsupported target_mode")
        if self.target_mode == "net_return" and self.require_relative_returns:
            raise ValueError("net_return target must not require relative returns")
        if self.target_mode == "industry_excess_return" and not self.require_relative_returns:
            raise ValueError("industry excess requires relative returns")
        if self.fill_cost_model is not None:
            if self.label_version != FILL_COST_OUTCOME_VERSION or self.round_trip_cost_bps != 0 or self.drawdown_penalty != 0:
                raise ValueError("fill-cost samples require v2 label and zero legacy cost/penalty")
            if self.market.upper() == "CN" and 1 in self.horizons:
                raise ValueError("CN fill-cost samples cannot use same-session exit")
        elif self.label_version == FILL_COST_OUTCOME_VERSION:
            raise ValueError("v2 label requires fill_cost_model")


@dataclass(frozen=True, slots=True)
class SampleBuildResult:
    dataset_version: str
    universe_version: str
    samples: tuple[LabeledSample, ...]
    eligible_count: int
    excluded_count: int
    exclusion_counts: Mapping[str, int]
    label_contract: Mapping[str, object] = field(default_factory=dict)

    def manifest(self) -> dict:
        return {
            "dataset_version": self.dataset_version,
            "universe_version": self.universe_version,
            "sample_count": len(self.samples),
            "eligible_count": self.eligible_count,
            "excluded_count": self.excluded_count,
            "exclusion_counts": dict(self.exclusion_counts),
            **({"label_contract": dict(self.label_contract)} if self.label_contract else {}),
        }


def _dataset_version(config: SampleBuildConfig, universe_version: str, execution_evidence=None) -> str:
    payload = {
        "config": {
            "market": config.market.upper(),
            "horizons": config.horizons,
            "round_trip_cost_bps": config.round_trip_cost_bps,
            "drawdown_penalty": config.drawdown_penalty,
            "require_relative_returns": config.require_relative_returns,
            "target_mode": config.target_mode,
            "label_version": config.label_version,
            "feature_set_version": config.feature_set_version,
            "schema_version": config.schema_version,
        },
        "universe_version": universe_version,
    }
    if config.fill_cost_model:
        payload["fill_cost_model"] = config.fill_cost_model.metadata()
        payload["execution_evidence_hash"] = execution_evidence.evidence_hash
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return f"stock_selection_dataset_v2:{config.market.upper()}:{digest}"


def build_training_samples(
    *,
    trading_dates: Sequence[date],
    bars_by_ticker: Mapping[str, Sequence[PriceBar]],
    universe_snapshots: Sequence[UniverseSnapshot],
    features_by_key: Mapping[tuple[str, date], Mapping[str, float]],
    market_returns: Mapping[tuple[date, int], float],
    industry_returns: Mapping[tuple[str, date, int], float],
    entry_exclusions: Mapping[tuple[str, date], str] | None,
    config: SampleBuildConfig,
    execution_evidence: ResearchExecutionEvidence | None = None,
) -> SampleBuildResult:
    dates = list(trading_dates)
    if dates != sorted(dates) or len(set(dates)) != len(dates):
        raise ValueError("trading_dates must be unique and ascending")
    date_index = {value: index for index, value in enumerate(dates)}
    snapshots = [item for item in universe_snapshots if item.market.upper() == config.market.upper()]
    universe_versions = {item.universe_version for item in snapshots}
    if len(universe_versions) != 1:
        raise ValueError("sample build requires exactly one universe_version")
    universe_version = next(iter(universe_versions))
    if config.fill_cost_model:
        if execution_evidence is None:
            raise ValueError("fill-cost sample build requires execution evidence")
        execution_evidence.validate_prices(bars_by_ticker, market=config.market.upper())
        if tuple(dates) != execution_evidence.trading_dates:
            raise ValueError("sample calendar differs from execution evidence")
    elif execution_evidence is not None:
        raise ValueError("execution evidence requires explicit fill-cost sample config")
    dataset_version = _dataset_version(config, universe_version, execution_evidence)

    normalized_bars: dict[str, dict[date, PriceBar]] = {}
    for ticker, bars in bars_by_ticker.items():
        normalized_ticker = str(ticker).strip().upper()
        bar_map = {bar.trade_date: bar for bar in bars}
        if len(bar_map) != len(bars):
            raise ValueError(f"duplicate price bars for {normalized_ticker}")
        normalized_bars[normalized_ticker] = bar_map

    exclusions = entry_exclusions or {}
    samples: list[LabeledSample] = []
    exclusion_counter: Counter[str] = Counter()
    for snapshot in sorted(snapshots, key=lambda item: (item.trade_date, item.ticker)):
        if not snapshot.included:
            continue
        ticker = snapshot.ticker.upper()
        signal_index = date_index.get(snapshot.trade_date)
        if signal_index is None:
            raise ValueError("universe snapshot date is missing from trading_dates")
        features = features_by_key.get((ticker, snapshot.trade_date))
        if features is None:
            exclusion_counter["missing_features"] += len(config.horizons)
            continue
        bar_map = normalized_bars.get(ticker) or {}
        for horizon_days in config.horizons:
            exit_index = signal_index + horizon_days
            if exit_index >= len(dates):
                exclusion_counter["label_not_mature"] += 1
                continue
            expected_dates = dates[signal_index : exit_index + 1]
            if any(item not in bar_map for item in expected_dates):
                exclusion_counter["missing_price_path"] += 1
                continue
            path = [bar_map[item] for item in expected_dates]
            market_return = market_returns.get((snapshot.trade_date, horizon_days))
            industry_return = industry_returns.get((ticker, snapshot.trade_date, horizon_days))
            if config.require_relative_returns and market_return is None:
                exclusion_counter["missing_market_return"] += 1
                continue
            if config.require_relative_returns and industry_return is None:
                exclusion_counter["missing_industry_return"] += 1
                continue
            entry_date = dates[signal_index + 1]
            entry_reason = exclusions.get((ticker, entry_date))
            if config.fill_cost_model:
                eligibility = execution_evidence.records.get((ticker, snapshot.trade_date, horizon_days),
                    ExecutionEligibility(None, None, "missing_execution_evidence"))
                if not entry_reason and path[1].volume <= 0:
                    entry_reason = "zero_volume_at_entry"
                if entry_reason:
                    eligibility = ExecutionEligibility(False, eligibility.exit_allowed, entry_reason)
                elif eligibility.entry_allowed is True and path[-1].volume <= 0:
                    eligibility = ExecutionEligibility(True, False, "zero_volume_at_exit")
                label = confirmed_outcome(path, signal_date=snapshot.trade_date, trading_dates=dates,
                    horizon_days=horizon_days, market=config.market.upper(), cost_model=config.fill_cost_model,
                    eligibility=eligibility, market_return=float(market_return or 0),
                    industry_return=float(industry_return or 0))
            else:
                label = build_executable_label(
                    path,
                    signal_index=0,
                    horizon_days=horizon_days,
                    round_trip_cost_bps=config.round_trip_cost_bps,
                    market_return=float(market_return or 0.0),
                    industry_return=float(industry_return or 0.0),
                    drawdown_penalty=config.drawdown_penalty,
                    entry_is_executable=entry_reason is None,
                    exclusion_reason=entry_reason,
                )
            if not label.tradable:
                exclusion_counter[label.exclusion_reason or "non_tradable"] += 1
            sample_id = (
                f"{config.market.upper()}:{ticker}:{snapshot.trade_date.isoformat()}:"
                f"{horizon_days}:{dataset_version}"
            )
            samples.append(
                LabeledSample(
                    sample_id=sample_id,
                    market=config.market.upper(),
                    ticker=ticker,
                    feature_date=snapshot.trade_date,
                    label_start_date=label.entry_date,
                    label_end_date=label.exit_date,
                    label_available_date=label.label_available_date,
                    horizon_days=horizon_days,
                    label_value=getattr(label, config.target_mode),
                    features=dict(features),
                    label_components={
                        key: value
                        for key, value in {
                            "gross_return": label.gross_return,
                            "net_return": label.net_return,
                            "market_excess_return": label.market_excess_return,
                            "industry_excess_return": label.industry_excess_return,
                            "path_drawdown": label.path_drawdown,
                            "risk_adjusted_return": label.risk_adjusted_return,
                            "profit_indicator": (
                                float(label.is_profitable)
                                if label.is_profitable is not None
                                else None
                            ),
                        }.items()
                        if value is not None
                    },
                    tradable=label.tradable,
                    exclusion_reason=label.exclusion_reason,
                    dataset_version=dataset_version,
                    target_mode=config.target_mode,
                )
            )

    eligible_count = sum(1 for item in samples if item.tradable)
    return SampleBuildResult(
        dataset_version=dataset_version,
        universe_version=universe_version,
        samples=tuple(samples),
        eligible_count=eligible_count,
        excluded_count=len(samples) - eligible_count,
        exclusion_counts=dict(sorted(exclusion_counter.items())),
        label_contract=({"label_version": config.label_version, "target_mode": config.target_mode,
                         "cost_model": config.fill_cost_model.metadata(),
                         "execution_evidence_hash": execution_evidence.evidence_hash,
                         "execution_source_reference": execution_evidence.source_reference}
                        if config.fill_cost_model else {}),
    )
