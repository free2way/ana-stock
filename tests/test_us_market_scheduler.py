from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from app.services.us_market_scheduler import USMarketSchedulerService


class USMarketSchedulerTests(unittest.TestCase):
    def test_priority_price_sync_keeps_per_symbol_resume_dates(self):
        service = USMarketSchedulerService()
        gaps = [{"ticker": "AAPL", "start_date": "2026-09-21"},
                {"ticker": "MSFT", "start_date": "2025-01-01"}]
        with patch.object(service, "_priority_us_price_gaps", return_value=gaps) as find, \
             patch("app.services.us_market_scheduler.sync_market_data", return_value=[
                 {"ticker": "AAPL", "status": "success", "rows": 3},
                 {"ticker": "MSFT", "status": "failed", "rows": 0}]) as sync:
            result = service._sync_priority_us_prices(target_trade_date="2026-09-23", limit=20)
        find.assert_called_once_with(target_trade_date="2026-09-23", limit=20)
        sync.assert_called_once_with(tickers=["AAPL", "MSFT"], start_date="2025-01-01",
            start_dates_by_ticker={"AAPL": "2026-09-21", "MSFT": "2025-01-01"},
            end_date="2026-09-23", provider="auto")
        self.assertEqual("partial", result["status"])
        self.assertEqual((2, 1, 1), (result["ticker_count"], result["success_count"], result["failure_count"]))

    def test_priority_price_sync_skips_current_symbols(self):
        service = USMarketSchedulerService()
        with patch.object(service, "_priority_us_price_gaps", return_value=[]), \
             patch("app.services.us_market_scheduler.sync_market_data") as sync:
            result = service._sync_priority_us_prices(target_trade_date="2026-09-23", limit=20)
        self.assertEqual("skipped", result["status"])
        sync.assert_not_called()

    def test_priority_price_sync_exposes_provider_failure(self):
        service = USMarketSchedulerService()
        with patch.object(service, "_priority_us_price_gaps", return_value=[
                 {"ticker": "AAPL", "start_date": "2026-09-21"}]), \
             patch("app.services.us_market_scheduler.sync_market_data", side_effect=RuntimeError("fixture failure")):
            result = service._sync_priority_us_prices(target_trade_date="2026-09-23", limit=20)
        self.assertEqual("failed", result["status"])
        self.assertEqual(1, result["failure_count"])
        self.assertIn("fixture failure", result["message"])

    def test_empty_legacy_backtest_does_not_suppress_structured_evaluation(self):
        service = USMarketSchedulerService()
        fake_db = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = fake_db
        context.__exit__.return_value = False
        job_repo = MagicMock()
        job_repo.has_running_job.return_value = False
        job_repo.create_job.return_value = SimpleNamespace(id=309)

        with patch(
            "app.services.us_market_scheduler.list_lake_symbols",
            return_value=["AAPL"],
        ), patch(
            "app.services.us_market_scheduler.build_us_trade_universe",
            return_value=(["AAPL"], {"eligible_count": 1}),
        ) as build_universe, patch(
            "app.services.us_market_scheduler.market_data_gate",
            return_value={"status": "ready"},
        ), patch(
            "app.services.us_market_scheduler.SessionLocal",
            return_value=context,
        ), patch(
            "app.services.us_market_scheduler.DataJobRepository",
            return_value=job_repo,
        ), patch(
            "app.services.us_market_scheduler.SignalTrainer.train",
            return_value=120,
        ), patch(
            "app.services.us_market_scheduler.BacktestRunner.run",
            side_effect=RuntimeError(
                "Backtest produced no daily metrics. The dataset may be too short "
                "or filtered out by tradeability gates."
            ),
        ), patch(
            "app.services.us_market_scheduler.refresh_workspace_snapshots",
        ), patch.object(
            service,
            "_run_structured_evaluation",
        ) as structured_evaluation:
            service._run_signal_training(source_job_id=100, trade_date="2026-09-02")

        completion = job_repo.complete_job.call_args.kwargs
        self.assertEqual("success", completion["status"])
        self.assertEqual("empty", completion["result"]["legacy_backtest_status"])
        self.assertEqual(0, completion["result"]["daily_rows_written"])
        structured_evaluation.assert_called_once_with(source_job_id=309)
        build_universe.assert_called_once_with(
            tickers=["AAPL"],
            expected_as_of_date="2026-09-02",
            include_summary=True,
        )

    def test_blocked_data_gate_prevents_us_training(self):
        service = USMarketSchedulerService()
        fake_db = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = fake_db
        context.__exit__.return_value = False
        job_repo = MagicMock()
        job_repo.has_running_job.return_value = False
        job_repo.create_job.return_value = SimpleNamespace(id=318)

        with patch(
            "app.services.us_market_scheduler.list_lake_symbols",
            return_value=["AAPL"],
        ), patch(
            "app.services.us_market_scheduler.build_us_trade_universe",
            return_value=(["AAPL"], {"eligible_count": 1}),
        ), patch(
            "app.services.us_market_scheduler.SessionLocal",
            return_value=context,
        ), patch(
            "app.services.us_market_scheduler.DataJobRepository",
            return_value=job_repo,
        ), patch(
            "app.services.us_market_scheduler.market_data_gate",
            return_value={"status": "blocked", "message": "US input blocked"},
        ), patch(
            "app.services.us_market_scheduler.SignalTrainer.train",
        ) as train:
            service._run_signal_training(source_job_id=101, trade_date="2026-09-08")

        train.assert_not_called()
        completion = job_repo.complete_job.call_args.kwargs
        self.assertEqual("failed", completion["status"])
        self.assertEqual("US input blocked", completion["message"])

    def test_post_close_stages_rebuild_adjusted_view_before_training(self):
        service = USMarketSchedulerService()
        order: list[str] = []

        def stage(name: str):
            def _inner(*args, **kwargs):
                order.append(name)
                return {"stage": name, "status": "success"}

            return _inner

        with patch.object(service, "_run_risk_guardrail", side_effect=stage("risk_guardrail")), patch.object(
            service, "_refresh_us_adjusted_view", side_effect=stage("us_adjusted_view")
        ), patch.object(
            service, "_run_signal_training", side_effect=stage("signal_training")
        ), patch.object(
            service, "_run_model_calibration_snapshot", side_effect=stage("model_calibration_snapshot")
        ), patch.object(
            service, "_run_screener_precompute", side_effect=stage("screener_precompute")
        ):
            service._run_post_close_stages(source_job_id=1, trade_date="2026-10-08")

        self.assertEqual(
            [
                "risk_guardrail",
                "us_adjusted_view",
                "signal_training",
                "model_calibration_snapshot",
                "screener_precompute",
            ],
            order,
        )

    def test_calibration_stage_persists_us_partition(self):
        service = USMarketSchedulerService()
        fake_db = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = fake_db
        context.__exit__.return_value = False
        job_repo = MagicMock()
        job_repo.has_running_job.return_value = False
        job_repo.create_job.return_value = SimpleNamespace(id=412)

        with patch(
            "app.services.us_market_scheduler.SessionLocal", return_value=context
        ), patch(
            "app.services.us_market_scheduler.DataJobRepository", return_value=job_repo
        ), patch(
            "app.services.us_market_scheduler.save_model_calibration_snapshot",
            return_value={
                "status": "success",
                "written_markets": ["US"],
                "reused_markets": [],
                "snapshot_id": 9001,
                "message": "ok",
            },
        ) as persist:
            service._run_model_calibration_snapshot(source_job_id=100, trade_date="2026-10-08")

        persist.assert_called_once_with(markets=["US"], source_job_id=412)
        created = job_repo.create_job.call_args.kwargs
        self.assertEqual("model_calibration_snapshot", created["job_type"])
        self.assertEqual(["US"], created["params"]["markets"])
        self.assertEqual("success", job_repo.complete_job.call_args.kwargs["status"])

    def test_calibration_stage_is_idempotent_when_already_running(self):
        service = USMarketSchedulerService()
        fake_db = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = fake_db
        context.__exit__.return_value = False
        job_repo = MagicMock()
        job_repo.has_running_job.return_value = True

        with patch(
            "app.services.us_market_scheduler.SessionLocal", return_value=context
        ), patch(
            "app.services.us_market_scheduler.DataJobRepository", return_value=job_repo
        ), patch(
            "app.services.us_market_scheduler.save_model_calibration_snapshot"
        ) as persist:
            service._run_model_calibration_snapshot(source_job_id=100, trade_date="2026-10-08")

        job_repo.create_job.assert_not_called()
        persist.assert_not_called()

    def test_adjusted_view_stage_skips_when_view_is_current(self):
        service = USMarketSchedulerService()
        with patch(
            "app.services.us_market_scheduler.rebuild_adjusted_view_if_stale",
            return_value={"status": "skipped", "latest_date": "2026-10-08", "required_date": "2026-10-08"},
        ) as rebuild, patch(
            "app.services.us_market_scheduler.us_adjusted_raw_glob",
            return_value="data/lake/_us_alpaca/raw/*.parquet",
        ):
            result = service._refresh_us_adjusted_view(source_job_id=7, trade_date="2026-10-08")

        self.assertEqual("skipped", result["status"])
        self.assertEqual("us_adjusted_view", result["stage"])
        rebuild.assert_called_once_with(
            "US",
            method="qfq",
            raw_glob="data/lake/_us_alpaca/raw/*.parquet",
            required_upper_bound="2026-10-08",
        )

    def test_adjusted_view_stage_contains_failure(self):
        service = USMarketSchedulerService()
        with patch(
            "app.services.us_market_scheduler.rebuild_adjusted_view_if_stale",
            side_effect=RuntimeError("raw namespace unreadable"),
        ), patch(
            "app.services.us_market_scheduler.us_adjusted_raw_glob",
            return_value="data/lake/_us_alpaca/raw/*.parquet",
        ):
            result = service._refresh_us_adjusted_view(source_job_id=7, trade_date="2026-10-08")

        self.assertEqual("failed", result["status"])
        self.assertIn("raw namespace unreadable", result["message"])


if __name__ == "__main__":
    unittest.main()
