from __future__ import annotations

from collections import defaultdict, deque
from datetime import date, timedelta
from dataclasses import replace
from math import floor

from app.services.backtesting.market_rules import entry_reject_reason, exit_reject_reason
from app.services.backtesting.schemas import (
    DailyBar,
    EngineConfig,
    EngineResult,
    PositionLot,
    MarketCorporateAction,
    SignalCandidate,
)
from app.services.market_calendar import is_market_open_date, next_market_open_date
from app.services.execution_costs import FillCostModel


class EventDrivenDailyEngine:
    """Deterministic daily engine: D close signal, D+1 open entry, close mark/exit."""

    def __init__(self, config: EngineConfig) -> None:
        self.config = config
        self.cost_model = FillCostModel(
            config.commission_bps,
            config.slippage_bps,
            sell_stamp_duty_bps_one_way=config.sell_stamp_duty_bps_one_way,
            transfer_fee_bps_one_way=config.transfer_fee_bps_one_way,
            sell_regulatory_fee_bps_one_way=config.sell_regulatory_fee_bps_one_way,
            sell_regulatory_fee_per_share=config.sell_regulatory_fee_per_share,
            min_commission=config.min_commission,
        )

    @staticmethod
    def _prepare_bars(bars: list[DailyBar]) -> tuple[dict[tuple[str, str], DailyBar], list[str]]:
        grouped: dict[str, list[DailyBar]] = defaultdict(list)
        for bar in bars:
            grouped[bar.ticker.upper()].append(bar)
        prepared: dict[tuple[str, str], DailyBar] = {}
        dates: set[str] = set()
        for ticker, ticker_bars in grouped.items():
            ticker_bars.sort(key=lambda item: item.trade_date)
            prior_turnovers: deque[float] = deque(maxlen=20)
            previous_close: float | None = None
            for bar in ticker_bars:
                adv20 = sum(prior_turnovers) / len(prior_turnovers) if prior_turnovers else 0.0
                enriched = replace(
                    bar,
                    ticker=ticker,
                    previous_close=previous_close,
                    adv20=adv20,
                )
                prepared[(ticker, bar.trade_date)] = enriched
                dates.add(bar.trade_date)
                if bar.close > 0 and bar.volume > 0:
                    prior_turnovers.append(bar.close * bar.volume)
                if bar.close > 0:
                    previous_close = bar.close
        return prepared, sorted(dates)

    @staticmethod
    def _rank_signals(signals: list[SignalCandidate], *, top_n: int) -> list[SignalCandidate]:
        """Freeze membership before execution checks; later rows never replace rejects."""
        return sorted(
            signals,
            key=lambda item: (
                int(item.ordinal) if item.ordinal is not None else 10**9,
                float(item.rank_value) if item.rank_value is not None else float("inf"),
                -float(item.score),
                item.ticker,
            ),
        )[:top_n]

    @staticmethod
    def _next_date(calendar: list[str], current_index: int) -> str | None:
        return calendar[current_index + 1] if current_index + 1 < len(calendar) else None

    def _lot_size(self) -> int:
        return 100 if self.config.market.upper() == "CN" else 1

    def _calendar(self, observed_dates: list[str], explicit: list[str] | tuple[str, ...] | None) -> list[str]:
        if explicit is not None:
            values = [str(value)[:10] for value in explicit]
            if values != sorted(values) or len(values) != len(set(values)):
                raise ValueError("calendar_sessions must be unique and ascending")
            if any(not is_market_open_date(self.config.market, value) for value in values):
                raise ValueError("calendar_sessions contains a closed market date")
            return values
        if not observed_dates:
            return []
        current = date.fromisoformat(observed_dates[0])
        end = date.fromisoformat(observed_dates[-1])
        values: list[str] = []
        while current <= end:
            if is_market_open_date(self.config.market, current):
                values.append(current.isoformat())
            current += timedelta(days=1)
        return values

    def _scheduled_exit(self, entry_date: str, calendar: list[str]) -> str:
        entry_index = calendar.index(entry_date)
        target_index = entry_index + self.config.holding_days - 1
        if target_index < len(calendar):
            return calendar[target_index]
        # The supplied history ended before maturity.  Keep the lot pending;
        # extrapolation is only a provisional next attempt, not a mature label.
        current = calendar[-1]
        for _ in range(target_index - len(calendar) + 1):
            current = next_market_open_date(self.config.market, current, include_self=False)
        return current

    def run(
        self,
        *,
        bars: list[DailyBar],
        signals: list[SignalCandidate],
        calendar_sessions: list[str] | tuple[str, ...] | None = None,
        corporate_actions: list[MarketCorporateAction] | tuple[MarketCorporateAction, ...] = (),
    ) -> EngineResult:
        if corporate_actions and any(bar.price_basis != "raw" for bar in bars):
            raise ValueError("corporate actions require raw OHLCV; adjusted bars would double count")
        bar_map, observed_dates = self._prepare_bars(bars)
        calendar = self._calendar(observed_dates, calendar_sessions)
        if len(calendar) < 2:
            raise ValueError("event-driven backtest requires at least two market sessions")
        if any(signal.signal_date not in set(calendar) for signal in signals):
            raise ValueError("signal_date must exist in explicit market calendar")
        signals_by_date: dict[str, list[SignalCandidate]] = defaultdict(list)
        for signal in signals:
            signals_by_date[signal.signal_date].append(signal)
        entries_by_date: dict[str, list[SignalCandidate]] = defaultdict(list)
        for index, signal_date in enumerate(calendar[:-1]):
            next_date = self._next_date(calendar, index)
            if next_date and signals_by_date.get(signal_date):
                entries_by_date[next_date].extend(
                    self._rank_signals(signals_by_date[signal_date], top_n=self.config.top_n)
                )

        cash = float(self.config.initial_cash)
        previous_nav = cash
        peak_nav = cash
        cumulative_fees = 0.0
        cumulative_slippage = 0.0
        lots: list[PositionLot] = []
        last_close_by_ticker: dict[str, float] = {}
        orders: list[dict] = []
        fills: list[dict] = []
        rejects: list[dict] = []
        metrics: list[dict] = []
        states: list[dict] = []
        gate_stats: dict[str, int] = defaultdict(int)
        outcomes: list[dict] = []
        corporate_action_events: list[dict] = []
        actions_by_date: dict[str, list[MarketCorporateAction]] = defaultdict(list)
        for action in corporate_actions:
            actions_by_date[action.effective_date].append(action)
        lot_counter = 0
        order_counter = 0
        calendar_index = {trade_date: index for index, trade_date in enumerate(calendar)}

        for trade_date in calendar:
            index = calendar_index[trade_date]
            traded_notional = 0.0
            for action in actions_by_date.get(trade_date, []):
                matched = [lot for lot in lots if lot.ticker == action.ticker.upper()]
                if action.action_type == "split":
                    for lot in matched:
                        factor = float(action.factor or 1.0)
                        lot.quantity *= factor
                        lot.entry_price /= factor
                    if action.ticker.upper() in last_close_by_ticker:
                        last_close_by_ticker[action.ticker.upper()] /= float(action.factor or 1.0)
                    corporate_action_events.append({
                        "ticker": action.ticker.upper(), "effective_date": trade_date,
                        "action_type": "split", "factor": action.factor, "affected_lots": len(matched),
                    })
                else:
                    for lot in matched:
                        lot.cash_distributions += lot.quantity * float(action.cash_amount or 0.0)
                    cash_delta = sum(lot.quantity for lot in matched) * float(action.cash_amount or 0.0)
                    cash += cash_delta
                    corporate_action_events.append({
                        "ticker": action.ticker.upper(), "effective_date": trade_date,
                        "action_type": "cash_dividend", "cash_amount": action.cash_amount,
                        "cash_delta": cash_delta, "affected_lots": len(matched),
                    })
            selected = entries_by_date.get(trade_date, [])
            daily_sleeve_weight = min(
                1.0 / self.config.holding_days,
                self.config.max_position_weight * max(1, len(selected)),
            )
            per_candidate_weight = (
                min(self.config.max_position_weight, daily_sleeve_weight / len(selected))
                if selected
                else 0.0
            )

            selected_tickers: set[str] = set()
            for candidate in selected:
                ticker = candidate.ticker.upper()
                bar = bar_map.get((ticker, trade_date))
                order_counter += 1
                order_id = f"order-{order_counter}"
                insight_id = candidate.insight_id or f"signal:{candidate.signal_date}:{ticker}"
                order = {
                    "order_id": order_id,
                    "insight_id": insight_id,
                    "ticker": ticker,
                    "side": "buy",
                    "signal_date": candidate.signal_date,
                    "effective_date": trade_date,
                    "order_type": "market_on_open",
                }
                orders.append(order)
                if ticker in selected_tickers:
                    reason = "duplicate_frozen_member"
                    gate_stats[reason] += 1
                    rejects.append({**order, "reject_reason": reason})
                    continue
                selected_tickers.add(ticker)
                if not candidate.qualified:
                    reason = "candidate_not_qualified"
                    gate_stats[reason] += 1
                    rejects.append({**order, "reject_reason": reason})
                    continue
                if float(candidate.score) < self.config.min_signal_score:
                    reason = "below_min_signal_score"
                    gate_stats[reason] += 1
                    rejects.append({**order, "reject_reason": reason})
                    continue
                reason = entry_reject_reason(
                    bar,
                    market=self.config.market,
                    min_adv=self.config.min_adv,
                    max_gap_pct=self.config.max_gap_pct,
                )
                if reason:
                    gate_stats[reason] += 1
                    rejects.append({**order, "reject_reason": reason})
                    continue
                assert bar is not None
                position_notional = sum(
                    lot.quantity * last_close_by_ticker.get(lot.ticker, lot.entry_price)
                    for lot in lots if lot.ticker == ticker
                )
                sector_notional = sum(
                    lot.quantity * last_close_by_ticker.get(lot.ticker, lot.entry_price)
                    for lot in lots if candidate.sector and lot.sector == candidate.sector
                )
                gross_notional = sum(
                    lot.quantity * last_close_by_ticker.get(lot.ticker, lot.entry_price)
                    for lot in lots
                )
                desired_notional = min(
                    previous_nav * per_candidate_weight,
                    max(0.0, previous_nav * self.config.max_position_weight - position_notional),
                    max(0.0, previous_nav * self.config.max_gross_exposure - gross_notional),
                    max(0.0, previous_nav * self.config.max_sector_weight - sector_notional)
                    if candidate.sector else previous_nav * per_candidate_weight,
                )
                commission_rate = self.config.commission_bps / 10000.0
                fill_price = self.cost_model.fill_price(bar.open, side="buy")
                lot_size = self._lot_size()
                affordable_notional = min(desired_notional, cash / (1.0 + commission_rate))
                requested_quantity = floor((affordable_notional / fill_price) / lot_size) * lot_size
                capacity_notional = bar.adv20 * self.config.max_participation_rate
                capacity_quantity = floor((capacity_notional / fill_price) / lot_size) * lot_size if bar.adv20 > 0 else requested_quantity
                quantity = min(requested_quantity, capacity_quantity)
                if quantity <= 0:
                    reason = "insufficient_cash_or_lot_size"
                    gate_stats[reason] += 1
                    rejects.append({**order, "reject_reason": reason})
                    continue
                fill_cost = self.cost_model.fill(bar.open, quantity, side="buy")
                notional = fill_cost["notional"]
                fee = fill_cost["fee"]
                if notional + fee > cash + 1e-8:
                    reason = "insufficient_cash"
                    gate_stats[reason] += 1
                    rejects.append({**order, "reject_reason": reason})
                    continue
                slippage = fill_cost["slippage"]
                cash -= notional + fee
                cumulative_fees += fee
                cumulative_slippage += slippage
                traded_notional += notional
                lot_counter += 1
                lot_id = f"lot-{lot_counter}"
                lots.append(
                    PositionLot(
                        lot_id=lot_id,
                        ticker=ticker,
                        quantity=float(quantity),
                        entry_price=fill_price,
                        entry_date=trade_date,
                        exit_date=self._scheduled_exit(trade_date, calendar),
                        insight_id=insight_id,
                        sector=candidate.sector,
                        entry_notional=notional,
                        entry_fee=fee,
                    )
                )
                fills.append(
                    {
                        **order,
                        "fill_date": trade_date,
                        "quantity": float(quantity),
                        "reference_price": bar.open,
                        "fill_price": fill_price,
                        "fee": fee,
                        "slippage": slippage,
                        "notional": notional,
                        "lot_id": lot_id,
                        "remaining_quantity": float(max(0, requested_quantity - quantity)),
                        "fill_status": "partial" if quantity < requested_quantity else "filled",
                    }
                )

            force_liquidation = self.config.liquidate_at_end and index == len(calendar) - 1
            remaining_lots: list[PositionLot] = []
            for lot in lots:
                if lot.exit_date > trade_date and not force_liquidation:
                    remaining_lots.append(lot)
                    continue
                bar = bar_map.get((lot.ticker, trade_date))
                order_counter += 1
                order_id = f"order-{order_counter}"
                order = {
                    "order_id": order_id,
                    "insight_id": lot.insight_id,
                    "ticker": lot.ticker,
                    "side": "sell",
                    "signal_date": None,
                    "effective_date": trade_date,
                    "order_type": "market_on_close",
                    "exit_reason": "final_liquidation" if force_liquidation else "holding_period_expired",
                }
                orders.append(order)
                reason = exit_reject_reason(bar, market=self.config.market)
                if reason:
                    gate_stats[reason] += 1
                    rejects.append({**order, "reject_reason": reason})
                    next_date = self._next_date(calendar, index)
                    lot.exit_date = next_date or next_market_open_date(self.config.market, trade_date, include_self=False)
                    remaining_lots.append(lot)
                    continue
                assert bar is not None
                fill_cost = self.cost_model.fill(bar.close, lot.quantity, side="sell")
                fill_price = fill_cost["fill_price"]
                notional = fill_cost["notional"]
                fee = fill_cost["fee"]
                slippage = fill_cost["slippage"]
                cash += notional - fee
                cumulative_fees += fee
                cumulative_slippage += slippage
                traded_notional += notional
                fills.append(
                    {
                        **order,
                        "fill_date": trade_date,
                        "quantity": lot.quantity,
                        "reference_price": bar.close,
                        "fill_price": fill_price,
                        "fee": fee,
                        "slippage": slippage,
                        "notional": notional,
                        "lot_id": lot.lot_id,
                        "entry_date": lot.entry_date,
                        "remaining_quantity": 0.0,
                        "fill_status": "filled",
                    }
                )
                proceeds = notional - fee
                net_pnl = proceeds + lot.cash_distributions - lot.entry_notional - lot.entry_fee
                outcomes.append({
                    "lot_id": lot.lot_id,
                    "insight_id": lot.insight_id,
                    "ticker": lot.ticker,
                    "entry_date": lot.entry_date,
                    "exit_date": trade_date,
                    "entry_notional": lot.entry_notional,
                    "exit_proceeds": proceeds,
                    "net_pnl": net_pnl,
                    "cash_distributions": lot.cash_distributions,
                    "net_return": net_pnl / (lot.entry_notional + lot.entry_fee),
                    "status": "matured",
                })
            lots = remaining_lots

            position_market_value = 0.0
            for lot in lots:
                bar = bar_map.get((lot.ticker, trade_date))
                mark_price = (
                    bar.close
                    if bar is not None and bar.close > 0
                    else last_close_by_ticker.get(lot.ticker, lot.entry_price)
                )
                position_market_value += lot.quantity * mark_price
            for (ticker, date_value), bar in bar_map.items():
                if date_value == trade_date and bar.close > 0:
                    last_close_by_ticker[ticker] = bar.close

            nav = cash + position_market_value
            daily_return = (nav / previous_nav) - 1.0 if previous_nav else 0.0
            peak_nav = max(peak_nav, nav)
            drawdown = (nav / peak_nav) - 1.0 if peak_nav else 0.0
            turnover = 0.5 * traded_notional / previous_nav if previous_nav else 0.0
            gross_exposure = position_market_value / nav if nav else 0.0
            metrics.append(
                {
                    "trade_date": trade_date,
                    "nav": nav / self.config.initial_cash,
                    "daily_return": daily_return,
                    "benchmark_return": None,
                    "drawdown": drawdown,
                    "turnover": turnover,
                }
            )
            states.append(
                {
                    "trade_date": trade_date,
                    "cash": cash,
                    "position_market_value": position_market_value,
                    "nav": nav,
                    "gross_exposure": gross_exposure,
                    "net_exposure": gross_exposure,
                    "cumulative_fees": cumulative_fees,
                    "cumulative_slippage": cumulative_slippage,
                    "open_lots": len(lots),
                    "stale_mark_lots": sum(
                        1 for lot in lots if bar_map.get((lot.ticker, trade_date)) is None
                    ),
                }
            )
            if abs(nav - (cash + position_market_value)) > 1e-8:
                raise RuntimeError("portfolio accounting identity failed")
            previous_nav = nav

        last_session = calendar[-1]
        for lot in lots:
            due_sell_rejected = any(
                row.get("ticker") == lot.ticker
                and row.get("side") == "sell"
                and row.get("effective_date") <= last_session
                and row.get("insight_id") == lot.insight_id
                for row in rejects
            )
            outcomes.append({
                "lot_id": lot.lot_id,
                "insight_id": lot.insight_id,
                "ticker": lot.ticker,
                "entry_date": lot.entry_date,
                "scheduled_exit_date": self._scheduled_exit(lot.entry_date, calendar),
                "next_exit_attempt_date": lot.exit_date,
                "as_of_date": last_session,
                "status": "EXIT_DEFERRED" if due_sell_rejected else "PENDING",
                "net_return": None,
                "net_pnl": None,
            })

        return EngineResult(
            metrics=tuple(metrics),
            orders=tuple(orders),
            fills=tuple(fills),
            rejects=tuple(rejects),
            portfolio_states=tuple(states),
            initial_cash=self.config.initial_cash,
            end_nav=previous_nav,
            cumulative_fees=cumulative_fees,
            cumulative_slippage=cumulative_slippage,
            open_position_count=len(lots),
            gate_stats=dict(sorted(gate_stats.items())),
            outcomes=tuple(outcomes),
            corporate_action_events=tuple(corporate_action_events),
            cost_model_metadata=self.cost_model.metadata(),
        )


__all__ = [
    "DailyBar",
    "EngineConfig",
    "EngineResult",
    "EventDrivenDailyEngine",
    "MarketCorporateAction",
    "SignalCandidate",
]
