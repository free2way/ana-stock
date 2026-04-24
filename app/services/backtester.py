import csv
from collections import defaultdict
from math import sqrt

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.services.market_lake import load_lake_rows
from app.services.repository import (
    ModelRunRepository,
    PredictionWriteRepository,
    StrategyRunRepository,
    SymbolRepository,
)


class BacktestRunner:
    """Run a lightweight but configurable top-N backtest from stored predictions."""

    def __init__(self) -> None:
        self.settings = get_settings()

    def _load_market_data(
        self,
        holding_days: int,
        *,
        tickers: set[str] | None = None,
        markets: list[str] | None = None,
    ) -> tuple[dict[tuple[str, str], float], dict[tuple[str, str], dict]]:
        rows_by_symbol: dict[str, list[dict]] = defaultdict(list)
        forward_returns: dict[tuple[str, str], float] = {}
        tradeability: dict[tuple[str, str], dict] = {}
        holding_days = max(1, int(holding_days))
        normalized_tickers = {str(ticker or "").strip().upper() for ticker in (tickers or set()) if str(ticker or "").strip()}
        normalized_markets = [
            str(market or "").strip().upper()
            for market in (markets or [])
            if str(market or "").strip().upper() in {"CN", "US"}
        ]

        for csv_path in sorted(self.settings.normalized_data_dir.glob("*.csv")):
            with csv_path.open("r", newline="", encoding="utf-8") as input_file:
                reader = csv.DictReader(input_file)
                for row in reader:
                    self._append_market_row(rows_by_symbol, row)
        if not rows_by_symbol:
            for row in load_lake_rows(markets=normalized_markets or None, tickers=normalized_tickers or None):
                self._append_market_row(rows_by_symbol, row)

        for symbol, items in rows_by_symbol.items():
            items.sort(key=lambda item: item["date"])
            rolling_turnovers: list[float] = []
            for index, item in enumerate(items):
                turnover_value = max(0.0, item["close"] * item["volume"])
                rolling_turnovers.append(turnover_value)
                window = rolling_turnovers[max(0, index - 19) : index + 1]
                prev_close = items[index - 1]["close"] if index > 0 else None
                gap_pct = None
                if prev_close not in (None, 0.0) and item["open"]:
                    gap_pct = (item["open"] / prev_close) - 1.0
                tradeability[(symbol, item["date"])] = {
                    "close": item["close"],
                    "open": item["open"],
                    "high": item["high"],
                    "low": item["low"],
                    "volume": item["volume"],
                    "adv20": sum(window) / len(window) if window else 0.0,
                    "gap_pct": gap_pct,
                }

            for index in range(len(items) - holding_days):
                current = items[index]
                future = items[index + holding_days]
                if current["ref_close"]:
                    forward_returns[(symbol, current["date"])] = (future["ref_close"] / current["ref_close"]) - 1.0

        return forward_returns, tradeability

    def _append_market_row(self, rows_by_symbol: dict[str, list[dict]], row: dict) -> None:
        symbol = row.get("symbol")
        date = row.get("date")
        ref_close_raw = row.get("adj_close") or row.get("close")
        if not symbol or not date or not ref_close_raw:
            return
        rows_by_symbol[str(symbol).strip().upper()].append(
            {
                "date": str(date),
                "open": float(row.get("open") or 0.0),
                "high": float(row.get("high") or 0.0),
                "low": float(row.get("low") or 0.0),
                "close": float(row.get("close") or 0.0),
                "ref_close": float(ref_close_raw or 0.0),
                "volume": float(row.get("volume") or 0.0),
            }
        )

    def _coerce_weight(self, value: float | None, fallback: float) -> float:
        if value is None:
            return fallback
        return max(0.01, min(1.0, float(value)))

    def _build_benchmark_returns(
        self,
        forward_returns: dict[tuple[str, str], float],
        *,
        benchmark_symbol: str | None,
    ) -> tuple[dict[str, float], str]:
        if benchmark_symbol:
            normalized = str(benchmark_symbol).strip().upper()
            benchmark_returns = {
                trade_date: value
                for (symbol, trade_date), value in forward_returns.items()
                if symbol.upper() == normalized
            }
            if benchmark_returns:
                return benchmark_returns, normalized

        grouped: dict[str, list[float]] = defaultdict(list)
        for (_, trade_date), value in forward_returns.items():
            grouped[trade_date].append(value)
        return (
            {trade_date: sum(values) / len(values) for trade_date, values in grouped.items() if values},
            "universe_equal_weight",
        )

    def _passes_tradeability(self, gate: dict | None, *, min_adv: float, max_gap_pct: float) -> tuple[bool, str | None]:
        if gate is None:
            return False, "missing_market_data"
        close = float(gate.get("close") or 0.0)
        volume = float(gate.get("volume") or 0.0)
        adv20 = float(gate.get("adv20") or 0.0)
        gap_pct = gate.get("gap_pct")
        if close < 1.0:
            return False, "price_below_floor"
        if volume <= 0:
            return False, "no_volume"
        if adv20 < max(0.0, min_adv):
            return False, "adv_below_min"
        if gap_pct is not None and abs(float(gap_pct)) > max(0.0, max_gap_pct):
            return False, "gap_exceeded"
        return True, None

    def _sector_room_available(self, positions: list[dict], candidate: dict, *, max_position_weight: float, max_sector_weight: float) -> bool:
        if max_sector_weight >= 1.0:
            return True
        sector = candidate.get("sector") or "UNKNOWN"
        same_sector = sum(1 for item in positions if (item.get("sector") or "UNKNOWN") == sector)
        return (same_sector + 1) * max_position_weight <= max_sector_weight + 1e-9

    def _select_positions(
        self,
        *,
        eligible: list[dict],
        current_positions: list[dict],
        top_n: int,
        max_position_weight: float,
        max_sector_weight: float,
        rebalance_threshold: float,
    ) -> list[dict]:
        if not eligible or top_n <= 0:
            return []

        ranked = sorted(
            eligible,
            key=lambda item: (item.get("rank_value") or 999999.0, -(item.get("score") or -999999.0), item["ticker"]),
        )
        eligible_map = {item["ticker"]: item for item in ranked}
        selected: list[dict] = []

        for existing in current_positions:
            refreshed = eligible_map.get(existing["ticker"])
            if refreshed is None:
                continue
            if self._sector_room_available(
                selected,
                refreshed,
                max_position_weight=max_position_weight,
                max_sector_weight=max_sector_weight,
            ):
                selected.append(refreshed)
            if len(selected) >= top_n:
                break

        selected_tickers = {item["ticker"] for item in selected}
        for candidate in ranked:
            if candidate["ticker"] in selected_tickers:
                continue
            if len(selected) < top_n:
                if self._sector_room_available(
                    selected,
                    candidate,
                    max_position_weight=max_position_weight,
                    max_sector_weight=max_sector_weight,
                ):
                    selected.append(candidate)
                    selected_tickers.add(candidate["ticker"])
                continue

            worst_index = min(
                range(len(selected)),
                key=lambda idx: (selected[idx].get("score") or -999999.0, -(selected[idx].get("rank_value") or 999999.0)),
            )
            worst = selected[worst_index]
            score_improvement = float(candidate.get("score") or 0.0) - float(worst.get("score") or 0.0)
            if score_improvement < rebalance_threshold:
                continue
            reduced = [item for idx, item in enumerate(selected) if idx != worst_index]
            if not self._sector_room_available(
                reduced,
                candidate,
                max_position_weight=max_position_weight,
                max_sector_weight=max_sector_weight,
            ):
                continue
            selected_tickers.discard(worst["ticker"])
            selected[worst_index] = candidate
            selected_tickers.add(candidate["ticker"])

        return sorted(selected, key=lambda item: (item.get("rank_value") or 999999.0, item["ticker"]))[:top_n]

    def _max_drawdown_from_navs(self, navs: list[float]) -> float:
        if not navs:
            return 0.0
        peak = navs[0]
        max_drawdown = 0.0
        for nav in navs:
            peak = max(peak, nav)
            if peak:
                max_drawdown = min(max_drawdown, (nav / peak) - 1.0)
        return max_drawdown

    def _stddev(self, values: list[float]) -> float:
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
        return sqrt(max(0.0, variance))

    def _safe_ratio(self, numerator: float, denominator: float) -> float | None:
        if abs(denominator) < 1e-12:
            return None
        return numerator / denominator

    def _build_tradeability_summary(
        self,
        *,
        total_candidates: int,
        total_eligible: int,
        total_selected: int,
        gate_stats: dict[str, int],
    ) -> dict:
        blocked = max(0, total_candidates - total_eligible)
        return {
            "total_candidates": total_candidates,
            "eligible_candidates": total_eligible,
            "selected_candidates": total_selected,
            "blocked_candidates": blocked,
            "pass_rate": (total_eligible / total_candidates) if total_candidates else 0.0,
            "selection_rate": (total_selected / total_eligible) if total_eligible else 0.0,
            "top_block_reasons": sorted(gate_stats.items(), key=lambda item: (-item[1], item[0]))[:5],
        }

    def _build_capacity_summary(
        self,
        *,
        min_adv: float,
        max_gap_pct: float,
        max_position_weight: float,
        max_sector_weight: float,
        avg_selected_names: float,
        avg_turnover: float,
        gate_stats: dict[str, int],
    ) -> dict:
        liquidity_block_rate = 0.0
        total_gate_failures = sum(gate_stats.values())
        if total_gate_failures:
            liquidity_block_rate = sum(
                gate_stats.get(key, 0)
                for key in ("adv_below_min", "no_volume", "price_below_floor", "gap_exceeded")
            ) / total_gate_failures
        return {
            "min_adv": min_adv,
            "max_gap_pct": max_gap_pct,
            "max_position_weight": max_position_weight,
            "max_sector_weight": max_sector_weight,
            "avg_selected_names": avg_selected_names,
            "avg_turnover": avg_turnover,
            "liquidity_block_rate": liquidity_block_rate,
            "estimated_gross_exposure": min(1.0, max_position_weight * avg_selected_names),
            "capacity_comment": (
                "Tradability assumptions are relatively tight; scale should be checked against ADV participation."
                if liquidity_block_rate >= 0.35 or avg_turnover >= 0.35
                else "Current assumptions look moderate for paper capacity, but live sizing still needs participation checks."
            ),
        }

    def _build_attribution_summary(
        self,
        *,
        total_return: float,
        benchmark_total_return: float,
        excess_total_return: float | None,
        avg_daily_return: float,
        avg_benchmark_return: float,
        cost_drag_bps: float,
    ) -> dict:
        return {
            "portfolio_total_return": total_return,
            "benchmark_total_return": benchmark_total_return,
            "excess_total_return": excess_total_return,
            "avg_daily_alpha": avg_daily_return - avg_benchmark_return,
            "cost_drag_bps": cost_drag_bps,
            "alpha_source_hint": (
                "Excess return remains positive after benchmark comparison and execution costs."
                if excess_total_return is not None and excess_total_return > 0
                else "Results are mostly explained by beta, weak alpha, or execution assumptions."
            ),
        }

    def _build_portfolio_construction_summary(
        self,
        *,
        top_n: int,
        holding_days: int,
        rebalance_threshold: float,
        max_position_weight: float,
        max_sector_weight: float,
        avg_selected_names: float,
    ) -> dict:
        return {
            "selection_model": "continuous_top_n_with_hysteresis",
            "top_n": top_n,
            "holding_days": holding_days,
            "rebalance_threshold": rebalance_threshold,
            "avg_selected_names": avg_selected_names,
            "max_position_weight": max_position_weight,
            "max_sector_weight": max_sector_weight,
            "continuity_rule": "Keep existing holdings when still eligible; only replace when score improvement exceeds rebalance_threshold.",
            "weighting_rule": "Equal weight across selected names, capped by max_position_weight and effective gross exposure.",
        }

    def _run_config(
        self,
        *,
        top_n: int,
        model_run_id: int,
        holding_days: int,
        commission_bps: float,
        slippage_bps: float,
        max_position_weight: float,
        min_signal_score: float,
        benchmark_symbol: str,
        max_sector_weight: float,
        min_adv: float,
        max_gap_pct: float,
        rebalance_threshold: float,
    ) -> dict:
        return {
            "top_n": top_n,
            "model_run_id": model_run_id,
            "holding_days": holding_days,
            "commission_bps": commission_bps,
            "slippage_bps": slippage_bps,
            "round_trip_cost_bps": commission_bps + slippage_bps,
            "max_position_weight": max_position_weight,
            "min_signal_score": min_signal_score,
            "benchmark_symbol": benchmark_symbol,
            "max_sector_weight": max_sector_weight,
            "min_adv": min_adv,
            "max_gap_pct": max_gap_pct,
            "rebalance_threshold": rebalance_threshold,
            "signal_gate": "score >= min_signal_score; tradeability filters applied; equal-weighted subject to max_position_weight",
        }

    def run(
        self,
        top_n: int = 1,
        model_run_id: int | None = None,
        *,
        holding_days: int | None = None,
        commission_bps: float | None = None,
        slippage_bps: float | None = None,
        max_position_weight: float | None = None,
        min_signal_score: float | None = None,
        benchmark_symbol: str | None = None,
        max_sector_weight: float | None = None,
        min_adv: float | None = None,
        max_gap_pct: float | None = None,
        rebalance_threshold: float | None = None,
    ) -> int:
        holding_days = max(1, int(holding_days or self.settings.backtest_default_holding_days))
        commission_bps = float(commission_bps if commission_bps is not None else self.settings.backtest_commission_bps)
        slippage_bps = float(slippage_bps if slippage_bps is not None else self.settings.backtest_slippage_bps)
        round_trip_cost = max(0.0, (commission_bps + slippage_bps) / 10000.0)
        max_position_weight = self._coerce_weight(max_position_weight, self.settings.backtest_max_position_weight)
        min_signal_score = float(min_signal_score if min_signal_score is not None else self.settings.backtest_min_signal_score)
        benchmark_symbol = str(benchmark_symbol).strip().upper() if benchmark_symbol not in (None, "") else self.settings.backtest_benchmark_symbol
        max_sector_weight = self._coerce_weight(max_sector_weight, self.settings.backtest_max_sector_weight)
        min_adv = float(min_adv if min_adv is not None else self.settings.backtest_min_adv)
        max_gap_pct = float(max_gap_pct if max_gap_pct is not None else self.settings.backtest_max_gap_pct)
        rebalance_threshold = float(rebalance_threshold if rebalance_threshold is not None else self.settings.backtest_rebalance_threshold)

        with SessionLocal() as db:
            model_repo = ModelRunRepository(db)
            symbol_repo = SymbolRepository(db)
            prediction_repo = PredictionWriteRepository(db)
            strategy_repo = StrategyRunRepository(db)

            model_run = model_repo.get_run_by_id(model_run_id) if model_run_id is not None else model_repo.get_latest_run()
            if model_run is None:
                raise RuntimeError("No model run found for the requested id. Train a model first.")

            predictions = prediction_repo.list_for_model_run(model_run.id)
            if not predictions:
                raise RuntimeError("No predictions found for the selected model run.")

            symbols = symbol_repo.list_symbols()
            ticker_by_id = {symbol.id: symbol.ticker for symbol in symbols}
            sector_by_ticker = {symbol.ticker: (symbol.sector or "UNKNOWN") for symbol in symbols}
            prediction_tickers = {
                ticker_by_id[prediction.symbol_id]
                for prediction in predictions
                if prediction.symbol_id in ticker_by_id
            }
            market_filters = [str(model_run.market or "").strip().upper()] if str(model_run.market or "").strip().upper() in {"CN", "US"} else []
            forward_returns, tradeability = self._load_market_data(
                holding_days,
                tickers=prediction_tickers,
                markets=market_filters,
            )
            if not forward_returns:
                raise RuntimeError("No local market data available for backtest. Refresh the Parquet market lake or rebuild normalized CSVs first.")
            benchmark_returns, benchmark_label = self._build_benchmark_returns(forward_returns, benchmark_symbol=benchmark_symbol)
            grouped: dict[str, list] = defaultdict(list)
            for prediction in predictions:
                grouped[prediction.trade_date].append(prediction)

            dates = sorted(grouped.keys())
            strategy_run = strategy_repo.create_run(
                model_run_id=model_run.id,
                name=f"top_n_costed_{model_run.name}",
                strategy_type="top_n_costed_signal_gate",
                start_date=dates[0] if dates else None,
                end_date=dates[-1] if dates else None,
                config={
                    **self._run_config(
                        top_n=top_n,
                        model_run_id=model_run.id,
                        holding_days=holding_days,
                        commission_bps=commission_bps,
                        slippage_bps=slippage_bps,
                        max_position_weight=max_position_weight,
                        min_signal_score=min_signal_score,
                        benchmark_symbol=benchmark_label,
                        max_sector_weight=max_sector_weight,
                        min_adv=min_adv,
                        max_gap_pct=max_gap_pct,
                        rebalance_threshold=rebalance_threshold,
                    ),
                    "model_run_name": model_run.name,
                },
                status="running",
            )

            nav = 1.0
            benchmark_nav = 1.0
            peak = 1.0
            metrics: list[dict] = []
            trade_days = 0
            total_turnover = 0.0
            current_positions: list[dict] = []
            gate_stats: dict[str, int] = defaultdict(int)
            benchmark_navs = [1.0]
            positive_days = 0
            excess_positive_days = 0
            total_candidates = 0
            total_eligible = 0
            total_selected = 0
            daily_selected_counts: list[int] = []
            net_returns: list[float] = []
            excess_returns: list[float] = []

            for date in dates:
                eligible: list[dict] = []
                daily_candidates = len(grouped[date])
                total_candidates += daily_candidates
                for prediction in grouped[date]:
                    ticker = ticker_by_id.get(prediction.symbol_id)
                    if ticker is None:
                        gate_stats["missing_symbol"] += 1
                        continue
                    if prediction.score is None or float(prediction.score) < min_signal_score:
                        gate_stats["below_signal_threshold"] += 1
                        continue
                    gate = tradeability.get((ticker, date))
                    passed, reason = self._passes_tradeability(gate, min_adv=min_adv, max_gap_pct=max_gap_pct)
                    if not passed:
                        gate_stats[reason or "unknown_gate_failure"] += 1
                        continue
                    if (ticker, date) not in forward_returns:
                        gate_stats["missing_forward_return"] += 1
                        continue
                    eligible.append(
                        {
                            "ticker": ticker,
                            "score": float(prediction.score),
                            "rank_value": prediction.rank_value,
                            "sector": sector_by_ticker.get(ticker, "UNKNOWN"),
                        }
                    )

                total_eligible += len(eligible)

                selected = self._select_positions(
                    eligible=eligible,
                    current_positions=current_positions,
                    top_n=top_n,
                    max_position_weight=max_position_weight,
                    max_sector_weight=max_sector_weight,
                    rebalance_threshold=rebalance_threshold,
                )

                returns: list[float] = []
                for position in selected:
                    forward_return = forward_returns.get((position["ticker"], date))
                    if forward_return is not None:
                        returns.append(forward_return)

                if not returns:
                    current_positions = []
                    continue

                gross_exposure = min(1.0, max_position_weight * len(returns))
                weight = gross_exposure / len(returns)
                total_selected += len(selected)
                daily_selected_counts.append(len(selected))
                prev_tickers = {item["ticker"] for item in current_positions}
                curr_tickers = {item["ticker"] for item in selected}
                turnover = (len(curr_tickers - prev_tickers) + len(prev_tickers - curr_tickers)) * weight
                gross_return = sum(item_return * weight for item_return in returns)
                cost_drag = gross_exposure * round_trip_cost
                daily_return = gross_return - cost_drag
                benchmark_return = benchmark_returns.get(date)
                excess_return = daily_return - benchmark_return if benchmark_return is not None else None
                net_returns.append(daily_return)
                if excess_return is not None:
                    excess_returns.append(excess_return)
                nav *= max(0.0, 1.0 + daily_return)
                if benchmark_return is not None:
                    benchmark_nav *= max(0.0, 1.0 + benchmark_return)
                    benchmark_navs.append(benchmark_nav)
                peak = max(peak, nav)
                drawdown = (nav / peak) - 1.0
                trade_days += 1
                total_turnover += turnover
                if daily_return > 0:
                    positive_days += 1
                if excess_return is not None and excess_return > 0:
                    excess_positive_days += 1
                metrics.append(
                    {
                        "trade_date": date,
                        "nav": nav,
                        "daily_return": daily_return,
                        "benchmark_return": benchmark_return,
                        "drawdown": drawdown,
                        "turnover": turnover,
                    }
                )
                current_positions = selected

            if not metrics:
                raise RuntimeError("Backtest produced no daily metrics. The dataset may be too short or filtered out by tradeability gates.")

            strategy_repo.replace_daily_metrics(strategy_run.id, metrics)
            benchmark_days = [metric["benchmark_return"] for metric in metrics if metric.get("benchmark_return") is not None]
            avg_daily_return = sum(metric["daily_return"] for metric in metrics) / len(metrics)
            avg_benchmark_return = sum(benchmark_days) / len(benchmark_days) if benchmark_days else 0.0
            daily_volatility = self._stddev(net_returns)
            excess_volatility = self._stddev(excess_returns)
            selected_avg = total_selected / trade_days if trade_days else 0.0
            candidate_pass_rate = total_eligible / total_candidates if total_candidates else 0.0
            selection_rate = total_selected / total_eligible if total_eligible else 0.0
            avg_turnover = total_turnover / trade_days if trade_days else 0.0
            avg_win = sum(value for value in net_returns if value > 0) / positive_days if positive_days else 0.0
            losing_days = len([value for value in net_returns if value < 0])
            avg_loss = (
                sum(value for value in net_returns if value < 0) / losing_days
                if losing_days
                else 0.0
            )
            total_return = metrics[-1]["nav"] - 1.0
            benchmark_total_return = benchmark_nav - 1.0
            excess_total_return = (nav / benchmark_nav) - 1.0 if benchmark_nav > 0 else None
            tradeability_summary = self._build_tradeability_summary(
                total_candidates=total_candidates,
                total_eligible=total_eligible,
                total_selected=total_selected,
                gate_stats=dict(gate_stats),
            )
            capacity_summary = self._build_capacity_summary(
                min_adv=min_adv,
                max_gap_pct=max_gap_pct,
                max_position_weight=max_position_weight,
                max_sector_weight=max_sector_weight,
                avg_selected_names=selected_avg,
                avg_turnover=avg_turnover,
                gate_stats=dict(gate_stats),
            )
            attribution_summary = self._build_attribution_summary(
                total_return=total_return,
                benchmark_total_return=benchmark_total_return,
                excess_total_return=excess_total_return,
                avg_daily_return=avg_daily_return,
                avg_benchmark_return=avg_benchmark_return,
                cost_drag_bps=(commission_bps + slippage_bps) * max(1.0, selected_avg),
            )
            portfolio_construction_summary = self._build_portfolio_construction_summary(
                top_n=top_n,
                holding_days=holding_days,
                rebalance_threshold=rebalance_threshold,
                max_position_weight=max_position_weight,
                max_sector_weight=max_sector_weight,
                avg_selected_names=selected_avg,
            )
            summary = {
                "model_run_id": model_run.id,
                "model_run_name": model_run.name,
                "start_nav": 1.0,
                "end_nav": metrics[-1]["nav"],
                "total_return": total_return,
                "benchmark_symbol": benchmark_label,
                "end_benchmark_nav": benchmark_nav,
                "benchmark_total_return": benchmark_total_return,
                "excess_total_return": excess_total_return,
                "avg_daily_return": avg_daily_return,
                "avg_benchmark_return": avg_benchmark_return,
                "avg_excess_return": avg_daily_return - avg_benchmark_return,
                "daily_volatility": daily_volatility,
                "annualized_return": ((metrics[-1]["nav"] ** (252 / len(metrics))) - 1.0) if metrics else 0.0,
                "annualized_volatility": daily_volatility * sqrt(252),
                "sharpe_like": self._safe_ratio(avg_daily_return * 252, daily_volatility * sqrt(252)),
                "information_ratio": self._safe_ratio((avg_daily_return - avg_benchmark_return) * 252, excess_volatility * sqrt(252)),
                "hit_ratio": positive_days / trade_days if trade_days else 0.0,
                "excess_hit_ratio": excess_positive_days / trade_days if trade_days else 0.0,
                "avg_win_return": avg_win,
                "avg_loss_return": avg_loss,
                "win_loss_ratio": self._safe_ratio(avg_win, abs(avg_loss)),
                "max_drawdown": min(metric["drawdown"] for metric in metrics),
                "benchmark_max_drawdown": self._max_drawdown_from_navs(benchmark_navs),
                "calmar_like": self._safe_ratio(
                    ((metrics[-1]["nav"] ** (252 / len(metrics))) - 1.0) if metrics else 0.0,
                    abs(min(metric["drawdown"] for metric in metrics)) or 0.0,
                ),
                "days": len(metrics),
                "trade_days": trade_days,
                "avg_selected_names": selected_avg,
                "avg_names_selected": selected_avg,
                "candidate_count": total_candidates,
                "eligible_count": total_eligible,
                "selected_count": total_selected,
                "candidate_pass_rate": candidate_pass_rate,
                "selection_rate": selection_rate,
                "capacity_flags": {
                    "min_adv": min_adv,
                    "max_gap_pct": max_gap_pct,
                    "avg_selected_names": selected_avg,
                    "max_position_weight": max_position_weight,
                    "max_sector_weight": max_sector_weight,
                },
                "avg_turnover": avg_turnover,
                "cost_assumption_bps": commission_bps + slippage_bps,
                "gate_stats": dict(gate_stats),
                "tradability_summary": tradeability_summary,
                "capacity_summary": capacity_summary,
                "attribution_summary": attribution_summary,
                "portfolio_construction_summary": portfolio_construction_summary,
                **self._run_config(
                    top_n=top_n,
                    model_run_id=model_run.id,
                    holding_days=holding_days,
                    commission_bps=commission_bps,
                    slippage_bps=slippage_bps,
                    max_position_weight=max_position_weight,
                    min_signal_score=min_signal_score,
                    benchmark_symbol=benchmark_label,
                    max_sector_weight=max_sector_weight,
                    min_adv=min_adv,
                    max_gap_pct=max_gap_pct,
                    rebalance_threshold=rebalance_threshold,
                ),
            }
            strategy_repo.complete_run(strategy_run.id, status="success", summary=summary)
            return len(metrics)
