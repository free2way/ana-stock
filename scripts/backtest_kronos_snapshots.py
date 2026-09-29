#!/usr/bin/env python3
from __future__ import annotations

import json

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models.tables import StrategyRun
from app.services.backtesting.runner import EventDrivenBacktestRunner
from app.services.kronos_backtest import KRONOS_SUPPORT_SCORE, persist_kronos_historical_runs


def _latest_summary(model_run_id: int) -> dict:
    with SessionLocal() as db:
        row = db.scalar(
            select(StrategyRun)
            .where(StrategyRun.model_run_id == model_run_id, StrategyRun.status == "success")
            .order_by(StrategyRun.id.desc())
            .limit(1)
        )
        if row is None:
            return {}
        return {
            "strategy_run_id": row.id,
            "model_run_id": model_run_id,
            "summary": json.loads(row.summary_json or "{}"),
        }


def main() -> int:
    with SessionLocal() as db:
        persisted = persist_kronos_historical_runs(db, market="CN")
    panel = persisted.pop("panel")
    runner = EventDrivenBacktestRunner()
    common = {
        "top_n": 5,
        "holding_days": 3,
        "commission_bps": 8.0,
        "slippage_bps": 12.0,
        "max_position_weight": 0.2,
        "min_adv": 50_000_000.0,
        "max_gap_pct": 0.08,
        "initial_cash": 1_000_000.0,
    }
    runner.run(
        model_run_id=persisted["baseline_model_run_id"],
        min_signal_score=0.0,
        **common,
    )
    runner.run(
        model_run_id=persisted["kronos_model_run_id"],
        min_signal_score=KRONOS_SUPPORT_SCORE,
        **common,
    )
    result = {
        "protocol": {
            "market": panel.market,
            "model_name": panel.model_name,
            "start_date": panel.start_date,
            "end_date": panel.end_date,
            "signal_date_count": len(panel.signal_dates),
            "snapshot_ids": list(panel.snapshot_ids),
            "decision_counts": panel.decision_counts,
            "top_n": common["top_n"],
            "holding_days": common["holding_days"],
            "support_score_threshold": KRONOS_SUPPORT_SCORE,
            "round_trip_cost_bps": 2 * (common["commission_bps"] + common["slippage_bps"]),
        },
        "baseline": _latest_summary(persisted["baseline_model_run_id"]),
        "kronos": _latest_summary(persisted["kronos_model_run_id"]),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
