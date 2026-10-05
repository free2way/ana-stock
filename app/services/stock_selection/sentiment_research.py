"""Optional assembly of the forward-only ``sentiment_v1`` family into research datasets.

The HiThink sentiment family is bounded (``coverage_window='trailing_one_year'``)
and ``forward_only``: it cannot be replayed over the multi-year CN price history
the way the lake can.  This module is the *optional* bridge that stitches the two
together without ever inventing sentiment history:

* **Price long history + sentiment last year** — every ``(ticker, feature_date)``
  row keeps its price/P1 columns, and the 38 sentiment columns are joined only for
  dates inside the observed sentiment coverage window.  Cells outside that window
  are ``None`` (never ``0``), and the coverage bounds / missing policy are recorded
  as dataset metadata.
* **Parameterised decision cutoff** — ``decision_cutoff='eod'`` builds the
  same-day post-close (16:00) feature set used by the next-session decision path;
  ``decision_cutoff='preopen_auction'`` builds the 09:25 pre-open path, which
  never sees the same-day end-of-day featured data.  The pre-open path is gated by
  an explicit ``auction_path_enabled`` switch so it can never be enabled silently.
* **Default off** — with no config (or a disabled config) the dataset is assembled
  exactly as before; nothing here runs unless explicitly requested.

This module is read-only over the traceable HiThink store and never writes to the
main CN price lake.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from app.services.stock_selection.sentiment_features import (
    FEATURE_NAMES as SENTIMENT_FEATURE_NAMES,
    SENTIMENT_FACTOR_SET_KEY,
    HithinkSentimentObservation,
    SentimentPITConfig,
    build_sentiment_feature_matrix,
    load_sentiment_observations,
)
from app.services.ticker_format import normalize_ticker_for_market


SENTIMENT_RESEARCH_JOIN_SCHEMA = "stock_selection_sentiment_research_join_v1"
SENTIMENT_COVERAGE_MISSING_POLICY = "none_outside_coverage_never_zero"
_NO_COVERAGE_SOURCE_VERSION = "hithink_sentiment_features_v1:no_coverage"

_EOD_CUTOFF_SEMANTICS = "same_trade_date_post_close_next_session_decision"
_PREOPEN_CUTOFF_SEMANTICS = "same_trade_date_preopen_auction_excludes_same_day_eod"

_HASH_COMPONENT_KEYS = (
    "factor_set_key",
    "source_version",
    "coverage_start",
    "coverage_end",
    "missing_policy",
    "decision_cutoff",
    "cutoff_time_local",
    "auction_path_enabled",
)


class SentimentResearchError(RuntimeError):
    """Raised when the sentiment research join cannot be built without guessing."""


class SentimentDecisionCutoff(StrEnum):
    """Which decision cutoff the assembled sentiment columns target."""

    EOD = "eod"
    PREOPEN_AUCTION = "preopen_auction"


@dataclass(frozen=True, slots=True)
class SentimentResearchFeatureConfig:
    """Switch + cutoff semantics for joining ``sentiment_v1`` into a dataset.

    Defaults leave the feature family off.  ``decision_cutoff='preopen_auction'``
    additionally requires ``auction_path_enabled=True`` so the pre-open path is
    never chosen silently.
    """

    enabled: bool = False
    decision_cutoff: str = SentimentDecisionCutoff.EOD.value
    auction_path_enabled: bool = False
    market: str = "CN"
    timezone_name: str = "Asia/Shanghai"
    featured_available_hour: int = 16
    featured_available_minute: int = 0
    auction_available_hour: int = 9
    auction_available_minute: int = 25

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be a boolean")
        if not isinstance(self.auction_path_enabled, bool):
            raise ValueError("auction_path_enabled must be a boolean")
        if str(self.market or "").strip().upper() != "CN":
            raise ValueError("sentiment research features currently support only the CN market")
        try:
            cutoff = SentimentDecisionCutoff(self.decision_cutoff)
        except ValueError as exc:
            raise ValueError(
                f"unsupported decision_cutoff: {self.decision_cutoff!r}"
            ) from exc
        if cutoff == SentimentDecisionCutoff.PREOPEN_AUCTION and not self.auction_path_enabled:
            raise ValueError(
                "pre-open auction sentiment path is not enabled; set "
                "auction_path_enabled=True explicitly (it is never enabled silently)"
            )
        ZoneInfo(self.timezone_name)
        for hour, minute, label in (
            (self.featured_available_hour, self.featured_available_minute, "featured"),
            (self.auction_available_hour, self.auction_available_minute, "auction"),
        ):
            if not 0 <= int(hour) <= 23 or not 0 <= int(minute) <= 59:
                raise ValueError(f"{label} availability time must be a valid time of day")

    @property
    def cutoff(self) -> SentimentDecisionCutoff:
        return SentimentDecisionCutoff(self.decision_cutoff)

    def pit_config(self) -> SentimentPITConfig:
        return SentimentPITConfig(
            market="CN",
            featured_available_hour=int(self.featured_available_hour),
            featured_available_minute=int(self.featured_available_minute),
            auction_available_hour=int(self.auction_available_hour),
            auction_available_minute=int(self.auction_available_minute),
            timezone_name=self.timezone_name,
        )

    def availability_time(self) -> time:
        if self.cutoff == SentimentDecisionCutoff.PREOPEN_AUCTION:
            return time(int(self.auction_available_hour), int(self.auction_available_minute))
        return time(int(self.featured_available_hour), int(self.featured_available_minute))

    def cutoff_time_local(self) -> str:
        return self.availability_time().strftime("%H:%M")

    def cutoff_semantics(self) -> str:
        if self.cutoff == SentimentDecisionCutoff.PREOPEN_AUCTION:
            return _PREOPEN_CUTOFF_SEMANTICS
        return _EOD_CUTOFF_SEMANTICS

    def cutoff_for(self, feature_date: date) -> datetime:
        return datetime.combine(
            feature_date, self.availability_time(), tzinfo=ZoneInfo(self.timezone_name)
        )

    def metadata(self) -> dict[str, object]:
        return {
            "sentiment_factor_set_key": SENTIMENT_FACTOR_SET_KEY,
            "sentiment_enabled": self.enabled,
            "sentiment_decision_cutoff": self.cutoff.value,
            "sentiment_cutoff_time_local": self.cutoff_time_local(),
            "sentiment_cutoff_semantics": self.cutoff_semantics(),
            "sentiment_auction_path_enabled": self.auction_path_enabled,
            "sentiment_auction_path_status": (
                "enabled" if self.auction_path_enabled else "not_enabled"
            ),
            "sentiment_missing_policy": SENTIMENT_COVERAGE_MISSING_POLICY,
        }


@dataclass(frozen=True, slots=True)
class SentimentResearchJoinResult:
    """Merged feature rows plus the coverage/cutoff metadata for a dataset."""

    schema_version: str
    factor_set_key: str
    source_version: str
    feature_names: tuple[str, ...]
    merged_feature_names: tuple[str, ...]
    features_by_key: Mapping[tuple[str, date], Mapping[str, float | None]]
    decision_cutoff: str
    cutoff_time_local: str
    cutoff_semantics: str
    auction_path_enabled: bool
    coverage_start: date | None
    coverage_end: date | None
    covered_date_count: int
    missing_policy: str
    observation_count: int
    selected_observations_by_date: Mapping[date, Mapping[str, str]]

    def metadata(self) -> dict[str, object]:
        return {
            "sentiment_schema_version": self.schema_version,
            "sentiment_factor_set_key": self.factor_set_key,
            "sentiment_source_version": self.source_version,
            "sentiment_feature_count": len(self.feature_names),
            "sentiment_coverage_start": (
                self.coverage_start.isoformat() if self.coverage_start else None
            ),
            "sentiment_coverage_end": (
                self.coverage_end.isoformat() if self.coverage_end else None
            ),
            "sentiment_covered_date_count": self.covered_date_count,
            "sentiment_observation_count": self.observation_count,
            "sentiment_missing_policy": self.missing_policy,
            "sentiment_decision_cutoff": self.decision_cutoff,
            "sentiment_cutoff_time_local": self.cutoff_time_local,
            "sentiment_cutoff_semantics": self.cutoff_semantics,
            "sentiment_auction_path_enabled": self.auction_path_enabled,
            "sentiment_auction_path_status": (
                "enabled" if self.auction_path_enabled else "not_enabled"
            ),
        }

    def hash_components(self) -> dict[str, object]:
        """Canonical coverage + cutoff components for ``freeze_dataset_hash``."""

        return {
            "factor_set_key": self.factor_set_key,
            "source_version": self.source_version,
            "coverage_start": (
                self.coverage_start.isoformat() if self.coverage_start else None
            ),
            "coverage_end": (
                self.coverage_end.isoformat() if self.coverage_end else None
            ),
            "missing_policy": self.missing_policy,
            "decision_cutoff": self.decision_cutoff,
            "cutoff_time_local": self.cutoff_time_local,
            "auction_path_enabled": self.auction_path_enabled,
        }


def _base_feature_names(
    base_features_by_key: Mapping[tuple[str, date], Mapping[str, float]],
) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for values in base_features_by_key.values():
        for name in values:
            if name not in seen:
                seen.add(name)
                ordered.append(name)
    return ordered


def build_sentiment_research_join(
    base_features_by_key: Mapping[tuple[str, date], Mapping[str, float]],
    *,
    config: SentimentResearchFeatureConfig | None,
    observations: Iterable[HithinkSentimentObservation] | None = None,
    root=None,
) -> SentimentResearchJoinResult | None:
    """Join the sentiment family onto base ``(ticker, feature_date)`` features.

    Returns ``None`` when ``config`` is absent or disabled, so the caller keeps
    the original dataset unchanged.  When enabled, every base row gains the 38
    sentiment columns; dates outside the observed coverage window hold ``None``
    for every sentiment column and are never zero-filled.  Within coverage the
    per-date decision cutoff is resolved from the configured cutoff semantics.
    """

    if config is None or not config.enabled:
        return None
    base_names = _base_feature_names(base_features_by_key)
    overlap = set(base_names) & set(SENTIMENT_FEATURE_NAMES)
    if overlap:
        raise SentimentResearchError(
            "sentiment feature names collide with base features: "
            + ", ".join(sorted(overlap))
        )

    observation_list = (
        list(observations)
        if observations is not None
        else load_sentiment_observations(root=root)
    )
    observation_dates = sorted({item.trade_date for item in observation_list})
    coverage_start = observation_dates[0] if observation_dates else None
    coverage_end = observation_dates[-1] if observation_dates else None

    base_dates = sorted({feature_date for (_ticker, feature_date) in base_features_by_key})
    if coverage_start is not None and coverage_end is not None:
        covered_dates = tuple(
            item for item in base_dates if coverage_start <= item <= coverage_end
        )
    else:
        covered_dates = ()
    covered_date_set = set(covered_dates)

    universe_by_date: dict[date, list[str]] = defaultdict(list)
    for ticker, feature_date in base_features_by_key:
        if feature_date in covered_date_set:
            universe_by_date[feature_date].append(
                normalize_ticker_for_market(ticker, "CN")
            )
    universe_by_date = {
        feature_date: sorted(set(tickers))
        for feature_date, tickers in universe_by_date.items()
    }
    cutoff_by_date = {
        feature_date: config.cutoff_for(feature_date) for feature_date in covered_dates
    }

    if covered_dates:
        matrix = build_sentiment_feature_matrix(
            observations=observation_list,
            universe_by_date=universe_by_date,
            cutoff_by_date=cutoff_by_date,
            config=config.pit_config(),
        )
        source_version = matrix.source_version
        selected_observations_by_date = dict(matrix.selected_observations_by_date)
        matrix_rows = matrix.features_by_key
    else:
        source_version = _NO_COVERAGE_SOURCE_VERSION
        selected_observations_by_date = {}
        matrix_rows = {}

    merged: dict[tuple[str, date], dict[str, float | None]] = {}
    for (ticker, feature_date), base_values in base_features_by_key.items():
        row: dict[str, float | None] = dict(base_values)
        matrix_row = (
            matrix_rows.get((feature_date, normalize_ticker_for_market(ticker, "CN")))
            if feature_date in covered_date_set
            else None
        )
        for name in SENTIMENT_FEATURE_NAMES:
            row[name] = matrix_row.get(name) if matrix_row is not None else None
        merged[(ticker, feature_date)] = row

    return SentimentResearchJoinResult(
        schema_version=SENTIMENT_RESEARCH_JOIN_SCHEMA,
        factor_set_key=SENTIMENT_FACTOR_SET_KEY,
        source_version=source_version,
        feature_names=tuple(SENTIMENT_FEATURE_NAMES),
        merged_feature_names=tuple(
            dict.fromkeys([*base_names, *SENTIMENT_FEATURE_NAMES])
        ),
        features_by_key=merged,
        decision_cutoff=config.cutoff.value,
        cutoff_time_local=config.cutoff_time_local(),
        cutoff_semantics=config.cutoff_semantics(),
        auction_path_enabled=config.auction_path_enabled,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        covered_date_count=len(covered_dates),
        missing_policy=SENTIMENT_COVERAGE_MISSING_POLICY,
        observation_count=len(observation_list),
        selected_observations_by_date=selected_observations_by_date,
    )


def sentiment_dataset_hash_components(
    result: SentimentResearchJoinResult | None,
) -> dict[str, object] | None:
    """Coverage + cutoff hash components for an assembled sentiment dataset."""

    return result.hash_components() if result is not None else None


def validate_sentiment_hash_components(components: Mapping[str, object]) -> dict[str, object]:
    """Validate an external sentiment metadata mapping before hashing.

    Mandatory coverage/cutoff keys must be present (``coverage_*`` may be null),
    so a factor set cannot claim sentiment coverage without declaring it.
    """

    missing = [
        key
        for key in _HASH_COMPONENT_KEYS
        if key not in components
    ]
    if missing:
        raise ValueError(
            "sentiment dataset metadata is missing required keys: "
            + ", ".join(sorted(missing))
        )
    resolved = dict(components)
    if resolved.get("factor_set_key") not in (None, SENTIMENT_FACTOR_SET_KEY):
        raise ValueError(
            f"sentiment metadata factor_set_key must be {SENTIMENT_FACTOR_SET_KEY!r}"
        )
    resolved["factor_set_key"] = SENTIMENT_FACTOR_SET_KEY
    if not isinstance(resolved.get("auction_path_enabled"), bool):
        raise ValueError("sentiment metadata auction_path_enabled must be a boolean")
    return resolved
