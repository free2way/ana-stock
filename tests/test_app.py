import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


class AppFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self._set_test_environment()

        from app.core.config import reset_settings_cache
        from app.core.db import configure_database, init_db

        reset_settings_cache()
        configure_database()
        init_db()

        from app.api.main import app

        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()

        from app.core.config import reset_settings_cache
        from app.core.db import configure_database

        for key in (
            "PQW_STORAGE_DIR",
            "PQW_DATA_DIR",
            "PQW_RAW_DATA_DIR",
            "PQW_NORMALIZED_DATA_DIR",
            "PQW_QLIB_DATA_DIR",
            "PQW_ARTIFACTS_DIR",
            "PQW_SQLITE_PATH",
        ):
            os.environ.pop(key, None)

        reset_settings_cache()
        configure_database()
        self.temp_dir.cleanup()

    def _set_test_environment(self) -> None:
        storage_dir = self.temp_path / "storage"
        data_dir = self.temp_path / "data"
        raw_dir = data_dir / "raw"
        normalized_dir = data_dir / "normalized"
        qlib_dir = data_dir / "qlib"
        artifacts_dir = data_dir / "artifacts"
        sqlite_path = storage_dir / "test.db"

        os.environ["PQW_STORAGE_DIR"] = str(storage_dir)
        os.environ["PQW_DATA_DIR"] = str(data_dir)
        os.environ["PQW_RAW_DATA_DIR"] = str(raw_dir)
        os.environ["PQW_NORMALIZED_DATA_DIR"] = str(normalized_dir)
        os.environ["PQW_QLIB_DATA_DIR"] = str(qlib_dir)
        os.environ["PQW_ARTIFACTS_DIR"] = str(artifacts_dir)
        os.environ["PQW_SQLITE_PATH"] = str(sqlite_path)

    def test_sample_workflow_populates_dashboard_and_symbol_pages(self) -> None:
        from app.services.backtester import BacktestRunner
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seeded = seed_sample_data()
        build_result = build_dataset(normalize_only=True)
        predictions_written = SignalTrainer().train(run_name="sample_flow", signal_type="momentum", lookback_days=3)
        daily_rows_written = BacktestRunner().run(top_n=1)

        self.assertEqual(2, len(seeded))
        self.assertEqual(2, len(build_result["normalized_files"]))
        self.assertGreater(predictions_written, 0)
        self.assertGreater(daily_rows_written, 0)

        summary_response = self.client.get("/dashboard/summary")
        self.assertEqual(200, summary_response.status_code)
        summary = summary_response.json()
        self.assertEqual("sample_flow", summary["latest_model"]["name"])
        self.assertTrue(summary["latest_signals"])
        self.assertTrue(summary["latest_backtest_curve"])

        symbol_response = self.client.get("/symbols/AAPL")
        self.assertEqual(200, symbol_response.status_code)
        self.assertIn("AAPL", symbol_response.text)
        self.assertIn("Back to dashboard", symbol_response.text)

    def test_run_pipeline_job_writes_job_model_and_backtest(self) -> None:
        from app.core.db import SessionLocal
        from app.services.openbb_client import OpenBBClient
        from app.services.repository import BacktestRepository, DataJobRepository, ModelRunRepository

        def fake_fetch(self, request) -> list[dict]:
            symbol = request.ticker
            return [
                {
                    "date": "2026-04-01",
                    "symbol": symbol,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.5,
                    "close": 100.5,
                    "volume": 1000000,
                },
                {
                    "date": "2026-04-02",
                    "symbol": symbol,
                    "open": 100.5,
                    "high": 102.0,
                    "low": 100.0,
                    "close": 101.8,
                    "volume": 1100000,
                },
                {
                    "date": "2026-04-03",
                    "symbol": symbol,
                    "open": 101.8,
                    "high": 103.0,
                    "low": 101.0,
                    "close": 102.6,
                    "volume": 1200000,
                },
            ]

        with patch.object(OpenBBClient, "fetch_historical_prices", new=fake_fetch):
            response = self.client.post(
                "/jobs/run-pipeline",
                data={
                    "tickers": "NVDA,AMD",
                    "provider": "yfinance",
                    "start_date": "2026-04-01",
                    "end_date": "2026-04-03",
                    "run_name": "pipeline_demo",
                    "signal_type": "momentum",
                    "lookback_days": "2",
                    "top_n": "1",
                },
            )

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("success", payload["status"])
        self.assertEqual(2, len(payload["sync_results"]))
        self.assertGreater(payload["predictions_written"], 0)
        self.assertGreater(payload["daily_rows_written"], 0)

        with SessionLocal() as db:
            latest_job = DataJobRepository(db).list_recent_jobs(limit=1)[0]
            latest_model = ModelRunRepository(db).list_recent_runs(limit=1)[0]
            latest_backtest = BacktestRepository(db).get_latest_backtest_summary()

        self.assertEqual("run_pipeline", latest_job["job_type"])
        self.assertEqual("success", latest_job["status"])
        self.assertEqual("pipeline_demo", latest_model["name"])
        self.assertEqual("top_n_pipeline_demo", latest_backtest["name"])

    def test_run_pipeline_redirects_back_to_dashboard(self) -> None:
        from app.services.openbb_client import OpenBBClient

        def fake_fetch(self, request) -> list[dict]:
            symbol = request.ticker
            return [
                {
                    "date": "2026-04-01",
                    "symbol": symbol,
                    "open": 50.0,
                    "high": 51.0,
                    "low": 49.5,
                    "close": 50.5,
                    "volume": 500000,
                },
                {
                    "date": "2026-04-02",
                    "symbol": symbol,
                    "open": 50.5,
                    "high": 52.0,
                    "low": 50.0,
                    "close": 51.4,
                    "volume": 520000,
                },
                {
                    "date": "2026-04-03",
                    "symbol": symbol,
                    "open": 51.4,
                    "high": 53.0,
                    "low": 51.0,
                    "close": 52.1,
                    "volume": 540000,
                },
            ]

        with patch.object(OpenBBClient, "fetch_historical_prices", new=fake_fetch):
            response = self.client.post(
                "/jobs/run-pipeline",
                data={
                    "tickers": "CRM,SHOP",
                    "provider": "yfinance",
                    "run_name": "pipeline_redirect",
                    "signal_type": "reversal",
                    "lookback_days": "3",
                    "top_n": "2",
                    "redirect_to": "/dashboard",
                },
                follow_redirects=False,
            )

        self.assertEqual(303, response.status_code)
        location = response.headers["location"]
        self.assertIn("/dashboard", location)
        self.assertIn("job_status=success", location)


if __name__ == "__main__":
    unittest.main()
