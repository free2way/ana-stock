from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from app.services.stock_selection.factor_pipeline import FactorDirection, FactorSpec
from app.services.stock_selection.sentiment_features import (
    SENTIMENT_FACTOR_SET_KEY,
    sentiment_feature_definitions,
)


@dataclass(frozen=True, slots=True)
class ResearchFactorSet:
    """Frozen factor hypothesis used by a reproducible challenger run."""

    key: str
    thesis: str
    specs: tuple[FactorSpec, ...]
    schema_version: str = "stock_selection_factor_set_v1"

    def __post_init__(self) -> None:
        if not str(self.key or "").strip():
            raise ValueError("factor set key must not be empty")
        if not str(self.thesis or "").strip():
            raise ValueError("factor set thesis must not be empty")
        if not self.specs:
            raise ValueError("factor set must contain at least one factor")
        names = [item.name for item in self.specs]
        if len(names) != len(set(names)):
            raise ValueError("factor set names must be unique")

    @property
    def feature_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.specs)

    def version(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "key": self.key,
            "thesis": self.thesis,
            "specs": [
                {
                    **asdict(item),
                    "direction": item.direction.value,
                }
                for item in self.specs
            ],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
        return f"{self.schema_version}:{self.key}:{digest}"


def original_price_factor_specs() -> tuple[FactorSpec, ...]:
    return (
        FactorSpec("momentum_5d", FactorDirection.HIGHER_BETTER),
        FactorSpec("momentum_20d", FactorDirection.HIGHER_BETTER),
        FactorSpec("momentum_60d", FactorDirection.HIGHER_BETTER),
        FactorSpec("price_vs_ma20", FactorDirection.HIGHER_BETTER),
        FactorSpec("ma20_slope_5d", FactorDirection.HIGHER_BETTER),
        FactorSpec("efficiency_ratio_20d", FactorDirection.HIGHER_BETTER),
        FactorSpec("close_location_20d", FactorDirection.HIGHER_BETTER),
        FactorSpec("volume_ratio_20d", FactorDirection.HIGHER_BETTER),
        FactorSpec("dollar_volume_log", FactorDirection.HIGHER_BETTER),
        FactorSpec("volatility_20d", FactorDirection.LOWER_BETTER),
        FactorSpec("drawdown_from_60d_high", FactorDirection.LOWER_BETTER),
        FactorSpec("intraday_range_5d", FactorDirection.LOWER_BETTER),
    )


def research_factor_sets() -> dict[str, ResearchFactorSet]:
    candidates = (
        ResearchFactorSet(
            key="original_v1",
            thesis="Frozen original price-factor directions used as the control group.",
            specs=original_price_factor_specs(),
        ),
        ResearchFactorSet(
            key="stable_low_risk_v1",
            thesis="Prefer low realized range and volatility plus directional efficiency.",
            specs=(
                FactorSpec("intraday_range_5d", FactorDirection.LOWER_BETTER),
                FactorSpec("volatility_20d", FactorDirection.LOWER_BETTER),
                FactorSpec("efficiency_ratio_20d", FactorDirection.HIGHER_BETTER),
            ),
        ),
        ResearchFactorSet(
            key="mean_reversion_v1",
            thesis="Test the formally observed reversal of medium-term trend signals.",
            specs=(
                FactorSpec("intraday_range_5d", FactorDirection.LOWER_BETTER),
                FactorSpec("efficiency_ratio_20d", FactorDirection.HIGHER_BETTER),
                FactorSpec("momentum_60d", FactorDirection.LOWER_BETTER),
                FactorSpec("ma20_slope_5d", FactorDirection.LOWER_BETTER),
                FactorSpec("close_location_20d", FactorDirection.LOWER_BETTER),
            ),
        ),
        ResearchFactorSet(
            key="decorrelated_v1",
            thesis="Use one representative from each strong correlation cluster.",
            specs=(
                FactorSpec("volatility_20d", FactorDirection.LOWER_BETTER),
                FactorSpec("efficiency_ratio_20d", FactorDirection.HIGHER_BETTER),
                FactorSpec("momentum_60d", FactorDirection.LOWER_BETTER),
            ),
        ),
        ResearchFactorSet(
            key="trend_head_v1",
            thesis=(
                "Frozen equal-weight trend consensus selected by the training-only "
                "head-separability audit; no OOS-fitted weights or directions."
            ),
            specs=(
                FactorSpec("momentum_5d", FactorDirection.HIGHER_BETTER),
                FactorSpec("momentum_60d", FactorDirection.HIGHER_BETTER),
                FactorSpec("price_vs_ma20", FactorDirection.HIGHER_BETTER),
                FactorSpec("ma20_slope_5d", FactorDirection.HIGHER_BETTER),
            ),
        ),
        ResearchFactorSet(
            key="quality_value_shadow_v1",
            thesis=(
                "Frozen point-in-time quality and value observation score for the new "
                "untouched forward shadow window; it is not a calibrated buy model."
            ),
            specs=(
                FactorSpec("positive_earnings_yield", FactorDirection.HIGHER_BETTER, weight=1.0),
                FactorSpec("dividend_yield", FactorDirection.HIGHER_BETTER, weight=0.5),
                FactorSpec("roe_avg_3y", FactorDirection.HIGHER_BETTER, weight=1.0),
                FactorSpec("net_profit_yoy", FactorDirection.HIGHER_BETTER, weight=0.75),
                FactorSpec("revenue_yoy", FactorDirection.HIGHER_BETTER, weight=0.5),
                FactorSpec("debt_to_assets", FactorDirection.LOWER_BETTER, weight=0.75),
            ),
        ),
        ResearchFactorSet(
            key="p1_residual_momentum_v1",
            thesis=(
                "Research-only incremental family: market-residual and dated-industry-relative "
                "20-session momentum; it does not inherit production qualification."
            ),
            specs=(
                FactorSpec("residual_momentum_20d", FactorDirection.HIGHER_BETTER),
                FactorSpec("industry_relative_momentum_20d", FactorDirection.HIGHER_BETTER),
            ),
        ),
        ResearchFactorSet(
            key="p1_liquidity_crowding_v1",
            thesis=(
                "Research-only incremental family using reproducible OHLCV liquidity and "
                "notional-volume crowding proxies, not mislabeled share turnover."
            ),
            specs=(
                FactorSpec("notional_volume_zscore_20d", FactorDirection.LOWER_BETTER),
                FactorSpec("amihud_illiquidity_20d_per_million", FactorDirection.LOWER_BETTER),
            ),
        ),
        ResearchFactorSet(
            key="p1_open_intraday_structure_v1",
            thesis=(
                "Research-only incremental family for close-known opening-gap and intraday "
                "structure; direction is a frozen hypothesis pending OOS ablation."
            ),
            specs=(
                FactorSpec("opening_gap_1d", FactorDirection.HIGHER_BETTER),
                FactorSpec("intraday_return_1d", FactorDirection.HIGHER_BETTER),
            ),
        ),
    )
    return {item.key: item for item in candidates}


def get_research_factor_set(key: str) -> ResearchFactorSet:
    normalized = str(key or "").strip()
    registry = registered_factor_sets()
    try:
        return registry[normalized]
    except KeyError as exc:
        available = ", ".join(sorted(registry))
        raise ValueError(f"unknown factor_set_key {key!r}; available: {available}") from exc


# Lower-is-better sentiment columns.  These directions are an explicit,
# unvalidated research hypothesis; the remaining columns default to
# higher-is-better until a forward ablation revises them.
_SENTIMENT_LOWER_BETTER_FEATURES = frozenset(
    {
        "hot_rank",
        "dragon_tiger_hot_rank",
        "limit_down_flag",
        "limit_down_turnover_pct",
        "limit_break_flag",
        "limit_break_open_times",
        "anomaly_limit_down_flag",
    }
)


def sentiment_factor_specs() -> tuple[FactorSpec, ...]:
    return tuple(
        FactorSpec(
            name=definition.name,
            direction=(
                FactorDirection.LOWER_BETTER
                if definition.name in _SENTIMENT_LOWER_BETTER_FEATURES
                else FactorDirection.HIGHER_BETTER
            ),
        )
        for definition in sentiment_feature_definitions().values()
    )


def sentiment_factor_set() -> ResearchFactorSet:
    """Forward-only HiThink sentiment family; excluded from the frozen candidates."""

    return ResearchFactorSet(
        key=SENTIMENT_FACTOR_SET_KEY,
        thesis=(
            "Research-only forward-only HiThink featured/auction sentiment family. "
            "Source depth is bounded to roughly one trailing year, so it is "
            "registered for research and experiment consumption but is not part of "
            "the frozen price/P1 candidates or the production training defaults. "
            "Directions are unvalidated placeholders pending a forward ablation."
        ),
        specs=sentiment_factor_specs(),
    )


def sentiment_factor_sets() -> dict[str, ResearchFactorSet]:
    return {SENTIMENT_FACTOR_SET_KEY: sentiment_factor_set()}


def registered_factor_sets() -> dict[str, ResearchFactorSet]:
    """All factory-loadable factor sets: frozen research candidates + sentiment."""

    return {**research_factor_sets(), **sentiment_factor_sets()}
