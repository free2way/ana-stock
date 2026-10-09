"""Frozen forward-validation batch protocol for the forward-only ``sentiment_v1`` family.

The HiThink sentiment family is **forward-only**: the traceable store covers at
most roughly one trailing year, so it cannot be replayed over a long history the
way the CN price lake can.  The only honest validation is therefore forward:
pre-register one immutable batch (this module + :mod:`scripts.init_sentiment_forward_batch`),
collect sentiment features every session, and only evaluate once enough frozen
decision dates have matured.

This module is deliberately DB- and network-free.  It owns:

* :class:`SentimentForwardBatchSpec` — the frozen batch contract (factor set,
  universe/label versions, ``cost_bps=50`` round-trip, EOD 16:00 cutoff semantics,
  coverage window, operator/created-at provenance, and the acceptance criteria).
* a canonical ``dataset_hash`` that changes whenever the coverage window, the
  cutoff semantics, or the cross-sectional missing-factor policy drift;
* an idempotent ``freeze`` writer: re-running with the same contract reuses the
  existing file, a different contract fails closed instead of silently forking;
* a maturity gate that refuses to emit a conclusion before the frozen threshold;
* a panel builder that enforces coverage and universe membership before any score
  is ranked, so an out-of-coverage sample never reaches the panel.  Ranking uses
  the shared ``factor_pipeline_for_factor_set`` factory, so the batch is scored
  with the same missing-factor contract as production and experiments
  (``sentiment_v1`` → :class:`~app.services.stock_selection.factor_pipeline.MissingFactorPolicy.EXCLUDE`).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from app.services.market_calendar import next_market_open_date
from app.services.stock_selection.experiment_framework import (
    ChangeSpec,
    DecisionSpec,
    ExperimentRow,
    PanelSpec,
    SelectionExperimentSpec,
    evaluate_experiment,
)
from app.services.stock_selection.factor_pipeline import FactorObservation
from app.services.stock_selection.factor_sets import (
    factor_pipeline_for_factor_set,
    get_research_factor_set,
    missing_factor_policy_for_factor_set,
)
from app.services.stock_selection.sentiment_features import (
    SENTIMENT_FACTOR_SET_KEY,
    SENTIMENT_FEATURE_COVERAGE_WINDOW,
)
from app.services.time_utils import app_now

SENTIMENT_FORWARD_BATCH_SCHEMA = "sentiment_v1_forward_batch_v1"
SENTIMENT_FORWARD_DATASET_PREFIX = "sentiment_forward_batch_v1"
SENTIMENT_FORWARD_SCOPE = "sentiment_v1_forward_only"

DEFAULT_BATCH_PATH = Path("data") / "experiments" / "sentiment_v1_forward_batch.json"
DEFAULT_BATCH_ID_PREFIX = "sentiment_v1_forward"
DEFAULT_COST_BPS = 50.0
DEFAULT_HORIZON_DAYS = 5
DEFAULT_TOP_N = 20
DEFAULT_MIN_PRESENT_FEATURES = 1
DEFAULT_LABEL_VERSION = "label_net_return_v1"
DEFAULT_CUTOFF_MAIN_PATH = "eod"
DEFAULT_CUTOFF_LOCAL_TIME = "16:00"
DEFAULT_CUTOFF_SEMANTICS = "same_trade_date_post_close_featured_data"
DEFAULT_CUTOFF_TIMEZONE = "Asia/Shanghai"
AUCTION_CUTOFF_LOCAL_TIME = "09:25"
AUCTION_CUTOFF_SEMANTICS = "same_trade_date_call_auction_close"
OPERATOR_ENV_VAR = "PQW_OPTIN_OPERATOR"
DEFAULT_OPERATOR = "unknown"

PRIMARY_METRIC = "post_cost_net_hit_rate"
PRIMARY_METRIC_SEMANTICS = (
    "fraction_of_matured_top_n_signals_with_positive_net_return_after_cost"
)
DEFAULT_MIN_NET_HIT_RATE = 0.55
DEFAULT_MIN_MATURED_DATES = 60


class SentimentForwardBatchError(RuntimeError):
    """Raised when a forward batch contract cannot be built or frozen safely."""


class SentimentForwardBatchConflict(SentimentForwardBatchError):
    """Raised when an existing frozen batch does not match the requested contract."""


@dataclass(frozen=True, slots=True)
class SentimentForwardCutoff:
    """Decision-cutoff semantics frozen with the batch.

    The first batch takes the EOD 16:00 Asia/Shanghai path only.  The pre-open
    call-auction path (09:25) is recorded explicitly but excluded from the first
    batch (``auction_path_included=False``) so it can be added as a separate,
    separately-hashed batch without silently changing this one.
    """

    main_path: str = DEFAULT_CUTOFF_MAIN_PATH
    local_time: str = DEFAULT_CUTOFF_LOCAL_TIME
    timezone: str = DEFAULT_CUTOFF_TIMEZONE
    semantics: str = DEFAULT_CUTOFF_SEMANTICS
    auction_path_included: bool = False
    auction_local_time: str = AUCTION_CUTOFF_LOCAL_TIME
    auction_semantics: str = AUCTION_CUTOFF_SEMANTICS

    def __post_init__(self) -> None:
        ZoneInfo(self.timezone)
        if self.main_path != DEFAULT_CUTOFF_MAIN_PATH:
            raise ValueError("the first batch supports only the eod main path")
        _validate_hhmm(self.local_time)
        _validate_hhmm(self.auction_local_time)

    def cutoff_for(self, feature_date: date) -> datetime:
        hour, minute = (int(part) for part in self.local_time.split(":", 1))
        return datetime(
            feature_date.year,
            feature_date.month,
            feature_date.day,
            hour,
            minute,
            tzinfo=ZoneInfo(self.timezone),
        )


@dataclass(frozen=True, slots=True)
class SentimentForwardCoverage:
    """Bounded forward coverage window; ``end_date=None`` leaves it open."""

    window: str = SENTIMENT_FEATURE_COVERAGE_WINDOW
    forward_only: bool = True
    start_date: str = ""
    end_date: str | None = None

    def __post_init__(self) -> None:
        if self.forward_only is not True:
            raise ValueError("sentiment forward batches are forward-only")
        _parse_iso_date(self.start_date, "coverage.start_date")
        if self.end_date is not None:
            end = _parse_iso_date(self.end_date, "coverage.end_date")
            if end < _parse_iso_date(self.start_date, "coverage.start_date"):
                raise ValueError("coverage.end_date must not precede coverage.start_date")

    def covers(self, feature_date: date) -> bool:
        if feature_date < _parse_iso_date(self.start_date, "coverage.start_date"):
            return False
        if self.end_date is not None and feature_date > _parse_iso_date(
            self.end_date, "coverage.end_date"
        ):
            return False
        return True


@dataclass(frozen=True, slots=True)
class SentimentForwardAcceptance:
    """Pre-registered acceptance criteria for the forward batch."""

    min_matured_dates: int = DEFAULT_MIN_MATURED_DATES
    primary_metric: str = PRIMARY_METRIC
    min_net_hit_rate: float = DEFAULT_MIN_NET_HIT_RATE
    min_effect_pp: float = 0.0
    alpha: float = 0.05
    fdr: float = 0.05
    min_independent_dates: int = 20
    bootstrap_iterations: int = 1000
    strict_t_threshold: float = 3.5
    random_control_seeds: int = 5

    def __post_init__(self) -> None:
        if self.min_matured_dates <= 0:
            raise ValueError("min_matured_dates must be positive")
        if self.primary_metric != PRIMARY_METRIC:
            raise ValueError(f"the first batch fixes primary_metric={PRIMARY_METRIC!r}")
        if not 0.0 <= self.min_net_hit_rate <= 1.0:
            raise ValueError("min_net_hit_rate must be in [0, 1]")
        if self.min_effect_pp < 0:
            raise ValueError("min_effect_pp must not be negative")
        if not 0.0 < self.alpha < 1.0:
            raise ValueError("alpha must be in (0, 1)")
        if not 0.0 < self.fdr < 1.0:
            raise ValueError("fdr must be in (0, 1)")
        if self.min_independent_dates <= 0:
            raise ValueError("min_independent_dates must be positive")
        if self.bootstrap_iterations < 50:
            raise ValueError("bootstrap_iterations must be at least 50")
        if self.strict_t_threshold <= 0:
            raise ValueError("strict_t_threshold must be positive")
        if self.random_control_seeds < 2:
            raise ValueError("random_control_seeds must be at least 2")

    def primary_metric_semantics(self) -> str:
        return PRIMARY_METRIC_SEMANTICS


@dataclass(frozen=True, slots=True)
class SentimentForwardBatchSpec:
    """Immutable forward batch contract written to the frozen spec file."""

    batch_id: str
    factor_set: str
    factor_set_version: str
    universe_version: str
    label_version: str
    cost_bps: float
    horizon_days: int
    top_n: int
    min_present_features: int
    start_date: str
    coverage: SentimentForwardCoverage
    cutoff: SentimentForwardCutoff
    acceptance: SentimentForwardAcceptance
    dataset_hash: str
    created_at: str
    operator: str
    source_version: str | None = None
    schema_version: str = SENTIMENT_FORWARD_BATCH_SCHEMA
    scope: str = SENTIMENT_FORWARD_SCOPE

    def __post_init__(self) -> None:
        if self.schema_version != SENTIMENT_FORWARD_BATCH_SCHEMA:
            raise ValueError(f"schema_version must be {SENTIMENT_FORWARD_BATCH_SCHEMA!r}")
        if self.factor_set != SENTIMENT_FACTOR_SET_KEY:
            raise ValueError(f"factor_set must be {SENTIMENT_FACTOR_SET_KEY!r}")
        for name in ("batch_id", "factor_set_version", "universe_version", "dataset_hash"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} must not be empty")
        if not str(self.label_version or "").strip():
            raise ValueError("label_version must not be empty")
        if self.cost_bps < 0:
            raise ValueError("cost_bps must not be negative")
        if self.horizon_days <= 0:
            raise ValueError("horizon_days must be positive")
        if self.top_n <= 0:
            raise ValueError("top_n must be positive")
        if self.min_present_features <= 0:
            raise ValueError("min_present_features must be positive")
        start = _parse_iso_date(self.start_date, "start_date")
        if start < _parse_iso_date(self.coverage.start_date, "coverage.start_date"):
            raise ValueError("start_date must not precede coverage.start_date")
        if self.coverage.end_date is not None and start > _parse_iso_date(
            self.coverage.end_date, "coverage.end_date"
        ):
            raise ValueError("start_date must not be after coverage.end_date")

    def cutoff_by_date(self, dates: Iterable[date]) -> dict[date, datetime]:
        return {item: self.cutoff.cutoff_for(item) for item in dates}

    def covers(self, feature_date: date) -> bool:
        return self.coverage.covers(feature_date)

    def missing_policy(self) -> str:
        """Cross-sectional missing-factor policy this batch is scored under.

        Derived from the frozen factor set through the shared factory, so the
        batch is always scored with the same contract production/experiments use
        (``sentiment_v1`` → ``EXCLUDE``).
        """

        return missing_factor_policy_for_factor_set(
            get_research_factor_set(self.factor_set)
        ).value

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "scope": self.scope,
            "batch_id": self.batch_id,
            "factor_set": self.factor_set,
            "factor_set_version": self.factor_set_version,
            "missing_policy": self.missing_policy(),
            "universe_version": self.universe_version,
            "label_version": self.label_version,
            "cost_bps": self.cost_bps,
            "horizon_days": self.horizon_days,
            "top_n": self.top_n,
            "min_present_features": self.min_present_features,
            "start_date": self.start_date,
            "source_version": self.source_version,
            "cutoff": asdict(self.cutoff),
            "coverage": asdict(self.coverage),
            "acceptance": {
                **asdict(self.acceptance),
                "primary_metric_semantics": self.acceptance.primary_metric_semantics(),
            },
            "dataset_hash": self.dataset_hash,
            "created_at": self.created_at,
            "operator": self.operator,
        }


def _validate_hhmm(value: str) -> None:
    hours, separator, minutes = str(value).partition(":")
    if (
        not separator
        or len(hours) != 2
        or len(minutes) != 2
        or not (hours + minutes).isdigit()
        or not 0 <= int(hours) <= 23
        or not 0 <= int(minutes) <= 59
    ):
        raise ValueError(f"cutoff time must be a valid HH:MM value, got {value!r}")


def _parse_iso_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO date (YYYY-MM-DD)") from exc


def resolve_operator(environ: Mapping[str, str] | None = None) -> str:
    """Read ``PQW_OPTIN_OPERATOR``; default to ``unknown`` when unset/blank."""

    source = os.environ if environ is None else environ
    return str(source.get(OPERATOR_ENV_VAR) or "").strip() or DEFAULT_OPERATOR


def default_start_date(today: date | None = None) -> str:
    """First available CN trading day at/after ``today`` (holidays skip forward)."""

    anchor = (today or app_now().date()).isoformat()
    return next_market_open_date("CN", anchor, include_self=True)


def resolve_latest_pit_universe_version(universes_root: Path | str) -> str:
    """Return the ``universe_version`` of the newest local PIT universe artifact.

    Reads only the on-disk ``universes/pit_universe_v1_*_*/manifest.json`` files;
    no database or network access.
    """

    root = Path(universes_root)
    candidates: list[tuple[float, str, str]] = []
    for manifest_path in root.glob("pit_universe_v1_*_*/manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        version = str(manifest.get("universe_version") or "").strip()
        if version:
            candidates.append((manifest_path.stat().st_mtime, manifest_path.parent.name, version))
    if not candidates:
        raise SentimentForwardBatchError(f"no pit_universe_v1 manifest found under {root}")
    candidates.sort(reverse=True)
    return candidates[0][2]


def compute_sentiment_forward_dataset_hash(
    *,
    factor_set: str,
    factor_set_version: str,
    universe_version: str,
    label_version: str,
    cost_bps: float,
    horizon_days: int,
    top_n: int,
    min_present_features: int,
    start_date: str,
    coverage: SentimentForwardCoverage,
    cutoff: SentimentForwardCutoff,
    source_version: str | None = None,
) -> str:
    """Canonical hash over the frozen inputs, including coverage window + cutoff semantics.

    The cross-sectional ``missing_policy`` is folded in as well: switching a
    family between ``NEUTRAL_ZERO`` and ``EXCLUDE`` changes every composite score,
    so it must fork the frozen ``dataset_hash`` instead of silently reinterpreting
    an existing batch.
    """

    missing_policy = missing_factor_policy_for_factor_set(
        get_research_factor_set(factor_set)
    ).value
    canonical = {
        "schema_version": SENTIMENT_FORWARD_BATCH_SCHEMA,
        "factor_set": factor_set,
        "factor_set_version": factor_set_version,
        "missing_policy": missing_policy,
        "universe_version": universe_version,
        "label_version": label_version,
        "cost_bps": float(cost_bps),
        "horizon_days": int(horizon_days),
        "top_n": int(top_n),
        "min_present_features": int(min_present_features),
        "start_date": str(start_date),
        "source_version": source_version,
        "coverage": asdict(coverage),
        "cutoff": asdict(cutoff),
    }
    digest = hashlib.sha256(
        json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()[:20]
    return f"{SENTIMENT_FORWARD_DATASET_PREFIX}:CN:{digest}"


def build_sentiment_forward_batch_spec(
    *,
    universe_version: str,
    start_date: str,
    operator: str = DEFAULT_OPERATOR,
    created_at: str | None = None,
    label_version: str = DEFAULT_LABEL_VERSION,
    cost_bps: float = DEFAULT_COST_BPS,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    top_n: int = DEFAULT_TOP_N,
    min_present_features: int = DEFAULT_MIN_PRESENT_FEATURES,
    coverage_start: str | None = None,
    coverage_end: str | None = None,
    source_version: str | None = None,
    auction_path_included: bool = False,
) -> SentimentForwardBatchSpec:
    """Build (but do not persist) one frozen forward batch contract."""

    factor_set = get_research_factor_set(SENTIMENT_FACTOR_SET_KEY)
    coverage = SentimentForwardCoverage(
        start_date=coverage_start or start_date,
        end_date=coverage_end,
    )
    cutoff = SentimentForwardCutoff(auction_path_included=auction_path_included)
    dataset_hash = compute_sentiment_forward_dataset_hash(
        factor_set=SENTIMENT_FACTOR_SET_KEY,
        factor_set_version=factor_set.version(),
        universe_version=universe_version,
        label_version=label_version,
        cost_bps=cost_bps,
        horizon_days=horizon_days,
        top_n=top_n,
        min_present_features=min_present_features,
        start_date=start_date,
        coverage=coverage,
        cutoff=cutoff,
        source_version=source_version,
    )
    resolved_start = _parse_iso_date(start_date, "start_date").isoformat()
    return SentimentForwardBatchSpec(
        batch_id=f"{DEFAULT_BATCH_ID_PREFIX}_{resolved_start}",
        factor_set=SENTIMENT_FACTOR_SET_KEY,
        factor_set_version=factor_set.version(),
        universe_version=str(universe_version),
        label_version=str(label_version),
        cost_bps=float(cost_bps),
        horizon_days=int(horizon_days),
        top_n=int(top_n),
        min_present_features=int(min_present_features),
        start_date=resolved_start,
        coverage=coverage,
        cutoff=cutoff,
        acceptance=SentimentForwardAcceptance(),
        dataset_hash=dataset_hash,
        created_at=str(created_at or app_now().isoformat()),
        operator=str(operator or DEFAULT_OPERATOR),
        source_version=source_version,
    )


def _spec_from_payload(raw: Mapping[str, Any]) -> SentimentForwardBatchSpec:
    cutoff_raw = raw.get("cutoff") or {}
    coverage_raw = raw.get("coverage") or {}
    acceptance_raw = raw.get("acceptance") or {}
    acceptance = {
        key: acceptance_raw[key]
        for key in SentimentForwardAcceptance.__dataclass_fields__
        if key in acceptance_raw
    }
    return SentimentForwardBatchSpec(
        batch_id=str(raw["batch_id"]),
        factor_set=str(raw["factor_set"]),
        factor_set_version=str(raw["factor_set_version"]),
        universe_version=str(raw["universe_version"]),
        label_version=str(raw["label_version"]),
        cost_bps=float(raw["cost_bps"]),
        horizon_days=int(raw["horizon_days"]),
        top_n=int(raw["top_n"]),
        min_present_features=int(raw.get("min_present_features", DEFAULT_MIN_PRESENT_FEATURES)),
        start_date=str(raw["start_date"]),
        coverage=SentimentForwardCoverage(
            window=str(coverage_raw.get("window", SENTIMENT_FEATURE_COVERAGE_WINDOW)),
            forward_only=bool(coverage_raw.get("forward_only", True)),
            start_date=str(coverage_raw["start_date"]),
            end_date=(
                None
                if coverage_raw.get("end_date") in (None, "")
                else str(coverage_raw["end_date"])
            ),
        ),
        cutoff=SentimentForwardCutoff(
            main_path=str(cutoff_raw.get("main_path", DEFAULT_CUTOFF_MAIN_PATH)),
            local_time=str(cutoff_raw.get("local_time", DEFAULT_CUTOFF_LOCAL_TIME)),
            timezone=str(cutoff_raw.get("timezone", DEFAULT_CUTOFF_TIMEZONE)),
            semantics=str(cutoff_raw.get("semantics", DEFAULT_CUTOFF_SEMANTICS)),
            auction_path_included=bool(cutoff_raw.get("auction_path_included", False)),
            auction_local_time=str(
                cutoff_raw.get("auction_local_time", AUCTION_CUTOFF_LOCAL_TIME)
            ),
            auction_semantics=str(cutoff_raw.get("auction_semantics", AUCTION_CUTOFF_SEMANTICS)),
        ),
        acceptance=SentimentForwardAcceptance(**acceptance),
        dataset_hash=str(raw["dataset_hash"]),
        created_at=str(raw["created_at"]),
        operator=str(raw.get("operator", DEFAULT_OPERATOR)),
        source_version=(
            None if raw.get("source_version") in (None, "") else str(raw["source_version"])
        ),
    )


def load_sentiment_forward_batch(path: Path | str) -> SentimentForwardBatchSpec:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise SentimentForwardBatchError("forward batch spec must be a JSON object")
    return _spec_from_payload(raw)


def freeze_sentiment_forward_batch(
    path: Path | str, spec: SentimentForwardBatchSpec
) -> dict[str, Any]:
    """Idempotently persist one frozen batch.

    Re-running with the same ``dataset_hash`` reuses the existing file.  A
    different hash fails closed: a frozen pre-registration must never be silently
    overwritten.
    """

    target = Path(path)
    if target.exists():
        existing = load_sentiment_forward_batch(target)
        if existing.dataset_hash != spec.dataset_hash:
            raise SentimentForwardBatchConflict(
                "existing forward batch has a different dataset_hash "
                f"({existing.dataset_hash} != {spec.dataset_hash}); "
                "choose a new batch path instead of overwriting the frozen batch"
            )
        if (
            existing.start_date != spec.start_date
            or existing.universe_version != spec.universe_version
            or existing.batch_id != spec.batch_id
        ):
            raise SentimentForwardBatchConflict(
                "existing forward batch identity does not match the requested batch"
            )
        return {
            "status": "reused_existing",
            "batch_id": existing.batch_id,
            "dataset_hash": existing.dataset_hash,
            "path": str(target),
            "spec": existing.to_payload(),
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(spec.to_payload(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "status": "created",
        "batch_id": spec.batch_id,
        "dataset_hash": spec.dataset_hash,
        "path": str(target),
        "spec": spec.to_payload(),
    }


# --------------------------------------------------------------------------- #
# Maturity gate
# --------------------------------------------------------------------------- #


def assess_sentiment_forward_maturity(
    *,
    matured_date_count: int,
    min_matured_dates: int,
    pending_date_count: int = 0,
    incomplete_date_count: int = 0,
) -> dict[str, Any]:
    """Block an immature batch; never emit a conclusion before the threshold."""

    if matured_date_count < 0 or pending_date_count < 0 or incomplete_date_count < 0:
        raise ValueError("maturity counters must not be negative")
    blockers: list[str] = []
    if matured_date_count < min_matured_dates:
        blockers.append(f"minimum_{min_matured_dates}_matured_dates_not_met")
    if incomplete_date_count > 0:
        blockers.append("incomplete_frozen_batches")
    return {
        "matured_date_count": int(matured_date_count),
        "min_matured_dates": int(min_matured_dates),
        "remaining_matured_dates": max(0, int(min_matured_dates) - int(matured_date_count)),
        "pending_date_count": int(pending_date_count),
        "incomplete_date_count": int(incomplete_date_count),
        "promotion_status": "BLOCKED" if blockers else "REVIEW_REQUIRED",
        "promotion_blockers": blockers,
        "conclusion_allowed": not blockers,
    }


# --------------------------------------------------------------------------- #
# Panel construction (coverage + universe enforcement)
# --------------------------------------------------------------------------- #


def _finite_positive(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _coerce_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return math.nan


def _price_index(
    price_rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[tuple[str, str], Mapping[str, Any]], dict[str, list[str]]]:
    index: dict[tuple[str, str], Mapping[str, Any]] = {}
    dates_by_ticker: dict[str, list[str]] = {}
    for row in price_rows:
        ticker = str(row.get("symbol") or row.get("ticker") or "").strip().upper()
        trade_date = str(row.get("date") or row.get("trade_date") or "").strip()[:10]
        if not ticker or not trade_date:
            continue
        index[(ticker, trade_date)] = row
        dates_by_ticker.setdefault(ticker, []).append(trade_date)
    for ticker in dates_by_ticker:
        dates_by_ticker[ticker] = sorted(set(dates_by_ticker[ticker]))
    return index, dates_by_ticker


def measure_forward_net_return(
    index: Mapping[tuple[str, str], Mapping[str, Any]],
    dates_by_ticker: Mapping[str, Sequence[str]],
    *,
    ticker: str,
    entry_date: str,
    exit_date: str,
    cost_bps: float,
    maximum_single_day_jump: float = 0.80,
) -> float | None:
    """Next-open -> exit-close diagnostic return net of round-trip cost.

    Mirrors :mod:`app.services.stock_selection.forward_shadow_evaluation`
    (entry at the frozen effective-date open, exit at the horizon close, and a
    reverse-split-like jump guard).
    """

    entry = index.get((ticker, entry_date))
    exit_row = index.get((ticker, exit_date))
    entry_open = _finite_positive((entry or {}).get("open"))
    exit_close = _finite_positive((exit_row or {}).get("close"))
    if entry_open is None or exit_close is None:
        return None
    path_dates = [
        value for value in dates_by_ticker.get(ticker, []) if entry_date <= value <= exit_date
    ]
    closes = [
        _finite_positive((index.get((ticker, value)) or {}).get("close")) for value in path_dates
    ]
    valid = [value for value in closes if value is not None]
    if any(
        abs(current / previous - 1.0) >= maximum_single_day_jump
        for previous, current in zip(valid, valid[1:])
    ):
        return None
    return exit_close / entry_open - 1.0 - cost_bps / 10_000.0


def build_sentiment_forward_scores(
    *,
    batch: SentimentForwardBatchSpec,
    features_by_key: Mapping[tuple[date, str], Mapping[str, float | None]],
    universe_by_date: Mapping[date, Iterable[str]],
) -> dict[str, Any]:
    """Rank the frozen universe; coverage and membership gates run before ranking.

    Any feature row whose date falls outside the frozen coverage window, or whose
    ticker is not in the frozen universe for that date, is dropped here and
    reported separately, so an out-of-coverage sample can never reach the panel.

    Ranking goes through ``factor_pipeline_for_factor_set`` so the batch uses the
    same missing-factor contract as production/experiments (``sentiment_v1`` →
    ``MissingFactorPolicy.EXCLUDE``): a cell missing from the bounded sentiment
    window is omitted and the composite renormalized, never scored as a zero.
    """

    universe = {
        feature_date: {
            str(ticker or "").strip().upper()
            for ticker in universe_by_date.get(feature_date, ())
            if str(ticker or "").strip()
        }
        for feature_date in universe_by_date
    }

    coverage_excluded_dates: set[str] = set()
    observations: list[FactorObservation] = []
    for (feature_date, ticker), raw_features in sorted(
        features_by_key.items(), key=lambda item: (item[0][0], item[0][1])
    ):
        normalized_ticker = str(ticker or "").strip().upper()
        if not batch.covers(feature_date):
            coverage_excluded_dates.add(feature_date.isoformat())
            continue
        if normalized_ticker not in universe.get(feature_date, set()):
            continue
        present = {
            str(name): float(value)
            for name, value in (raw_features or {}).items()
            if value is not None and math.isfinite(_coerce_float(value))
        }
        if len(present) < batch.min_present_features:
            continue
        observations.append(
            FactorObservation(
                observation_id=f"sentiment-forward:{feature_date.isoformat()}:{normalized_ticker}",
                ticker=normalized_ticker,
                feature_date=feature_date,
                horizon_days=batch.horizon_days,
                features=present,
            )
        )
    if not observations:
        raise SentimentForwardBatchError("no in-coverage universe rows have sentiment features")

    factor_set = get_research_factor_set(batch.factor_set)
    scores = factor_pipeline_for_factor_set(factor_set).transform(observations)
    score_rows = [
        {
            "feature_date": score.feature_date.isoformat(),
            "ticker": score.ticker,
            "composite_score": float(score.composite_score),
            "cross_sectional_rank": float(score.cross_sectional_rank),
            "missing_policy": score.missing_policy,
        }
        for score in sorted(
            scores, key=lambda item: (item.feature_date, -item.cross_sectional_rank, item.ticker)
        )
    ]
    return {
        "score_rows": score_rows,
        "coverage_excluded_dates": sorted(coverage_excluded_dates),
    }


def build_sentiment_forward_panel_from_scores(
    *,
    batch: SentimentForwardBatchSpec,
    score_rows: Iterable[Mapping[str, Any]],
    price_rows: Iterable[Mapping[str, Any]],
    as_of_date: str,
) -> dict[str, Any]:
    """Build the maturity-gated treated/control panel from frozen score rows."""

    normalized_as_of = str(as_of_date or "").strip()[:10]
    if not normalized_as_of:
        raise ValueError("as_of_date is required")

    scheduled: dict[date, list[tuple[float, float, str]]] = {}
    coverage_excluded_dates: set[str] = set()
    for row in score_rows:
        try:
            feature_date = date.fromisoformat(str(row.get("feature_date"))[:10])
        except (TypeError, ValueError):
            continue
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        if not batch.covers(feature_date):
            coverage_excluded_dates.add(feature_date.isoformat())
            continue
        scheduled.setdefault(feature_date, []).append(
            (
                float(row.get("cross_sectional_rank") or 0.0),
                float(row.get("composite_score") or 0.0),
                ticker,
            )
        )

    index, dates_by_ticker = _price_index(price_rows)
    treated_rows: list[ExperimentRow] = []
    control_rows: list[ExperimentRow] = []
    daily: list[dict[str, Any]] = []
    frozen_candidates: list[dict[str, Any]] = []
    missing_returns: list[dict[str, Any]] = []
    pending_dates: list[str] = []
    matured_dates: list[str] = []
    for feature_date in sorted(scheduled):
        entry_date = next_market_open_date("CN", feature_date, include_self=False)
        exit_date = entry_date
        for _ in range(max(0, batch.horizon_days - 1)):
            exit_date = next_market_open_date("CN", exit_date, include_self=False)
        if exit_date > normalized_as_of:
            pending_dates.append(feature_date.isoformat())
            continue

        # Freeze treated/control membership from the frozen ranking *before* any
        # outcome is inspected.  A frozen name with no measurable return is
        # counted as missing and never backfilled by the next-ranked name, so the
        # panel cannot silently re-select a later winner (selection bias).
        ranked = sorted(scheduled[feature_date], key=lambda item: (-item[0], item[2]))
        frozen_treated = [ticker for _, _, ticker in ranked[: batch.top_n]]
        frozen_control = [ticker for _, _, ticker in ranked[batch.top_n :]]
        treated_names = set(frozen_treated)
        control_names = set(frozen_control)

        measured: list[ExperimentRow] = []
        missing_tickers: list[str] = []
        for rank, composite, ticker in ranked:
            net = measure_forward_net_return(
                index,
                dates_by_ticker,
                ticker=ticker,
                entry_date=entry_date,
                exit_date=exit_date,
                cost_bps=batch.cost_bps,
            )
            if net is None:
                missing_tickers.append(ticker)
                continue
            gross = net + batch.cost_bps / 10_000.0
            measured.append(
                ExperimentRow(
                    feature_date=feature_date,
                    ticker=ticker,
                    raw_score=composite,
                    net_return=float(net),
                    gross_return=float(gross),
                    label_value=float(gross),
                    ranking_values={"raw_score": composite},
                )
            )
        for ticker in missing_tickers:
            missing_returns.append(
                {
                    "feature_date": feature_date.isoformat(),
                    "ticker": ticker,
                    "reason": "no_measurable_forward_return",
                    "frozen_group": "treated" if ticker in treated_names else "control",
                }
            )
        frozen_candidates.append(
            {
                "feature_date": feature_date.isoformat(),
                "entry_date": entry_date,
                "exit_date": exit_date,
                "top_n": batch.top_n,
                "treated": frozen_treated,
                "control": frozen_control,
                "missing": missing_tickers,
                "measured_count": len(measured),
            }
        )
        if len(measured) < 2 * batch.top_n:
            # Too few measurable returns for a stable treated/control contrast:
            # the date stays out of the matured set, but the frozen list above
            # keeps the (unbackfilled) selection auditable.
            continue
        treated = [row for row in measured if row.ticker in treated_names]
        remainder = [row for row in measured if row.ticker in control_names]
        if not treated or not remainder:
            continue
        treated_rows.extend(treated)
        control_rows.extend(remainder)
        matured_dates.append(feature_date.isoformat())
        daily.append(
            {
                "feature_date": feature_date.isoformat(),
                "entry_date": entry_date,
                "exit_date": exit_date,
                "measured_count": len(measured),
                "top_n": batch.top_n,
                "treated_frozen": frozen_treated,
                "control_frozen": frozen_control,
                "treated_measured_count": len(treated),
                "control_measured_count": len(remainder),
                "missing_count": len(missing_tickers),
                "missing_tickers": missing_tickers,
                "treated_mean_net_return": sum(row.net_return for row in treated) / len(treated),
                "control_mean_net_return": sum(row.net_return for row in remainder)
                / len(remainder),
            }
        )

    maturity = assess_sentiment_forward_maturity(
        matured_date_count=len(matured_dates),
        min_matured_dates=batch.acceptance.min_matured_dates,
        pending_date_count=len(pending_dates),
    )
    return {
        "batch_id": batch.batch_id,
        "dataset_hash": batch.dataset_hash,
        "as_of_date": normalized_as_of,
        "matured_dates": matured_dates,
        "pending_dates": pending_dates,
        "coverage_excluded_dates": sorted(coverage_excluded_dates),
        "evaluated_date_count": len(matured_dates),
        "treated_rows": treated_rows,
        "control_rows": control_rows,
        "daily": daily,
        "frozen_candidates": frozen_candidates,
        "missing_returns": missing_returns,
        "missing_return_count": len(missing_returns),
        "dates_with_missing_returns": sorted(
            {entry["feature_date"] for entry in missing_returns}
        ),
        "maturity": maturity,
    }


def build_sentiment_forward_panel(
    *,
    batch: SentimentForwardBatchSpec,
    features_by_key: Mapping[tuple[date, str], Mapping[str, float | None]],
    universe_by_date: Mapping[date, Iterable[str]],
    price_rows: Iterable[Mapping[str, Any]],
    as_of_date: str,
) -> dict[str, Any]:
    """Convenience wrapper: score the frozen universe, then build the panel."""

    scored = build_sentiment_forward_scores(
        batch=batch,
        features_by_key=features_by_key,
        universe_by_date=universe_by_date,
    )
    panel = build_sentiment_forward_panel_from_scores(
        batch=batch,
        score_rows=scored["score_rows"],
        price_rows=price_rows,
        as_of_date=as_of_date,
    )
    panel["coverage_excluded_dates"] = sorted(
        set(panel["coverage_excluded_dates"]) | set(scored["coverage_excluded_dates"])
    )
    return panel


# --------------------------------------------------------------------------- #
# Experiment-framework bridge
# --------------------------------------------------------------------------- #


def build_sentiment_forward_experiment_spec(
    batch: SentimentForwardBatchSpec, *, oos_end: str
) -> SelectionExperimentSpec:
    """Map the frozen batch contract onto the shared experiment framework spec."""

    end = _parse_iso_date(oos_end, "oos_end")
    start = _parse_iso_date(batch.start_date, "start_date")
    if end < start:
        end = start
    return SelectionExperimentSpec(
        experiment_id=f"sentiment-v1-forward-{batch.start_date}",
        market="CN",
        hypothesis=(
            "冻结的前向情绪因子批次：sentiment_v1 Top-N 的扣费净收益命中率优于"
            "同一冻结面板内的次优 N（rank N+1..2N），需通过聚类 CI + BH-FDR + "
            "shuffle 随机对照检验。"
        ),
        change=ChangeSpec(
            kind="forward_only_sentiment_factor",
            patch={
                "factor_set": batch.factor_set,
                "dataset_hash": batch.dataset_hash,
                "coverage_window": batch.coverage.window,
                "cutoff_semantics": batch.cutoff.semantics,
            },
        ),
        panel=PanelSpec(
            oos_start=start,
            oos_end=end,
            universe_version=batch.universe_version,
            label_version=batch.label_version,
            cost_bps=batch.cost_bps,
            horizons=(batch.horizon_days,),
            top_n=batch.top_n,
        ),
        strata=("regime",),
        decision=DecisionSpec(
            primary_metric="net_return",
            min_effect_pp=batch.acceptance.min_effect_pp,
            alpha=batch.acceptance.alpha,
            fdr=batch.acceptance.fdr,
            min_independent_dates=batch.acceptance.min_independent_dates,
            strict_t_threshold=batch.acceptance.strict_t_threshold,
            bootstrap_iterations=batch.acceptance.bootstrap_iterations,
        ),
    )


def run_sentiment_forward_experiment(
    *,
    batch: SentimentForwardBatchSpec,
    treated_rows: Sequence[ExperimentRow],
    control_rows: Sequence[ExperimentRow],
    oos_end: str,
    attempts: int = 0,
) -> dict[str, Any]:
    """Run the shared experiment framework on the frozen sentiment panel."""

    spec = build_sentiment_forward_experiment_spec(batch, oos_end=oos_end)
    report = evaluate_experiment(
        spec,
        treated_rows=treated_rows,
        control_rows=control_rows,
        dataset_hash=batch.dataset_hash,
        attempts=attempts,
        random_control_seeds=batch.acceptance.random_control_seeds,
    )
    return report.as_dict()


__all__ = [
    "SENTIMENT_FORWARD_BATCH_SCHEMA",
    "DEFAULT_BATCH_PATH",
    "DEFAULT_COST_BPS",
    "PRIMARY_METRIC",
    "OPERATOR_ENV_VAR",
    "SentimentForwardAcceptance",
    "SentimentForwardBatchConflict",
    "SentimentForwardBatchError",
    "SentimentForwardBatchSpec",
    "SentimentForwardCoverage",
    "SentimentForwardCutoff",
    "assess_sentiment_forward_maturity",
    "build_sentiment_forward_batch_spec",
    "build_sentiment_forward_experiment_spec",
    "build_sentiment_forward_panel",
    "build_sentiment_forward_panel_from_scores",
    "build_sentiment_forward_scores",
    "compute_sentiment_forward_dataset_hash",
    "default_start_date",
    "freeze_sentiment_forward_batch",
    "load_sentiment_forward_batch",
    "measure_forward_net_return",
    "resolve_latest_pit_universe_version",
    "resolve_operator",
    "run_sentiment_forward_experiment",
]
