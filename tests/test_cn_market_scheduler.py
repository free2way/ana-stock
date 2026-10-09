from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.cn_market_scheduler import (
    CNMarketSchedulerService,
    _post_refresh_ready,
    _refresh_cn_price_lake,
)


class CNMarketSchedulerTests(unittest.TestCase):
    def test_partial_refresh_with_current_lake_starts_post_close_pipeline(self) -> None:
        self.assertTrue(
            _post_refresh_ready(
                {"status": "partial"},
                target_trade_date="2026-07-23",
                latest_lake_trade_date="2026-07-23",
            )
        )

    def test_stale_lake_never_starts_post_close_pipeline(self) -> None:
        self.assertFalse(
            _post_refresh_ready(
                {"status": "success"},
                target_trade_date="2026-07-23",
                latest_lake_trade_date="2026-07-22",
            )
        )

    def test_failed_refresh_never_starts_post_close_pipeline(self) -> None:
        self.assertFalse(
            _post_refresh_ready(
                {"status": "failed"},
                target_trade_date="2026-07-23",
                latest_lake_trade_date="2026-07-23",
            )
        )

    def test_current_hithink_dump_is_preferred_without_per_symbol_refresh(self) -> None:
        dump = {
            "status": "success",
            "last_trade_date": "2026-07-23",
            "latest_symbol_count": 5540,
            "rows_written": 55400,
        }
        with patch(
            "app.services.cn_market_scheduler.get_settings",
            return_value=SimpleNamespace(
                hithink_finance_daily_dump_enabled=True,
                hithink_finance_api_key="fixture",
            ),
        ), patch(
            "app.services.cn_market_scheduler.import_hithink_market_dump",
            return_value=dump,
        ), patch(
            "app.services.cn_market_scheduler.refresh_cn_market_data_lake_only"
        ) as fallback:
            result = _refresh_cn_price_lake("2026-07-23")

        self.assertEqual("hithink_finance_dump", result["provider_used"])
        self.assertEqual(5540, result["success_count"])
        fallback.assert_not_called()

    def test_stale_hithink_dump_falls_back_to_existing_cn_refresh(self) -> None:
        with patch(
            "app.services.cn_market_scheduler.get_settings",
            return_value=SimpleNamespace(
                hithink_finance_daily_dump_enabled=True,
                hithink_finance_api_key="fixture",
            ),
        ), patch(
            "app.services.cn_market_scheduler.import_hithink_market_dump",
            return_value={"status": "success", "last_trade_date": "2026-07-22"},
        ), patch(
            "app.services.cn_market_scheduler.refresh_cn_market_data_lake_only",
            return_value={"status": "success", "providers_attempted": ["tushare_lake"]},
        ) as fallback:
            result = _refresh_cn_price_lake("2026-07-23")

        fallback.assert_called_once_with(start_date="2026-07-23", end_date="2026-07-23")
        self.assertEqual(["hithink_finance_dump", "tushare_lake"], result["providers_attempted"])

    def test_structured_evaluation_persists_storage_acceptance_progress(self) -> None:
        service = CNMarketSchedulerService()
        fake_db = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = fake_db
        context.__exit__.return_value = False
        job_repo = MagicMock()
        evaluation = {"status": "success", "message": "Evaluation complete."}
        acceptance = {
            "status": "pending",
            "required_runs": 5,
            "passed_runs": [301],
        }
        with patch.object(service, "_create_stage_job", return_value=99), patch(
            "app.services.cn_market_scheduler.SessionLocal",
            return_value=context,
        ), patch(
            "app.services.cn_market_scheduler.evaluate_model_runs",
            return_value=evaluation,
        ), patch(
            "app.services.cn_market_scheduler.audit_recent_compact_dual_writes",
            return_value=acceptance,
        ) as audit, patch(
            "app.services.cn_market_scheduler.DataJobRepository",
            return_value=job_repo,
        ):
            service._run_structured_evaluation(source_job_id=77)

        audit.assert_called_once_with(
            fake_db,
            market="CN",
            required_runs=5,
            scan_limit=50,
        )
        completed = job_repo.complete_job.call_args.kwargs
        self.assertEqual("success", completed["status"])
        self.assertIn("1/5 consecutive trading days", completed["message"])
        self.assertEqual(
            acceptance,
            completed["result"]["prediction_storage_acceptance"],
        )

    def test_cn_risk_guardrail_does_not_process_us_as_side_effect(self) -> None:
        service = CNMarketSchedulerService()
        fake_db = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = fake_db
        context.__exit__.return_value = False
        job_repo = MagicMock()
        job_repo.has_running_job.return_value = False
        job_repo.create_job.return_value = SimpleNamespace(id=88)
        with patch(
            "app.services.cn_market_scheduler.SessionLocal",
            return_value=context,
        ), patch(
            "app.services.cn_market_scheduler.DataJobRepository",
            return_value=job_repo,
        ), patch(
            "app.services.cn_market_scheduler.save_risk_guardrail_snapshots",
            return_value={"status": "success"},
        ) as save:
            service._run_risk_guardrail(source_job_id=77)

        save.assert_called_once_with(
            fake_db,
            source_job_id=88,
            markets=["CN"],
        )
        created = job_repo.create_job.call_args.kwargs
        self.assertEqual(["CN"], created["params"]["markets"])


    def test_market_workspace_refresh_stage_rebuilds_heatmap_snapshot(self) -> None:
        service = CNMarketSchedulerService()
        fake_db = MagicMock()
        context = MagicMock()
        context.__enter__.return_value = fake_db
        context.__exit__.return_value = False
        created = {
            "market_heatmap_workspace": {"id": 7, "snapshot_date": "2026-10-09"},
            "market_workspace": {"id": 8, "snapshot_date": "2026-10-09"},
        }
        with patch(
            "app.services.cn_market_scheduler.SessionLocal",
            return_value=context,
        ), patch(
            "app.services.cn_market_scheduler.save_market_workspace_snapshots",
            return_value=created,
        ) as save:
            result = service._refresh_market_workspace_snapshots(source_job_id=123)

        save.assert_called_once_with(fake_db, source_job_id=123)
        self.assertEqual("success", result["status"])
        self.assertIn("market_heatmap_workspace", result["snapshots"])

    def test_post_close_pipeline_rebuilds_market_workspace_before_daily_report(self) -> None:
        service = CNMarketSchedulerService()
        order: list[str] = []

        def stage(name: str):
            def _inner(*args, **kwargs):
                order.append(name)
                return {"stage": name, "status": "success"}

            return _inner

        context = MagicMock()
        with patch.object(service, "_refresh_cn_adjusted_view", side_effect=stage("cn_adjusted_view")), patch.object(
            service, "_run_signal_training", side_effect=stage("training")
        ), patch.object(
            service, "_run_screener_precompute_core", side_effect=stage("core")
        ), patch.object(service, "_run_screener_precompute_rest", side_effect=stage("rest")), patch.object(
            service, "_run_screener_precompute_combos", side_effect=stage("combos")
        ), patch.object(
            service, "_refresh_market_workspace_snapshots", side_effect=stage("market_workspace_snapshots")
        ), patch.object(
            service, "_run_ai_daily_report_delivery", side_effect=stage("ai_daily_report")
        ), patch(
            "app.services.cn_market_scheduler.SessionLocal", return_value=context
        ), patch(
            "app.services.cn_market_scheduler.DataJobRepository"
        ):
            service._run_post_close_pipeline(pipeline_job_id=1, source_job_id=2, trade_date="2026-10-09")

        self.assertEqual(
            [
                "cn_adjusted_view",
                "training",
                "core",
                "rest",
                "combos",
                "market_workspace_snapshots",
                "ai_daily_report",
            ],
            order,
        )

    def test_cn_adjusted_view_stage_skips_when_view_is_current(self) -> None:
        service = CNMarketSchedulerService()
        with patch(
            "app.services.cn_market_scheduler.rebuild_adjusted_view_if_stale",
            return_value={"status": "skipped", "latest_date": "2026-10-09", "required_date": "2026-10-09"},
        ) as rebuild:
            result = service._refresh_cn_adjusted_view(source_job_id=7, trade_date="2026-10-09")

        # An idempotent skip is a usable stage (not a pipeline failure).
        self.assertEqual("success", result["status"])
        self.assertEqual("skipped", result["view_status"])
        self.assertEqual("cn_adjusted_view", result["stage"])
        self.assertEqual("2026-10-09", result["latest_date"])
        rebuild.assert_called_once_with(
            "CN",
            method="qfq",
            required_upper_bound="2026-10-09",
        )

    def test_cn_adjusted_view_stage_reports_rebuilt_as_usable(self) -> None:
        service = CNMarketSchedulerService()
        with patch(
            "app.services.cn_market_scheduler.rebuild_adjusted_view_if_stale",
            return_value={"status": "rebuilt", "symbols": 5583, "rows": 2076859},
        ):
            result = service._refresh_cn_adjusted_view(source_job_id=7, trade_date="2026-10-09")

        self.assertEqual("success", result["status"])
        self.assertEqual("rebuilt", result["view_status"])
        self.assertEqual(2076859, result["rows"])

    def test_cn_adjusted_view_stage_contains_failure(self) -> None:
        service = CNMarketSchedulerService()
        with patch(
            "app.services.cn_market_scheduler.rebuild_adjusted_view_if_stale",
            side_effect=RuntimeError("raw lake unreadable"),
        ):
            result = service._refresh_cn_adjusted_view(source_job_id=7, trade_date="2026-10-09")

        self.assertEqual("failed", result["status"])
        self.assertEqual("cn_adjusted_view", result["stage"])
        self.assertIn("raw lake unreadable", result["message"])


if __name__ == "__main__":
    unittest.main()
