from __future__ import annotations

import csv
import json
import math
import statistics
import warnings
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.services.market_lake import load_lake_rows
from app.services.model_signal_summary import enrich_model_output, summarize_model_output
from app.services.repository import (
    ConceptSnapshotRepository,
    FundamentalSnapshotRepository,
    ModelRunRepository,
    PredictionDetailRepository,
    PredictionExplanationRepository,
    PredictionWriteRepository,
    SymbolRepository,
    WorkspaceSnapshotRepository,
)

try:
    import lightgbm as lgb  # type: ignore
except ImportError:  # pragma: no cover - handled at runtime
    lgb = None


class SignalTrainer:
    """Train the production LightGBM multifactor signal model over the local market lake."""

    MODEL_CALIBRATION_SNAPSHOT_TYPE = "model_calibration_snapshot"

    def __init__(self) -> None:
        self.settings = get_settings()

    def _normalize_market_code(self, market: str | None) -> str | None:
        normalized = str(market or "").strip().upper()
        return normalized or None

    def _filter_rows_by_market(self, rows: list[dict], *, market: str | None) -> list[dict]:
        normalized_market = self._normalize_market_code(market)
        if normalized_market in {None, "", "ALL"}:
            return rows
        tickers = {
            str(row.get("symbol") or "").strip().upper()
            for row in rows
            if str(row.get("symbol") or "").strip()
        }
        if not tickers:
            return rows
        with SessionLocal() as db:
            symbol_overviews = SymbolRepository(db).list_overviews_for_tickers(sorted(tickers))
        market_by_ticker = {
            str(ticker or "").strip().upper(): self._normalize_market_code((overview or {}).get("market"))
            for ticker, overview in symbol_overviews.items()
        }
        filtered = [
            row
            for row in rows
            if market_by_ticker.get(str(row.get("symbol") or "").strip().upper()) == normalized_market
        ]
        return filtered or rows

    def _load_rows(self, *, tickers: set[str] | None = None, market: str | None = None) -> list[dict]:
        rows: list[dict] = []
        csv_paths = sorted(self.settings.normalized_data_dir.glob("*.csv"))
        selected_paths = csv_paths
        if tickers:
            matched_paths = [csv_path for csv_path in csv_paths if csv_path.stem.upper() in tickers]
            if matched_paths:
                selected_paths = matched_paths
        for csv_path in selected_paths:
            with csv_path.open("r", newline="", encoding="utf-8") as input_file:
                reader = csv.DictReader(input_file)
                if tickers:
                    rows.extend(
                        row for row in reader if str(row.get("symbol") or "").strip().upper() in tickers
                    )
                else:
                    rows.extend(reader)
        if not rows:
            rows = load_lake_rows(tickers=tickers)
        rows = self._filter_rows_by_market(rows, market=market)
        rows.sort(key=lambda row: (row.get("symbol") or "", row.get("date") or ""))
        return rows

    def _moving_average(self, values: list[float], window: int) -> float | None:
        if not values:
            return None
        sample = values[-window:] if len(values) >= window else values
        return sum(sample) / len(sample)

    def _clamp(self, value: float, lower: float, upper: float) -> float:
        return max(lower, min(upper, value))

    def _safe_float(self, value: object, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _stddev(self, values: list[float]) -> float:
        if len(values) < 2:
            return 0.0
        try:
            return float(statistics.pstdev(values))
        except statistics.StatisticsError:
            return 0.0

    def _future_return(self, future_price: float, anchor_price: float) -> float | None:
        if future_price <= 0 or anchor_price <= 0:
            return None
        return (future_price / anchor_price) - 1.0

    def _safe_price_series(self, symbol_rows: list[dict], *, field: str) -> list[float]:
        return [self._safe_float(row.get(field)) for row in symbol_rows]

    def _parse_iso_date(self, value: object) -> date | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.strptime(text, "%Y-%m-%d").date()
        except ValueError:
            return None

    def _listing_days(self, *, trade_date: str, listing_date: str | None) -> int | None:
        trade_day = self._parse_iso_date(trade_date)
        listing_day = self._parse_iso_date(listing_date)
        if trade_day is None or listing_day is None:
            return None
        return max(0, (trade_day - listing_day).days)

    def _board_tier(self, *, ticker: str, exchange: str | None) -> float:
        normalized_ticker = str(ticker or "").strip().upper()
        normalized_exchange = str(exchange or "").strip().upper()
        if normalized_ticker.startswith(("300", "301")):
            return 1.0
        if normalized_ticker.startswith(("688", "689")) or normalized_exchange in {"STAR", "SSE STAR"}:
            return 1.0
        if normalized_ticker.endswith(".BJ") or normalized_exchange in {"BSE", "BJ"}:
            return 1.25
        return 0.0

    def _resolve_cn_limit_band_pct(self, *, ticker: str, exchange: str | None, name: str | None) -> float | None:
        normalized_ticker = str(ticker or "").strip().upper()
        normalized_exchange = str(exchange or "").strip().upper()
        normalized_name = str(name or "").strip().upper().replace(" ", "")
        if not normalized_ticker:
            return None
        code = normalized_ticker.split(".", 1)[0]
        if normalized_name.startswith(("ST", "*ST", "S*ST", "PT")):
            return 5.0
        if normalized_ticker.endswith(".BJ") or normalized_exchange in {"BSE", "BJ"}:
            return 30.0
        if code.startswith(("688", "689")) or normalized_exchange in {"STAR", "SSE STAR"}:
            return 20.0
        if code.startswith(("300", "301")):
            return 20.0
        if normalized_ticker.endswith((".SS", ".SZ", ".SH")):
            return 10.0
        return None

    def _load_symbol_feature_context(
        self,
        *,
        rows: list[dict],
        market: str | None,
        normalized_tickers: set[str] | None,
    ) -> dict[str, dict]:
        tickers = normalized_tickers or {
            str(row.get("symbol") or "").strip().upper()
            for row in rows
            if str(row.get("symbol") or "").strip()
        }
        if not tickers:
            return {}
        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            symbol_overviews = symbol_repo.list_overviews_for_tickers(sorted(tickers))
            latest_fundamentals = {
                str(item.get("ticker") or "").strip().upper(): item
                for item in FundamentalSnapshotRepository(db).list_latest_for_market(market, tickers=sorted(tickers))
            }
            fundamental_history_rows = FundamentalSnapshotRepository(db).list_history_for_market(market, tickers=sorted(tickers))
            concept_history_rows = ConceptSnapshotRepository(db).list_history_for_market(market, tickers=sorted(tickers))
        fundamentals_by_ticker: dict[str, list[dict]] = defaultdict(list)
        for item in fundamental_history_rows:
            ticker = str(item.get("ticker") or "").strip().upper()
            if ticker:
                fundamentals_by_ticker[ticker].append(item)
        concepts_by_ticker_date: dict[str, dict[str, dict]] = defaultdict(dict)
        for item in concept_history_rows:
            ticker = str(item.get("ticker") or "").strip().upper()
            as_of_date = str(item.get("as_of_date") or "").strip()
            if not ticker or not as_of_date:
                continue
            bucket = concepts_by_ticker_date[ticker].setdefault(
                as_of_date,
                {"concept_count": 0.0, "max_strength": 0.0},
            )
            bucket["concept_count"] = float(bucket.get("concept_count") or 0.0) + 1.0
            strength = self._safe_float(item.get("strength"))
            if strength > float(bucket.get("max_strength") or 0.0):
                bucket["max_strength"] = strength
        context: dict[str, dict] = {}
        for ticker in sorted(tickers):
            symbol_meta = symbol_overviews.get(ticker) or {}
            fundamentals = latest_fundamentals.get(ticker) or {}
            concept_timeline = [
                {"as_of_date": as_of_date, **payload}
                for as_of_date, payload in sorted((concepts_by_ticker_date.get(ticker) or {}).items())
            ]
            context[ticker] = {
                "name": symbol_meta.get("name"),
                "exchange": symbol_meta.get("exchange"),
                "sector": symbol_meta.get("sector"),
                "industry": symbol_meta.get("industry"),
                "listing_date": fundamentals.get("listing_date"),
                "fundamental_history": fundamentals_by_ticker.get(ticker) or [],
                "concept_history": concept_timeline,
                "board_tier": self._board_tier(
                    ticker=ticker,
                    exchange=symbol_meta.get("exchange"),
                ),
                "limit_band_pct": self._resolve_cn_limit_band_pct(
                    ticker=ticker,
                    exchange=symbol_meta.get("exchange"),
                    name=symbol_meta.get("name"),
                ),
            }
        return context

    def _advance_fundamental_cursor(
        self,
        *,
        history: list[dict],
        cursor: int,
        trade_date: str,
    ) -> tuple[int, dict | None]:
        active: dict | None = None
        index = cursor
        while index < len(history):
            report_date = str(history[index].get("report_date") or "").strip()
            if report_date and report_date <= trade_date:
                active = history[index]
                index += 1
                continue
            break
        if active is None and index > 0:
            active = history[index - 1]
        return index, active

    def _advance_concept_cursor(
        self,
        *,
        history: list[dict],
        cursor: int,
        trade_date: str,
    ) -> tuple[int, dict | None]:
        active: dict | None = None
        index = cursor
        while index < len(history):
            as_of_date = str(history[index].get("as_of_date") or "").strip()
            if as_of_date and as_of_date <= trade_date:
                active = history[index]
                index += 1
                continue
            break
        if active is None and index > 0:
            active = history[index - 1]
        return index, active

    def _build_short_horizon_target_profile(
        self,
        *,
        symbol_rows: list[dict],
        index: int,
        anchor_close: float,
        limit_band_pct: float | None = None,
    ) -> tuple[float | None, dict[str, float]]:
        if anchor_close <= 0:
            return None, {}

        future_rows = symbol_rows[index + 1 :]
        if len(future_rows) < 3:
            return None, {}

        next_row = future_rows[0]
        next_open = self._safe_float(next_row.get("open"))
        next_high = self._safe_float(next_row.get("high"))
        next_low = self._safe_float(next_row.get("low"))
        next_close = self._safe_float(next_row.get("close"))

        future_3d = future_rows[:3]
        future_5d = future_rows[:5]
        future_3d_highs = [self._safe_float(row.get("high")) for row in future_3d]
        future_3d_lows = [self._safe_float(row.get("low")) for row in future_3d]
        future_5d_highs = [self._safe_float(row.get("high")) for row in future_5d]
        future_5d_lows = [self._safe_float(row.get("low")) for row in future_5d]

        next_1d_close_return = self._future_return(next_close, anchor_close)
        next_1d_open_gap = self._future_return(next_open, anchor_close)
        next_1d_open_to_high = self._future_return(next_high, next_open) if next_open > 0 else None
        next_1d_open_to_close = self._future_return(next_close, next_open) if next_open > 0 else None
        next_1d_low_drawdown = self._future_return(next_low, anchor_close)
        next_3d_max_return = self._future_return(max(future_3d_highs), anchor_close)
        next_3d_max_drawdown = self._future_return(min(future_3d_lows), anchor_close)
        next_5d_max_return = self._future_return(max(future_5d_highs), anchor_close) if len(future_5d) >= 5 else None
        next_5d_max_drawdown = self._future_return(min(future_5d_lows), anchor_close) if len(future_5d) >= 5 else None
        next_5d_close_return = (
            self._future_return(self._safe_float(future_5d[-1].get("close")), anchor_close) if len(future_5d) >= 5 else None
        )

        failed_after_gap_up = 0.0
        if (
            next_1d_open_gap is not None
            and next_1d_open_gap >= 0.025
            and next_1d_open_to_close is not None
            and next_1d_open_to_close <= -0.02
        ):
            failed_after_gap_up = 1.0

        tradable_next_day = 1.0
        cn_limit_threshold = ((limit_band_pct - 0.2) / 100.0) if limit_band_pct and limit_band_pct > 0 else None
        if next_1d_open_gap is not None and cn_limit_threshold is not None and next_1d_open_gap >= cn_limit_threshold:
            tradable_next_day = 0.0
        elif next_1d_open_gap is not None and next_1d_open_gap >= 0.095:
            tradable_next_day = 0.0

        upside = (
            max(next_1d_close_return or 0.0, -0.12) * 0.20
            + max(next_1d_open_to_high or 0.0, 0.0) * 0.30
            + max(next_3d_max_return or 0.0, 0.0) * 0.35
            + max(next_5d_max_return or 0.0, 0.0) * 0.15
        )
        downside = (
            abs(min(next_1d_low_drawdown or 0.0, 0.0)) * 0.18
            + abs(min(next_3d_max_drawdown or 0.0, 0.0)) * 0.32
            + abs(min(next_5d_max_drawdown or 0.0, 0.0)) * 0.18
        )
        penalty = failed_after_gap_up * 0.05 + (0.04 if tradable_next_day < 0.5 else 0.0)
        composite_target = self._clamp(upside - downside - penalty, -0.35, 0.45)

        target_profile = {
            "next_1d_close_return": round((next_1d_close_return or 0.0) * 100.0, 2),
            "next_1d_open_gap": round((next_1d_open_gap or 0.0) * 100.0, 2),
            "next_1d_open_to_high": round((next_1d_open_to_high or 0.0) * 100.0, 2),
            "next_1d_open_to_close": round((next_1d_open_to_close or 0.0) * 100.0, 2),
            "next_3d_max_return": round((next_3d_max_return or 0.0) * 100.0, 2),
            "next_3d_max_drawdown": round((next_3d_max_drawdown or 0.0) * 100.0, 2),
            "next_5d_max_return": round((next_5d_max_return or 0.0) * 100.0, 2),
            "next_5d_max_drawdown": round((next_5d_max_drawdown or 0.0) * 100.0, 2),
            "next_5d_close_return": round((next_5d_close_return or 0.0) * 100.0, 2),
            "failed_after_gap_up": failed_after_gap_up,
            "tradable_next_day": tradable_next_day,
            "next_day_limit_band_pct": round(limit_band_pct or 0.0, 2),
            "composite_target": round(composite_target * 100.0, 2),
        }
        return composite_target, target_profile

    def _baseline_explanations(
        self,
        *,
        symbol_id: int,
        trade_date: str,
        components: list[dict],
    ) -> list[dict]:
        rows: list[dict] = []
        for index, component in enumerate(components, start=1):
            contribution = component.get("contribution") or 0.0
            rows.append(
                {
                    "symbol_id": symbol_id,
                    "trade_date": trade_date,
                    "feature_name": component["feature_name"],
                    "feature_value": round((component.get("feature_value") or 0.0) * 100, 4),
                    "contribution": round(contribution * 100, 4),
                    "direction": "positive" if contribution >= 0 else "negative",
                    "display_order": index,
                }
            )
        return rows

    def _feature_names(self, *, lookback_days: int) -> list[str]:
        return [
            "recent_daily_return",
            "open_gap_pct",
            f"lookback_momentum_{lookback_days}d",
            "price_vs_ma20",
            "price_vs_ma10",
            "ma_alignment",
            "ma_stack",
            "ma20_slope_5d",
            "volume_ratio_20d",
            "volume_accel_3d",
            "dollar_volume_log",
            "dollar_volume_ratio_20d",
            "volatility_10d",
            "intraday_range_pct",
            "close_location_in_day_range",
            "close_location_in_20d_range",
            "swing_drawdown_5d",
            "breakout_gap_20d",
            "drawdown_from_20d_high",
            "market_cap_log",
            "roe_avg_3y_scaled",
            "net_profit_yoy_scaled",
            "revenue_yoy_scaled",
            "debt_to_assets_scaled",
            "concept_count_norm",
            "concept_strength_norm",
            "listing_days_log",
            "board_tier",
        ]

    def _feature_direction(self, feature_name: str) -> float:
        if feature_name in {
            "volatility_10d",
            "drawdown_from_20d_high",
            "swing_drawdown_5d",
            "debt_to_assets_scaled",
        }:
            return -1.0
        return 1.0

    def _build_lightgbm_samples(
        self,
        *,
        rows: list[dict],
        lookback_days: int,
        horizon_days: int,
        symbol_feature_context: dict[str, dict] | None = None,
    ) -> list[dict]:
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            trade_date = str(row.get("date") or "").strip()
            close = row.get("close")
            if not symbol or not trade_date or close in {None, ""}:
                continue
            grouped[symbol].append(row)

        samples: list[dict] = []
        for symbol, symbol_rows in grouped.items():
            context = (symbol_feature_context or {}).get(symbol) or {}
            fundamental_history = list(context.get("fundamental_history") or [])
            concept_history = list(context.get("concept_history") or [])
            fundamental_cursor = 0
            concept_cursor = 0
            symbol_rows.sort(key=lambda row: str(row.get("date") or ""))
            closes = [self._safe_float(row.get("close")) for row in symbol_rows]
            opens = [self._safe_float(row.get("open")) for row in symbol_rows]
            highs = [self._safe_float(row.get("high")) for row in symbol_rows]
            lows = [self._safe_float(row.get("low")) for row in symbol_rows]
            volumes = [self._safe_float(row.get("volume")) for row in symbol_rows]
            for index, row in enumerate(symbol_rows):
                if index < 1:
                    continue
                trade_date = str(row.get("date") or "").strip()
                fundamental_cursor, active_fundamental = self._advance_fundamental_cursor(
                    history=fundamental_history,
                    cursor=fundamental_cursor,
                    trade_date=trade_date,
                )
                concept_cursor, active_concept = self._advance_concept_cursor(
                    history=concept_history,
                    cursor=concept_cursor,
                    trade_date=trade_date,
                )
                close = closes[index]
                day_open = opens[index]
                day_high = highs[index]
                day_low = lows[index]
                previous_close = closes[index - 1]
                if close <= 0 or previous_close <= 0:
                    continue
                history_closes = closes[: index + 1]
                history_highs = highs[: index + 1]
                history_lows = lows[: index + 1]
                history_volumes = volumes[: index + 1]
                history_dollar_volumes = [
                    max(0.0, history_closes[pos] * history_volumes[pos]) for pos in range(len(history_closes))
                ]
                ma5 = self._moving_average(history_closes, 5)
                ma10 = self._moving_average(history_closes, 10)
                ma20 = self._moving_average(history_closes, 20)
                ma60 = self._moving_average(history_closes, 60)
                avg_volume_20 = self._moving_average(history_volumes, 20)
                avg_volume_3 = self._moving_average(history_volumes[:-1], 3) if len(history_volumes) > 1 else None
                avg_dollar_volume_20 = self._moving_average(history_dollar_volumes, 20)
                ma20_history = [
                    self._moving_average(history_closes[: pos + 1], 20)
                    for pos in range(len(history_closes))
                ]
                recent_returns = [
                    (history_closes[pos] / history_closes[pos - 1]) - 1.0
                    for pos in range(max(1, index - 9), index + 1)
                    if history_closes[pos - 1] > 0
                ]
                prior_window = history_closes[max(0, index - 20) : index]
                prior_high_20 = max(prior_window) if prior_window else previous_close
                recent_high_20 = max(history_highs[max(0, index - 19) : index + 1]) if history_highs else day_high
                recent_low_20 = min(history_lows[max(0, index - 19) : index + 1]) if history_lows else day_low
                recent_high_5 = max(history_highs[max(0, index - 4) : index + 1]) if history_highs else day_high
                lookback_anchor = history_closes[max(0, index - lookback_days)]
                lookback_momentum = ((close / lookback_anchor) - 1.0) if lookback_anchor > 0 else 0.0
                breakout_gap_20d = ((close / prior_high_20) - 1.0) if prior_high_20 > 0 else 0.0
                drawdown_from_20d_high = ((prior_high_20 / close) - 1.0) if prior_high_20 > 0 and close > 0 else 0.0
                volume_ratio_20d = (history_volumes[-1] / avg_volume_20) if avg_volume_20 and history_volumes[-1] > 0 else 1.0
                volume_accel_3d = (history_volumes[-1] / avg_volume_3) - 1.0 if avg_volume_3 and history_volumes[-1] > 0 else 0.0
                today_dollar_volume = max(0.0, close * history_volumes[-1])
                dollar_volume_ratio_20d = (
                    (today_dollar_volume / avg_dollar_volume_20) - 1.0
                    if avg_dollar_volume_20 and today_dollar_volume > 0
                    else 0.0
                )
                intraday_range_pct = ((day_high - day_low) / previous_close) if day_high > 0 and previous_close > 0 else 0.0
                close_location_in_day_range = (
                    ((close - day_low) / max(day_high - day_low, 1e-9)) - 0.5
                    if day_high > day_low
                    else 0.0
                )
                close_location_in_20d_range = (
                    ((close - recent_low_20) / max(recent_high_20 - recent_low_20, 1e-9)) - 0.5
                    if recent_high_20 > recent_low_20
                    else 0.0
                )
                ma20_reference = ma20_history[max(0, len(ma20_history) - 6)]
                ma20_slope_5d = ((ma20 / ma20_reference) - 1.0) if ma20 and ma20_reference else 0.0
                swing_drawdown_5d = ((close / recent_high_5) - 1.0) if recent_high_5 > 0 else 0.0
                listing_days = self._listing_days(
                    trade_date=trade_date,
                    listing_date=context.get("listing_date"),
                )
                listing_days_log = (
                    math.log1p(min(listing_days, 6000)) / 8.0
                    if listing_days is not None and listing_days > 0
                    else 0.0
                )
                market_cap = self._safe_float((active_fundamental or {}).get("market_cap"))
                roe_avg_3y = self._safe_float((active_fundamental or {}).get("roe_avg_3y"))
                net_profit_yoy = self._safe_float((active_fundamental or {}).get("net_profit_yoy"))
                revenue_yoy = self._safe_float((active_fundamental or {}).get("revenue_yoy"))
                debt_to_assets = self._safe_float((active_fundamental or {}).get("debt_to_assets"))
                concept_count = self._safe_float((active_concept or {}).get("concept_count"))
                concept_strength = self._safe_float((active_concept or {}).get("max_strength"))
                sample = {
                    "symbol": symbol,
                    "trade_date": trade_date,
                    "features": {
                        "recent_daily_return": (close / previous_close) - 1.0,
                        "open_gap_pct": ((day_open / previous_close) - 1.0) if day_open > 0 else 0.0,
                        f"lookback_momentum_{lookback_days}d": lookback_momentum,
                        "price_vs_ma20": ((close / ma20) - 1.0) if ma20 else 0.0,
                        "price_vs_ma10": ((close / ma10) - 1.0) if ma10 else 0.0,
                        "ma_alignment": ((ma5 / ma20) - 1.0) if ma5 and ma20 else 0.0,
                        "ma_stack": ((ma20 / ma60) - 1.0) if ma20 and ma60 else 0.0,
                        "ma20_slope_5d": ma20_slope_5d,
                        "volume_ratio_20d": volume_ratio_20d - 1.0,
                        "volume_accel_3d": volume_accel_3d,
                        "dollar_volume_log": math.log10(today_dollar_volume + 1.0) / 10.0,
                        "dollar_volume_ratio_20d": dollar_volume_ratio_20d,
                        "volatility_10d": self._stddev(recent_returns),
                        "intraday_range_pct": intraday_range_pct,
                        "close_location_in_day_range": close_location_in_day_range,
                        "close_location_in_20d_range": close_location_in_20d_range,
                        "swing_drawdown_5d": swing_drawdown_5d,
                        "breakout_gap_20d": breakout_gap_20d,
                        "drawdown_from_20d_high": drawdown_from_20d_high,
                        "market_cap_log": math.log10(market_cap + 1.0) / 12.0 if market_cap > 0 else 0.0,
                        "roe_avg_3y_scaled": self._clamp(roe_avg_3y / 30.0, -1.0, 1.5),
                        "net_profit_yoy_scaled": self._clamp(net_profit_yoy / 80.0, -1.5, 2.0),
                        "revenue_yoy_scaled": self._clamp(revenue_yoy / 60.0, -1.5, 2.0),
                        "debt_to_assets_scaled": self._clamp(debt_to_assets / 100.0, 0.0, 1.5),
                        "concept_count_norm": self._clamp(concept_count / 8.0, 0.0, 1.5),
                        "concept_strength_norm": self._clamp(concept_strength / 100.0, 0.0, 1.0),
                        "listing_days_log": listing_days_log,
                        "board_tier": self._safe_float(context.get("board_tier")),
                    },
                    "target": None,
                    "target_profile": {},
                }
                target_value, target_profile = self._build_short_horizon_target_profile(
                    symbol_rows=symbol_rows,
                    index=index,
                    anchor_close=close,
                    limit_band_pct=self._safe_float(context.get("limit_band_pct"), default=0.0) or None,
                )
                if target_value is not None:
                    sample["target"] = target_value
                    sample["target_profile"] = target_profile
                samples.append(sample)
        samples.sort(key=lambda item: (item["trade_date"], item["symbol"]))
        return samples

    def _training_stats(self, samples: list[dict], feature_names: list[str]) -> dict[str, tuple[float, float]]:
        stats: dict[str, tuple[float, float]] = {}
        for feature_name in feature_names:
            values = [self._safe_float(sample["features"].get(feature_name)) for sample in samples]
            if not values:
                stats[feature_name] = (0.0, 1.0)
                continue
            mean = sum(values) / len(values)
            std = self._stddev(values) or 1.0
            stats[feature_name] = (mean, std)
        return stats

    def _predict_scores(self, model: object, rows: list[list[float]]) -> list[float]:
        if not rows:
            return []
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="X does not have valid feature names, but LGBMRegressor was fitted with feature names",
                category=UserWarning,
            )
            return [float(value) for value in list(model.predict(rows))]

    def _build_lightgbm_explanations(
        self,
        *,
        symbol_id: int,
        trade_date: str,
        feature_values: dict[str, float],
        feature_names: list[str],
        feature_importance: dict[str, float],
        feature_stats: dict[str, tuple[float, float]],
    ) -> list[dict]:
        rows: list[dict] = []
        for feature_name in feature_names:
            value = self._safe_float(feature_values.get(feature_name))
            mean, std = feature_stats.get(feature_name, (0.0, 1.0))
            z_score = ((value - mean) / std) if std else 0.0
            contribution = z_score * feature_importance.get(feature_name, 0.0) * self._feature_direction(feature_name)
            rows.append(
                {
                    "symbol_id": symbol_id,
                    "trade_date": trade_date,
                    "feature_name": feature_name,
                    "feature_value": round(value * 100, 4),
                    "contribution": round(contribution * 100, 4),
                    "direction": "positive" if contribution >= 0 else "negative",
                    "display_order": 0,
                }
            )
        ranked = sorted(rows, key=lambda item: abs(float(item.get("contribution") or 0.0)), reverse=True)[:5]
        for index, row in enumerate(ranked, start=1):
            row["display_order"] = index
        return ranked

    def _summarize_target_profile(self, samples: list[dict]) -> dict[str, float | int]:
        metric_keys = [
            "next_1d_close_return",
            "next_1d_open_gap",
            "next_1d_open_to_high",
            "next_1d_open_to_close",
            "next_3d_max_return",
            "next_3d_max_drawdown",
            "next_5d_max_return",
            "next_5d_max_drawdown",
            "next_5d_close_return",
            "failed_after_gap_up",
            "tradable_next_day",
            "composite_target",
        ]
        summary: dict[str, float | int] = {"sample_count": len(samples)}
        for metric_key in metric_keys:
            values = [
                self._safe_float((sample.get("target_profile") or {}).get(metric_key))
                for sample in samples
                if (sample.get("target_profile") or {}).get(metric_key) is not None
            ]
            if not values:
                continue
            summary[f"{metric_key}_avg"] = round(sum(values) / len(values), 4)
        return summary

    def _summarize_symbol_feature_context(self, symbol_feature_context: dict[str, dict]) -> dict[str, float | int]:
        total = len(symbol_feature_context)
        listing_count = sum(1 for item in symbol_feature_context.values() if item.get("listing_date"))
        board_tier_count = sum(1 for item in symbol_feature_context.values() if self._safe_float(item.get("board_tier")) > 0)
        fundamental_history_count = sum(1 for item in symbol_feature_context.values() if item.get("fundamental_history"))
        concept_history_count = sum(1 for item in symbol_feature_context.values() if item.get("concept_history"))
        return {
            "symbol_count": total,
            "listing_date_count": listing_count,
            "listing_date_coverage_pct": round((listing_count / max(total, 1)) * 100.0, 1),
            "fundamental_history_count": fundamental_history_count,
            "fundamental_history_coverage_pct": round((fundamental_history_count / max(total, 1)) * 100.0, 1),
            "concept_history_count": concept_history_count,
            "concept_history_coverage_pct": round((concept_history_count / max(total, 1)) * 100.0, 1),
            "non_main_board_count": board_tier_count,
            "non_main_board_pct": round((board_tier_count / max(total, 1)) * 100.0, 1),
        }

    def _feature_enhancement_meta(self, symbol_feature_context: dict[str, dict]) -> dict[str, object]:
        summary = self._summarize_symbol_feature_context(symbol_feature_context)
        listing_cov = float(summary.get("listing_date_coverage_pct") or 0.0)
        fundamental_cov = float(summary.get("fundamental_history_coverage_pct") or 0.0)
        concept_cov = float(summary.get("concept_history_coverage_pct") or 0.0)
        if max(listing_cov, fundamental_cov, concept_cov) >= 40.0:
            mode = "enhanced"
            note = "Historical fundamental/concept inputs are materially present in this run."
        elif max(listing_cov, fundamental_cov, concept_cov) > 0.0:
            mode = "partial"
            note = "Enhanced inputs are partially available; treat this as a mixed price-plus-context run."
        else:
            mode = "price_action_only"
            note = "Historical fundamental/concept coverage is absent, so this run is effectively price/volume only."
        return {
            "mode": mode,
            "coverage": summary,
            "note": note,
        }

    def _build_score_calibration(
        self,
        *,
        model: object,
        train_window: list[dict],
        feature_names: list[str],
        bucket_count: int = 12,
    ) -> list[dict]:
        if not train_window:
            return []
        x_train = [
            [self._safe_float(sample["features"].get(feature_name)) for feature_name in feature_names]
            for sample in train_window
        ]
        predicted_scores = self._predict_scores(model, x_train)
        ranked_pairs = sorted(
            zip(predicted_scores, train_window, strict=False),
            key=lambda pair: float(pair[0]),
        )
        if not ranked_pairs:
            return []
        bucket_size = max(25, math.ceil(len(ranked_pairs) / max(bucket_count, 1)))
        buckets: list[dict] = []
        for start in range(0, len(ranked_pairs), bucket_size):
            chunk = ranked_pairs[start : start + bucket_size]
            if not chunk:
                continue
            chunk_scores = [float(pair[0]) for pair in chunk]
            chunk_samples = [pair[1] for pair in chunk]
            profile_summary = self._summarize_target_profile(chunk_samples)
            buckets.append(
                {
                    "score_low": round(min(chunk_scores), 6),
                    "score_high": round(max(chunk_scores), 6),
                    "score_mid": round(sum(chunk_scores) / len(chunk_scores), 6),
                    "sample_count": len(chunk_samples),
                    "metrics": profile_summary,
                }
            )
        return buckets

    def _lookup_calibrated_metrics(
        self,
        *,
        score: float,
        calibration_buckets: list[dict],
    ) -> dict[str, float | int] | None:
        if not calibration_buckets:
            return None
        for bucket in calibration_buckets:
            if float(bucket.get("score_low") or 0.0) <= score <= float(bucket.get("score_high") or 0.0):
                return dict(bucket.get("metrics") or {})
        nearest = min(
            calibration_buckets,
            key=lambda bucket: abs(score - float(bucket.get("score_mid") or 0.0)),
        )
        return dict(nearest.get("metrics") or {})

    def _load_oos_score_calibration(self, *, market: str | None) -> tuple[list[dict], dict]:
        market_code = self._normalize_market_code(market) or "ALL"
        try:
            with SessionLocal() as db:
                snapshot = WorkspaceSnapshotRepository(db).get_latest_snapshot(self.MODEL_CALIBRATION_SNAPSHOT_TYPE)
        except Exception:
            return [], {"source": "unavailable", "market": market_code}
        payload = (snapshot or {}).get("payload") if isinstance(snapshot, dict) else None
        if not isinstance(payload, dict):
            return [], {"source": "missing", "market": market_code}
        payload_markets = {str(item or "").upper() for item in (payload.get("markets") or []) if str(item or "").strip()}
        if market_code not in {"", "ALL"} and payload_markets and market_code not in payload_markets:
            return [], {"source": "market_mismatch", "market": market_code, "payload_markets": sorted(payload_markets)}
        buckets = payload.get("score_calibration_buckets")
        if not isinstance(buckets, list) or not buckets:
            return [], {"source": "empty", "market": market_code, "snapshot_id": (snapshot or {}).get("id")}
        usable_buckets = [
            bucket
            for bucket in buckets
            if isinstance(bucket, dict) and isinstance(bucket.get("metrics"), dict) and int(bucket.get("sample_count") or 0) >= 5
        ]
        if not usable_buckets:
            return [], {"source": "thin", "market": market_code, "snapshot_id": (snapshot or {}).get("id")}
        return usable_buckets, {
            "source": "model_calibration_snapshot",
            "market": market_code,
            "snapshot_id": (snapshot or {}).get("id"),
            "snapshot_date": (snapshot or {}).get("snapshot_date"),
            "created_at": (snapshot or {}).get("created_at"),
            "sample_count": payload.get("sample_count"),
            "latest_trade_date": payload.get("latest_trade_date"),
        }

    def _build_detail_row(
        self,
        *,
        symbol_id: int,
        trade_date: str,
        score: float,
        rank_value: float,
        universe_size: int,
        horizon_days: int,
        run_name: str,
        calibrated_metrics: dict[str, float | int] | None = None,
    ) -> dict:
        metrics = calibrated_metrics or {}
        expected_return_5d = metrics.get("next_5d_close_return_avg")
        expected_return_20d = metrics.get("next_5d_max_return_avg")
        expected_drawdown_20d = None
        if metrics.get("next_5d_max_drawdown_avg") is not None:
            expected_drawdown_20d = abs(float(metrics.get("next_5d_max_drawdown_avg") or 0.0))
        elif metrics.get("next_3d_max_drawdown_avg") is not None:
            expected_drawdown_20d = abs(float(metrics.get("next_3d_max_drawdown_avg") or 0.0))
        reward_risk_ratio = None
        if expected_return_20d not in (None, 0) and expected_drawdown_20d not in (None, 0):
            reward_risk_ratio = round(abs(float(expected_return_20d)) / float(expected_drawdown_20d), 2)
        risk_score = None
        if expected_drawdown_20d is not None:
            risk_score = round(self._clamp(expected_drawdown_20d * 4.3, 8.0, 92.0), 1)
        target_horizon = 5
        if metrics.get("next_3d_max_return_avg") is not None and metrics.get("next_5d_close_return_avg") is not None:
            next_3d = float(metrics.get("next_3d_max_return_avg") or 0.0)
            next_5d = float(metrics.get("next_5d_close_return_avg") or 0.0)
            if next_3d >= next_5d + 1.5:
                target_horizon = 3
        enriched = enrich_model_output(
            {
                "score": score,
                "rank_value": rank_value,
                "universe_size": universe_size,
                "percentile": round(
                    max(0.0, min(100.0, (1 - ((float(rank_value) - 1) / max(universe_size, 1))) * 100.0)),
                    1,
                ),
                "target_horizon_days": target_horizon or max(5, min(20, horizon_days)),
                "expected_return_5d": expected_return_5d,
                "expected_return_20d": expected_return_20d,
                "expected_drawdown_20d": expected_drawdown_20d,
                "model_reward_risk_ratio": reward_risk_ratio,
                "risk_score": risk_score,
                "model_run": {"name": run_name},
            },
            lang="en",
        ) or {}
        return {
            "symbol_id": symbol_id,
            "trade_date": trade_date,
            "confidence": enriched.get("confidence"),
            "bullish_prob": enriched.get("bullish_prob"),
            "bearish_prob": enriched.get("bearish_prob"),
            "expected_return_5d": enriched.get("expected_return_5d"),
            "expected_return_20d": enriched.get("expected_return_20d"),
            "expected_drawdown_20d": enriched.get("expected_drawdown_20d"),
            "model_reward_risk_ratio": enriched.get("model_reward_risk_ratio"),
            "risk_score": enriched.get("risk_score"),
            "target_horizon_days": enriched.get("target_horizon_days"),
            "universe_size": enriched.get("universe_size"),
            "percentile": enriched.get("percentile"),
            "regime_label": enriched.get("regime_label"),
            "conviction_bucket": enriched.get("conviction_bucket"),
            "position_size_hint": enriched.get("position_size_hint"),
            "entry_style": enriched.get("entry_style"),
            "signal_label": enriched.get("signal_label"),
            "signal_strength": enriched.get("signal_strength"),
            "summary_text": enriched.get("summary_text") or summarize_model_output(enriched, lang="en"),
        }

    def _train_baseline(
        self,
        *,
        run_name: str,
        signal_type: str,
        lookback_days: int,
        normalized_tickers: set[str] | None,
        market: str | None,
        universe: str | None,
        rows: list[dict],
    ) -> int:
        signal_rows: list[dict] = []
        detail_rows: list[dict] = []
        explanation_rows: list[dict] = []
        by_date: dict[str, list[dict]] = defaultdict(list)
        close_history_by_symbol: dict[str, list[float]] = defaultdict(list)
        volume_history_by_symbol: dict[str, list[float]] = defaultdict(list)
        explanation_row_limit = 2000
        if str(market or "").upper() == "US" and len(normalized_tickers or []) >= 5000:
            explanation_row_limit = 0

        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            model_repo = ModelRunRepository(db)
            prediction_repo = PredictionWriteRepository(db)
            detail_repo = PredictionDetailRepository(db)
            explanation_repo = PredictionExplanationRepository(db)
            model_repo.complete_stale_running_runs(
                stale_after_hours=6,
                message_prefix="Trainer cleanup closed a stale running model run.",
            )

            dates = sorted({row["date"] for row in rows if row.get("date")})
            latest_prediction_date = dates[-1] if dates else None
            run = model_repo.create_run(
                name=run_name,
                model_type="local_baseline",
                market=market or "US",
                universe=universe or ("local_watchlist" if normalized_tickers else "full_dataset"),
                train_start=dates[0] if dates else None,
                train_end=dates[-1] if dates else None,
                test_start=dates[0] if dates else None,
                test_end=dates[-1] if dates else None,
                config={
                    "model_type": "baseline",
                    "signal_type": signal_type,
                    "lookback_days": lookback_days,
                    "ticker_count": len(normalized_tickers or []),
                    "tickers": sorted(normalized_tickers) if normalized_tickers else None,
                },
                artifact_path=None,
                status="running",
            )

            for row in rows:
                symbol = row.get("symbol")
                date = row.get("date")
                close_value = row.get("close")
                if not symbol or not date or not close_value:
                    continue

                symbol_record = symbol_repo.get_by_ticker(symbol)
                if symbol_record is None:
                    continue

                closes = close_history_by_symbol[symbol]
                volumes = volume_history_by_symbol[symbol]
                close = float(close_value)
                volume = float(row.get("volume") or 0.0)
                score = None
                components: list[dict] = []
                if closes:
                    daily_return = (close / closes[-1]) - 1.0
                    trailing = closes[-lookback_days:]
                    if trailing:
                        trailing_returns = []
                        previous = None
                        for trailing_close in trailing:
                            if previous is not None:
                                trailing_returns.append((trailing_close / previous) - 1.0)
                            previous = trailing_close
                        trailing_returns.append(daily_return)
                        momentum_component = sum(trailing_returns) / len(trailing_returns)

                        projected_closes = closes + [close]
                        ma5 = self._moving_average(projected_closes, 5)
                        ma20 = self._moving_average(projected_closes, 20)
                        ma60 = self._moving_average(projected_closes, 60)
                        price_vs_ma20 = ((close / ma20) - 1.0) if ma20 else 0.0
                        ma_alignment = ((ma5 / ma20) - 1.0) if ma5 and ma20 else 0.0
                        ma_stack = ((ma20 / ma60) - 1.0) if ma20 and ma60 else 0.0
                        projected_volumes = volumes + ([volume] if volume else [])
                        avg_volume_20 = self._moving_average(projected_volumes, 20)
                        volume_ratio = (volume / avg_volume_20) if avg_volume_20 and volume else 1.0

                        structure_component = self._clamp(price_vs_ma20, -0.08, 0.08) * 0.35
                        alignment_component = self._clamp(ma_alignment + ma_stack, -0.08, 0.08) * 0.25
                        volume_component = self._clamp(volume_ratio - 1.0, -0.75, 1.25) * 0.03

                        score = momentum_component + structure_component + alignment_component + volume_component
                        if signal_type == "reversal":
                            score = -score

                        polarity = -1.0 if signal_type == "reversal" else 1.0
                        components = [
                            {
                                "feature_name": "recent_daily_return",
                                "feature_value": daily_return,
                                "contribution": (daily_return / len(trailing_returns)) * polarity,
                            },
                            {
                                "feature_name": f"lookback_momentum_{lookback_days}d",
                                "feature_value": momentum_component,
                                "contribution": momentum_component * polarity,
                            },
                            {
                                "feature_name": "price_vs_ma20",
                                "feature_value": price_vs_ma20,
                                "contribution": structure_component * polarity,
                            },
                            {
                                "feature_name": "ma_alignment",
                                "feature_value": ma_alignment + ma_stack,
                                "contribution": alignment_component * polarity,
                            },
                            {
                                "feature_name": "volume_ratio_20d",
                                "feature_value": volume_ratio - 1.0,
                                "contribution": volume_component * polarity,
                            },
                        ]

                closes.append(close)
                if volume:
                    volumes.append(volume)

                if score is None:
                    continue

                record = {
                    "symbol_id": symbol_record.id,
                    "trade_date": date,
                    "score": score,
                    "rank_value": None,
                    "_explanations": self._baseline_explanations(
                        symbol_id=symbol_record.id,
                        trade_date=date,
                        components=components,
                    ),
                }
                by_date[date].append(record)

            for date, date_rows in by_date.items():
                ranked = sorted(date_rows, key=lambda item: item["score"], reverse=True)
                for idx, record in enumerate(ranked, start=1):
                    record["rank_value"] = float(idx)
                    if latest_prediction_date and record["trade_date"] == latest_prediction_date:
                        detail_rows.append(
                            self._build_detail_row(
                                symbol_id=record["symbol_id"],
                                trade_date=record["trade_date"],
                                score=float(record["score"]),
                                rank_value=float(record["rank_value"]),
                                universe_size=len(ranked),
                                horizon_days=lookback_days * 5,
                                run_name=run_name,
                            )
                        )
                    if (
                        explanation_row_limit != 0
                        and latest_prediction_date
                        and record["trade_date"] == latest_prediction_date
                        and idx <= explanation_row_limit
                    ):
                        explanation_rows.extend(record.pop("_explanations", []))
                    else:
                        record.pop("_explanations", None)
                    signal_rows.append(record)

            if not signal_rows:
                model_repo.complete_run(run.id, status="failed", artifact_path=None)
                raise RuntimeError(
                    "The baseline trainer produced no predictions. You likely need at least 2-3 trading days per symbol."
                )

            count = prediction_repo.replace_for_model_run(run.id, signal_rows)
            detail_repo.replace_for_model_run(run.id, detail_rows)
            explanation_repo.replace_for_model_run(run.id, explanation_rows)
            artifact_path = str((self.settings.artifacts_dir / f"model_run_{run.id}.json").resolve())
            Path(artifact_path).write_text(
                json.dumps(
                    {
                        "model": run_name,
                        "model_type": "baseline",
                        "signal_type": signal_type,
                        "lookback_days": lookback_days,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            model_repo.complete_run(run.id, status="success", artifact_path=artifact_path)
            return count

    def _train_lightgbm(
        self,
        *,
        run_name: str,
        signal_type: str,
        lookback_days: int,
        normalized_tickers: set[str] | None,
        market: str | None,
        universe: str | None,
        rows: list[dict],
    ) -> int:
        if lgb is None:
            raise RuntimeError("LightGBM is not installed. Run `.venv/bin/pip install -r requirements.txt` first.")
        if signal_type != "momentum":
            raise RuntimeError("The LightGBM trainer currently supports `momentum` signal_type only.")

        horizon_days = max(5, min(10, lookback_days * 2))
        feature_names = self._feature_names(lookback_days=lookback_days)
        symbol_feature_context = self._load_symbol_feature_context(
            rows=rows,
            market=market,
            normalized_tickers=normalized_tickers,
        )
        samples = self._build_lightgbm_samples(
            rows=rows,
            lookback_days=lookback_days,
            horizon_days=horizon_days,
            symbol_feature_context=symbol_feature_context,
        )
        if not samples:
            raise RuntimeError("LightGBM trainer found no usable feature rows. The market lake may still be too short.")

        samples_by_date: dict[str, list[dict]] = defaultdict(list)
        labeled_by_date: dict[str, list[dict]] = defaultdict(list)
        all_dates: list[str] = []
        seen_dates: set[str] = set()
        for sample in samples:
            trade_date = sample["trade_date"]
            samples_by_date[trade_date].append(sample)
            if sample.get("target") is not None:
                labeled_by_date[trade_date].append(sample)
            if trade_date not in seen_dates:
                seen_dates.add(trade_date)
                all_dates.append(trade_date)
        all_dates.sort()
        warmup_dates = max(20, lookback_days * 8)
        if len(all_dates) <= warmup_dates + 5:
            raise RuntimeError("LightGBM trainer needs a longer price history before it can score recent trade dates.")
        prediction_start_index = max(warmup_dates, len(all_dates) - 60)
        prediction_dates = all_dates[prediction_start_index:]
        if not prediction_dates:
            raise RuntimeError("LightGBM trainer found no prediction dates.")

        first_prediction_date = prediction_dates[0]
        train_pool = [
            sample
            for sample in samples
            if sample.get("target") is not None and sample["trade_date"] < first_prediction_date
        ]
        if len(train_pool) < 1000:
            raise RuntimeError("LightGBM trainer needs more labeled history before the first prediction date.")

        enhancement_meta = self._feature_enhancement_meta(symbol_feature_context)
        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            model_repo = ModelRunRepository(db)
            prediction_repo = PredictionWriteRepository(db)
            detail_repo = PredictionDetailRepository(db)
            explanation_repo = PredictionExplanationRepository(db)
            model_repo.complete_stale_running_runs(
                stale_after_hours=6,
                message_prefix="Trainer cleanup closed a stale running model run.",
            )
            symbol_map = {symbol.ticker.upper(): symbol.id for symbol in symbol_repo.list_symbols()}
            run = model_repo.create_run(
                name=run_name,
                model_type="lightgbm_multifactor",
                market=market or "US",
                universe=universe or ("local_watchlist" if normalized_tickers else "full_dataset"),
                train_start=all_dates[0] if all_dates else None,
                train_end=prediction_dates[-1] if prediction_dates else None,
                test_start=prediction_dates[0] if prediction_dates else None,
                test_end=prediction_dates[-1] if prediction_dates else None,
                config={
                    "model_type": "lightgbm",
                    "signal_type": signal_type,
                    "lookback_days": lookback_days,
                    "prediction_horizon_days": horizon_days,
                    "target_profile": "short_horizon_composite_v1",
                    "target_metric_keys": [
                        "next_1d_close_return",
                        "next_1d_open_gap",
                        "next_1d_open_to_high",
                        "next_3d_max_return",
                        "next_3d_max_drawdown",
                        "next_5d_max_return",
                        "next_5d_close_return",
                        "failed_after_gap_up",
                        "tradable_next_day",
                    ],
                    "feature_families": [
                        "price_trend",
                        "price_extension",
                        "volume_intensity",
                        "intraday_structure",
                        "liquidity_proxy",
                        "listing_maturity",
                        "board_tier",
                    ],
                    "feature_enhancement_mode": enhancement_meta.get("mode"),
                    "feature_enhancement_note": enhancement_meta.get("note"),
                    "symbol_context_summary": enhancement_meta.get("coverage"),
                    "ticker_count": len(normalized_tickers or []),
                    "prediction_dates": len(prediction_dates),
                },
                artifact_path=None,
                status="running",
            )

            signal_rows: list[dict] = []
            detail_rows: list[dict] = []
            explanation_rows: list[dict] = []
            retrain_interval = 5
            max_training_rows = 120000 if str(market or "").upper() == "US" else 80000
            normalized_market = str(market or "").upper()
            full_market_run = (
                len(normalized_tickers or []) >= 5000
                or str(universe or "").lower() in {"full_market_cn_lake", "full_market_us_lake", "full_dataset"}
            )
            explanation_rank_limit = 2000
            if full_market_run and normalized_market in {"CN", "US"}:
                explanation_rank_limit = 0
            model = None
            feature_importance: dict[str, float] = {}
            feature_stats: dict[str, tuple[float, float]] = {}
            calibration_buckets: list[dict] = []
            latest_prediction_date = prediction_dates[-1]
            oos_calibration_buckets, oos_calibration_meta = self._load_oos_score_calibration(market=normalized_market)

            for index, trade_date in enumerate(prediction_dates):
                if not train_pool:
                    train_pool.extend(labeled_by_date.get(trade_date, []))
                    continue
                if model is None or index % retrain_interval == 0:
                    train_window = train_pool[-max_training_rows:]
                    x_train = [
                        [self._safe_float(sample["features"].get(feature_name)) for feature_name in feature_names]
                        for sample in train_window
                    ]
                    y_train = [self._safe_float(sample.get("target")) for sample in train_window]
                    sample_weights = [
                        0.65 + (position / max(len(train_window) - 1, 1)) * 0.7
                        for position in range(len(train_window))
                    ]
                    model = lgb.LGBMRegressor(
                        objective="regression",
                        n_estimators=260,
                        learning_rate=0.05,
                        num_leaves=63,
                        min_child_samples=40,
                        subsample=0.8,
                        colsample_bytree=0.8,
                        reg_alpha=0.05,
                        reg_lambda=0.1,
                        random_state=42,
                        n_jobs=-1,
                    )
                    model.fit(x_train, y_train, sample_weight=sample_weights)
                    raw_importance_obj = getattr(model, "feature_importances_", None)
                    if raw_importance_obj is None:
                        raw_importance = [0.0] * len(feature_names)
                    else:
                        raw_importance = [float(value) for value in list(raw_importance_obj)]
                    if len(raw_importance) < len(feature_names):
                        raw_importance.extend([0.0] * (len(feature_names) - len(raw_importance)))
                    raw_importance = raw_importance[: len(feature_names)]
                    importance_total = sum(raw_importance) or 1.0
                    feature_importance = {
                        feature_name: raw_importance[pos] / importance_total
                        for pos, feature_name in enumerate(feature_names)
                    }
                    feature_stats = self._training_stats(train_window, feature_names)
                    calibration_buckets = self._build_score_calibration(
                        model=model,
                        train_window=train_window,
                        feature_names=feature_names,
                    )

                date_samples = samples_by_date.get(trade_date) or []
                if not date_samples:
                    train_pool.extend(labeled_by_date.get(trade_date, []))
                    continue
                x_date = [
                    [self._safe_float(sample["features"].get(feature_name)) for feature_name in feature_names]
                    for sample in date_samples
                ]
                predicted_scores = self._predict_scores(model, x_date) if model is not None else []
                ranked_pairs = sorted(
                    zip(date_samples, predicted_scores, strict=False),
                    key=lambda pair: float(pair[1]),
                    reverse=True,
                )
                for rank_index, (sample, raw_score) in enumerate(ranked_pairs, start=1):
                    symbol = sample["symbol"]
                    symbol_id = symbol_map.get(symbol)
                    if symbol_id is None:
                        continue
                    score = self._clamp(float(raw_score), -0.35, 0.35)
                    signal_rows.append(
                        {
                            "symbol_id": symbol_id,
                            "trade_date": trade_date,
                            "score": score,
                            "rank_value": float(rank_index),
                        }
                    )
                    if trade_date == latest_prediction_date:
                        calibrated_metrics = (
                            self._lookup_calibrated_metrics(
                                score=score,
                                calibration_buckets=oos_calibration_buckets,
                            )
                            if oos_calibration_buckets
                            else None
                        )
                        detail_rows.append(
                            self._build_detail_row(
                                symbol_id=symbol_id,
                                trade_date=trade_date,
                                score=score,
                                rank_value=float(rank_index),
                                universe_size=len(ranked_pairs),
                                horizon_days=horizon_days,
                                run_name=run_name,
                                calibrated_metrics=calibrated_metrics,
                            )
                        )
                        if explanation_rank_limit and rank_index <= explanation_rank_limit:
                            explanation_rows.extend(
                                self._build_lightgbm_explanations(
                                    symbol_id=symbol_id,
                                    trade_date=trade_date,
                                    feature_values=sample["features"],
                                    feature_names=feature_names,
                                    feature_importance=feature_importance,
                                    feature_stats=feature_stats,
                                )
                            )
                train_pool.extend(labeled_by_date.get(trade_date, []))

            if not signal_rows:
                model_repo.complete_run(run.id, status="failed", artifact_path=None)
                raise RuntimeError("LightGBM trainer produced no predictions.")

            count = prediction_repo.replace_for_model_run(run.id, signal_rows)
            detail_repo.replace_for_model_run(run.id, detail_rows)
            explanation_repo.replace_for_model_run(run.id, explanation_rows)
            artifact_path = str((self.settings.artifacts_dir / f"model_run_{run.id}.json").resolve())
            Path(artifact_path).write_text(
                json.dumps(
                    {
                        "model": run_name,
                        "model_type": "lightgbm",
                        "signal_type": signal_type,
                        "lookback_days": lookback_days,
                        "prediction_horizon_days": horizon_days,
                        "target_profile": "short_horizon_composite_v1",
                        "train_window_target_profile": self._summarize_target_profile(train_pool[-max_training_rows:]),
                        "calibration_buckets": oos_calibration_buckets,
                        "train_window_calibration_buckets": calibration_buckets,
                        "calibration_source": "model_calibration_snapshot" if oos_calibration_buckets else "disabled_without_oos",
                        "oos_calibration_meta": oos_calibration_meta,
                        "oos_calibration_bucket_count": len(oos_calibration_buckets),
                        "feature_names": feature_names,
                        "feature_families": [
                            "price_trend",
                            "price_extension",
                            "volume_intensity",
                            "intraday_structure",
                            "liquidity_proxy",
                            "listing_maturity",
                            "board_tier",
                        ],
                        "feature_enhancement_mode": enhancement_meta.get("mode"),
                        "feature_enhancement_note": enhancement_meta.get("note"),
                        "symbol_context_summary": enhancement_meta.get("coverage"),
                        "prediction_dates": prediction_dates,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            model_repo.complete_run(run.id, status="success", artifact_path=artifact_path)
            return count

    def train(
        self,
        run_name: str = "lightgbm_momentum",
        signal_type: str = "momentum",
        lookback_days: int = 3,
        tickers: list[str] | None = None,
        market: str | None = None,
        universe: str | None = None,
        model_type: str = "lightgbm",
    ) -> int:
        normalized_tickers = {
            str(ticker).strip().upper() for ticker in (tickers or []) if str(ticker).strip()
        } or None
        rows = self._load_rows(tickers=normalized_tickers, market=market)
        if not rows:
            raise RuntimeError("No local market data found. Refresh the Parquet market lake or rebuild normalized CSVs first.")
        if lookback_days < 1:
            raise RuntimeError("lookback_days must be at least 1.")
        normalized_model_type = str(model_type or "lightgbm").strip().lower()
        if normalized_model_type in {"baseline", "local_baseline"}:
            raise RuntimeError(
                "The legacy baseline trainer has been retired. Use model_type=`lightgbm` for all new signal runs."
            )
        if normalized_model_type in {"lightgbm", "lightgbm_multifactor", "lgbm"}:
            return self._train_lightgbm(
                run_name=run_name,
                signal_type=signal_type,
                lookback_days=lookback_days,
                normalized_tickers=normalized_tickers,
                market=market,
                universe=universe,
                rows=rows,
            )
        raise RuntimeError(f"Unsupported model_type `{model_type}`.")
