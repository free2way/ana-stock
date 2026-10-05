"""Point-in-time sentiment features from public HiThink featured/auction data.

This module is a read-only adapter: it turns raw HiThink public featured-data and
auction payloads into ``(date, symbol)`` sentiment features. It never fetches and
never writes to the main CN price lake.

Point-in-time contract
----------------------
* Every observation carries the trading date it describes and a semantic
  availability time: end-of-day featured data becomes knowable at the configured
  post-close hour, while a ``final`` call-auction snapshot becomes knowable at the
  configured pre-open time (default 09:25 Asia/Shanghai).
* For a feature date ``F`` the adapter only uses observations whose trading date is
  ``<= F`` and whose availability is ``<= cutoff_by_date[F]``; among usable ones it
  takes the freshest. A feature date therefore never sees its own post-close
  featured data at a pre-open cutoff.
* Call-auction features are only emitted for cutoffs at/after the auction time, so
  they can be used as pre-open signals but never as look-ahead intraday inputs.
* Missing data is ``None``; only exhaustive event pools map an absent symbol to a
  zero membership flag.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from app.services.hithink_feature_store import load_hithink_feature_records
from app.services.ticker_format import normalize_ticker_for_market


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

EOD_FEATURE_NAMES = (
    "limit_up_pool",
    "limit_down_pool",
    "limit_break_pool",
    "limit_up_ladder",
    "hot_stock_list",
    "skyrocket_list",
    "hot_stock_list_history",
    "hot_stock_rank_trend",
    "dragon_tiger_list",
    "anomaly_analysis_list",
    "anomaly_analysis_stock",
)
AUCTION_FEATURE_NAMES = ("auction_snapshot", "auction_short_term_benchmark")
SUPPORTED_FEATURE_NAMES = EOD_FEATURE_NAMES + AUCTION_FEATURE_NAMES

# Low -> high precedence when several families feed the same column.
_FAMILY_PRIORITY = (
    "hot_stock_list_history",
    "hot_stock_rank_trend",
    "skyrocket_list",
    "hot_stock_list",
    "limit_down_pool",
    "limit_break_pool",
    "limit_up_pool",
    "limit_up_ladder",
    "dragon_tiger_list",
    "anomaly_analysis_list",
    "anomaly_analysis_stock",
    "auction_short_term_benchmark",
    "auction_snapshot",
)

FAMILY_COLUMNS: dict[str, tuple[str, ...]] = {
    "limit_up_pool": (
        "limit_up_flag",
        "limit_up_streak",
        "limit_up_seal_money",
        "limit_up_max_seal_money",
        "limit_up_time_minutes",
        "limit_up_change_pct",
    ),
    "limit_down_pool": ("limit_down_flag", "limit_down_change_pct", "limit_down_turnover_pct"),
    "limit_break_pool": ("limit_break_flag", "limit_break_open_times", "limit_break_change_pct"),
    "limit_up_ladder": ("ladder_board_num", "ladder_sign_level"),
    "hot_stock_list": ("hot_rank", "hot_rank_score", "hot_rank_change"),
    "skyrocket_list": ("hot_rank", "hot_rank_score", "hot_rank_change"),
    "hot_stock_list_history": ("hot_rank", "hot_rank_score"),
    "hot_stock_rank_trend": ("hot_rank",),
    "dragon_tiger_list": (
        "dragon_tiger_flag",
        "dragon_tiger_net_value",
        "dragon_tiger_net_rate",
        "dragon_tiger_hot_rank",
        "dragon_tiger_org_net_value",
        "dragon_tiger_hot_money_net_value",
        "dragon_tiger_range_days",
    ),
    "anomaly_analysis_list": (
        "anomaly_flag",
        "anomaly_count",
        "anomaly_limit_up_flag",
        "anomaly_limit_down_flag",
    ),
    "anomaly_analysis_stock": (
        "anomaly_flag",
        "anomaly_count",
        "anomaly_limit_up_flag",
        "anomaly_limit_down_flag",
    ),
    "auction_snapshot": (
        "auction_pct",
        "auction_price",
        "auction_volume_ratio",
        "auction_turnover_pct",
        "auction_yesterday_ratio_pct",
        "auction_amount",
        "auction_strength",
    ),
    "auction_short_term_benchmark": (
        "auction_benchmark_pct",
        "auction_benchmark_high_open_flag",
        "auction_benchmark_heavy_volume_flag",
    ),
}

# Families that return the full daily set, so an absent symbol means "did not occur".
_EXHAUSTIVE_FAMILIES = frozenset(
    {
        "limit_up_pool",
        "limit_down_pool",
        "limit_break_pool",
        "limit_up_ladder",
        "dragon_tiger_list",
        "anomaly_analysis_list",
        "anomaly_analysis_stock",
    }
)

# Value used when an exhaustive family is available but the symbol is absent (or
# the field is null). ``None`` keeps "unknown"; ``0.0`` is a true "did not occur".
_COLUMN_DEFAULTS: dict[str, float | None] = {
    "limit_up_flag": 0.0,
    "limit_up_streak": 0.0,
    "limit_up_seal_money": 0.0,
    "limit_up_max_seal_money": 0.0,
    "limit_up_time_minutes": None,
    "limit_up_change_pct": None,
    "limit_down_flag": 0.0,
    "limit_down_change_pct": None,
    "limit_down_turnover_pct": None,
    "limit_break_flag": 0.0,
    "limit_break_open_times": 0.0,
    "limit_break_change_pct": None,
    "ladder_board_num": 0.0,
    "ladder_sign_level": 0.0,
    "dragon_tiger_flag": 0.0,
    "dragon_tiger_net_value": 0.0,
    "dragon_tiger_net_rate": 0.0,
    "dragon_tiger_hot_rank": None,
    "dragon_tiger_org_net_value": 0.0,
    "dragon_tiger_hot_money_net_value": 0.0,
    "dragon_tiger_range_days": None,
    "anomaly_flag": 0.0,
    "anomaly_count": 0.0,
    "anomaly_limit_up_flag": 0.0,
    "anomaly_limit_down_flag": 0.0,
}

FEATURE_NAMES: tuple[str, ...] = tuple(
    dict.fromkeys(
        column for family in _FAMILY_PRIORITY for column in FAMILY_COLUMNS[family]
    )
)

_BLOCKED_AUCTION_STATUS = frozenset({"not_ready", "unavailable", "empty"})


class SentimentFeatureError(RuntimeError):
    """Raised when sentiment features cannot be built without guessing."""


@dataclass(frozen=True, slots=True)
class SentimentPITConfig:
    market: str = "CN"
    featured_available_hour: int = 16
    featured_available_minute: int = 0
    auction_available_hour: int = 9
    auction_available_minute: int = 25
    timezone_name: str = "Asia/Shanghai"

    def __post_init__(self) -> None:
        if str(self.market or "").strip().upper() != "CN":
            raise ValueError("sentiment features currently support only the CN market")
        ZoneInfo(self.timezone_name)
        for hour, minute, label in (
            (self.featured_available_hour, self.featured_available_minute, "featured"),
            (self.auction_available_hour, self.auction_available_minute, "auction"),
        ):
            if not 0 <= int(hour) <= 23 or not 0 <= int(minute) <= 59:
                raise ValueError(f"{label} availability time must be a valid time of day")

    def availability_time(self, *, auction: bool) -> time:
        if auction:
            return time(int(self.auction_available_hour), int(self.auction_available_minute))
        return time(int(self.featured_available_hour), int(self.featured_available_minute))


@dataclass(frozen=True, slots=True)
class HithinkSentimentObservation:
    feature_name: str
    trade_date: date
    provider: str
    source_reference: str
    fetched_at: str
    data: Mapping[str, Any]
    slot: str | None = None


@dataclass(frozen=True, slots=True)
class SentimentFeatureBuild:
    schema_version: str
    source_version: str
    feature_names: tuple[str, ...]
    features_by_key: Mapping[tuple[date, str], Mapping[str, float | None]]
    selected_observations_by_date: Mapping[date, Mapping[str, str]]


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_datetime(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise SentimentFeatureError("observation is missing fetched_at")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SentimentFeatureError(f"invalid fetched_at: {text!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed


def _symbol_of(item: Mapping[str, Any]) -> str | None:
    thscode = str(item.get("thscode") or item.get("ticker") or "").strip()
    if not thscode:
        return None
    return normalize_ticker_for_market(thscode, "CN")


def _items(data: Mapping[str, Any]) -> list[dict]:
    return [item for item in (data.get("item") or []) if isinstance(item, dict)]


def _date_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return text


def _minutes_from_hhmm(value: Any) -> float | None:
    text = str(value or "").strip()
    if len(text) < 5 or ":" not in text:
        return None
    hours, _, minutes = text.partition(":")
    try:
        return float(int(hours) * 60 + int(minutes))
    except ValueError:
        return None


def _hot_rank_rows(items: list[dict], *, with_change: bool, with_score: bool) -> dict[str, dict]:
    ranked = [item for item in items if _as_float(item.get("rank")) is not None]
    total = len(ranked)
    rows: dict[str, dict] = {}
    for item in ranked:
        symbol = _symbol_of(item)
        if symbol is None:
            continue
        rank = _as_float(item.get("rank"))
        row: dict[str, float | None] = {"hot_rank": rank}
        if with_score:
            row["hot_rank_score"] = 1.0 - (rank - 1.0) / total if total else None
        if with_change:
            row["hot_rank_change"] = _as_float(item.get("rank_change"))
        rows[symbol] = row
    return rows


def _extract_limit_up_pool(observation: HithinkSentimentObservation) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for item in _items(observation.data):
        symbol = _symbol_of(item)
        if symbol is None:
            continue
        rows[symbol] = {
            "limit_up_flag": 1.0,
            "limit_up_streak": _as_float(item.get("continue_day_cnt")),
            "limit_up_seal_money": _as_float(item.get("seal_money")),
            "limit_up_max_seal_money": _as_float(item.get("max_seal_money")),
            "limit_up_time_minutes": _minutes_from_hhmm(item.get("limit_up_time")),
            "limit_up_change_pct": _as_float(item.get("price_change_ratio_pct")),
        }
    return rows


def _extract_limit_down_pool(observation: HithinkSentimentObservation) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for item in _items(observation.data):
        symbol = _symbol_of(item)
        if symbol is None:
            continue
        rows[symbol] = {
            "limit_down_flag": 1.0,
            "limit_down_change_pct": _as_float(item.get("price_change_ratio_pct")),
            "limit_down_turnover_pct": _as_float(item.get("turnover_ratio_pct")),
        }
    return rows


def _extract_limit_break_pool(observation: HithinkSentimentObservation) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for item in _items(observation.data):
        symbol = _symbol_of(item)
        if symbol is None:
            continue
        rows[symbol] = {
            "limit_break_flag": 1.0,
            "limit_break_open_times": _as_float(item.get("open_times")),
            "limit_break_change_pct": _as_float(item.get("price_change_ratio_pct")),
        }
    return rows


def _extract_limit_up_ladder(observation: HithinkSentimentObservation) -> dict[str, dict]:
    target = observation.trade_date.isoformat()
    candidates = [
        item
        for item in _items(observation.data)
        if _date_text(item.get("date")) <= target
    ]
    if not candidates:
        return {}
    latest = max(candidates, key=lambda item: _date_text(item.get("date")))
    rows: dict[str, dict] = {}
    for board_items in (latest.get("boards") or {}).values():
        for item in board_items or []:
            if not isinstance(item, dict):
                continue
            symbol = _symbol_of(item)
            if symbol is None:
                continue
            row = rows.setdefault(symbol, {})
            board_num = _as_float(item.get("board_num"))
            sign_level = _as_float(item.get("sign_level"))
            if board_num is not None:
                row["ladder_board_num"] = max(row.get("ladder_board_num") or board_num, board_num)
            if sign_level is not None:
                row["ladder_sign_level"] = max(
                    row.get("ladder_sign_level") or sign_level, sign_level
                )
    return rows


def _extract_hot_stock_list(observation: HithinkSentimentObservation) -> dict[str, dict]:
    return _hot_rank_rows(_items(observation.data), with_change=True, with_score=True)


def _extract_skyrocket_list(observation: HithinkSentimentObservation) -> dict[str, dict]:
    return _hot_rank_rows(_items(observation.data), with_change=True, with_score=True)


def _extract_hot_stock_list_history(observation: HithinkSentimentObservation) -> dict[str, dict]:
    return _hot_rank_rows(_items(observation.data), with_change=False, with_score=True)


def _extract_hot_stock_rank_trend(observation: HithinkSentimentObservation) -> dict[str, dict]:
    target = observation.trade_date.isoformat()
    ranked = [
        item
        for item in _items(observation.data)
        if _as_float(item.get("rank")) is not None
        and _date_text(item.get("date")) == target
    ]
    rows: dict[str, dict] = {}
    for item in ranked:
        symbol = _symbol_of(item)
        if symbol is not None:
            rows[symbol] = {"hot_rank": _as_float(item.get("rank"))}
    return rows


def _extract_dragon_tiger_list(observation: HithinkSentimentObservation) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for item in observation.data.get("stock_items") or []:
        if not isinstance(item, dict):
            continue
        symbol = _symbol_of(item)
        if symbol is None:
            continue
        row = rows.setdefault(symbol, {})
        row["dragon_tiger_flag"] = 1.0
        for source_key, column in (
            ("net_value", "dragon_tiger_net_value"),
            ("net_rate", "dragon_tiger_net_rate"),
            ("hot_rank", "dragon_tiger_hot_rank"),
            ("org_net_value", "dragon_tiger_org_net_value"),
            ("hot_money_net_value", "dragon_tiger_hot_money_net_value"),
            ("range_days", "dragon_tiger_range_days"),
        ):
            value = _as_float(item.get(source_key))
            if value is not None:
                row[column] = value
    return rows


def _extract_anomaly(observation: HithinkSentimentObservation) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for item in _items(observation.data):
        symbol = _symbol_of(item)
        if symbol is None:
            continue
        row = rows.setdefault(
            symbol,
            {"anomaly_flag": 1.0, "anomaly_count": 0.0, "anomaly_limit_up_flag": 0.0, "anomaly_limit_down_flag": 0.0},
        )
        row["anomaly_count"] = float(row["anomaly_count"]) + 1.0
        tag_name = str(item.get("tag_name") or "")
        if "涨停" in tag_name:
            row["anomaly_limit_up_flag"] = 1.0
        if "跌停" in tag_name:
            row["anomaly_limit_down_flag"] = 1.0
    return rows


def _extract_auction_snapshot(observation: HithinkSentimentObservation) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for item in _items(observation.data):
        symbol = _symbol_of(item)
        if symbol is None:
            continue
        auction_pct = _as_float(item.get("auction_pct"))
        volume_ratio = _as_float(item.get("auction_volume_ratio"))
        strength = None
        if auction_pct is not None and volume_ratio is not None:
            strength = auction_pct * volume_ratio
        rows[symbol] = {
            "auction_pct": auction_pct,
            "auction_price": _as_float(item.get("auction_price")),
            "auction_volume_ratio": volume_ratio,
            "auction_turnover_pct": _as_float(item.get("auction_turnover_pct")),
            "auction_yesterday_ratio_pct": _as_float(item.get("auction_yesterday_ratio_pct")),
            "auction_amount": _as_float(item.get("auction_amount")),
            "auction_strength": strength,
        }
    return rows


def _extract_auction_short_term_benchmark(
    observation: HithinkSentimentObservation,
) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for item in _items(observation.data):
        symbol = _symbol_of(item)
        if symbol is None:
            continue
        tags = {str(tag) for tag in (item.get("tags") or [])}
        rows[symbol] = {
            "auction_benchmark_pct": _as_float(item.get("auction_pct")),
            "auction_benchmark_high_open_flag": 1.0 if "高开" in tags else 0.0,
            "auction_benchmark_heavy_volume_flag": 1.0 if "放量" in tags else 0.0,
        }
    return rows


_EXTRACTORS = {
    "limit_up_pool": _extract_limit_up_pool,
    "limit_down_pool": _extract_limit_down_pool,
    "limit_break_pool": _extract_limit_break_pool,
    "limit_up_ladder": _extract_limit_up_ladder,
    "hot_stock_list": _extract_hot_stock_list,
    "skyrocket_list": _extract_skyrocket_list,
    "hot_stock_list_history": _extract_hot_stock_list_history,
    "hot_stock_rank_trend": _extract_hot_stock_rank_trend,
    "dragon_tiger_list": _extract_dragon_tiger_list,
    "anomaly_analysis_list": _extract_anomaly,
    "anomaly_analysis_stock": _extract_anomaly,
    "auction_snapshot": _extract_auction_snapshot,
    "auction_short_term_benchmark": _extract_auction_short_term_benchmark,
}


def _availability(observation: HithinkSentimentObservation, config: SentimentPITConfig) -> datetime:
    timezone = ZoneInfo(config.timezone_name)
    if observation.feature_name in AUCTION_FEATURE_NAMES:
        if observation.feature_name == "auction_snapshot" and observation.slot == "live":
            return _parse_datetime(observation.fetched_at).astimezone(timezone)
        return datetime.combine(
            observation.trade_date, config.availability_time(auction=True), tzinfo=timezone
        )
    return datetime.combine(
        observation.trade_date, config.availability_time(auction=False), tzinfo=timezone
    )


def _is_usable(observation: HithinkSentimentObservation) -> bool:
    if observation.feature_name == "auction_snapshot":
        status = str(observation.data.get("data_status") or "").strip().lower()
        if status in _BLOCKED_AUCTION_STATUS:
            return False
    return True


def observations_from_records(records: Iterable[Mapping[str, Any]]) -> list[HithinkSentimentObservation]:
    observations: list[HithinkSentimentObservation] = []
    for record in records:
        feature_name = str(record.get("feature_name") or "").strip()
        if feature_name not in SUPPORTED_FEATURE_NAMES:
            raise SentimentFeatureError(
                f"unsupported hithink feature name in store: {feature_name!r}"
            )
        trade_date_raw = str(record.get("trade_date") or "").strip()
        try:
            trade_date = date.fromisoformat(trade_date_raw[:10])
        except ValueError as exc:
            raise SentimentFeatureError(
                f"invalid trade_date in hithink feature record: {trade_date_raw!r}"
            ) from exc
        data = record.get("data")
        if not isinstance(data, Mapping):
            raise SentimentFeatureError("hithink feature record is missing a raw data object")
        for provenance_field in ("provider", "source_reference", "fetched_at"):
            if not str(record.get(provenance_field) or "").strip():
                raise SentimentFeatureError(
                    f"hithink feature record is missing provenance field: {provenance_field}"
                )
        observations.append(
            HithinkSentimentObservation(
                feature_name=feature_name,
                trade_date=trade_date,
                provider=str(record["provider"]),
                source_reference=str(record["source_reference"]),
                fetched_at=str(record["fetched_at"]),
                data=data,
                slot=str(record["slot"]) if record.get("slot") else None,
            )
        )
    return observations


def load_sentiment_observations(*, root=None) -> list[HithinkSentimentObservation]:
    return observations_from_records(load_hithink_feature_records(root=root))


def _normalized_universe(universe_by_date: Mapping[date, Iterable[str]]) -> dict[date, tuple[str, ...]]:
    return {
        feature_date: tuple(
            sorted(
                {
                    normalize_ticker_for_market(ticker, "CN")
                    for ticker in universe_by_date[feature_date]
                    if str(ticker or "").strip()
                }
            )
        )
        for feature_date in universe_by_date
    }


def build_sentiment_features(
    observations: Iterable[HithinkSentimentObservation],
    *,
    universe_by_date: Mapping[date, Iterable[str]],
    cutoff_by_date: Mapping[date, datetime],
    config: SentimentPITConfig | None = None,
) -> SentimentFeatureBuild:
    """Build PIT-strict ``(date, symbol)`` sentiment features.

    ``cutoff_by_date`` must cover every universe date with a timezone-aware
    decision cutoff; missing or unexpected dates fail closed rather than silently
    defaulting to a look-ahead-safe assumption.
    """
    resolved_config = config or SentimentPITConfig()
    dates = tuple(sorted(universe_by_date))
    if not dates:
        raise SentimentFeatureError("universe_by_date must not be empty")
    missing_cutoffs = sorted(set(dates) - set(cutoff_by_date))
    if missing_cutoffs:
        raise SentimentFeatureError(
            "cutoff_by_date is missing universe dates: "
            + ", ".join(item.isoformat() for item in missing_cutoffs)
        )
    unexpected_cutoffs = sorted(set(cutoff_by_date) - set(dates))
    if unexpected_cutoffs:
        raise SentimentFeatureError(
            "cutoff_by_date contains dates outside universe_by_date: "
            + ", ".join(item.isoformat() for item in unexpected_cutoffs)
        )
    for feature_date, cutoff in cutoff_by_date.items():
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise SentimentFeatureError(
                f"cutoff_by_date[{feature_date.isoformat()}] must be timezone-aware"
            )

    universe = _normalized_universe(universe_by_date)
    by_family: dict[str, list[HithinkSentimentObservation]] = {}
    for observation in observations:
        if observation.feature_name not in SUPPORTED_FEATURE_NAMES:
            raise SentimentFeatureError(
                f"unsupported hithink feature name: {observation.feature_name!r}"
            )
        by_family.setdefault(observation.feature_name, []).append(observation)

    features_by_key: dict[tuple[date, str], Mapping[str, float | None]] = {}
    selected_by_date: dict[date, dict[str, str]] = {}
    selected_tokens: list[tuple[str, str, str]] = []
    for feature_date in dates:
        cutoff = cutoff_by_date[feature_date]
        tickers = universe[feature_date]
        rows: dict[str, dict[str, float | None]] = {
            ticker: {name: None for name in FEATURE_NAMES} for ticker in tickers
        }
        selected: dict[str, str] = {}
        for family in _FAMILY_PRIORITY:
            candidates = [
                observation
                for observation in by_family.get(family, [])
                if observation.trade_date <= feature_date
                and _is_usable(observation)
                and _availability(observation, resolved_config) <= cutoff
            ]
            if not candidates:
                continue
            chosen = max(
                candidates,
                key=lambda observation: (
                    _availability(observation, resolved_config),
                    observation.trade_date,
                    observation.source_reference,
                ),
            )
            selected[family] = chosen.source_reference
            selected_tokens.append(
                (family, chosen.trade_date.isoformat(), chosen.source_reference)
            )
            extracted = _EXTRACTORS[family](chosen)
            columns = FAMILY_COLUMNS[family]
            if family in _EXHAUSTIVE_FAMILIES:
                for ticker in tickers:
                    values = extracted.get(ticker) or {}
                    for column in columns:
                        value = values.get(column)
                        rows[ticker][column] = value if value is not None else _COLUMN_DEFAULTS.get(column)
            else:
                for ticker in tickers:
                    values = extracted.get(ticker)
                    if not values:
                        continue
                    for column in columns:
                        value = values.get(column)
                        if value is not None:
                            rows[ticker][column] = value
        selected_by_date[feature_date] = selected
        for ticker in tickers:
            features_by_key[(feature_date, ticker)] = rows[ticker]

    source_version = hashlib.sha256(
        json.dumps(sorted(selected_tokens), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return SentimentFeatureBuild(
        schema_version="hithink_sentiment_features_v1",
        source_version=f"hithink_sentiment_features_v1:{source_version}",
        feature_names=FEATURE_NAMES,
        features_by_key=features_by_key,
        selected_observations_by_date=selected_by_date,
    )


def build_sentiment_features_from_store(
    *,
    root=None,
    universe_by_date: Mapping[date, Iterable[str]],
    cutoff_by_date: Mapping[date, datetime],
    config: SentimentPITConfig | None = None,
) -> SentimentFeatureBuild:
    return build_sentiment_features(
        load_sentiment_observations(root=root),
        universe_by_date=universe_by_date,
        cutoff_by_date=cutoff_by_date,
        config=config,
    )


# --------------------------------------------------------------------------- #
# Factor/feature factory registration metadata
#
# These definitions are the single source of truth for the ``sentiment_v1``
# factor set and the feature-availability manifest.  The family is read-only
# and forward-only: HiThink stores at most roughly one trailing year, so the
# registered coverage window is explicitly bounded instead of implying the
# depth of the CN price lake.
# --------------------------------------------------------------------------- #

SENTIMENT_FACTOR_SET_KEY = "sentiment_v1"
SENTIMENT_FEATURE_SOURCE = "hithink"
SENTIMENT_FEATURE_FORWARD_ONLY = True
SENTIMENT_FEATURE_COVERAGE_WINDOW = "trailing_one_year"
SENTIMENT_FEATURE_CATALOG_SCHEMA = "hithink_sentiment_feature_catalog_v1"
SENTIMENT_FEATURE_MATRIX_SCHEMA = "hithink_sentiment_feature_matrix_v1"

EOD_AVAILABILITY_SLOT = "eod"
AUCTION_AVAILABILITY_SLOT = "auction"

_EOD_AVAILABILITY_SEMANTICS = "same_trade_date_post_close_featured_data"
_AUCTION_AVAILABILITY_SEMANTICS = "same_trade_date_call_auction_close"
_MISSING_POLICY_NONE_FROM_SOURCE = "none_when_absent_from_source"
_MISSING_POLICY_NONE_EXHAUSTIVE = "none_when_absent_even_in_exhaustive_family"
_MISSING_POLICY_ZERO_EXHAUSTIVE = "zero_when_absent_from_exhaustive_family"


@dataclass(frozen=True, slots=True)
class SentimentFeatureDefinition:
    """Registration metadata for one point-in-time sentiment feature column."""

    name: str
    information_family: str
    availability_slot: str
    available_time_local: str
    availability_semantics: str
    missing_policy: str
    source: str = SENTIMENT_FEATURE_SOURCE
    forward_only: bool = SENTIMENT_FEATURE_FORWARD_ONLY
    coverage_window: str = SENTIMENT_FEATURE_COVERAGE_WINDOW

    def __post_init__(self) -> None:
        if not str(self.name or "").strip():
            raise ValueError("sentiment feature name must not be empty")
        if not str(self.information_family or "").strip():
            raise ValueError("information_family must not be empty")
        if self.availability_slot not in {EOD_AVAILABILITY_SLOT, AUCTION_AVAILABILITY_SLOT}:
            raise ValueError("availability_slot must be eod or auction")
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
        if not str(self.availability_semantics or "").strip():
            raise ValueError("availability_semantics must not be empty")
        if not str(self.source or "").strip():
            raise ValueError("source must not be empty")
        if self.forward_only is not True:
            raise ValueError("sentiment features are forward-only and must be registered as such")
        if not str(self.coverage_window or "").strip():
            raise ValueError("coverage_window must not be empty")
        if not str(self.missing_policy or "").strip():
            raise ValueError("missing_policy must not be empty")


def _sentiment_owning_families(column: str) -> tuple[str, ...]:
    return tuple(family for family in _FAMILY_PRIORITY if column in FAMILY_COLUMNS[family])


def _sentiment_missing_policy(column: str, families: tuple[str, ...]) -> str:
    if not any(family in _EXHAUSTIVE_FAMILIES for family in families):
        return _MISSING_POLICY_NONE_FROM_SOURCE
    if _COLUMN_DEFAULTS.get(column) is None:
        return _MISSING_POLICY_NONE_EXHAUSTIVE
    return _MISSING_POLICY_ZERO_EXHAUSTIVE


def sentiment_feature_definitions() -> dict[str, SentimentFeatureDefinition]:
    """Return the registered definition for every sentiment feature column."""

    config = SentimentPITConfig()
    definitions: dict[str, SentimentFeatureDefinition] = {}
    for column in FEATURE_NAMES:
        families = _sentiment_owning_families(column)
        family = families[0]
        auction = family in AUCTION_FEATURE_NAMES
        definitions[column] = SentimentFeatureDefinition(
            name=column,
            information_family=family,
            availability_slot=(
                AUCTION_AVAILABILITY_SLOT if auction else EOD_AVAILABILITY_SLOT
            ),
            available_time_local=config.availability_time(auction=auction).strftime("%H:%M"),
            availability_semantics=(
                _AUCTION_AVAILABILITY_SEMANTICS if auction else _EOD_AVAILABILITY_SEMANTICS
            ),
            missing_policy=_sentiment_missing_policy(column, families),
        )
    return definitions


def sentiment_feature_catalog_version() -> str:
    payload = {
        "schema_version": SENTIMENT_FEATURE_CATALOG_SCHEMA,
        "factor_set_key": SENTIMENT_FACTOR_SET_KEY,
        "source": SENTIMENT_FEATURE_SOURCE,
        "forward_only": SENTIMENT_FEATURE_FORWARD_ONLY,
        "coverage_window": SENTIMENT_FEATURE_COVERAGE_WINDOW,
        "definitions": {
            name: asdict(definition)
            for name, definition in sentiment_feature_definitions().items()
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    return f"{SENTIMENT_FEATURE_CATALOG_SCHEMA}:{digest}"


@dataclass(frozen=True, slots=True)
class SentimentFeatureMatrix:
    """Read-only ``(date, symbol)`` sentiment matrix. Missing values stay ``None``."""

    schema_version: str
    factor_set_key: str
    source_version: str
    feature_names: tuple[str, ...]
    features_by_key: Mapping[tuple[date, str], Mapping[str, float | None]]
    selected_observations_by_date: Mapping[date, Mapping[str, str]]

    def __post_init__(self) -> None:
        if self.factor_set_key != SENTIMENT_FACTOR_SET_KEY:
            raise ValueError(
                f"factor_set_key must be {SENTIMENT_FACTOR_SET_KEY!r}, got {self.factor_set_key!r}"
            )
        if self.feature_names != FEATURE_NAMES:
            raise ValueError("feature_names must match the registered sentiment feature catalog")

    def row(self, feature_date: date, ticker: str) -> Mapping[str, float | None]:
        return self.features_by_key[
            (feature_date, normalize_ticker_for_market(ticker, "CN"))
        ]


def build_sentiment_feature_matrix(
    *,
    universe_by_date: Mapping[date, Iterable[str]],
    cutoff_by_date: Mapping[date, datetime],
    observations: Iterable[HithinkSentimentObservation] | None = None,
    root=None,
    config: SentimentPITConfig | None = None,
) -> SentimentFeatureMatrix:
    """Build the registered sentiment feature matrix from universe and cutoffs.

    This is a thin read-only entry over :func:`build_sentiment_features` /
    :func:`build_sentiment_features_from_store`, so the PIT gate and the
    ``cutoff_by_date`` coverage check apply unchanged.  ``observations=None``
    reads the traceable HiThink store under ``root``.  Absent fields are kept as
    ``None`` exactly like the underlying builder; this entry never zero-fills.
    """

    if observations is None:
        build = build_sentiment_features_from_store(
            root=root,
            universe_by_date=universe_by_date,
            cutoff_by_date=cutoff_by_date,
            config=config,
        )
    else:
        build = build_sentiment_features(
            observations,
            universe_by_date=universe_by_date,
            cutoff_by_date=cutoff_by_date,
            config=config,
        )
    return SentimentFeatureMatrix(
        schema_version=SENTIMENT_FEATURE_MATRIX_SCHEMA,
        factor_set_key=SENTIMENT_FACTOR_SET_KEY,
        source_version=build.source_version,
        feature_names=build.feature_names,
        features_by_key=build.features_by_key,
        selected_observations_by_date=build.selected_observations_by_date,
    )
