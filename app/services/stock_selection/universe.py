from __future__ import annotations

import hashlib
import json
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from app.services.stock_selection.schemas import UniverseSnapshot


@dataclass(frozen=True, slots=True)
class UniverseRuleConfig:
    market: str
    min_price: float
    min_adv20: float
    min_avg_volume20: float
    min_history_sessions: int
    adv_lookback_sessions: int = 20
    allowed_security_types: tuple[str, ...] = ("equity", "common_stock", "common_equity")
    exclude_st: bool = True
    exclude_suspended: bool = True
    # CN execution reality: a signal-day limit-up close frequently opens as a
    # one-way board next session, so the assumed next-open entry is not
    # fillable.  Default flipped to True as P0 action #2 (2026-09-22); the
    # 20cm/30cm boards still enter above their band via the >=-band test.
    exclude_signal_day_limit_up: bool = True
    schema_version: str = "point_in_time_universe_rules_v1"

    def __post_init__(self) -> None:
        if str(self.market or "").upper() not in {"CN", "US"}:
            raise ValueError("market must be CN or US")
        if min(self.min_price, self.min_adv20, self.min_avg_volume20) < 0:
            raise ValueError("universe liquidity thresholds must not be negative")
        if self.min_history_sessions <= 0 or self.adv_lookback_sessions <= 0:
            raise ValueError("universe history windows must be positive")
        if not self.allowed_security_types:
            raise ValueError("allowed_security_types must not be empty")

    def content_hash(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def default_universe_rules(market: str) -> UniverseRuleConfig:
    market_code = str(market or "").upper()
    if market_code == "CN":
        # P0-2: a signal-day limit-up close cannot be bought at the next
        # open in practice, so such names are excluded from the tradable
        # universe by default instead of poisoning Top-N lists.
        return UniverseRuleConfig(
            market="CN",
            min_price=1.0,
            min_adv20=50_000_000.0,
            min_avg_volume20=0.0,
            min_history_sessions=120,
            exclude_signal_day_limit_up=True,
        )
    if market_code == "US":
        return UniverseRuleConfig(
            market="US",
            min_price=3.0,
            min_adv20=50_000_000.0,
            min_avg_volume20=0.0,
            min_history_sessions=120,
        )
    raise ValueError("market must be CN or US")


@dataclass(frozen=True, slots=True)
class SecurityMetadata:
    ticker: str
    security_type: str = "equity"
    listing_date: date | None = None
    delisting_date: date | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        if not str(self.ticker or "").strip():
            raise ValueError("ticker must not be empty")
        if self.listing_date and self.delisting_date and self.delisting_date < self.listing_date:
            raise ValueError("delisting_date cannot precede listing_date")


@dataclass(frozen=True, slots=True)
class DailySecurityState:
    suspended: bool = False
    is_st: bool = False
    active: bool = True
    limit_status: str | None = None
    corporate_action_status: str | None = None
    exclusion_reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UniverseBuildResult:
    market: str
    universe_version: str
    source_version: str
    rules_hash: str
    snapshots: tuple[UniverseSnapshot, ...]
    included_count: int
    excluded_count: int
    exclusion_counts: Mapping[str, int]

    def by_key(self) -> dict[tuple[str, date], UniverseSnapshot]:
        return {(item.ticker, item.trade_date): item for item in self.snapshots}

    def manifest(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "universe_version": self.universe_version,
            "source_version": self.source_version,
            "rules_hash": self.rules_hash,
            "snapshot_count": len(self.snapshots),
            "included_count": self.included_count,
            "excluded_count": self.excluded_count,
            "exclusion_counts": dict(self.exclusion_counts),
        }


def market_close_as_utc(trade_date: date, market: str) -> datetime:
    market_code = str(market or "").upper()
    if market_code == "CN":
        local = datetime.combine(trade_date, time(15, 0), tzinfo=ZoneInfo("Asia/Shanghai"))
    elif market_code == "US":
        local = datetime.combine(trade_date, time(16, 0), tzinfo=ZoneInfo("America/New_York"))
    else:
        raise ValueError("market must be CN or US")
    return local.astimezone(timezone.utc)


def _safe_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed


def _row_date(row: Mapping[str, Any]) -> date:
    value = row.get("date") or row.get("trade_date")
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError as exc:
        raise ValueError(f"invalid market row date `{value}`") from exc


def _universe_version(*, source_version: str, rules: UniverseRuleConfig) -> str:
    payload = f"{source_version}:{rules.content_hash()}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"pit_universe_v1:{rules.market.upper()}:{digest}"


def build_point_in_time_universe(
    rows: Iterable[Mapping[str, Any]],
    *,
    trading_dates: Sequence[date],
    metadata: Mapping[str, SecurityMetadata],
    daily_states: Mapping[tuple[str, date], DailySecurityState] | None = None,
    rules: UniverseRuleConfig,
    source_version: str,
    source_as_of_by_date: Mapping[date, datetime] | None = None,
) -> UniverseBuildResult:
    """Build a dated universe without borrowing future liquidity or membership.

    Every known security receives a snapshot on every requested market session.
    Missing bars and inactive membership are explicit exclusions, which makes
    delisted and not-yet-listed names visible to survivorship-bias audits.
    """

    dates = list(trading_dates)
    if dates != sorted(dates) or len(set(dates)) != len(dates):
        raise ValueError("trading_dates must be unique and ascending")
    if not dates:
        raise ValueError("trading_dates must not be empty")
    if not str(source_version or "").strip():
        raise ValueError("source_version must not be empty")
    if rules.market.upper() not in {"CN", "US"}:
        raise ValueError("rules market must be CN or US")

    normalized_metadata = {str(key).strip().upper(): value for key, value in metadata.items()}
    rows_by_ticker_date: dict[tuple[str, date], Mapping[str, Any]] = {}
    history_by_ticker: dict[str, list[tuple[date, float, float]]] = defaultdict(list)
    known_tickers = set(normalized_metadata)
    for row in rows:
        ticker = str(row.get("symbol") or row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        trade_date = _row_date(row)
        key = (ticker, trade_date)
        if key in rows_by_ticker_date:
            raise ValueError(f"duplicate market row for {ticker} on {trade_date.isoformat()}")
        rows_by_ticker_date[key] = row
        known_tickers.add(ticker)
        close = _safe_float(row.get("close")) or 0.0
        volume = _safe_float(row.get("volume")) or 0.0
        if close > 0 and volume >= 0:
            history_by_ticker[ticker].append((trade_date, close, volume))
    for history in history_by_ticker.values():
        history.sort(key=lambda item: item[0])

    market_code = rules.market.upper()
    states = daily_states or {}
    version = _universe_version(source_version=source_version, rules=rules)
    snapshots: list[UniverseSnapshot] = []
    exclusion_counter: Counter[str] = Counter()
    allowed_security_types = {item.lower() for item in rules.allowed_security_types}

    for ticker in sorted(known_tickers):
        meta = normalized_metadata.get(ticker) or SecurityMetadata(ticker=ticker)
        history = history_by_ticker.get(ticker) or []
        history_dates = [item[0] for item in history]
        for trade_date in dates:
            row = rows_by_ticker_date.get((ticker, trade_date))
            state = states.get((ticker, trade_date)) or DailySecurityState()
            reasons: list[str] = list(state.exclusion_reason_codes)
            if meta.listing_date and trade_date < meta.listing_date:
                reasons.append("not_yet_listed")
            if meta.delisting_date and trade_date >= meta.delisting_date:
                reasons.append("delisted")
            if meta.security_type.lower() not in allowed_security_types:
                reasons.append("unsupported_security_type")
            if not state.active:
                reasons.append("inactive")
            if rules.exclude_st and state.is_st:
                reasons.append("st_security")
            if rules.exclude_suspended and state.suspended:
                reasons.append("suspended")
            if state.corporate_action_status in {"unresolved", "discontinuity", "pending_delisting"}:
                reasons.append("corporate_action_unresolved")
            if rules.exclude_signal_day_limit_up and state.limit_status == "limit_up_locked":
                reasons.append("signal_day_limit_up_locked")
            if row is None:
                reasons.append("missing_daily_bar")

            close = _safe_float((row or {}).get("close"))
            volume = _safe_float((row or {}).get("volume"))
            history_sessions = bisect_right(history_dates, trade_date)
            lookback_start = max(0, history_sessions - rules.adv_lookback_sessions)
            lookback = history[lookback_start:history_sessions]
            adv20 = (
                sum(item_close * item_volume for _, item_close, item_volume in lookback) / len(lookback)
                if lookback
                else None
            )
            avg_volume20 = (
                sum(item_volume for _, _, item_volume in lookback) / len(lookback)
                if lookback
                else None
            )
            if row is not None:
                if close is None or close <= 0:
                    reasons.append("invalid_price")
                elif close < rules.min_price:
                    reasons.append("low_price")
                if volume is None or volume < 0:
                    reasons.append("invalid_volume")
                if history_sessions < rules.min_history_sessions:
                    reasons.append("insufficient_history")
                if adv20 is None or adv20 < rules.min_adv20:
                    reasons.append("low_adv20")
                if avg_volume20 is None or avg_volume20 < rules.min_avg_volume20:
                    reasons.append("low_avg_volume20")

            normalized_reasons = tuple(dict.fromkeys(reason for reason in reasons if reason))
            for reason in normalized_reasons:
                exclusion_counter[reason] += 1
            included = not normalized_reasons
            source_as_of = (source_as_of_by_date or {}).get(trade_date) or market_close_as_utc(trade_date, market_code)
            listing_anchor = meta.listing_date or (history[0][0] if history else None)
            listing_age = (
                bisect_right(dates, trade_date) - bisect_left(dates, listing_anchor)
                if listing_anchor and trade_date >= listing_anchor
                else None
            )
            snapshot_key = f"{version}:{trade_date.isoformat()}:{ticker}"
            snapshot_id = hashlib.sha256(snapshot_key.encode("utf-8")).hexdigest()[:24]
            snapshots.append(
                UniverseSnapshot(
                    snapshot_id=snapshot_id,
                    market=market_code,
                    trade_date=trade_date,
                    ticker=ticker,
                    included=included,
                    exclusion_reason_codes=normalized_reasons,
                    source_as_of=source_as_of,
                    universe_version=version,
                    security_type=meta.security_type,
                    price=close,
                    adv20=adv20,
                    volume=volume,
                    listing_age_sessions=listing_age,
                    suspended=state.suspended,
                    limit_status=state.limit_status,
                    corporate_action_status=state.corporate_action_status,
                    metadata={
                        "history_sessions": history_sessions,
                        "avg_volume20": avg_volume20,
                        "source_version": source_version,
                    },
                )
            )

    included_count = sum(1 for item in snapshots if item.included)
    return UniverseBuildResult(
        market=market_code,
        universe_version=version,
        source_version=source_version,
        rules_hash=rules.content_hash(),
        snapshots=tuple(sorted(snapshots, key=lambda item: (item.trade_date, item.ticker))),
        included_count=included_count,
        excluded_count=len(snapshots) - included_count,
        exclusion_counts=dict(sorted(exclusion_counter.items())),
    )
