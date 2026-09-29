from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from math import sqrt

from app.core.db import SessionLocal
from app.services.backtesting.engine import EventDrivenDailyEngine
from app.services.backtesting.schemas import DailyBar, EngineConfig, SignalCandidate
from app.services.market_lake import load_lake_rows
from app.services.repository import (
    ModelRunRepository,
    PredictionWriteRepository,
    StrategyRunRepository,
    SymbolRepository,
)


class EventDrivenBacktestRunner:
    engine_version = "event_driven_daily_v2"

    @staticmethod
    def _stddev(values: list[float]) -> float:
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        return sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))

    @staticmethod
    def _safe_ratio(numerator: float, denominator: float) -> float | None:
        return numerator / denominator if abs(denominator) > 1e-12 else None

    @staticmethod
    def _infer_market(model_market: str | None, tickers: set[str]) -> str:
        normalized = str(model_market or "").strip().upper()
        if normalized in {"CN", "US"}:
            return normalized
        return "CN" if any(ticker.endswith((".SS", ".SZ", ".BJ")) for ticker in tickers) else "US"

    @staticmethod
    def _benchmark_returns(bars: list[DailyBar]) -> dict[str, float]:
        grouped: dict[str, list[DailyBar]] = defaultdict(list)
        for bar in bars:
            grouped[bar.ticker].append(bar)
        daily: dict[str, list[float]] = defaultdict(list)
        for ticker_bars in grouped.values():
            ticker_bars.sort(key=lambda item: item.trade_date)
            previous_close: float | None = None
            for bar in ticker_bars:
                if previous_close and previous_close > 0 and bar.close > 0:
                    daily[bar.trade_date].append((bar.close / previous_close) - 1.0)
                if bar.close > 0:
                    previous_close = bar.close
        return {
            trade_date: sum(values) / len(values)
            for trade_date, values in daily.items()
            if values
        }

    @staticmethod
    def _trim_rows(rows: list[dict], *, signal_dates: list[str], holding_days: int) -> list[dict]:
        calendar = sorted({str(row.get("date") or "")[:10] for row in rows if row.get("date")})
        if not calendar or not signal_dates:
            return []
        start_date = min(signal_dates)
        end_signal_date = max(signal_dates)
        signal_start_index = next((index for index, value in enumerate(calendar) if value >= start_date), 0)
        # Keep the full ADV20 lookback before the first signal while reporting
        # performance only from the first signal date onward.
        start_index = max(0, signal_start_index - 20)
        end_index = max(
            (index for index, value in enumerate(calendar) if value <= end_signal_date),
            default=len(calendar) - 1,
        )
        end_index = min(len(calendar) - 1, end_index + max(1, holding_days) + 1)
        allowed = set(calendar[start_index : end_index + 1])
        return [row for row in rows if str(row.get("date") or "")[:10] in allowed]

    def run(
        self,
        *,
        top_n: int,
        model_run_id: int | None,
        holding_days: int,
        commission_bps: float,
        slippage_bps: float,
        max_position_weight: float,
        min_signal_score: float,
        min_adv: float,
        max_gap_pct: float,
        initial_cash: float = 1_000_000.0,
    ) -> int:
        with SessionLocal() as db:
            model_repo = ModelRunRepository(db)
            symbol_repo = SymbolRepository(db)
            prediction_repo = PredictionWriteRepository(db)
            strategy_repo = StrategyRunRepository(db)
            model_run = (
                model_repo.get_run_by_id(model_run_id)
                if model_run_id is not None
                else model_repo.get_latest_run()
            )
            if model_run is None:
                raise RuntimeError("No model run found for the requested id. Train a model first.")
            predictions = prediction_repo.list_for_model_run(model_run.id)
            if not predictions:
                raise RuntimeError("No predictions found for the selected model run.")
            symbols = symbol_repo.list_symbols()
            ticker_by_id = {symbol.id: symbol.ticker for symbol in symbols}
            st_by_ticker = {
                symbol.ticker: str(symbol.name or "").strip().upper().replace(" ", "").startswith(
                    ("ST", "*ST", "SST", "S*ST")
                )
                for symbol in symbols
            }
            prediction_tickers = {
                ticker_by_id[prediction.symbol_id]
                for prediction in predictions
                if prediction.symbol_id in ticker_by_id
            }
            market = self._infer_market(model_run.market, prediction_tickers)
            signal_dates = sorted({str(prediction.trade_date)[:10] for prediction in predictions})
            strategy_run = strategy_repo.create_run(
                model_run_id=model_run.id,
                name=f"event_v2_top_n_{model_run.name}",
                strategy_type="event_driven_top_n",
                start_date=signal_dates[0] if signal_dates else None,
                end_date=signal_dates[-1] if signal_dates else None,
                config={
                    "engine_version": self.engine_version,
                    "reality_model_version": "daily_ohlcv_basic_v1",
                    "signal_cutoff": "close",
                    "entry_price_mode": "next_open",
                    "exit_price_mode": "close",
                    "holding_period_basis": "trading_sessions",
                    "top_n": top_n,
                    "holding_days": holding_days,
                    "commission_bps_one_way": commission_bps,
                    "slippage_bps_one_way": slippage_bps,
                    "max_position_weight": max_position_weight,
                    "min_signal_score": min_signal_score,
                    "min_adv": min_adv,
                    "max_gap_pct": max_gap_pct,
                    "initial_cash": initial_cash,
                    "model_run_name": model_run.name,
                },
                status="running",
            )
            try:
                first_signal = datetime.strptime(signal_dates[0], "%Y-%m-%d").date()
                last_signal = datetime.strptime(signal_dates[-1], "%Y-%m-%d").date()
                raw_rows = load_lake_rows(
                    markets=[market],
                    tickers=prediction_tickers,
                    start_date=(first_signal - timedelta(days=45)).isoformat(),
                    end_date=(
                        last_signal + timedelta(days=max(14, holding_days * 3 + 7))
                    ).isoformat(),
                )
                raw_rows = self._trim_rows(
                    raw_rows,
                    signal_dates=signal_dates,
                    holding_days=holding_days,
                )
                bars = [
                    DailyBar(
                        ticker=str(row.get("symbol") or "").strip().upper(),
                        trade_date=str(row.get("date") or "")[:10],
                        open=float(row.get("open") or 0.0),
                        high=float(row.get("high") or 0.0),
                        low=float(row.get("low") or 0.0),
                        close=float(row.get("close") or 0.0),
                        volume=float(row.get("volume") or 0.0),
                        is_st=bool(st_by_ticker.get(str(row.get("symbol") or "").strip().upper(), False)),
                    )
                    for row in raw_rows
                    if row.get("symbol") and row.get("date")
                ]
                signals = [
                    SignalCandidate(
                        signal_date=str(prediction.trade_date)[:10],
                        ticker=ticker_by_id[prediction.symbol_id],
                        score=float(prediction.score or 0.0),
                        rank_value=float(prediction.rank_value) if prediction.rank_value is not None else None,
                        insight_id=f"prediction:{prediction.id}",
                    )
                    for prediction in predictions
                    if prediction.symbol_id in ticker_by_id
                ]
                result = EventDrivenDailyEngine(
                    EngineConfig(
                        market=market,
                        top_n=top_n,
                        holding_days=holding_days,
                        initial_cash=initial_cash,
                        commission_bps=commission_bps,
                        slippage_bps=slippage_bps,
                        max_position_weight=max_position_weight,
                        min_signal_score=min_signal_score,
                        min_adv=min_adv,
                        max_gap_pct=max_gap_pct,
                    )
                ).run(bars=bars, signals=signals)
                benchmark_returns = self._benchmark_returns(bars)
                metrics = []
                benchmark_nav = 1.0
                for metric in result.metrics:
                    if str(metric.get("trade_date") or "") < signal_dates[0]:
                        continue
                    row = dict(metric)
                    row["benchmark_return"] = benchmark_returns.get(row["trade_date"])
                    if row["benchmark_return"] is not None:
                        benchmark_nav *= max(0.0, 1.0 + float(row["benchmark_return"]))
                    metrics.append(row)
                reported_states = [
                    dict(item)
                    for item in result.portfolio_states
                    if str(item.get("trade_date") or "") >= signal_dates[0]
                ]
                strategy_repo.replace_daily_metrics(strategy_run.id, metrics)
                audit_counts = strategy_repo.replace_execution_audit(
                    strategy_run.id,
                    orders=result.orders,
                    fills=result.fills,
                    rejects=result.rejects,
                    portfolio_states=reported_states,
                )
                daily_returns = [float(row.get("daily_return") or 0.0) for row in metrics]
                benchmark_daily = [
                    float(row["benchmark_return"])
                    for row in metrics
                    if row.get("benchmark_return") is not None
                ]
                avg_daily_return = sum(daily_returns) / len(daily_returns) if daily_returns else 0.0
                avg_benchmark_return = (
                    sum(benchmark_daily) / len(benchmark_daily) if benchmark_daily else 0.0
                )
                volatility = self._stddev(daily_returns)
                total_return = (result.end_nav / result.initial_cash) - 1.0
                buy_fills = [item for item in result.fills if item.get("side") == "buy"]
                sell_fills = [item for item in result.fills if item.get("side") == "sell"]
                entry_orders = [item for item in result.orders if item.get("side") == "buy"]
                entry_rejects = [item for item in result.rejects if item.get("side") == "buy"]
                open_lot_counts = [
                    float(item.get("open_lots") or 0.0)
                    for item in result.portfolio_states
                    if str(item.get("trade_date") or "") >= signal_dates[0]
                ]
                avg_open_lots = (
                    sum(open_lot_counts) / len(open_lot_counts) if open_lot_counts else 0.0
                )
                summary = {
                    "engine_version": self.engine_version,
                    "legacy": False,
                    "reality_model_version": "daily_ohlcv_basic_v1",
                    "model_run_id": model_run.id,
                    "model_run_name": model_run.name,
                    "market": market,
                    "signal_cutoff": "close",
                    "entry_price_mode": "next_open",
                    "exit_price_mode": "close",
                    "holding_period_basis": "trading_sessions",
                    "start_nav": 1.0,
                    "end_nav": result.end_nav / result.initial_cash,
                    "initial_cash": result.initial_cash,
                    "ending_cash": result.portfolio_states[-1]["cash"],
                    "total_return": total_return,
                    "annualized_return": (
                        ((1.0 + total_return) ** (252 / max(1, len(metrics) - 1))) - 1.0
                        if total_return > -1.0
                        else -1.0
                    ),
                    "avg_daily_return": avg_daily_return,
                    "annualized_volatility": volatility * sqrt(252),
                    "sharpe_like": self._safe_ratio(avg_daily_return * 252, volatility * sqrt(252)),
                    "max_drawdown": min((float(row.get("drawdown") or 0.0) for row in metrics), default=0.0),
                    "benchmark_symbol": "universe_equal_weight_close_to_close",
                    "end_benchmark_nav": benchmark_nav,
                    "benchmark_total_return": benchmark_nav - 1.0,
                    "avg_benchmark_return": avg_benchmark_return,
                    "days": len(metrics),
                    "trade_days": sum(bool(row.get("turnover")) for row in metrics),
                    "avg_turnover": (
                        sum(float(row.get("turnover") or 0.0) for row in metrics) / len(metrics)
                        if metrics
                        else 0.0
                    ),
                    "turnover_definition": "0.5 * traded_notional / prior_close_nav",
                    "candidate_count": len(signals),
                    "selected_count": len(buy_fills),
                    "eligible_count": len(buy_fills),
                    "entry_order_count": len(entry_orders),
                    "entry_reject_count": len(entry_rejects),
                    "candidate_pass_rate": (
                        len(buy_fills) / len(entry_orders) if entry_orders else 0.0
                    ),
                    "selection_rate": len(buy_fills) / len(signals) if signals else 0.0,
                    "avg_selected_names": avg_open_lots,
                    "avg_names_selected": avg_open_lots,
                    "avg_open_lots": avg_open_lots,
                    "fill_count": len(result.fills),
                    "buy_fill_count": len(buy_fills),
                    "sell_fill_count": len(sell_fills),
                    "reject_count": len(result.rejects),
                    "open_position_count": result.open_position_count,
                    "cumulative_fees": result.cumulative_fees,
                    "cumulative_slippage": result.cumulative_slippage,
                    "commission_bps_one_way": commission_bps,
                    "slippage_bps_one_way": slippage_bps,
                    "round_trip_cost_bps": 2.0 * (commission_bps + slippage_bps),
                    "cost_assumption_bps": 2.0 * (commission_bps + slippage_bps),
                    "gate_stats": result.gate_stats,
                    "audit_storage": "normalized_tables_v1",
                    "audit_counts": audit_counts,
                }
                strategy_repo.complete_run(strategy_run.id, status="success", summary=summary)
                return len(metrics)
            except Exception as exc:
                strategy_repo.complete_run(
                    strategy_run.id,
                    status="failed",
                    summary={
                        "engine_version": self.engine_version,
                        "legacy": False,
                        "error": str(exc),
                    },
                )
                raise
