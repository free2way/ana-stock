from __future__ import annotations

import bisect
import hashlib
import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from app.services.stock_selection.feature_availability import (
    FUNDAMENTAL_FEATURE_NAMES,
    PointInTimeFeatureRecord,
)


DEFAULT_MAX_AGE_DAYS = (
    ("pe_ttm", 7),
    ("dividend_yield", 7),
    ("market_cap", 7),
    ("roe_avg_3y", 550),
    ("net_profit_yoy", 550),
    ("revenue_yoy", 550),
    ("debt_to_assets", 550),
)


@dataclass(frozen=True, slots=True)
class PointInTimeFeatureJoinConfig:
    market: str
    required_features: tuple[str, ...] = FUNDAMENTAL_FEATURE_NAMES
    minimum_cross_section_coverage: float = 0.60
    post_close_hour: int = 16
    timezone_name: str = "Asia/Shanghai"
    max_age_days: tuple[tuple[str, int], ...] = DEFAULT_MAX_AGE_DAYS

    def __post_init__(self) -> None:
        if str(self.market or "").strip().upper() not in {"CN", "US"}:
            raise ValueError("market must be CN or US")
        if not self.required_features or len(set(self.required_features)) != len(
            self.required_features
        ):
            raise ValueError("required_features must be non-empty and unique")
        if not 0.0 < self.minimum_cross_section_coverage <= 1.0:
            raise ValueError("minimum_cross_section_coverage must be in (0, 1]")
        if not 0 <= self.post_close_hour <= 23:
            raise ValueError("post_close_hour must be in [0, 23]")
        ZoneInfo(self.timezone_name)
        ages = dict(self.max_age_days)
        if len(ages) != len(self.max_age_days):
            raise ValueError("max_age_days feature names must be unique")
        missing = set(self.required_features) - set(ages)
        if missing:
            raise ValueError("max_age_days is missing required features: " + ", ".join(sorted(missing)))
        if any(not str(name).strip() or int(days) <= 0 for name, days in self.max_age_days):
            raise ValueError("max_age_days entries must contain a name and positive days")


@dataclass(frozen=True, slots=True)
class PointInTimeFeatureDateCoverage:
    feature_date: date
    eligible_ticker_count: int
    feature_coverage: Mapping[str, float]
    minimum_feature_coverage: float
    enabled: bool
    reason: str


@dataclass(frozen=True, slots=True)
class PointInTimeFeatureJoinResult:
    schema_version: str
    feature_set_version: str
    source_version: str
    feature_names: tuple[str, ...]
    features_by_key: Mapping[tuple[str, date], Mapping[str, float]]
    date_coverage: tuple[PointInTimeFeatureDateCoverage, ...]
    enabled_dates: tuple[date, ...]
    disabled_dates: tuple[date, ...]
    selected_record_count: int


def _config_version(config: PointInTimeFeatureJoinConfig, source_version: str) -> str:
    payload = {
        "schema_version": "point_in_time_feature_join_v1",
        "config": asdict(config),
        "source_version": source_version,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return f"point_in_time_feature_join_v1:{config.market.upper()}:{digest}"


def _derived_values(raw: Mapping[str, float]) -> dict[str, float]:
    output = {name: float(value) for name, value in raw.items()}
    pe_ttm = raw.get("pe_ttm")
    if pe_ttm is not None and math.isfinite(float(pe_ttm)) and float(pe_ttm) > 0:
        output["positive_earnings_yield"] = 1.0 / float(pe_ttm)
    market_cap = raw.get("market_cap")
    if market_cap is not None and math.isfinite(float(market_cap)) and float(market_cap) > 0:
        output["market_cap_log"] = math.log10(float(market_cap))
    return output


def build_point_in_time_feature_join(
    records: Iterable[PointInTimeFeatureRecord],
    *,
    universe_by_date: Mapping[date, Iterable[str]],
    source_version: str,
    config: PointInTimeFeatureJoinConfig,
    cutoff_by_date: Mapping[date, datetime] | None = None,
) -> PointInTimeFeatureJoinResult:
    """Build feature rows using only information known by each decision cutoff.

    Historical research defaults to the configured same-day post-close hour. A
    live shadow job may pass an explicit, timezone-aware cutoff so data collected
    after the close is attributed to the next executable session instead of
    being backdated to the feature date.
    """

    if not str(source_version or "").strip():
        raise ValueError("source_version must not be empty")
    dates = tuple(sorted(universe_by_date))
    if not dates:
        raise ValueError("point-in-time join requires universe dates")
    normalized_universe = {
        feature_date: tuple(
            sorted(
                {
                    str(ticker or "").strip().upper()
                    for ticker in universe_by_date[feature_date]
                    if str(ticker or "").strip()
                }
            )
        )
        for feature_date in dates
    }
    grouped: dict[tuple[str, str], list[PointInTimeFeatureRecord]] = defaultdict(list)
    market_code = config.market.strip().upper()
    required = set(config.required_features)
    for record in records:
        if record.market.strip().upper() == market_code and record.feature_name in required:
            grouped[(record.ticker.strip().upper(), record.feature_name)].append(record)
    knowledge_times: dict[tuple[str, str], list[datetime]] = {}
    for key, values in grouped.items():
        values.sort(key=lambda item: (item.knowledge_time, item.event_time, item.revision_id))
        knowledge_times[key] = [item.knowledge_time for item in values]

    timezone = ZoneInfo(config.timezone_name)
    explicit_cutoffs = dict(cutoff_by_date or {})
    unexpected_cutoff_dates = set(explicit_cutoffs) - set(dates)
    if unexpected_cutoff_dates:
        raise ValueError(
            "cutoff_by_date contains dates outside universe_by_date: "
            + ", ".join(item.isoformat() for item in sorted(unexpected_cutoff_dates))
        )
    for feature_date, cutoff in explicit_cutoffs.items():
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError(f"cutoff_by_date[{feature_date.isoformat()}] must be timezone-aware")
    max_age = dict(config.max_age_days)
    output: dict[tuple[str, date], Mapping[str, float]] = {}
    coverage_rows: list[PointInTimeFeatureDateCoverage] = []
    selected_record_ids: set[str] = set()
    enabled_dates: list[date] = []
    disabled_dates: list[date] = []
    for feature_date in dates:
        tickers = normalized_universe[feature_date]
        cutoff = explicit_cutoffs.get(
            feature_date,
            datetime.combine(feature_date, time(hour=config.post_close_hour), tzinfo=timezone),
        ).astimezone(timezone)
        selected_by_ticker: dict[str, dict[str, PointInTimeFeatureRecord]] = defaultdict(dict)
        feature_counts = {name: 0 for name in config.required_features}
        for ticker in tickers:
            for feature_name in config.required_features:
                key = (ticker, feature_name)
                times = knowledge_times.get(key)
                if not times:
                    continue
                index = bisect.bisect_right(times, cutoff) - 1
                if index < 0:
                    continue
                record = grouped[key][index]
                age_days = (cutoff - record.knowledge_time.astimezone(timezone)).total_seconds() / 86400
                if age_days < 0 or age_days > max_age[feature_name]:
                    continue
                selected_by_ticker[ticker][feature_name] = record
                feature_counts[feature_name] += 1
        feature_coverage = {
            name: (feature_counts[name] / len(tickers) if tickers else 0.0)
            for name in config.required_features
        }
        minimum_coverage = min(feature_coverage.values(), default=0.0)
        enabled = bool(tickers) and minimum_coverage >= config.minimum_cross_section_coverage
        reason = "enabled" if enabled else "below_cross_section_coverage"
        coverage_rows.append(
            PointInTimeFeatureDateCoverage(
                feature_date=feature_date,
                eligible_ticker_count=len(tickers),
                feature_coverage=dict(sorted(feature_coverage.items())),
                minimum_feature_coverage=minimum_coverage,
                enabled=enabled,
                reason=reason,
            )
        )
        if not enabled:
            disabled_dates.append(feature_date)
            continue
        enabled_dates.append(feature_date)
        for ticker, by_name in selected_by_ticker.items():
            raw = {name: record.value for name, record in by_name.items()}
            output[(ticker, feature_date)] = _derived_values(raw)
            selected_record_ids.update(record.record_id for record in by_name.values())

    feature_names = tuple(config.required_features) + (
        "positive_earnings_yield",
        "market_cap_log",
    )
    return PointInTimeFeatureJoinResult(
        schema_version="point_in_time_feature_join_v1",
        feature_set_version=_config_version(config, source_version),
        source_version=source_version,
        feature_names=feature_names,
        features_by_key=output,
        date_coverage=tuple(coverage_rows),
        enabled_dates=tuple(enabled_dates),
        disabled_dates=tuple(disabled_dates),
        selected_record_count=len(selected_record_ids),
    )


def merge_point_in_time_features(
    base_features_by_key: Mapping[tuple[str, date], Mapping[str, float]],
    point_in_time_result: PointInTimeFeatureJoinResult,
) -> dict[tuple[str, date], Mapping[str, float]]:
    """Merge only dates whose complete feature group passed its daily gate."""

    enabled = set(point_in_time_result.enabled_dates)
    merged: dict[tuple[str, date], Mapping[str, float]] = {}
    for key, base in base_features_by_key.items():
        values = dict(base)
        if key[1] in enabled:
            values.update(point_in_time_result.features_by_key.get(key, {}))
        merged[key] = values
    return merged
