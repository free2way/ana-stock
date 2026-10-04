"""Backtest and strategy-run domain repositories."""

import json

from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from app.models.tables import (
    StrategyDailyMetric,
    StrategyFill,
    StrategyOrder,
    StrategyPortfolioState,
    StrategyReject,
    StrategyRun,
)

from app.services.repositories.shared import (
    _loads_json_object,
    utc_now_iso,
)


class BacktestRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _build_backtest_payload(self, row: StrategyRun) -> dict:
        summary = _loads_json_object(row.summary_json)
        validation = None
        if summary is not None:
            validation = {
                "annualized_return": summary.get("annualized_return"),
                "annualized_volatility": summary.get("annualized_volatility"),
                "sharpe_like": summary.get("sharpe_like"),
                "information_ratio": summary.get("information_ratio"),
                "calmar_like": summary.get("calmar_like"),
                "max_drawdown": summary.get("max_drawdown"),
                "hit_ratio": summary.get("hit_ratio"),
                "excess_hit_ratio": summary.get("excess_hit_ratio"),
                "avg_turnover": summary.get("avg_turnover"),
                "candidate_pass_rate": summary.get("candidate_pass_rate"),
                "selection_rate": summary.get("selection_rate"),
                "avg_selected_names": summary.get("avg_selected_names"),
                "cost_assumption_bps": summary.get("cost_assumption_bps"),
                "gate_stats": summary.get("gate_stats") or {},
                "capacity_flags": summary.get("capacity_flags") or {},
            }
        return {
            "id": row.id,
            "name": row.name,
            "strategy_type": row.strategy_type,
            "start_date": row.start_date,
            "end_date": row.end_date,
            "status": row.status,
            "summary_json": row.summary_json,
            "summary": summary,
            "validation_summary": validation,
            "created_at": row.created_at,
            "finished_at": row.finished_at,
        }

    def list_backtests(self) -> list[dict]:
        stmt = select(StrategyRun).order_by(StrategyRun.created_at.desc())
        rows = self.db.scalars(stmt).all()
        return [self._build_backtest_payload(row) for row in rows]

    def get_backtest(self, strategy_run_id: int) -> dict | None:
        row = self.db.scalar(select(StrategyRun).where(StrategyRun.id == strategy_run_id))
        if row is None:
            return None
        payload = self._build_backtest_payload(row)
        payload["config"] = _loads_json_object(row.config_json)
        payload["audit_counts"] = {
            "orders": self.db.scalar(
                select(func.count()).select_from(StrategyOrder).where(
                    StrategyOrder.strategy_run_id == strategy_run_id
                )
            )
            or 0,
            "fills": self.db.scalar(
                select(func.count()).select_from(StrategyFill).where(
                    StrategyFill.strategy_run_id == strategy_run_id
                )
            )
            or 0,
            "rejects": self.db.scalar(
                select(func.count()).select_from(StrategyReject).where(
                    StrategyReject.strategy_run_id == strategy_run_id
                )
            )
            or 0,
            "portfolio_states": self.db.scalar(
                select(func.count()).select_from(StrategyPortfolioState).where(
                    StrategyPortfolioState.strategy_run_id == strategy_run_id
                )
            )
            or 0,
        }
        return payload

    def list_execution_audit(
        self,
        strategy_run_id: int,
        *,
        side: str | None = None,
        status: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict]:
        stmt = select(StrategyOrder).where(StrategyOrder.strategy_run_id == strategy_run_id)
        normalized_side = str(side or "").strip().lower()
        normalized_status = str(status or "").strip().lower()
        if normalized_side in {"buy", "sell"}:
            stmt = stmt.where(StrategyOrder.side == normalized_side)
        if normalized_status in {"filled", "rejected", "submitted"}:
            stmt = stmt.where(StrategyOrder.status == normalized_status)
        stmt = stmt.order_by(StrategyOrder.effective_date.asc(), StrategyOrder.id.asc()).offset(
            max(0, int(offset))
        ).limit(min(1000, max(1, int(limit))))
        orders = list(self.db.scalars(stmt).all())
        order_ids = [row.order_id for row in orders]
        if not order_ids:
            return []
        fills = {
            row.order_id: row
            for row in self.db.scalars(
                select(StrategyFill).where(
                    StrategyFill.strategy_run_id == strategy_run_id,
                    StrategyFill.order_id.in_(order_ids),
                )
            ).all()
        }
        rejects = {
            row.order_id: row
            for row in self.db.scalars(
                select(StrategyReject).where(
                    StrategyReject.strategy_run_id == strategy_run_id,
                    StrategyReject.order_id.in_(order_ids),
                )
            ).all()
        }
        payload: list[dict] = []
        for row in orders:
            fill = fills.get(row.order_id)
            reject = rejects.get(row.order_id)
            payload.append(
                {
                    "order_id": row.order_id,
                    "insight_id": row.insight_id,
                    "ticker": row.ticker,
                    "side": row.side,
                    "signal_date": row.signal_date,
                    "effective_date": row.effective_date,
                    "order_type": row.order_type,
                    "exit_reason": row.exit_reason,
                    "status": row.status,
                    "fill_date": fill.fill_date if fill else None,
                    "quantity": fill.quantity if fill else None,
                    "reference_price": fill.reference_price if fill else None,
                    "fill_price": fill.fill_price if fill else None,
                    "fee": fill.fee if fill else None,
                    "slippage": fill.slippage if fill else None,
                    "notional": fill.notional if fill else None,
                    "lot_id": fill.lot_id if fill else None,
                    "entry_date": fill.entry_date if fill else None,
                    "reject_reason": reject.reject_reason if reject else None,
                }
            )
        return payload

    def get_portfolio_states(self, strategy_run_id: int) -> list[dict]:
        rows = self.db.scalars(
            select(StrategyPortfolioState)
            .where(StrategyPortfolioState.strategy_run_id == strategy_run_id)
            .order_by(StrategyPortfolioState.trade_date.asc())
        ).all()
        return [
            {
                "trade_date": row.trade_date,
                "cash": row.cash,
                "position_market_value": row.position_market_value,
                "nav": row.nav,
                "gross_exposure": row.gross_exposure,
                "net_exposure": row.net_exposure,
                "cumulative_fees": row.cumulative_fees,
                "cumulative_slippage": row.cumulative_slippage,
                "open_lots": row.open_lots,
            }
            for row in rows
        ]

    def get_latest_backtest(self) -> StrategyRun | None:
        stmt = select(StrategyRun).order_by(StrategyRun.id.desc()).limit(1)
        return self.db.scalar(stmt)

    def get_latest_backtest_summary(self) -> dict | None:
        backtest = self.get_latest_backtest()
        if backtest is None:
            return None
        return self._build_backtest_payload(backtest)

    def get_daily_metrics(self, strategy_run_id: int) -> list[dict]:
        stmt = (
            select(StrategyDailyMetric)
            .where(StrategyDailyMetric.strategy_run_id == strategy_run_id)
            .order_by(StrategyDailyMetric.trade_date.asc())
        )
        rows = self.db.scalars(stmt).all()
        return [
            {
                "trade_date": row.trade_date,
                "nav": row.nav,
                "daily_return": row.daily_return,
                "benchmark_return": row.benchmark_return,
                "drawdown": row.drawdown,
                "turnover": row.turnover,
            }
            for row in rows
        ]

    def get_latest_backtest_curve(self) -> list[dict]:
        latest = self.get_latest_backtest()
        if latest is None:
            return []
        return self.get_daily_metrics(latest.id)

class StrategyRunRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_run(
        self,
        *,
        model_run_id: int | None,
        name: str,
        strategy_type: str,
        start_date: str | None,
        end_date: str | None,
        config: dict | None,
        status: str,
    ) -> StrategyRun:
        run = StrategyRun(
            model_run_id=model_run_id,
            name=name,
            strategy_type=strategy_type,
            start_date=start_date,
            end_date=end_date,
            config_json=json.dumps(config) if config is not None else None,
            summary_json=None,
            status=status,
            created_at=utc_now_iso(),
            finished_at=None,
        )
        self.db.add(run)
        self.db.commit()
        self.db.refresh(run)
        return run

    def merge_config(
        self,
        strategy_run_id: int,
        updates: dict,
        *,
        commit: bool = True,
    ) -> StrategyRun | None:
        """Persist audit metadata (e.g. corporate-action details) after creation.

        The run config is written when the run is created, which is before the
        market corporate actions are loaded.  Callers use this method to fold
        late-arriving audit fields into the persisted config without clobbering
        the fields already stored there.
        """

        run = self.db.scalar(select(StrategyRun).where(StrategyRun.id == int(strategy_run_id)))
        if run is None:
            return None
        config = _loads_json_object(run.config_json) or {}
        config.update(dict(updates))
        run.config_json = json.dumps(config, ensure_ascii=False, sort_keys=True, default=str)
        if commit:
            self.db.commit()
            self.db.refresh(run)
        else:
            self.db.flush()
        return run

    def replace_daily_metrics(self, strategy_run_id: int, rows: list[dict]) -> int:
        existing_stmt = select(StrategyDailyMetric).where(StrategyDailyMetric.strategy_run_id == strategy_run_id)
        for metric in self.db.scalars(existing_stmt).all():
            self.db.delete(metric)
        self.db.flush()

        now = utc_now_iso()
        for row in rows:
            metric = StrategyDailyMetric(
                strategy_run_id=strategy_run_id,
                trade_date=row["trade_date"],
                nav=row.get("nav"),
                daily_return=row.get("daily_return"),
                benchmark_return=row.get("benchmark_return"),
                drawdown=row.get("drawdown"),
                turnover=row.get("turnover"),
                created_at=now,
            )
            self.db.add(metric)

        self.db.commit()
        return len(rows)

    def replace_execution_audit(
        self,
        strategy_run_id: int,
        *,
        orders: list[dict] | tuple[dict, ...],
        fills: list[dict] | tuple[dict, ...],
        rejects: list[dict] | tuple[dict, ...],
        portfolio_states: list[dict] | tuple[dict, ...],
    ) -> dict[str, int]:
        for model in (StrategyFill, StrategyReject, StrategyOrder, StrategyPortfolioState):
            self.db.execute(delete(model).where(model.strategy_run_id == strategy_run_id))
        now = utc_now_iso()
        fill_order_ids = {str(row.get("order_id") or "") for row in fills}
        reject_order_ids = {str(row.get("order_id") or "") for row in rejects}
        order_rows = [
            {
                "strategy_run_id": strategy_run_id,
                "order_id": str(row["order_id"]),
                "insight_id": row.get("insight_id"),
                "ticker": str(row["ticker"]),
                "side": str(row["side"]),
                "signal_date": row.get("signal_date"),
                "effective_date": str(row["effective_date"]),
                "order_type": str(row["order_type"]),
                "exit_reason": row.get("exit_reason"),
                "status": (
                    "filled"
                    if str(row["order_id"]) in fill_order_ids
                    else "rejected"
                    if str(row["order_id"]) in reject_order_ids
                    else "submitted"
                ),
                "created_at": now,
            }
            for row in orders
        ]
        fill_rows = [
            {
                "strategy_run_id": strategy_run_id,
                "order_id": str(row["order_id"]),
                "insight_id": row.get("insight_id"),
                "ticker": str(row["ticker"]),
                "side": str(row["side"]),
                "fill_date": str(row["fill_date"]),
                "quantity": float(row.get("quantity") or 0.0),
                "reference_price": float(row.get("reference_price") or 0.0),
                "fill_price": float(row.get("fill_price") or 0.0),
                "fee": float(row.get("fee") or 0.0),
                "slippage": float(row.get("slippage") or 0.0),
                "notional": float(row.get("notional") or 0.0),
                "lot_id": row.get("lot_id"),
                "entry_date": row.get("entry_date"),
                "created_at": now,
            }
            for row in fills
        ]
        reject_rows = [
            {
                "strategy_run_id": strategy_run_id,
                "order_id": str(row["order_id"]),
                "ticker": str(row["ticker"]),
                "side": str(row["side"]),
                "effective_date": str(row["effective_date"]),
                "reject_reason": str(row["reject_reason"]),
                "created_at": now,
            }
            for row in rejects
        ]
        state_rows = [
            {
                "strategy_run_id": strategy_run_id,
                "trade_date": str(row["trade_date"]),
                "cash": float(row.get("cash") or 0.0),
                "position_market_value": float(row.get("position_market_value") or 0.0),
                "nav": float(row.get("nav") or 0.0),
                "gross_exposure": float(row.get("gross_exposure") or 0.0),
                "net_exposure": float(row.get("net_exposure") or 0.0),
                "cumulative_fees": float(row.get("cumulative_fees") or 0.0),
                "cumulative_slippage": float(row.get("cumulative_slippage") or 0.0),
                "open_lots": int(row.get("open_lots") or 0),
                "created_at": now,
            }
            for row in portfolio_states
        ]
        for model, rows in (
            (StrategyOrder, order_rows),
            (StrategyFill, fill_rows),
            (StrategyReject, reject_rows),
            (StrategyPortfolioState, state_rows),
        ):
            if rows:
                self.db.execute(insert(model), rows)
        self.db.commit()
        return {
            "orders": len(order_rows),
            "fills": len(fill_rows),
            "rejects": len(reject_rows),
            "portfolio_states": len(state_rows),
        }

    def complete_run(self, strategy_run_id: int, status: str, summary: dict | None) -> StrategyRun | None:
        stmt = select(StrategyRun).where(StrategyRun.id == strategy_run_id)
        run = self.db.scalar(stmt)
        if run is None:
            return None
        run.status = status
        run.summary_json = json.dumps(summary) if summary is not None else None
        run.finished_at = utc_now_iso()
        self.db.commit()
        self.db.refresh(run)
        return run
