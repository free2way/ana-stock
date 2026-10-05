from __future__ import annotations

import bisect
import hashlib
import json
import math
import tempfile
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

import polars as pl

from app.services.stock_selection.sentiment_features import (
    SENTIMENT_FACTOR_SET_KEY,
    SentimentPITConfig,
    sentiment_feature_definitions,
)


FUNDAMENTAL_FEATURE_NAMES = (
    "pe_ttm",
    "dividend_yield",
    "market_cap",
    "roe_avg_3y",
    "net_profit_yoy",
    "revenue_yoy",
    "debt_to_assets",
)

FEATURE_AVAILABILITY_MANIFEST_SCHEMA = "stock_selection_feature_availability_manifest_v1"


@dataclass(frozen=True, slots=True)
class FeatureAvailabilityEntry:
    """Registered source/availability metadata for one engineered feature."""

    feature_name: str
    source: str
    available_time_local: str
    timezone_name: str
    availability_slot: str
    availability_semantics: str
    forward_only: bool
    coverage_window: str
    missing_policy: str
    information_family: str
    factor_set_key: str

    def __post_init__(self) -> None:
        for name, value in (
            ("feature_name", self.feature_name),
            ("source", self.source),
            ("timezone_name", self.timezone_name),
            ("availability_slot", self.availability_slot),
            ("availability_semantics", self.availability_semantics),
            ("coverage_window", self.coverage_window),
            ("missing_policy", self.missing_policy),
            ("information_family", self.information_family),
            ("factor_set_key", self.factor_set_key),
        ):
            if not str(value or "").strip():
                raise ValueError(f"{name} must not be empty")
        hours, separator, minutes = str(self.available_time_local).partition(":")
        if (
            not separator
            or len(hours) != 2
            or len(minutes) != 2
            or not (hours + minutes).isdigit()
            or not 0 <= int(hours) <= 23
            or not 0 <= int(minutes) <= 59
        ):
            raise ValueError("available_time_local must be a valid HH:MM time of day")
        ZoneInfo(self.timezone_name)
        if not isinstance(self.forward_only, bool):
            raise ValueError("forward_only must be a boolean")


def sentiment_feature_availability_entries() -> tuple[FeatureAvailabilityEntry, ...]:
    """Availability entries for the forward-only HiThink sentiment family."""

    return tuple(
        FeatureAvailabilityEntry(
            feature_name=definition.name,
            source=definition.source,
            available_time_local=definition.available_time_local,
            timezone_name=SentimentPITConfig().timezone_name,
            availability_slot=definition.availability_slot,
            availability_semantics=definition.availability_semantics,
            forward_only=definition.forward_only,
            coverage_window=definition.coverage_window,
            missing_policy=definition.missing_policy,
            information_family=definition.information_family,
            factor_set_key=SENTIMENT_FACTOR_SET_KEY,
        )
        for definition in sentiment_feature_definitions().values()
    )


def feature_availability_manifest() -> dict[str, FeatureAvailabilityEntry]:
    """Explicitly registered feature-availability entries, keyed by feature name.

    Fundamental features remain governed by :data:`FUNDAMENTAL_FEATURE_NAMES`
    and :class:`FeatureAvailabilityConfig`; this manifest records sources that
    must carry an explicit availability contract, such as the bounded
    (``trailing_one_year``), ``forward_only`` HiThink sentiment family.
    """

    return {
        entry.feature_name: entry for entry in sentiment_feature_availability_entries()
    }


@dataclass(frozen=True, slots=True)
class PointInTimeFeatureRecord:
    record_id: str
    market: str
    ticker: str
    feature_name: str
    value: float
    event_time: datetime
    available_time: datetime
    ingested_time: datetime
    source: str
    revision_id: str

    def __post_init__(self) -> None:
        for name, value in (
            ("record_id", self.record_id),
            ("market", self.market),
            ("ticker", self.ticker),
            ("feature_name", self.feature_name),
            ("source", self.source),
            ("revision_id", self.revision_id),
        ):
            if not str(value or "").strip():
                raise ValueError(f"{name} must not be empty")
        for name, value in (
            ("event_time", self.event_time),
            ("available_time", self.available_time),
            ("ingested_time", self.ingested_time),
        ):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if not math.isfinite(float(self.value)):
            raise ValueError("feature value must be finite")
        if self.available_time < self.event_time:
            raise ValueError("available_time must not precede event_time")

    @property
    def knowledge_time(self) -> datetime:
        return max(self.available_time, self.ingested_time)


@dataclass(frozen=True, slots=True)
class FundamentalFeatureAdaptResult:
    records: tuple[PointInTimeFeatureRecord, ...]
    source_row_count: int
    rejected_row_count: int
    emitted_value_count: int
    revision_history_preserved: bool
    rejection_reasons: Mapping[str, int]


def adapt_point_in_time_feature_snapshots(
    rows: Iterable[Mapping[str, object]],
    *,
    market: str,
    feature_names: Iterable[str] = FUNDAMENTAL_FEATURE_NAMES,
) -> FundamentalFeatureAdaptResult:
    """Adapt the append-only feature table without routing through legacy wide snapshots."""

    names = set(feature_names)
    market_code = str(market or "").strip().upper()
    source_rows = tuple(rows)
    records: list[PointInTimeFeatureRecord] = []
    rejected: dict[str, int] = defaultdict(int)
    revision_flags: list[bool] = []
    for row in source_rows:
        try:
            row_market = str(row.get("market") or "").strip().upper()
            if row_market and row_market != market_code:
                continue
            feature_name = str(row.get("feature_name") or "").strip()
            if feature_name not in names:
                continue
            payload: dict = {}
            payload_raw = row.get("payload_json")
            if isinstance(payload_raw, str) and payload_raw.strip():
                parsed = json.loads(payload_raw)
                if isinstance(parsed, dict):
                    payload = parsed
            revision_flags.append(bool(payload.get("revision_history_preserved")))
            record_id = str(row.get("id") or row.get("source_record_id") or "").strip()
            records.append(
                PointInTimeFeatureRecord(
                    record_id=f"pit:{market_code}:{record_id}",
                    market=market_code,
                    ticker=str(row.get("ticker") or "").strip().upper(),
                    feature_name=feature_name,
                    value=float(row.get("feature_value")),
                    event_time=_parse_aware_datetime(row.get("event_time")),
                    available_time=_parse_aware_datetime(row.get("available_time")),
                    ingested_time=_parse_aware_datetime(row.get("ingested_time")),
                    source=str(row.get("source") or "").strip(),
                    revision_id=str(row.get("revision_id") or "").strip(),
                )
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            rejected[str(exc) or type(exc).__name__] += 1
    return FundamentalFeatureAdaptResult(
        records=tuple(records),
        source_row_count=len(source_rows),
        rejected_row_count=sum(rejected.values()),
        emitted_value_count=len(records),
        revision_history_preserved=bool(revision_flags) and all(revision_flags),
        rejection_reasons=dict(sorted(rejected.items())),
    )


@dataclass(frozen=True, slots=True)
class FeatureAvailabilityConfig:
    market: str
    required_features: tuple[str, ...] = FUNDAMENTAL_FEATURE_NAMES
    minimum_cross_section_coverage: float = 0.60
    minimum_date_coverage: float = 0.80
    maximum_rejected_row_rate: float = 0.001
    require_revision_history: bool = True
    post_close_hour: int = 16
    timezone_name: str = "Asia/Shanghai"

    def __post_init__(self) -> None:
        if str(self.market or "").strip().upper() not in {"CN", "US"}:
            raise ValueError("market must be CN or US")
        if not self.required_features or len(set(self.required_features)) != len(
            self.required_features
        ):
            raise ValueError("required_features must be non-empty and unique")
        for name, value in (
            ("minimum_cross_section_coverage", self.minimum_cross_section_coverage),
            ("minimum_date_coverage", self.minimum_date_coverage),
            ("maximum_rejected_row_rate", self.maximum_rejected_row_rate),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")
        if not 0 <= self.post_close_hour <= 23:
            raise ValueError("post_close_hour must be in [0, 23]")
        ZoneInfo(self.timezone_name)


@dataclass(frozen=True, slots=True)
class FeatureCoverage:
    feature_name: str
    eligible_pair_count: int
    covered_pair_count: int
    pair_coverage: float
    evaluated_date_count: int
    passing_date_count: int
    date_coverage: float
    minimum_daily_coverage: float
    median_daily_coverage: float


@dataclass(frozen=True, slots=True)
class FeatureAvailabilityReport:
    schema_version: str
    market: str
    analysis_dates: tuple[date, ...]
    universe_symbol_count: int
    source_row_count: int
    rejected_row_count: int
    rejected_row_rate: float
    emitted_value_count: int
    revision_history_preserved: bool
    config: FeatureAvailabilityConfig
    feature_coverage: tuple[FeatureCoverage, ...]
    blockers: tuple[str, ...]
    verdict: str


@dataclass(frozen=True, slots=True)
class FeatureAvailabilityEvidenceWriteResult:
    evidence_version: str
    artifact_dir: Path
    manifest_path: Path
    reused_existing: bool


def _parse_aware_datetime(value: object) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("timestamp is missing")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed


def _optional_aware_datetime(value: object, *, fallback: datetime) -> datetime:
    if value is None or not str(value).strip():
        return fallback
    return _parse_aware_datetime(value)


def adapt_fundamental_snapshots(
    rows: Iterable[Mapping[str, object]],
    *,
    market: str,
    feature_names: Iterable[str] = FUNDAMENTAL_FEATURE_NAMES,
) -> FundamentalFeatureAdaptResult:
    names = tuple(feature_names)
    market_code = str(market or "").strip().upper()
    timezone = ZoneInfo("Asia/Shanghai" if market_code == "CN" else "America/New_York")
    records: list[PointInTimeFeatureRecord] = []
    rejected: dict[str, int] = defaultdict(int)
    source_rows = tuple(rows)
    for row in source_rows:
        try:
            ticker = str(row.get("ticker") or "").strip().upper()
            source = str(row.get("source") or "").strip()
            report_date = date.fromisoformat(str(row.get("report_date") or "")[:10])
            created_at = _parse_aware_datetime(row.get("created_at"))
            updated_at = _parse_aware_datetime(row.get("updated_at"))
            if not ticker or not source:
                raise ValueError("identity is missing")
            row_event_time = _optional_aware_datetime(
                row.get("event_time"),
                fallback=datetime.combine(report_date, time.min, tzinfo=timezone),
            )
            # Legacy snapshot rows have no provider publication timestamp, so
            # their safe fallback is the latest local observation time.  New
            # providers may supply explicit source availability and ingestion
            # timestamps for point-in-time historical records.
            row_available_time = _optional_aware_datetime(
                row.get("available_time"),
                fallback=max(created_at, updated_at),
            )
            row_ingested_time = _optional_aware_datetime(
                row.get("ingested_time"),
                fallback=max(created_at, updated_at),
            )
            if row_available_time < row_event_time:
                raise ValueError("availability precedes report date")
            revision_payload = {
                "ticker": ticker,
                "source": source,
                "report_date": report_date.isoformat(),
                "values": {name: row.get(name) for name in names},
            }
            revision_id = str(row.get("revision_id") or "").strip() or hashlib.sha256(
                json.dumps(revision_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()[:20]
            feature_times = row.get("feature_times")
            if not isinstance(feature_times, Mapping):
                feature_times = {}
            for feature_name in names:
                raw_value = row.get(feature_name)
                if raw_value is None:
                    continue
                value = float(raw_value)
                if not math.isfinite(value):
                    continue
                timing = feature_times.get(feature_name)
                if not isinstance(timing, Mapping):
                    timing = {}
                event_time = _optional_aware_datetime(
                    timing.get("event_time"),
                    fallback=row_event_time,
                )
                available_time = _optional_aware_datetime(
                    timing.get("available_time"),
                    fallback=row_available_time,
                )
                ingested_time = _optional_aware_datetime(
                    timing.get("ingested_time"),
                    fallback=row_ingested_time,
                )
                feature_revision_id = str(timing.get("revision_id") or "").strip() or revision_id
                if available_time < event_time:
                    raise ValueError("availability precedes feature event time")
                records.append(
                    PointInTimeFeatureRecord(
                        record_id=(
                            f"fundamental:{market_code}:{ticker}:{source}:"
                            f"{report_date.isoformat()}:{feature_name}:{feature_revision_id}"
                        ),
                        market=market_code,
                        ticker=ticker,
                        feature_name=feature_name,
                        value=value,
                        event_time=event_time,
                        available_time=available_time,
                        ingested_time=ingested_time,
                        source=source,
                        revision_id=feature_revision_id,
                    )
                )
        except (TypeError, ValueError) as exc:
            rejected[str(exc) or type(exc).__name__] += 1
    return FundamentalFeatureAdaptResult(
        records=tuple(records),
        source_row_count=len(source_rows),
        rejected_row_count=sum(rejected.values()),
        emitted_value_count=len(records),
        revision_history_preserved=bool(source_rows) and all(
            bool(row.get("revision_history_preserved")) for row in source_rows
        ),
        rejection_reasons=dict(sorted(rejected.items())),
    )


def select_feature_records_as_of(
    records: Iterable[PointInTimeFeatureRecord],
    *,
    cutoff: datetime,
) -> dict[tuple[str, str], PointInTimeFeatureRecord]:
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("cutoff must be timezone-aware")
    selected: dict[tuple[str, str], PointInTimeFeatureRecord] = {}
    for record in records:
        if record.event_time > cutoff or record.knowledge_time > cutoff:
            continue
        key = (record.ticker, record.feature_name)
        previous = selected.get(key)
        if previous is None or (
            record.knowledge_time,
            record.event_time,
            record.revision_id,
        ) > (
            previous.knowledge_time,
            previous.event_time,
            previous.revision_id,
        ):
            selected[key] = record
    return selected


def assess_feature_availability(
    records: Iterable[PointInTimeFeatureRecord],
    *,
    universe_by_date: Mapping[date, Iterable[str]],
    source_row_count: int,
    rejected_row_count: int,
    emitted_value_count: int,
    revision_history_preserved: bool,
    config: FeatureAvailabilityConfig,
) -> FeatureAvailabilityReport:
    dates = tuple(sorted(universe_by_date))
    if not dates:
        raise ValueError("feature availability audit requires analysis dates")
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
    timezone = ZoneInfo(config.timezone_name)
    grouped: dict[tuple[str, str], list[PointInTimeFeatureRecord]] = defaultdict(list)
    for record in records:
        if record.market == config.market.strip().upper():
            grouped[(record.ticker, record.feature_name)].append(record)
    knowledge_times: dict[tuple[str, str], list[datetime]] = {}
    for key, values in grouped.items():
        values.sort(key=lambda item: (item.knowledge_time, item.event_time, item.revision_id))
        knowledge_times[key] = [item.knowledge_time for item in values]

    coverage_rows: list[FeatureCoverage] = []
    blockers: list[str] = []
    for feature_name in config.required_features:
        eligible_pairs = 0
        covered_pairs = 0
        daily_coverages: list[float] = []
        passing_dates = 0
        for feature_date in dates:
            tickers = normalized_universe[feature_date]
            cutoff = datetime.combine(
                feature_date,
                time(hour=config.post_close_hour),
                tzinfo=timezone,
            )
            covered = 0
            for ticker in tickers:
                key = (ticker, feature_name)
                times = knowledge_times.get(key)
                if not times:
                    continue
                index = bisect.bisect_right(times, cutoff) - 1
                if index >= 0 and grouped[key][index].event_time <= cutoff:
                    covered += 1
            eligible_pairs += len(tickers)
            covered_pairs += covered
            daily_coverage = covered / len(tickers) if tickers else 0.0
            daily_coverages.append(daily_coverage)
            if daily_coverage >= config.minimum_cross_section_coverage:
                passing_dates += 1
        pair_coverage = covered_pairs / eligible_pairs if eligible_pairs else 0.0
        date_coverage = passing_dates / len(dates)
        coverage_rows.append(
            FeatureCoverage(
                feature_name=feature_name,
                eligible_pair_count=eligible_pairs,
                covered_pair_count=covered_pairs,
                pair_coverage=pair_coverage,
                evaluated_date_count=len(dates),
                passing_date_count=passing_dates,
                date_coverage=date_coverage,
                minimum_daily_coverage=min(daily_coverages),
                median_daily_coverage=float(
                    sorted(daily_coverages)[len(daily_coverages) // 2]
                ),
            )
        )
        if pair_coverage < config.minimum_cross_section_coverage:
            blockers.append(f"insufficient_pair_coverage:{feature_name}")
        if date_coverage < config.minimum_date_coverage:
            blockers.append(f"insufficient_date_coverage:{feature_name}")

    rejected_rate = rejected_row_count / source_row_count if source_row_count else 0.0
    if rejected_rate > config.maximum_rejected_row_rate:
        blockers.append("invalid_source_timestamps")
    if config.require_revision_history and not revision_history_preserved:
        blockers.append("revision_history_not_preserved")
    universe_symbols = {
        ticker for tickers in normalized_universe.values() for ticker in tickers
    }
    return FeatureAvailabilityReport(
        schema_version="stock_selection_feature_availability_v1",
        market=config.market.strip().upper(),
        analysis_dates=dates,
        universe_symbol_count=len(universe_symbols),
        source_row_count=source_row_count,
        rejected_row_count=rejected_row_count,
        rejected_row_rate=rejected_rate,
        emitted_value_count=emitted_value_count,
        revision_history_preserved=revision_history_preserved,
        config=config,
        feature_coverage=tuple(coverage_rows),
        blockers=tuple(blockers),
        verdict="PASS" if not blockers else "FAIL",
    )


def load_feature_audit_universe(
    *,
    samples_path: Path,
    horizon_days: int,
    analysis_date_count: int,
    exclude_tail_date_count: int,
) -> dict[date, tuple[str, ...]]:
    base = pl.scan_parquet(samples_path).filter(
        (pl.col("horizon_days") == horizon_days) & pl.col("tradable")
    )
    date_strings = (
        base.select(pl.col("feature_date").cast(pl.String).alias("feature_date"))
        .unique()
        .sort("feature_date")
        .collect()["feature_date"]
        .to_list()
    )
    available = date_strings[:-exclude_tail_date_count] if exclude_tail_date_count else date_strings
    selected = available[-analysis_date_count:]
    if len(selected) < analysis_date_count:
        raise ValueError("dataset does not contain the requested feature audit window")
    frame = (
        base.filter(pl.col("feature_date").cast(pl.String).is_in(selected))
        .select(
            pl.col("feature_date").cast(pl.String).alias("feature_date"),
            "ticker",
        )
        .unique()
        .collect()
    )
    grouped: dict[date, list[str]] = defaultdict(list)
    for item in frame.iter_rows(named=True):
        grouped[date.fromisoformat(str(item["feature_date"]))].append(str(item["ticker"]))
    return {
        feature_date: tuple(sorted(set(grouped[feature_date])))
        for feature_date in sorted(grouped)
    }


def persist_feature_availability_report(
    report: FeatureAvailabilityReport,
    *,
    source_version: str,
    root: Path,
) -> FeatureAvailabilityEvidenceWriteResult:
    payload = {
        "schema_version": report.schema_version,
        "source_version": source_version,
        "report": asdict(report),
    }
    canonical = json.dumps(payload, default=_json_default, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    evidence_version = (
        f"stock_selection_feature_availability_v1:{report.market}:{digest[:20]}"
    )
    root.mkdir(parents=True, exist_ok=True)
    target_dir = root / evidence_version.replace(":", "_")
    with tempfile.TemporaryDirectory(prefix=".feature-availability-", dir=root) as name:
        temporary_dir = Path(name)
        report_path = temporary_dir / "feature_availability.json"
        report_path.write_text(
            json.dumps(payload, default=_json_default, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
        manifest = {
            "schema_version": "stock_selection_feature_availability_manifest_v1",
            "evidence_version": evidence_version,
            "source_version": source_version,
            "report_file": report_path.name,
            "report_sha256": report_sha256,
            "market": report.market,
            "verdict": report.verdict,
        }
        (temporary_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if target_dir.exists():
            existing = json.loads((target_dir / "manifest.json").read_text(encoding="utf-8"))
            if existing.get("report_sha256") != report_sha256:
                raise RuntimeError(f"refusing to overwrite feature evidence: {target_dir}")
            return FeatureAvailabilityEvidenceWriteResult(
                evidence_version=evidence_version,
                artifact_dir=target_dir,
                manifest_path=target_dir / "manifest.json",
                reused_existing=True,
            )
        temporary_dir.replace(target_dir)
    return FeatureAvailabilityEvidenceWriteResult(
        evidence_version=evidence_version,
        artifact_dir=target_dir,
        manifest_path=target_dir / "manifest.json",
        reused_existing=False,
    )


def _json_default(value: object) -> object:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")
