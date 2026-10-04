from __future__ import annotations

from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from app.services.stock_selection.history_backfill import (
    CNHistoryBackfillConfig,
    backfill_cn_stock_selection_history,
    build_cn_history_backfill_plan,
    summarize_cn_history_backfill_plan,
)


class _FakeClient:
    def __init__(self, *, empty_dates: set[str] | None = None) -> None:
        self.empty_dates = empty_dates or set()
        self.calls: list[str] = []
        self.last_error: str | None = None

    def fetch_cn_daily_history_bulk(self, tickers, *, start_date, end_date):
        self.last_error = None
        self.calls.append(start_date)
        if start_date in self.empty_dates:
            return {}
        return {
            ticker: [
                {
                    "date": start_date,
                    "symbol": ticker,
                    "open": 10.0,
                    "high": 10.0,
                    "low": 10.0,
                    "close": 10.0,
                    "volume": 100.0,
                    "adj_close": 10.0,
                    "dividend": None,
                    "split_ratio": None,
                }
            ]
            for ticker in tickers
        }


class StockSelectionHistoryBackfillTests(TestCase):
    def test_plan_prioritizes_latest_undercovered_target_partitions(self) -> None:
        symbols = [f"S{index}" for index in range(10)]
        dates = ["2026-08-14", "2026-08-13", "2026-08-12", "2026-08-11", "2026-08-10"]
        plan = build_cn_history_backfill_plan(
            config=CNHistoryBackfillConfig(
                target_history_sessions=4,
                minimum_partition_coverage=0.60,
            ),
            universe_symbols=symbols,
            trade_dates=dates,
            partition_symbol_counts={
                "2026-08-14": 10,
                "2026-08-13": 6,
                "2026-08-12": 5,
                "2026-08-11": 0,
            },
        )

        self.assertEqual(6, plan.minimum_symbols_per_partition)
        self.assertEqual(("2026-08-12", "2026-08-11"), plan.pending_dates)
        self.assertEqual(0.5, plan.completion_ratio)
        self.assertFalse(plan.ready_for_data_readiness_audit)
        summary = summarize_cn_history_backfill_plan(plan, pending_date_limit=1)
        self.assertEqual(2, summary["pending_partition_count"])
        self.assertEqual(["2026-08-12"], summary["next_pending_dates"])

    def test_execute_repairs_dates_individually_and_is_resumable(self) -> None:
        symbols = {f"S{index}" for index in range(10)}
        dates = ["2026-08-14", "2026-08-13", "2026-08-12"]
        counts = {"2026-08-14": 10, "2026-08-13": 2, "2026-08-12": 2}
        client = _FakeClient()

        def write_rows(*, market, rows, provenance=None):
            counts[str(rows[0]["date"])] = len({row["symbol"] for row in rows})
            return [Path(f"/{rows[0]['date']}.parquet")]

        with patch(
            "app.services.stock_selection.history_backfill.list_lake_symbols",
            return_value=symbols,
        ), patch(
            "app.services.stock_selection.history_backfill.list_lake_trade_dates",
            return_value=dates,
        ), patch(
            "app.services.stock_selection.history_backfill.count_lake_symbols_for_trade_date",
            side_effect=lambda **kwargs: counts[kwargs["trade_date"]],
        ), patch(
            "app.services.stock_selection.history_backfill.write_ohlcv_rows_to_lake",
            side_effect=write_rows,
        ):
            result = backfill_cn_stock_selection_history(
                config=CNHistoryBackfillConfig(
                    target_history_sessions=3,
                    minimum_partition_coverage=0.60,
                    max_dates_per_run=2,
                    inter_date_delay_seconds=0,
                    dry_run=False,
                ),
                client=client,
            )

        self.assertEqual("success", result.status)
        self.assertEqual(["2026-08-13", "2026-08-12"], client.calls)
        self.assertEqual(2, result.completed_date_count)
        self.assertTrue(result.plan_after.ready_for_data_readiness_audit)

    def test_consecutive_provider_failures_stop_the_run(self) -> None:
        symbols = {f"S{index}" for index in range(10)}
        dates = ["2026-08-14", "2026-08-13", "2026-08-12"]
        counts = {trade_date: 1 for trade_date in dates}
        client = _FakeClient(empty_dates=set(dates))
        with patch(
            "app.services.stock_selection.history_backfill.list_lake_symbols",
            return_value=symbols,
        ), patch(
            "app.services.stock_selection.history_backfill.list_lake_trade_dates",
            return_value=dates,
        ), patch(
            "app.services.stock_selection.history_backfill.count_lake_symbols_for_trade_date",
            side_effect=lambda **kwargs: counts[kwargs["trade_date"]],
        ):
            result = backfill_cn_stock_selection_history(
                config=CNHistoryBackfillConfig(
                    target_history_sessions=3,
                    max_dates_per_run=3,
                    maximum_consecutive_failures=2,
                    inter_date_delay_seconds=0,
                    dry_run=False,
                ),
                client=client,
            )

        self.assertEqual("failed", result.status)
        self.assertEqual(2, result.attempted_date_count)
        self.assertEqual(2, result.failed_date_count)

    def test_provider_pagination_error_does_not_write_partial_day(self) -> None:
        class _PartialClient(_FakeClient):
            def fetch_cn_daily_history_bulk(self, tickers, *, start_date, end_date):
                rows = super().fetch_cn_daily_history_bulk(
                    tickers,
                    start_date=start_date,
                    end_date=end_date,
                )
                self.last_error = "rate limit after the first page"
                return {ticker: values for ticker, values in list(rows.items())[:2]}

        symbols = {f"S{index}" for index in range(10)}
        counts = {"2026-08-14": 1}
        with patch(
            "app.services.stock_selection.history_backfill.list_lake_symbols",
            return_value=symbols,
        ), patch(
            "app.services.stock_selection.history_backfill.list_lake_trade_dates",
            return_value=["2026-08-14"],
        ), patch(
            "app.services.stock_selection.history_backfill.count_lake_symbols_for_trade_date",
            side_effect=lambda **kwargs: counts[kwargs["trade_date"]],
        ), patch(
            "app.services.stock_selection.history_backfill.write_ohlcv_rows_to_lake"
        ) as writer:
            result = backfill_cn_stock_selection_history(
                config=CNHistoryBackfillConfig(
                    target_history_sessions=1,
                    max_dates_per_run=1,
                    inter_date_delay_seconds=0,
                    dry_run=False,
                ),
                client=_PartialClient(),
            )

        self.assertEqual("failed", result.status)
        self.assertIn("full trading-date page set", result.date_results[0].error or "")
        writer.assert_not_called()
