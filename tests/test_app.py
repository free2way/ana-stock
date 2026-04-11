import os
import tempfile
import unittest
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
from fastapi.testclient import TestClient
from app.services.tushare_client import CNFundamentalRow
from app.models.schema import SymbolCreate
from app.services.database_migration import DatabaseMigrationService
from app.services.repository import DashboardReadRepository


class AppFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self._set_test_environment()
        from app.services.runtime_cache import clear_namespace

        from app.core.config import reset_settings_cache
        from app.core.db import configure_database, init_db

        for namespace in (
            "safe_symbol_analysis",
            "market_headlines",
            "symbol_headlines",
            "market_snapshot",
            "tradingview_rating",
            "tradingview_multi_timeframe",
            "symbol_page_bundle",
            "dashboard_home_panels",
            "dashboard_watchlist_map",
            "dashboard_focus_items",
            "dashboard_summary",
            "dashboard_recent_jobs",
            "ai_symbol_analysis",
            "watchlist_items",
            "watchlist_analysis_fragment",
            "watchlist_table_fragment",
        ):
            clear_namespace(namespace)
        reset_settings_cache()
        configure_database()
        init_db()

        from app.api.main import app

        self.client = TestClient(app)
        self._login()

    def tearDown(self) -> None:
        self.client.close()
        from app.services.runtime_cache import clear_namespace

        from app.core.config import reset_settings_cache
        from app.core.db import configure_database

        for namespace in (
            "safe_symbol_analysis",
            "market_headlines",
            "symbol_headlines",
            "market_snapshot",
            "tradingview_rating",
            "tradingview_multi_timeframe",
            "symbol_page_bundle",
            "dashboard_home_panels",
            "dashboard_watchlist_map",
            "dashboard_focus_items",
            "dashboard_summary",
            "dashboard_recent_jobs",
            "ai_symbol_analysis",
            "watchlist_items",
            "watchlist_analysis_fragment",
            "watchlist_table_fragment",
        ):
            clear_namespace(namespace)
        for key in (
            "PQW_STORAGE_DIR",
            "PQW_DATA_DIR",
            "PQW_RAW_DATA_DIR",
            "PQW_NORMALIZED_DATA_DIR",
            "PQW_QLIB_DATA_DIR",
            "PQW_ARTIFACTS_DIR",
            "PQW_SQLITE_PATH",
            "PQW_TUSHARE_TOKEN",
            "PQW_DATABASE_URL",
            "PQW_POSTGRES_POOL_SIZE",
            "PQW_POSTGRES_MAX_OVERFLOW",
            "PQW_POSTGRES_POOL_TIMEOUT_SECONDS",
            "PQW_POSTGRES_POOL_RECYCLE_SECONDS",
            "PQW_POSTGRES_CONNECT_TIMEOUT_SECONDS",
            "PQW_POSTGRES_STATEMENT_TIMEOUT_MS",
            "PQW_POSTGRES_IDLE_TRANSACTION_TIMEOUT_MS",
            "PQW_POSTGRES_APPLICATION_NAME",
            "PQW_AUTH_USERNAME",
            "PQW_AUTH_PASSWORD",
            "PQW_AUTH_SECRET",
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
        os.environ["PQW_DATABASE_URL"] = ""
        os.environ["PQW_AUTH_USERNAME"] = "admin"
        os.environ["PQW_AUTH_PASSWORD"] = "admin1234"
        os.environ["PQW_AUTH_SECRET"] = "test-secret"

    def _login(self) -> None:
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "admin1234", "next": "/watchlist"},
            follow_redirects=False,
        )
        self.assertEqual(303, response.status_code)

    def _seed_symbol(self, ticker: str, name: str, market: str, exchange: str) -> None:
        from app.core.db import SessionLocal
        from app.services.repository import SymbolRepository

        with SessionLocal() as db:
            SymbolRepository(db).get_or_create_symbol(
                SymbolCreate(ticker=ticker, name=name, market=market, exchange=exchange)
            )

    def _write_price_history(self, ticker: str, rows: list[dict]) -> None:
        raw_path = self.temp_path / "data" / "raw" / f"{ticker}.csv"
        normalized_path = self.temp_path / "data" / "normalized" / f"{ticker}.csv"
        frame = pd.DataFrame(rows)
        frame.to_csv(raw_path, index=False)
        frame.to_csv(normalized_path, index=False)

    def _build_bullish_cn_history(self) -> list[dict]:
        rows: list[dict] = []
        for index in range(70):
            close = 10.0 + index * 0.22
            rows.append(
                {
                    "date": f"2026-02-{(index % 28) + 1:02d}" if index < 28 else f"2026-03-{((index - 28) % 31) + 1:02d}" if index < 59 else f"2026-04-{(index - 58):02d}",
                    "open": round(close - 0.08, 2),
                    "high": round(close + 0.12, 2),
                    "low": round(close - 0.18, 2),
                    "close": round(close, 2),
                    "volume": 1000000 + index * 12000,
                    "adj_close": round(close, 2),
                }
            )
        return rows

    def _build_squeeze_cn_history(self) -> list[dict]:
        rows: list[dict] = []
        base_close = 10.0
        for index in range(37):
            drift = ((index % 5) - 2) * 0.01
            close = round(base_close + drift, 2)
            open_value = round(close - 0.02 if index % 2 == 0 else close + 0.01, 2)
            high = round(max(open_value, close) + 0.05, 2)
            low = round(min(open_value, close) - 0.05, 2)
            rows.append(
                {
                    "date": f"2026-03-{index + 1:02d}",
                    "open": open_value,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": 1000000 + index * 5000,
                    "adj_close": close,
                }
            )
        trailing = [
            ("2026-04-07", 10.02, 10.14, 9.99, 10.12, 1320000),
            ("2026-04-08", 10.11, 10.25, 10.08, 10.23, 1460000),
            ("2026-04-09", 10.22, 10.36, 10.18, 10.34, 1580000),
        ]
        for trade_date, open_value, high, low, close, volume in trailing:
            rows.append(
                {
                    "date": trade_date,
                    "open": open_value,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "adj_close": close,
                }
            )
        return rows

    def _build_bullish_engulfing_history(self) -> list[dict]:
        rows: list[dict] = []
        for index in range(36):
            close = round(12.0 - index * 0.04, 2)
            rows.append(
                {
                    "date": f"2026-03-{index + 1:02d}",
                    "open": round(close + 0.06, 2),
                    "high": round(close + 0.12, 2),
                    "low": round(close - 0.12, 2),
                    "close": close,
                    "volume": 900000 + index * 4000,
                    "adj_close": close,
                }
            )
        rows.extend(
            [
                {"date": "2026-04-06", "open": 10.55, "high": 10.58, "low": 10.18, "close": 10.24, "volume": 1180000, "adj_close": 10.24},
                {"date": "2026-04-07", "open": 10.18, "high": 10.68, "low": 10.12, "close": 10.62, "volume": 1360000, "adj_close": 10.62},
            ]
        )
        return rows

    def _build_hammer_reversal_history(self) -> list[dict]:
        rows: list[dict] = []
        for index in range(34):
            close = round(11.8 - index * 0.05, 2)
            rows.append(
                {
                    "date": f"2026-03-{index + 1:02d}",
                    "open": round(close + 0.05, 2),
                    "high": round(close + 0.1, 2),
                    "low": round(close - 0.1, 2),
                    "close": close,
                    "volume": 820000 + index * 3500,
                    "adj_close": close,
                }
            )
        rows.extend(
            [
                {"date": "2026-04-05", "open": 10.28, "high": 10.33, "low": 10.02, "close": 10.08, "volume": 1020000, "adj_close": 10.08},
                {"date": "2026-04-06", "open": 10.06, "high": 10.14, "low": 9.62, "close": 10.12, "volume": 1280000, "adj_close": 10.12},
            ]
        )
        return rows

    def test_login_page_and_protected_redirect(self) -> None:
        fresh_client = TestClient(self.client.app)
        try:
            login_page = fresh_client.get("/login")
            self.assertEqual(200, login_page.status_code)
            self.assertNotIn("admin1234", login_page.text)
            self.assertNotIn('value="admin"', login_page.text)
            self.assertIn('name="next" value="/dashboard"', login_page.text)

            protected = fresh_client.get("/dashboard", follow_redirects=False)
            self.assertEqual(303, protected.status_code)
            self.assertIn("/login", protected.headers["location"])

            login_submit = fresh_client.post(
                "/login",
                data={"username": "admin", "password": "admin1234", "next": "/watchlist"},
                follow_redirects=False,
            )
            self.assertEqual(303, login_submit.status_code)
            self.assertEqual("/watchlist", login_submit.headers["location"])

            default_login_submit = fresh_client.post(
                "/login",
                data={"username": "admin", "password": "admin1234"},
                follow_redirects=False,
            )
            self.assertEqual(303, default_login_submit.status_code)
            self.assertEqual("/dashboard", default_login_submit.headers["location"])

            external_redirect_submit = fresh_client.post(
                "/login",
                data={"username": "admin", "password": "admin1234", "next": "https://evil.example/phish"},
                follow_redirects=False,
            )
            self.assertEqual(303, external_redirect_submit.status_code)
            self.assertEqual("/dashboard", external_redirect_submit.headers["location"])

            forged_client = TestClient(self.client.app)
            try:
                forged_client.cookies.set("pqw_auth", "admin")
                forged = forged_client.get("/dashboard", follow_redirects=False)
                self.assertEqual(303, forged.status_code)
                self.assertIn("/login", forged.headers["location"])
            finally:
                forged_client.close()
        finally:
            fresh_client.close()

    def test_json_and_mutation_routes_require_authentication(self) -> None:
        fresh_client = TestClient(self.client.app)
        try:
            protected_gets = [
                "/symbols",
                "/symbols/600000.SS",
                "/symbols/600000.SS/combined-analysis",
                "/symbols/600000.SS/page-bundle",
                "/signals/latest",
                "/backtests",
                "/backtests/latest/curve",
                "/jobs/recent",
                "/jobs/sync-states",
            ]
            for path in protected_gets:
                response = fresh_client.get(path, follow_redirects=False)
                self.assertEqual(303, response.status_code, path)
                self.assertIn("/login", response.headers["location"], path)

            create_response = fresh_client.post(
                "/symbols",
                json={"ticker": "TEST.SS", "name": "Test", "market": "CN", "exchange": "SSE"},
                follow_redirects=False,
            )
            self.assertEqual(303, create_response.status_code)
            self.assertIn("/login", create_response.headers["location"])
        finally:
            fresh_client.close()

    def test_sample_workflow_populates_dashboard_and_symbol_pages(self) -> None:
        from app.services.backtester import BacktestRunner
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seeded = seed_sample_data()
        build_result = build_dataset(normalize_only=True)
        predictions_written = SignalTrainer().train(run_name="sample_flow", signal_type="momentum", lookback_days=3)
        daily_rows_written = BacktestRunner().run(top_n=1)

        self.assertGreaterEqual(len(seeded), 3)
        self.assertGreaterEqual(len(build_result["normalized_files"]), 3)
        self.assertGreater(predictions_written, 0)
        self.assertGreater(daily_rows_written, 0)

        summary_response = self.client.get("/dashboard/summary")
        self.assertEqual(200, summary_response.status_code)
        summary = summary_response.json()
        self.assertEqual("sample_flow", summary["latest_model"]["name"])
        self.assertTrue(summary["latest_signals"])
        self.assertTrue(summary["latest_backtest_curve"])
        self.assertIn("data_sources", summary)
        self.assertIn("historical_price_strategy", summary["data_sources"])
        self.assertIn("concept_data", summary["data_sources"])
        self.assertIn("freshness", summary["data_sources"]["concept_data"])

        symbol_response = self.client.get("/symbols/AAPL")
        self.assertEqual(200, symbol_response.status_code)
        self.assertIn("AAPL", symbol_response.text)
        self.assertIn("Back to dashboard", symbol_response.text)

        data_sources_response = self.client.get("/dashboard/data-sources")
        self.assertEqual(200, data_sources_response.status_code)
        self.assertIn("text/html", data_sources_response.headers.get("content-type", ""))
        self.assertIn("Where This App Gets Data", data_sources_response.text)
        self.assertIn("Per Symbol Sync Source", data_sources_response.text)
        self.assertIn("CN Concepts", data_sources_response.text)

        insight_response = self.client.get("/insights/ASTS")
        self.assertEqual(200, insight_response.status_code)
        self.assertIn("Trend Score", insight_response.text)
        self.assertIn("Buy Zone", insight_response.text)
        self.assertIn("Action Now", insight_response.text)
        self.assertIn("Volume Strength", insight_response.text)
        self.assertIn("ASTS", insight_response.text)

        insight_zh_response = self.client.get("/insights/ASTS?lang=zh")
        self.assertEqual(200, insight_zh_response.status_code)
        self.assertIn("趋势评分", insight_zh_response.text)
        self.assertIn("买入观察区", insight_zh_response.text)

    def test_dashboard_summary_includes_market_context(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer
        from app.core.db import SessionLocal
        from app.services.repository import ConceptSnapshotRepository, SymbolRepository

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="dashboard_context_demo", signal_type="momentum", lookback_days=3)
        with SessionLocal() as db:
            symbol = SymbolRepository(db).get_by_ticker("AAPL")
            self.assertIsNotNone(symbol)
            ConceptSnapshotRepository(db).upsert_snapshot(
                symbol_id=symbol.id,
                concept_name="Consumer Electronics",
                concept_code="C001",
                as_of_date="2026-04-03",
                source="test",
            )

        response = self.client.get("/dashboard/summary")
        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertIn("market_context", payload)
        self.assertIn("market_distribution", payload["market_context"])
        self.assertIn("top_concepts", payload["market_context"])
        self.assertIn("concept_tracker", payload["market_context"])
        self.assertIn("continuous_leaders", payload["market_context"])
        self.assertIn("risk_overview", payload["market_context"])
        if payload["market_context"]["top_concepts"]:
            self.assertIn("avg_move_5d", payload["market_context"]["top_concepts"][0])
            self.assertIn("breadth_pct", payload["market_context"]["top_concepts"][0])
            self.assertIn("ticker_details", payload["market_context"]["top_concepts"][0])
            if payload["market_context"]["top_concepts"][0]["ticker_details"]:
                self.assertIn("state", payload["market_context"]["top_concepts"][0]["ticker_details"][0])
                self.assertIn("confidence", payload["market_context"]["top_concepts"][0]["ticker_details"][0])
                self.assertIn("signal_label", payload["market_context"]["top_concepts"][0]["ticker_details"][0])
                self.assertIn("signal_strength", payload["market_context"]["top_concepts"][0]["ticker_details"][0])
        if payload["market_context"]["continuous_leaders"]:
            self.assertIn("state", payload["market_context"]["continuous_leaders"][0])
            self.assertIn("confidence", payload["market_context"]["continuous_leaders"][0])
            self.assertIn("signal_label", payload["market_context"]["continuous_leaders"][0])
            self.assertIn("signal_strength", payload["market_context"]["continuous_leaders"][0])
        self.assertIn("tagged_names", payload["market_context"]["risk_overview"])
        self.assertIn("top_tags", payload["market_context"]["risk_overview"])

    def test_dashboard_page_supports_lookback_snapshot_window(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer
        from app.core.db import SessionLocal
        from app.services.repository import ConceptSnapshotRepository, SymbolRepository

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="dashboard_lookback_demo", signal_type="momentum", lookback_days=3)
        with SessionLocal() as db:
            symbol = SymbolRepository(db).get_by_ticker("AAPL")
            self.assertIsNotNone(symbol)
            ConceptSnapshotRepository(db).upsert_snapshot(
                symbol_id=symbol.id,
                concept_name="Consumer Electronics",
                concept_code="C001",
                as_of_date="2026-04-03",
                source="test",
            )

        response = self.client.get("/dashboard?lookback_runs=3")
        self.assertEqual(200, response.status_code)
        self.assertIn("Snapshot Window", response.text)
        self.assertIn("Continuous Leaders", response.text)

    def test_dashboard_page_supports_chinese_language(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="dashboard_zh_demo", signal_type="momentum", lookback_days=3)

        with patch(
            "app.api.routes.dashboard.MarketNewsService.fetch_headlines",
            return_value=[{"source": "Reuters Markets", "title": "Asia equities hold gains into the close", "link": "https://example.com/asia"}],
        ):
            response = self.client.get("/dashboard?lang=zh&lookback_runs=3&mode=postmarket")

        self.assertEqual(200, response.status_code)
        self.assertIn("个人量化工作台", response.text)
        self.assertIn("风险概览", response.text)
        self.assertIn("连续强势股", response.text)
        self.assertIn("市场脉冲", response.text)
        self.assertIn("运维操作台", response.text)
        self.assertIn("打开市场脉冲页", response.text)
        self.assertIn("会话模式", response.text)
        self.assertIn("盘后复盘", response.text)
        self.assertIn("打开市场快照榜单", response.text)
        self.assertIn("今日行动板", response.text)
        self.assertIn("今日投研流程", response.text)
        self.assertIn("市场叙事", response.text)
        self.assertIn("持仓账本", response.text)
        self.assertIn("3 次", response.text)
        self.assertIn("lookback_runs=3", response.text)
        self.assertIn("dashboard-home-panels", response.text)
        self.assertIn("dashboard-top-panels", response.text)

    def test_dashboard_home_panels_fragment_renders_market_headlines(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="dashboard_fragment_demo", signal_type="momentum", lookback_days=3)

        with patch(
            "app.api.routes.dashboard.MarketNewsService.fetch_headlines",
            return_value=[{"source": "Reuters Markets", "title": "Asia equities hold gains into the close", "link": "https://example.com/asia"}],
        ):
            response = self.client.get("/dashboard/home-panels-fragment?lang=zh&lookback_runs=3&mode=postmarket")

        self.assertEqual(200, response.status_code)
        self.assertIn("今日行动板", response.text)
        self.assertIn("市场叙事", response.text)
        self.assertIn("最近任务状态", response.text)
        self.assertIn("Asia equities hold gains into the close", response.text)

    def test_dashboard_top_fragment_renders_risk_and_signals(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="dashboard_top_fragment_demo", signal_type="momentum", lookback_days=3)

        response = self.client.get("/dashboard/top-fragment?lang=zh&lookback_runs=3")

        self.assertEqual(200, response.status_code)
        self.assertIn("风险概览", response.text)
        self.assertIn("最新信号", response.text)
        self.assertIn("ASTS", response.text)

    def test_dashboard_home_panels_are_cached_between_requests(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="dashboard_cache_demo", signal_type="momentum", lookback_days=3)

        with patch(
            "app.api.routes.dashboard.ScreenerService.build_market_snapshot",
            return_value=[],
        ) as snapshot_mock, patch(
            "app.api.routes.dashboard.MarketNewsService.fetch_headlines",
            return_value=[],
        ) as headline_mock:
            first = self.client.get("/dashboard/home-panels-fragment?lang=zh&lookback_runs=3&mode=monitor")
            second = self.client.get("/dashboard/home-panels-fragment?lang=zh&lookback_runs=3&mode=monitor")

        self.assertEqual(200, first.status_code)
        self.assertEqual(200, second.status_code)
        self.assertEqual(1, snapshot_mock.call_count)
        self.assertEqual(1, headline_mock.call_count)

    def test_dashboard_summary_is_cached_between_requests(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="dashboard_summary_cache_demo", signal_type="momentum", lookback_days=3)

        with patch(
            "app.api.routes.dashboard._build_market_context",
            return_value={"concept_board": [], "continuous_leaders": [], "risk_overview": {}},
        ) as market_context_mock:
            first = self.client.get("/dashboard/summary?lookback_runs=3")
            second = self.client.get("/dashboard/summary?lookback_runs=3")

        self.assertEqual(200, first.status_code)
        self.assertEqual(200, second.status_code)
        self.assertEqual(1, market_context_mock.call_count)

    def test_dashboard_watchlist_derived_context_is_cached_between_requests(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="dashboard_watchlist_cache_demo", signal_type="momentum", lookback_days=3)

        with patch(
            "app.api.routes.dashboard.WatchlistRepository.list_ticker_map",
            return_value={},
        ) as map_mock, patch(
            "app.api.routes.dashboard.load_today_focus_pool",
            return_value=[],
        ) as focus_mock:
            first = self.client.get("/dashboard?lang=zh&lookback_runs=3&mode=monitor")
            second = self.client.get("/dashboard?lang=zh&lookback_runs=3&mode=monitor")

        self.assertEqual(200, first.status_code)
        self.assertEqual(200, second.status_code)
        self.assertEqual(1, map_mock.call_count)
        self.assertEqual(1, focus_mock.call_count)

    def test_dashboard_ai_daily_report_page_renders(self) -> None:
        from app.services.ai_daily_report import save_ai_daily_report

        save_ai_daily_report(
            {
                "status": "success",
                "mood": "偏进攻",
                "headline": "今日 AI 决策面板偏进攻",
                "strategy": {
                    "headline": "市场更适合围绕强势股做顺势交易",
                    "playbook": "优先做高置信度 BUY 标的。",
                    "bullets": ["优先跟踪: ASTS / AAPL"],
                },
                "rows": [
                    {
                        "ticker": "ASTS",
                        "name": "AST SpaceMobile",
                        "verdict": "BUY",
                        "confidence": 81,
                        "strategy": "进攻/顺势跟踪",
                        "headline": "ASTS remains constructive",
                        "summary": "Momentum and breadth are aligned.",
                    }
                ],
                "buy_the_dip_rows": [
                    {
                        "ticker": "600330.SS",
                        "name": "天通股份",
                        "verdict": "BUY",
                        "strategy": "回踩低吸",
                        "headline": "回踩后量价结构稳定",
                        "summary": "趋势未破坏，等待承接确认。",
                        "buy_zone": {"low": 9.8, "high": 10.1},
                    }
                ],
            }
        )

        response = self.client.get("/dashboard/ai-daily-report")

        self.assertEqual(200, response.status_code)
        self.assertIn("AI 每日决策面板", response.text)
        self.assertIn("ASTS", response.text)
        self.assertIn("偏进攻", response.text)
        self.assertIn("市场更适合围绕强势股做顺势交易", response.text)
        self.assertIn("Buy The Dip 10", response.text)
        self.assertIn("600330.SS", response.text)

    def test_dashboard_ai_daily_report_message_page_renders(self) -> None:
        from app.services.ai_daily_report import render_ai_daily_report_message, save_ai_daily_report

        save_ai_daily_report(
            {
                "status": "success",
                "mood": "偏进攻",
                "headline": "今日 AI 决策面板偏进攻",
                "strategy": {
                    "headline": "市场更适合围绕强势股做顺势交易",
                    "playbook": "优先做高置信度 BUY 标的。",
                    "bullets": ["优先跟踪: ASTS / AAPL"],
                },
                "rows": [
                    {
                        "ticker": "ASTS",
                        "name": "AST SpaceMobile",
                        "verdict": "BUY",
                        "confidence": 81,
                        "strategy": "进攻/顺势跟踪",
                        "headline": "ASTS remains constructive",
                        "summary": "Momentum and breadth are aligned.",
                        "buy_zone": {"low": 10.2, "high": 10.6},
                        "stop_loss": 9.8,
                        "take_profit": {"low": 11.2, "high": 11.8},
                    }
                ],
                "buy_the_dip_rows": [
                    {
                        "ticker": "600330.SS",
                        "name": "天通股份",
                        "verdict": "BUY",
                        "headline": "回踩承接稳定",
                        "buy_zone": {"low": 9.8, "high": 10.1},
                        "setup_label": "pullback_buy",
                    }
                ],
            }
        )

        response = self.client.get("/dashboard/ai-daily-report/message")

        self.assertEqual(200, response.status_code)
        self.assertIn("Push Ready", response.text)
        self.assertIn("AI 每日决策面板", response.text)
        self.assertIn("ASTS", response.text)
        message = render_ai_daily_report_message(
            {
                "mood": "偏进攻",
                "headline": "今日 AI 决策面板偏进攻",
                "strategy": {
                    "headline": "市场更适合围绕强势股做顺势交易",
                    "playbook": "优先做高置信度 BUY 标的。",
                    "bullets": ["优先跟踪: ASTS / AAPL"],
                },
                "rows": [
                    {
                        "ticker": "ASTS",
                        "name": "AST SpaceMobile",
                        "verdict": "BUY",
                        "confidence": 81,
                        "strategy": "进攻/顺势跟踪",
                        "headline": "ASTS remains constructive",
                        "summary": "Momentum and breadth are aligned.",
                        "buy_zone": {"low": 10.2, "high": 10.6},
                        "stop_loss": 9.8,
                        "take_profit": {"low": 11.2, "high": 11.8},
                    }
                ],
                "buy_the_dip_rows": [
                    {
                        "ticker": "600330.SS",
                        "name": "天通股份",
                        "verdict": "BUY",
                        "buy_zone": {"low": 9.8, "high": 10.1},
                        "setup_label": "pullback_buy",
                    }
                ],
            }
        )
        self.assertIn("买入区：10.2 - 10.6", message)
        self.assertIn("止盈区：11.2 - 11.8", message)
        self.assertIn("策略主线：市场更适合围绕强势股做顺势交易", message)
        self.assertIn("Buy The Dip 候选", message)
        self.assertIn("600330.SS", message)

    def test_send_ai_daily_report_endpoint_uses_notifier(self) -> None:
        from app.services.ai_daily_report import save_ai_daily_report

        save_ai_daily_report(
            {
                "status": "success",
                "mood": "偏进攻",
                "headline": "今日 AI 决策面板偏进攻",
                "rows": [],
            }
        )

        with patch(
            "app.api.routes.jobs.PushNotificationService.send_text",
            return_value={"status": "success", "sent": ["wechat"], "failed": []},
        ):
            response = self.client.post(
                "/jobs/send-ai-daily-report",
                data={"redirect_to": "/dashboard/ai-daily-report"},
                follow_redirects=False,
            )

        self.assertEqual(303, response.status_code)
        self.assertIn("job_status=success", response.headers["location"])

        summary_response = self.client.get("/dashboard/summary?lookback_runs=3")
        self.assertEqual(200, summary_response.status_code)
        self.assertEqual(3, summary_response.json()["lookback_runs"])

    def test_screener_full_market_cn_technical_template_returns_pattern_match(self) -> None:
        from app.services.screener import ScreenerService

        self._seed_symbol("600001.SS", "测试科技", "CN", "SSE")
        self._write_price_history("600001.SS", self._build_bullish_cn_history())

        results = ScreenerService().screen(
            model_template="cn_bullish_ma_stack",
            universe="full_market",
            market="CN",
            min_trend_score=0,
            limit=50,
        )
        tickers = [item["ticker"] for item in results]
        self.assertIn("600001.SS", tickers)
        matched = next(item for item in results if item["ticker"] == "600001.SS")
        self.assertIn("均线多头排列", matched["matched_patterns"])

    def test_technical_pattern_service_detects_bollinger_squeeze_and_three_white_soldiers(self) -> None:
        from app.services.technical_patterns import TechnicalPatternService

        self._seed_symbol("600006.SS", "形态测试股", "CN", "SSE")
        self._write_price_history("600006.SS", self._build_squeeze_cn_history())

        snapshot = TechnicalPatternService().evaluate_ticker("600006.SS")

        self.assertIsNotNone(snapshot)
        self.assertTrue(snapshot.bollinger_squeeze)
        self.assertTrue(snapshot.three_white_soldiers)
        self.assertIn("布林带收口", snapshot.matched_patterns)
        self.assertIn("三连阳", snapshot.matched_patterns)

    def test_technical_pattern_service_detects_bullish_engulfing_and_hammer(self) -> None:
        from app.services.technical_patterns import TechnicalPatternService

        self._seed_symbol("600011.SS", "吞没测试股", "CN", "SSE")
        self._seed_symbol("600012.SS", "锤子测试股", "CN", "SSE")
        self._write_price_history("600011.SS", self._build_bullish_engulfing_history())
        self._write_price_history("600012.SS", self._build_hammer_reversal_history())

        service = TechnicalPatternService()
        engulfing = service.evaluate_ticker("600011.SS")
        hammer = service.evaluate_ticker("600012.SS")

        self.assertIsNotNone(engulfing)
        self.assertIsNotNone(hammer)
        self.assertTrue(engulfing.bullish_engulfing)
        self.assertIn("看涨吞没", engulfing.matched_patterns)
        self.assertTrue(hammer.hammer_reversal)
        self.assertIn("锤子线", hammer.matched_patterns)

    def test_screener_new_pattern_templates_can_use_cached_matches(self) -> None:
        from app.core.db import SessionLocal
        from app.services.repository import SymbolRepository, TechnicalSnapshotRepository
        from app.services.screener import ScreenerService

        self._seed_symbol("600007.SS", "缓存形态增强股", "CN", "SSE")

        with SessionLocal() as db:
            symbol = SymbolRepository(db).get_by_ticker("600007.SS")
            TechnicalSnapshotRepository(db).upsert_snapshot(
                symbol_id=symbol.id,
                as_of_date="2026-04-09",
                source="technical_patterns",
                limit_up_yesterday=False,
                volume_breakout=False,
                ma_cluster=False,
                bullish_ma_stack=False,
                macd_underwater_cross=False,
                matched_patterns=["布林带收口", "三连阳"],
            )

        service = ScreenerService()
        squeeze_results = service.screen(
            model_template="cn_bollinger_squeeze_watch",
            universe="full_market",
            market="CN",
            min_trend_score=0,
            limit=50,
        )
        soldier_results = service.screen(
            model_template="cn_three_white_soldiers",
            universe="full_market",
            market="CN",
            min_trend_score=0,
            limit=50,
        )

        self.assertIn("600007.SS", [item["ticker"] for item in squeeze_results])
        self.assertIn("600007.SS", [item["ticker"] for item in soldier_results])

    def test_screener_tradingview_multi_timeframe_template_filters_for_alignment(self) -> None:
        from app.services.screener import ScreenerService

        self._seed_symbol("600008.SS", "多周期共振股", "CN", "SSE")
        self._write_price_history("600008.SS", self._build_bullish_cn_history())

        def fake_rating(*, ticker, market=None, exchange=None, interval="1d"):
            mapping = {
                "1d": {"status": "success", "recommendation": "BUY"},
                "1w": {"status": "success", "recommendation": "STRONG_BUY"},
                "1M": {"status": "success", "recommendation": "NEUTRAL"},
            }
            return {
                "ticker": ticker,
                "market": market,
                "exchange": exchange,
                "interval": interval,
                **mapping[interval],
            }

        with patch(
            "app.services.screener.TradingViewClient.get_technical_rating",
            side_effect=fake_rating,
        ):
            results = ScreenerService().screen(
                model_template="tv_multi_timeframe_bullish",
                universe="full_market",
                market="CN",
                min_trend_score=0,
                min_volume_ratio=0.0,
                limit=20,
            )

        self.assertIn("600008.SS", [item["ticker"] for item in results])
        matched = next(item for item in results if item["ticker"] == "600008.SS")
        self.assertIn("1d BUY", matched["selection_reason"])
        self.assertIn("1w STRONG_BUY", matched["selection_reason"])

    def test_screener_tradingview_multi_timeframe_template_excludes_bearish_interval(self) -> None:
        from app.services.screener import ScreenerService

        self._seed_symbol("600009.SS", "多周期分歧股", "CN", "SSE")
        self._write_price_history("600009.SS", self._build_bullish_cn_history())

        def fake_rating(*, ticker, market=None, exchange=None, interval="1d"):
            mapping = {
                "1d": {"status": "success", "recommendation": "BUY"},
                "1w": {"status": "success", "recommendation": "SELL"},
                "1M": {"status": "success", "recommendation": "BUY"},
            }
            return {
                "ticker": ticker,
                "market": market,
                "exchange": exchange,
                "interval": interval,
                **mapping[interval],
            }

        with patch(
            "app.services.screener.TradingViewClient.get_technical_rating",
            side_effect=fake_rating,
        ):
            results = ScreenerService().screen(
                model_template="tv_multi_timeframe_bullish",
                universe="full_market",
                market="CN",
                min_trend_score=0,
                min_volume_ratio=0.0,
                limit=20,
            )

        self.assertNotIn("600009.SS", [item["ticker"] for item in results])

    def test_screener_action_filter_matches_buy_the_dip_label(self) -> None:
        from app.services.screener import ScreenerService

        self._seed_symbol("600013.SS", "抄底候选股", "CN", "SSE")
        self._write_price_history("600013.SS", self._build_bullish_cn_history())

        with patch(
            "app.services.screener.InsightEngine.get_insight",
            return_value={
                "ticker": "600013.SS",
                "name": "抄底候选股",
                "company_name": "抄底候选股",
                "market": "CN",
                "exchange": "SSE",
                "as_of_date": "2026-04-09",
                "trend_score": 72,
                "action_label": "Buy The Dip",
                "action_summary": "Pull back into support and look for confirmation.",
                "volume_ratio": 1.4,
                "latest_close": 12.34,
                "momentum_5": -2.1,
                "momentum_20": 6.8,
                "distance_to_breakout_pct": 3.2,
                "signal_label": "buy",
                "signal_strength": 74,
                "selection_reason": "Pullback near support with improving volume.",
            },
        ):
            results = ScreenerService().screen(
                model_template="technical_momentum",
                universe="full_market",
                market="CN",
                min_trend_score=0,
                action_filter="buy_the_dip",
                limit=20,
            )

        self.assertIn("600013.SS", [item["ticker"] for item in results])

    def test_screener_page_renders_tradingview_multi_timeframe_ratings(self) -> None:
        self._seed_symbol("600010.SS", "页面多周期股", "CN", "SSE")
        self._write_price_history("600010.SS", self._build_bullish_cn_history())

        def fake_rating(*, ticker, market=None, exchange=None, interval="1d"):
            mapping = {
                "1d": {"status": "success", "recommendation": "BUY"},
                "1w": {"status": "success", "recommendation": "STRONG_BUY"},
                "1M": {"status": "success", "recommendation": "NEUTRAL"},
            }
            return {
                "ticker": ticker,
                "market": market,
                "exchange": exchange,
                "interval": interval,
                **mapping[interval],
            }

        with patch(
            "app.services.screener.TradingViewClient.get_technical_rating",
            side_effect=fake_rating,
        ):
            response = self.client.get(
                "/screeners?lang=zh&model_template=tv_multi_timeframe_bullish&market=CN&universe=full_market&min_trend_score=0&min_volume_ratio=0"
            )

        self.assertEqual(200, response.status_code)
        self.assertIn("TradingView多周期共振", response.text)
        self.assertIn("技术评级", response.text)
        self.assertIn("1D BUY", response.text)
        self.assertIn("1W STRONG_BUY", response.text)

    def test_screener_can_add_current_results_to_today_focus_pool(self) -> None:
        self._seed_symbol("600001.SS", "测试科技", "CN", "SSE")
        self._write_price_history("600001.SS", self._build_bullish_cn_history())

        response = self.client.post(
            "/screeners/add-to-focus",
            data={
                "lang": "zh",
                "model_template": "cn_bullish_ma_stack",
                "universe": "full_market",
                "market": "CN",
                "min_trend_score": "0",
                "focus_top_n": "5",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, response.status_code)

        focus_page = self.client.get("/screeners/focus/today?lang=zh")
        self.assertEqual(200, focus_page.status_code)
        self.assertIn("今日重点盯盘池", focus_page.text)
        self.assertIn("600001.SS", focus_page.text)
        self.assertIn("均线多头排列", focus_page.text)

    def test_screener_service_builds_market_snapshot_boards(self) -> None:
        from app.services.screener import ScreenerService

        with patch.object(
            ScreenerService,
            "screen",
            side_effect=[
                [{"ticker": "600111.SS", "name": "强势股"}],
                [{"ticker": "600222.SS", "name": "收口股", "distance_to_breakout_pct": 2.0}],
                [{"ticker": "600333.SS", "name": "连阳股", "momentum_5": 6.2}],
                [{"ticker": "600444.SS", "name": "放量股", "volume_ratio": 2.1}],
            ],
        ):
            boards = ScreenerService().build_market_snapshot(market="CN", limit_per_board=8, mode="premarket")

        self.assertEqual(["leaders", "squeeze", "three_white_soldiers", "volume_breakout"], [item["key"] for item in boards])
        self.assertEqual("600111.SS", boards[0]["rows"][0]["ticker"])
        self.assertEqual("收口榜", boards[1]["title_zh"])
        self.assertEqual("600444.SS", boards[3]["rows"][0]["ticker"])
        self.assertIn("snapshot_score", boards[0]["rows"][0])
        self.assertIn("snapshot_score_breakdown", boards[1]["rows"][0])
        self.assertEqual("premarket", boards[0]["mode"])

    def test_market_snapshot_page_renders_boards(self) -> None:
        boards = [
            {
                "key": "leaders",
                "title_en": "Momentum Leaders",
                "title_zh": "强势榜",
                "description_en": "Trend leaders",
                "description_zh": "趋势强股",
                "rows": [
                    {
                        "ticker": "600111.SS",
                        "name": "强势股",
                        "trend_score": 88,
                        "momentum_5": 12.6,
                        "volume_ratio": 2.3,
                        "snapshot_score": 91,
                        "snapshot_score_breakdown": ["trend 88", "volume 2.3x", "5D 12.6%", "均线多头排列"],
                        "matched_patterns": ["均线多头排列"],
                        "tradingview_ratings": {"1d": {"recommendation": "BUY"}, "1w": {"recommendation": "BUY"}, "1M": {"recommendation": "NEUTRAL"}},
                    }
                ],
            },
            {
                "key": "squeeze",
                "title_en": "Squeeze Watch",
                "title_zh": "收口榜",
                "description_en": "Squeeze names",
                "description_zh": "收口候选",
                "rows": [],
            },
            {
                "key": "three_white_soldiers",
                "title_en": "Three White Soldiers",
                "title_zh": "连阳榜",
                "description_en": "Strong candles",
                "description_zh": "连阳候选",
                "rows": [],
            },
            {
                "key": "volume_breakout",
                "title_en": "Volume Breakout",
                "title_zh": "放量榜",
                "description_en": "Volume names",
                "description_zh": "放量候选",
                "rows": [],
            },
        ]

        with patch("app.api.routes.screener.ScreenerService.build_market_snapshot", return_value=boards):
            response = self.client.get("/screeners/market-snapshot?lang=zh&mode=postmarket")

        self.assertEqual(200, response.status_code)
        self.assertIn("市场快照榜单", response.text)
        self.assertIn("强势榜", response.text)
        self.assertIn("收口榜", response.text)
        self.assertIn("600111.SS", response.text)
        self.assertIn("技术评级", response.text)
        self.assertIn("加入今日重点盯盘池", response.text)
        self.assertIn("快照分", response.text)
        self.assertIn("分数驱动", response.text)
        self.assertIn("市场情绪", response.text)
        self.assertIn("盘后复盘", response.text)

    def test_market_snapshot_can_add_single_ticker_to_today_focus_pool(self) -> None:
        response = self.client.post(
            "/screeners/market-snapshot/add-to-focus",
            data={
                "lang": "zh",
                "ticker": "600111.SS",
                "name": "强势股",
                "market": "CN",
                "selection_reason": "趋势与量能共振",
                "matched_patterns": "均线多头排列 / 布林带收口",
            },
            follow_redirects=False,
        )

        self.assertEqual(303, response.status_code)

        focus_page = self.client.get("/screeners/focus/today?lang=zh")
        self.assertEqual(200, focus_page.status_code)
        self.assertIn("600111.SS", focus_page.text)
        self.assertIn("均线多头排列", focus_page.text)
        self.assertIn("布林带收口", focus_page.text)

    def test_dashboard_market_page_supports_chinese_language(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="dashboard_market_zh_demo", signal_type="momentum", lookback_days=3)

        response = self.client.get("/dashboard/market?lang=zh&lookback_runs=3")

        self.assertEqual(200, response.status_code)
        self.assertIn("市场脉冲", response.text)
        self.assertIn("板块热力图", response.text)
        self.assertIn("概念异动追踪", response.text)
        self.assertIn("打开板块热力图", response.text)
        self.assertIn("打开概念追踪", response.text)
        self.assertIn("信号聚焦", response.text)
        self.assertIn("最低强度", response.text)
        self.assertIn("最少买点数", response.text)

    def test_dashboard_market_heatmap_page_supports_chinese_language(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="dashboard_market_heatmap_zh_demo", signal_type="momentum", lookback_days=3)

        response = self.client.get("/dashboard/market/heatmap?lang=zh&lookback_runs=3")

        self.assertEqual(200, response.status_code)
        self.assertIn("板块热力图", response.text)
        self.assertIn("信号分布", response.text)
        self.assertIn("概念共振", response.text)
        self.assertIn("风险概览", response.text)
        self.assertIn("信号聚焦", response.text)
        self.assertIn("最低强度", response.text)
        self.assertIn("最少买点数", response.text)
        self.assertIn("买点", response.text)
        self.assertIn("最强", response.text)

    def test_dashboard_market_concepts_page_supports_chinese_language(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="dashboard_market_concepts_zh_demo", signal_type="momentum", lookback_days=3)

        response = self.client.get("/dashboard/market/concepts?lang=zh&lookback_runs=3")

        self.assertEqual(200, response.status_code)
        self.assertIn("概念异动追踪", response.text)
        self.assertIn("信号聚焦", response.text)
        self.assertIn("最低强度", response.text)
        self.assertIn("最少买点数", response.text)
        self.assertIn("买点股数量", response.text)
        self.assertIn("最强信号强度", response.text)
        self.assertIn("执行提醒", response.text)

    def test_dashboard_market_concepts_page_supports_signal_filter(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer
        from app.core.db import SessionLocal
        from app.services.repository import ConceptSnapshotRepository, SymbolRepository

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="market_concepts_signal_demo", signal_type="momentum", lookback_days=3)
        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            aapl = symbol_repo.get_by_ticker("AAPL")
            msft = symbol_repo.get_by_ticker("MSFT")
            repo = ConceptSnapshotRepository(db)
            repo.upsert_snapshot(symbol_id=aapl.id, concept_name="Consumer Electronics", concept_code="C001", as_of_date="2026-04-03", source="test")
            repo.upsert_snapshot(symbol_id=msft.id, concept_name="Consumer Electronics", concept_code="C001", as_of_date="2026-04-03", source="test")

        response = self.client.get("/dashboard/market/concepts?signal_filter=BUY&min_signal_strength=1")

        self.assertEqual(200, response.status_code)
        self.assertIn("Signal Focus", response.text)
        self.assertIn("Min Strength", response.text)
        self.assertIn("Concept Activity Tracker", response.text)
        self.assertIn("signal_filter=BUY", response.text)

    def test_dashboard_market_concepts_page_supports_min_buy_signal_count(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer
        from app.core.db import SessionLocal
        from app.services.repository import ConceptSnapshotRepository, SymbolRepository

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="market_concepts_buy_count_demo", signal_type="momentum", lookback_days=3)
        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            aapl = symbol_repo.get_by_ticker("AAPL")
            msft = symbol_repo.get_by_ticker("MSFT")
            repo = ConceptSnapshotRepository(db)
            repo.upsert_snapshot(symbol_id=aapl.id, concept_name="Consumer Electronics", concept_code="C001", as_of_date="2026-04-03", source="test")
            repo.upsert_snapshot(symbol_id=msft.id, concept_name="Consumer Electronics", concept_code="C001", as_of_date="2026-04-03", source="test")

        response = self.client.get("/dashboard/market/concepts?signal_filter=BUY&min_buy_signal_count=1")

        self.assertEqual(200, response.status_code)
        self.assertIn("Min Buy Count", response.text)
        self.assertIn("min_buy_signal_count=1", response.text)

    def test_dashboard_market_pages_support_execution_tag_filter(self) -> None:
        from sqlalchemy import select

        from app.core.db import SessionLocal
        from app.models.tables import ModelRun, Prediction
        from app.services.dataset_build import build_dataset
        from app.services.repository import ConceptSnapshotRepository, PredictionTradePlanRepository, SymbolRepository
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="market_execution_tag_demo", signal_type="momentum", lookback_days=3)
        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            aapl = symbol_repo.get_by_ticker("AAPL")
            msft = symbol_repo.get_by_ticker("MSFT")
            repo = ConceptSnapshotRepository(db)
            repo.upsert_snapshot(symbol_id=aapl.id, concept_name="Consumer Electronics", concept_code="C001", as_of_date="2026-04-03", source="test")
            repo.upsert_snapshot(symbol_id=msft.id, concept_name="Consumer Electronics", concept_code="C001", as_of_date="2026-04-03", source="test")
            model_run = db.scalar(select(ModelRun).order_by(ModelRun.id.desc()))
            predictions = list(
                db.execute(
                    select(Prediction)
                    .where(Prediction.model_run_id == model_run.id)
                ).scalars()
            )
            trade_plan_rows = []
            for prediction in predictions:
                if prediction.symbol_id == aapl.id:
                    trade_plan_rows.append(
                        {
                            "symbol_id": prediction.symbol_id,
                            "trade_date": prediction.trade_date,
                            "execution_tags": ["gap-risk", "earnings-soon"],
                        }
                    )
                elif prediction.symbol_id == msft.id:
                    trade_plan_rows.append(
                        {
                            "symbol_id": prediction.symbol_id,
                            "trade_date": prediction.trade_date,
                            "execution_tags": ["thin-liquidity"],
                        }
                    )
            PredictionTradePlanRepository(db).replace_for_model_run(model_run.id, trade_plan_rows)

        concept_response = self.client.get("/dashboard/market/concepts?execution_tag_filter=gap-risk")
        self.assertEqual(200, concept_response.status_code)
        self.assertIn("Risk Overview", concept_response.text)
        self.assertIn("execution_tag_filter=gap-risk", concept_response.text)
        self.assertIn("gap-risk", concept_response.text)
        self.assertIn("Consumer Electronics", concept_response.text)

        heatmap_response = self.client.get("/dashboard/market/heatmap?execution_tag_filter=gap-risk")
        self.assertEqual(200, heatmap_response.status_code)
        self.assertIn("Risk Overview", heatmap_response.text)
        self.assertIn("execution_tag_filter=gap-risk", heatmap_response.text)
        self.assertIn("gap-risk", heatmap_response.text)

    def test_dashboard_market_pages_support_excluding_execution_tag(self) -> None:
        from sqlalchemy import select

        from app.core.db import SessionLocal
        from app.models.tables import ModelRun, Prediction
        from app.services.dataset_build import build_dataset
        from app.services.repository import ConceptSnapshotRepository, PredictionTradePlanRepository, SymbolRepository
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="market_execution_tag_exclude_demo", signal_type="momentum", lookback_days=3)
        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            aapl = symbol_repo.get_by_ticker("AAPL")
            msft = symbol_repo.get_by_ticker("MSFT")
            repo = ConceptSnapshotRepository(db)
            repo.upsert_snapshot(symbol_id=aapl.id, concept_name="Consumer Electronics", concept_code="C001", as_of_date="2026-04-03", source="test")
            repo.upsert_snapshot(symbol_id=msft.id, concept_name="Consumer Electronics", concept_code="C001", as_of_date="2026-04-03", source="test")
            model_run = db.scalar(select(ModelRun).order_by(ModelRun.id.desc()))
            predictions = list(db.execute(select(Prediction).where(Prediction.model_run_id == model_run.id)).scalars())
            rows = []
            for prediction in predictions:
                if prediction.symbol_id == aapl.id:
                    rows.append({"symbol_id": prediction.symbol_id, "trade_date": prediction.trade_date, "execution_tags": ["gap-risk"]})
                elif prediction.symbol_id == msft.id:
                    rows.append({"symbol_id": prediction.symbol_id, "trade_date": prediction.trade_date, "execution_tags": ["earnings-soon"]})
            PredictionTradePlanRepository(db).replace_for_model_run(model_run.id, rows)

        concept_response = self.client.get("/dashboard/market/concepts?exclude_execution_tag_filter=gap-risk")
        self.assertEqual(200, concept_response.status_code)
        self.assertIn("exclude_execution_tag_filter=gap-risk", concept_response.text)
        self.assertIn("No concept data yet", concept_response.text)
        self.assertIn("earnings-soon", concept_response.text)

        heatmap_response = self.client.get("/dashboard/market/heatmap?exclude_execution_tag_filter=gap-risk")
        self.assertEqual(200, heatmap_response.status_code)
        self.assertIn("exclude_execution_tag_filter=gap-risk", heatmap_response.text)
        self.assertIn("No concept heatmap yet", heatmap_response.text)

    def test_execution_tag_filters_support_multiple_tags(self) -> None:
        from sqlalchemy import select

        from app.core.db import SessionLocal
        from app.models.tables import ModelRun, Prediction
        from app.services.dataset_build import build_dataset
        from app.services.repository import ConceptSnapshotRepository, PredictionTradePlanRepository, SymbolRepository, WatchlistRepository
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="multi_execution_tag_demo", signal_type="momentum", lookback_days=3)
        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            aapl = symbol_repo.get_by_ticker("AAPL")
            msft = symbol_repo.get_by_ticker("MSFT")
            asts = symbol_repo.get_by_ticker("ASTS")
            watchlist_repo = WatchlistRepository(db)
            watchlist = watchlist_repo.get_or_create_default()
            for symbol in (aapl, msft, asts):
                watchlist_repo.add_symbol(watchlist.id, symbol.id)
            concept_repo = ConceptSnapshotRepository(db)
            for symbol in (aapl, msft, asts):
                concept_repo.upsert_snapshot(
                    symbol_id=symbol.id,
                    concept_name="Consumer Electronics",
                    concept_code="C001",
                    as_of_date="2026-04-03",
                    source="test",
                )
            model_run = db.scalar(select(ModelRun).order_by(ModelRun.id.desc()))
            predictions = list(db.execute(select(Prediction).where(Prediction.model_run_id == model_run.id)).scalars())
            rows = []
            for prediction in predictions:
                if prediction.symbol_id == aapl.id:
                    rows.append({"symbol_id": prediction.symbol_id, "trade_date": prediction.trade_date, "execution_tags": ["gap-risk"]})
                elif prediction.symbol_id == msft.id:
                    rows.append({"symbol_id": prediction.symbol_id, "trade_date": prediction.trade_date, "execution_tags": ["earnings-soon"]})
                elif prediction.symbol_id == asts.id:
                    rows.append({"symbol_id": prediction.symbol_id, "trade_date": prediction.trade_date, "execution_tags": ["thin-liquidity"]})
            PredictionTradePlanRepository(db).replace_for_model_run(model_run.id, rows)

        watchlist_include = self.client.get("/watchlist?execution_tag_filter=gap-risk,earnings-soon")
        self.assertEqual(200, watchlist_include.status_code)
        self.assertIn("/watchlist/open/1", watchlist_include.text)
        self.assertIn("/watchlist/open/2", watchlist_include.text)

        screener_exclude = self.client.get("/screeners?universe=synced&exclude_execution_tag_filter=gap-risk,earnings-soon")
        self.assertEqual(200, screener_exclude.status_code)
        self.assertIn("/insights/ASTS?lang=en", screener_exclude.text)
        self.assertNotIn("/insights/AAPL?lang=en", screener_exclude.text)
        self.assertNotIn("/insights/MSFT?lang=en", screener_exclude.text)

        continuous_include = self.client.get("/dashboard/continuous-leaders?lookback_runs=3&execution_tag_filter=gap-risk,earnings-soon")
        self.assertEqual(200, continuous_include.status_code)
        self.assertIn("/insights/AAPL?lang=en", continuous_include.text)
        self.assertIn("/insights/MSFT?lang=en", continuous_include.text)

        market_concepts = self.client.get("/dashboard/market/concepts?execution_tag_filter=gap-risk,earnings-soon")
        self.assertEqual(200, market_concepts.status_code)
        self.assertIn("Consumer Electronics", market_concepts.text)

        market_heatmap = self.client.get("/dashboard/market/heatmap?exclude_execution_tag_filter=gap-risk,earnings-soon")
        self.assertEqual(200, market_heatmap.status_code)
        self.assertIn("No concept heatmap yet", market_heatmap.text)

    def test_dashboard_market_concepts_page_supports_sorting_links(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer
        from app.core.db import SessionLocal
        from app.services.repository import ConceptSnapshotRepository, SymbolRepository

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="market_concepts_sort_demo", signal_type="momentum", lookback_days=3)
        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            aapl = symbol_repo.get_by_ticker("AAPL")
            msft = symbol_repo.get_by_ticker("MSFT")
            repo = ConceptSnapshotRepository(db)
            repo.upsert_snapshot(symbol_id=aapl.id, concept_name="Consumer Electronics", concept_code="C001", as_of_date="2026-04-03", source="test")
            repo.upsert_snapshot(symbol_id=msft.id, concept_name="Consumer Electronics", concept_code="C001", as_of_date="2026-04-03", source="test")

        response = self.client.get("/dashboard/market/concepts?concept_sort_by=buy_count&concept_sort_order=desc")

        self.assertEqual(200, response.status_code)
        self.assertIn("concept_sort_by=buy_count", response.text)
        self.assertIn("concept_sort_order=asc", response.text)

    def test_dashboard_market_concepts_export_csv_returns_rows(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer
        from app.core.db import SessionLocal
        from app.services.repository import ConceptSnapshotRepository, SymbolRepository

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="market_concepts_export_demo", signal_type="momentum", lookback_days=3)
        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            aapl = symbol_repo.get_by_ticker("AAPL")
            msft = symbol_repo.get_by_ticker("MSFT")
            repo = ConceptSnapshotRepository(db)
            repo.upsert_snapshot(symbol_id=aapl.id, concept_name="Consumer Electronics", concept_code="C001", as_of_date="2026-04-03", source="test")
            repo.upsert_snapshot(symbol_id=msft.id, concept_name="Consumer Electronics", concept_code="C001", as_of_date="2026-04-03", source="test")

        response = self.client.get("/dashboard/market/concepts/export")

        self.assertEqual(200, response.status_code)
        self.assertIn("text/csv", response.headers["content-type"])
        self.assertIn("concept_name", response.text)
        self.assertIn("buy_signal_count", response.text)
        self.assertIn("max_signal_strength", response.text)
        self.assertIn("execution_tags", response.text)
        self.assertIn("Consumer Electronics", response.text)

    def test_dashboard_ops_page_supports_chinese_language(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="dashboard_ops_zh_demo", signal_type="momentum", lookback_days=3)

        response = self.client.get("/dashboard/ops?lang=zh&lookback_runs=3")

        self.assertEqual(200, response.status_code)
        self.assertIn("运维操作台", response.text)
        self.assertIn("同步中心", response.text)
        self.assertIn("模型运行", response.text)
        self.assertIn("任务记录", response.text)

    def test_dashboard_ops_sync_page_supports_chinese_language(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data

        seed_sample_data()
        build_dataset(normalize_only=True)

        response = self.client.get("/dashboard/ops/sync?lang=zh&lookback_runs=3")

        self.assertEqual(200, response.status_code)
        self.assertIn("同步中心", response.text)
        self.assertIn("行情与基本面同步", response.text)
        self.assertIn("同步状态", response.text)
        self.assertIn("A股全市场初始化进度", response.text)
        self.assertIn("同步 A 股股票池", response.text)
        self.assertIn("初始化 A 股全市场数据", response.text)
        self.assertIn("刷新 A 股最近行情", response.text)
        self.assertIn("重建技术形态缓存", response.text)
        self.assertIn("offset", response.text)
        self.assertIn("batch_size", response.text)
        self.assertIn("去全市场技术选股", response.text)

    def test_jobs_can_sync_cn_symbol_universe_and_init_cn_market_data(self) -> None:
        with patch(
            "app.api.routes.jobs.sync_cn_symbol_universe",
            return_value={
                "status": "success",
                "message": "Synced 2 CN universe row(s) for 2 stock(s).",
                "symbols_written": 2,
                "tickers": ["600519.SS", "000001.SZ"],
            },
        ):
            universe_response = self.client.post(
                "/jobs/sync-cn-symbol-universe",
                data={"redirect_to": "/dashboard/ops/sync?lang=zh&lookback_runs=3"},
                follow_redirects=False,
            )
        self.assertEqual(303, universe_response.status_code)
        self.assertIn("job_status=success", universe_response.headers["location"])

        with patch(
            "app.api.routes.jobs.init_cn_market_data",
            return_value={
                "status": "success",
                "message": "Initialized CN market data for 2 stock(s) over ~180 days.",
                "days_back": 180,
                "offset": 0,
                "batch_size": 200,
                "total_symbols": 2,
                "success_count": 2,
                "failure_count": 0,
                "results": [],
            },
        ):
            init_response = self.client.post(
                "/jobs/init-cn-market-data",
                data={
                    "redirect_to": "/dashboard/ops/sync?lang=zh&lookback_runs=3",
                    "days_back": "180",
                    "offset": "0",
                    "batch_size": "200",
                    "limit": "0",
                    "provider": "yfinance",
                },
                follow_redirects=False,
            )
        self.assertEqual(303, init_response.status_code)
        self.assertIn("job_status=success", init_response.headers["location"])

        with patch(
            "app.api.routes.jobs.refresh_cn_market_data",
            return_value={
                "status": "success",
                "message": "Refreshed CN market data for 2 stock(s) over the recent ~7 days.",
                "days_back": 7,
                "total_symbols": 2,
                "success_count": 2,
                "failure_count": 0,
                "results": [],
            },
        ):
            refresh_response = self.client.post(
                "/jobs/refresh-cn-market-data",
                data={
                    "redirect_to": "/dashboard/ops/sync?lang=zh&lookback_runs=3",
                    "days_back": "7",
                    "limit": "0",
                    "provider": "yfinance",
                },
                follow_redirects=False,
            )
        self.assertEqual(303, refresh_response.status_code)
        self.assertIn("job_status=success", refresh_response.headers["location"])

        with patch(
            "app.api.routes.jobs.refresh_cn_market_data_daily",
            return_value={
                "status": "success",
                "message": "Incrementally refreshed CN market data for 2 stock(s) over the recent ~7 days.",
                "days_back": 7,
                "incremental": True,
                "overlap_days": 3,
                "total_symbols": 2,
                "success_count": 2,
                "failure_count": 0,
                "results": [],
            },
        ):
            daily_refresh_response = self.client.post(
                "/jobs/refresh-cn-market-data-daily",
                data={
                    "redirect_to": "/dashboard/ops/sync?lang=zh&lookback_runs=3",
                    "days_back": "7",
                    "limit": "0",
                    "overlap_days": "3",
                    "provider": "yfinance",
                },
                follow_redirects=False,
            )
        self.assertEqual(303, daily_refresh_response.status_code)
        self.assertIn("job_status=success", daily_refresh_response.headers["location"])

    def test_job_redirect_appends_status_with_existing_query_string(self) -> None:
        with patch(
            "app.api.routes.jobs.sync_cn_symbol_universe",
            return_value={
                "status": "not_configured",
                "message": "Set PQW_TUSHARE_TOKEN to enable CN market universe sync.",
                "symbols_written": 0,
                "tickers": [],
            },
        ):
            response = self.client.post(
                "/jobs/sync-cn-symbol-universe",
                data={"redirect_to": "/dashboard/ops/sync?lang=zh&lookback_runs=5"},
                follow_redirects=False,
            )

        self.assertEqual(303, response.status_code)
        self.assertIn("/dashboard/ops/sync?lang=zh&lookback_runs=5&job_status=not_configured", response.headers["location"])

        with patch(
            "app.api.routes.jobs.rebuild_technical_snapshots",
            return_value={
                "status": "success",
                "message": "Rebuilt 2 technical snapshot(s) for CN.",
                "market": "CN",
                "rows_written": 2,
                "skipped": 0,
                "tickers": ["600519.SS", "000001.SZ"],
            },
        ):
            rebuild_response = self.client.post(
                "/jobs/rebuild-technical-snapshots",
                data={
                    "redirect_to": "/dashboard/ops/sync?lang=zh&lookback_runs=3",
                    "market": "CN",
                    "limit": "0",
                },
                follow_redirects=False,
            )
        self.assertEqual(303, rebuild_response.status_code)
        self.assertIn("job_status=success", rebuild_response.headers["location"])

    def test_screener_full_market_cn_technical_template_can_use_cached_snapshots(self) -> None:
        from app.core.db import SessionLocal
        from app.services.repository import SymbolRepository, TechnicalSnapshotRepository
        from app.services.screener import ScreenerService

        self._seed_symbol("600002.SS", "缓存形态股", "CN", "SSE")

        with SessionLocal() as db:
            symbol = SymbolRepository(db).get_by_ticker("600002.SS")
            TechnicalSnapshotRepository(db).upsert_snapshot(
                symbol_id=symbol.id,
                as_of_date="2026-04-05",
                source="technical_patterns",
                limit_up_yesterday=False,
                volume_breakout=False,
                ma_cluster=False,
                bullish_ma_stack=True,
                macd_underwater_cross=False,
                matched_patterns=["均线多头排列"],
            )

        results = ScreenerService().screen(
            model_template="cn_bullish_ma_stack",
            universe="full_market",
            market="CN",
            min_trend_score=0,
            limit=50,
        )
        tickers = [item["ticker"] for item in results]
        self.assertIn("600002.SS", tickers)

    def test_init_cn_market_data_auto_rebuilds_technical_snapshots(self) -> None:
        from app.services.cn_market_universe import init_cn_market_data

        self._seed_symbol("600003.SS", "自动缓存股", "CN", "SSE")

        with patch(
            "app.services.cn_market_universe.sync_market_data",
            return_value=[{"ticker": "600003.SS", "status": "success", "rows": 180}],
        ), patch(
            "app.services.cn_market_universe.rebuild_technical_snapshots",
            return_value={"status": "success", "message": "Rebuilt 1 technical snapshot(s) for CN."},
        ) as rebuild_mock:
            result = init_cn_market_data(days_back=180, offset=0, batch_size=1)

        self.assertEqual("success", result["status"])
        self.assertIn("technical_snapshot_rebuild", result)
        rebuild_mock.assert_called_once()
        self.assertEqual(0, result["offset"])
        self.assertEqual(1, result["batch_size"])

    def test_init_cn_market_data_can_continue_missing_symbols_only(self) -> None:
        from app.core.db import SessionLocal
        from app.services.cn_market_universe import init_cn_market_data
        from app.services.repository import PriceSyncStateRepository, SymbolRepository

        self._seed_symbol("600010.SS", "已完成股", "CN", "SSE")
        self._seed_symbol("600011.SS", "待续跑股", "CN", "SSE")

        with SessionLocal() as db:
            symbol = next(item for item in SymbolRepository(db).list_symbols() if item.ticker == "600010.SS")
            PriceSyncStateRepository(db).upsert_state(
                symbol_id=symbol.id,
                provider="yfinance",
                last_synced_date="2026-04-03",
                status="success",
                message="already synced",
            )

        with patch(
            "app.services.cn_market_universe.sync_market_data",
            return_value=[{"ticker": "600011.SS", "status": "success", "rows": 180}],
        ) as sync_mock, patch(
            "app.services.cn_market_universe.rebuild_technical_snapshots",
            return_value={"status": "success", "message": "Rebuilt 1 technical snapshot(s) for CN."},
        ):
            result = init_cn_market_data(days_back=180, pending_only=True)

        self.assertEqual("success", result["status"])
        self.assertTrue(result["pending_only"])
        sync_mock.assert_called_once()
        self.assertEqual(["600011.SS"], sync_mock.call_args.kwargs["tickers"])

    def test_init_cn_market_data_can_retry_failed_symbols(self) -> None:
        from app.core.db import SessionLocal
        from app.services.cn_market_universe import init_cn_market_data
        from app.services.repository import PriceSyncStateRepository, SymbolRepository

        self._seed_symbol("600012.SS", "失败股", "CN", "SSE")
        self._seed_symbol("600013.SS", "成功股", "CN", "SSE")

        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            failed_symbol = symbol_repo.get_by_ticker("600012.SS")
            success_symbol = symbol_repo.get_by_ticker("600013.SS")
            PriceSyncStateRepository(db).upsert_state(
                symbol_id=failed_symbol.id,
                provider="yfinance",
                last_synced_date=None,
                status="failed",
                message="temporary failure",
            )
            PriceSyncStateRepository(db).upsert_state(
                symbol_id=success_symbol.id,
                provider="yfinance",
                last_synced_date="2026-04-03",
                status="success",
                message="already synced",
            )

        with patch(
            "app.services.cn_market_universe.sync_market_data",
            return_value=[{"ticker": "600012.SS", "status": "success", "rows": 180}],
        ) as sync_mock, patch(
            "app.services.cn_market_universe.rebuild_technical_snapshots",
            return_value={"status": "success", "message": "Rebuilt 1 technical snapshot(s) for CN."},
        ):
            result = init_cn_market_data(days_back=180, retry_failed=True)

        self.assertEqual("success", result["status"])
        self.assertTrue(result["retry_failed"])
        sync_mock.assert_called_once()
        self.assertEqual(["600012.SS"], sync_mock.call_args.kwargs["tickers"])

    def test_init_cn_market_data_skips_beijing_exchange_symbols(self) -> None:
        from app.services.cn_market_universe import init_cn_market_data

        self._seed_symbol("600011.SS", "沪市股", "CN", "SSE")
        self._seed_symbol("830001.BJ", "北交所股", "CN", "BSE")

        with patch(
            "app.services.cn_market_universe.sync_market_data",
            return_value=[{"ticker": "600011.SS", "status": "success", "rows": 180}],
        ) as sync_mock, patch(
            "app.services.cn_market_universe.rebuild_technical_snapshots",
            return_value={"status": "success", "message": "Rebuilt 1 technical snapshot(s) for CN."},
        ):
            result = init_cn_market_data(days_back=180)

        self.assertEqual("success", result["status"])
        self.assertEqual(1, result["total_symbols"])
        self.assertEqual(["600011.SS"], sync_mock.call_args.kwargs["tickers"])

    def test_refresh_cn_market_data_auto_rebuilds_technical_snapshots(self) -> None:
        from app.services.cn_market_universe import refresh_cn_market_data

        self._seed_symbol("600004.SS", "刷新缓存股", "CN", "SSE")

        with patch(
            "app.services.cn_market_universe.sync_market_data",
            return_value=[{"ticker": "600004.SS", "status": "success", "rows": 7}],
        ), patch(
            "app.services.cn_market_universe.rebuild_technical_snapshots",
            return_value={"status": "success", "message": "Rebuilt 1 technical snapshot(s) for CN."},
        ) as rebuild_mock:
            result = refresh_cn_market_data(days_back=7, limit=1)

        self.assertEqual("success", result["status"])
        self.assertIn("technical_snapshot_rebuild", result)
        rebuild_mock.assert_called_once()

    def test_refresh_cn_market_data_skips_beijing_exchange_symbols(self) -> None:
        from app.services.cn_market_universe import refresh_cn_market_data

        self._seed_symbol("600004.SS", "刷新缓存股", "CN", "SSE")
        self._seed_symbol("830002.BJ", "北交刷新股", "CN", "BSE")

        with patch(
            "app.services.cn_market_universe.sync_market_data",
            return_value=[{"ticker": "600004.SS", "status": "success", "rows": 7}],
        ) as sync_mock, patch(
            "app.services.cn_market_universe.rebuild_technical_snapshots",
            return_value={"status": "success", "message": "Rebuilt 1 technical snapshot(s) for CN."},
        ):
            result = refresh_cn_market_data(days_back=7)

        self.assertEqual("success", result["status"])
        self.assertEqual(1, result["total_symbols"])
        self.assertEqual(["600004.SS"], sync_mock.call_args.kwargs["tickers"])

    def test_refresh_cn_market_data_incremental_uses_last_synced_date_window(self) -> None:
        from app.core.db import SessionLocal
        from app.services.cn_market_universe import refresh_cn_market_data
        from app.services.repository import PriceSyncStateRepository, SymbolRepository

        self._seed_symbol("600004.SS", "刷新缓存股", "CN", "SSE")

        with SessionLocal() as db:
            symbol = SymbolRepository(db).get_by_ticker("600004.SS")
            PriceSyncStateRepository(db).upsert_state(
                symbol_id=symbol.id,
                provider="eastmoney",
                last_synced_date="2026-04-03",
                status="success",
                message="already synced",
            )

        with patch(
            "app.services.cn_market_universe.sync_market_data",
            return_value=[{"ticker": "600004.SS", "status": "success", "rows": 3, "stored_rows": 180}],
        ) as sync_mock, patch(
            "app.services.cn_market_universe.rebuild_technical_snapshots",
            return_value={"status": "success", "message": "Rebuilt 1 technical snapshot(s) for CN."},
        ):
            result = refresh_cn_market_data(days_back=7, limit=1, incremental=True, overlap_days=3)

        self.assertEqual("success", result["status"])
        self.assertTrue(result["incremental"])
        self.assertEqual({"600004.SS": "2026-03-31"}, sync_mock.call_args.kwargs["start_dates_by_ticker"])

    def test_sync_cn_symbol_universe_falls_back_to_akshare_when_tushare_fails(self) -> None:
        from app.core.db import SessionLocal
        from app.services.cn_market_universe import sync_cn_symbol_universe
        from app.services.repository import SymbolRepository

        with patch(
            "app.services.cn_market_universe.TushareClient.is_configured",
            return_value=True,
        ), patch(
            "app.services.cn_market_universe.TushareClient.fetch_cn_symbol_universe",
            side_effect=RuntimeError("抱歉，您没有接口访问权限"),
        ), patch(
            "app.services.cn_market_universe._fetch_cn_symbol_universe_from_akshare",
            return_value=[
                {"ticker": "600010.SS", "name": "包钢股份", "exchange": "SSE"},
                {"ticker": "000001.SZ", "name": "平安银行", "exchange": "SZSE"},
            ],
        ):
            result = sync_cn_symbol_universe()

        self.assertEqual("success", result["status"])
        self.assertEqual("akshare", result["source"])
        with SessionLocal() as db:
            tickers = [symbol.ticker for symbol in SymbolRepository(db).list_symbols()]
        self.assertIn("600010.SS", tickers)
        self.assertIn("000001.SZ", tickers)

    def test_sync_cn_symbol_universe_skips_beijing_exchange_rows(self) -> None:
        from app.core.db import SessionLocal
        from app.services.cn_market_universe import sync_cn_symbol_universe
        from app.services.repository import SymbolRepository

        with patch(
            "app.services.cn_market_universe.TushareClient.is_configured",
            return_value=True,
        ), patch(
            "app.services.cn_market_universe.TushareClient.fetch_cn_symbol_universe",
            return_value=[
                {"ticker": "600010.SS", "name": "包钢股份", "exchange": "SSE"},
                {"ticker": "830001.BJ", "name": "北交测试", "exchange": "BSE"},
            ],
        ):
            result = sync_cn_symbol_universe()

        self.assertEqual("success", result["status"])
        self.assertEqual(1, result["symbols_written"])
        self.assertEqual(["600010.SS"], result["tickers"])
        with SessionLocal() as db:
            tickers = [symbol.ticker for symbol in SymbolRepository(db).list_symbols()]
        self.assertIn("600010.SS", tickers)
        self.assertNotIn("830001.BJ", tickers)

    def test_dashboard_ops_models_page_supports_chinese_language(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="dashboard_ops_models_demo", signal_type="momentum", lookback_days=3)

        response = self.client.get("/dashboard/ops/models?lang=zh&lookback_runs=3")

        self.assertEqual(200, response.status_code)
        self.assertIn("模型运行", response.text)
        self.assertIn("训练与回测视图", response.text)
        self.assertIn("最近模型运行", response.text)

    def test_dashboard_ops_jobs_page_supports_chinese_language(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data

        seed_sample_data()
        build_dataset(normalize_only=True)

        response = self.client.get("/dashboard/ops/jobs?lang=zh&lookback_runs=3")

        self.assertEqual(200, response.status_code)
        self.assertIn("任务记录", response.text)
        self.assertIn("最近任务", response.text)

    def test_dashboard_data_sources_supports_chinese_language(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data

        seed_sample_data()
        build_dataset(normalize_only=True)

        response = self.client.get("/dashboard/data-sources?lang=zh")

        self.assertEqual(200, response.status_code)
        self.assertIn("这个应用的数据来自哪里", response.text)
        self.assertIn("数据源分布", response.text)
        self.assertIn("逐股同步来源", response.text)
        self.assertIn("A股概念", response.text)

    def test_dashboard_continuous_leader_action_adds_watchlist_item(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer
        from app.core.db import SessionLocal
        from app.services.repository import WatchlistRepository

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="continuous_leader_action_demo", signal_type="momentum", lookback_days=3)

        response = self.client.post(
            "/dashboard/continuous-leaders/action",
            data={"ticker": "AAPL", "action": "add", "lookback_runs": "5"},
            follow_redirects=True,
        )

        self.assertEqual(200, response.status_code)
        self.assertIn("Added AAPL to watchlist", response.text)
        self.assertIn("Continuous Leaders", response.text)

        with SessionLocal() as db:
            watchlist = WatchlistRepository(db).get_or_create_default()
            watchlist_map = WatchlistRepository(db).list_ticker_map(watchlist.id)
            self.assertIn("AAPL", watchlist_map)

    def test_dashboard_can_add_top_continuous_leaders_to_watchlist(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer
        from app.core.db import SessionLocal
        from app.services.repository import WatchlistRepository

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="continuous_leader_bulk_demo", signal_type="momentum", lookback_days=3)

        response = self.client.post(
            "/dashboard/continuous-leaders/watchlist-top",
            data={
                "tickers_csv": "AAPL,ASTS",
                "top_n": "1",
                "lookback_runs": "5",
                "auto_enable_sync": "1",
            },
            follow_redirects=True,
        )

        self.assertEqual(200, response.status_code)
        self.assertIn("Added top 1 continuous leader(s) to watchlist", response.text)

        with SessionLocal() as db:
            watchlist = WatchlistRepository(db).get_or_create_default()
            watchlist_map = WatchlistRepository(db).list_ticker_map(watchlist.id)
            self.assertEqual(1, len(watchlist_map))
            only_item = next(iter(watchlist_map.values()))
            self.assertEqual(1, only_item["sync_enabled"])

    def test_dashboard_continuous_leaders_page_supports_chinese_language(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="continuous_leaders_page_demo", signal_type="momentum", lookback_days=3)

        response = self.client.get("/dashboard/continuous-leaders?lang=zh&lookback_runs=3")

        self.assertEqual(200, response.status_code)
        self.assertIn("连续强势股", response.text)
        self.assertIn("连续强势股详情", response.text)
        self.assertIn("应用筛选", response.text)
        self.assertIn("全部信号", response.text)
        self.assertIn("最低强度", response.text)

    def test_dashboard_continuous_leaders_page_supports_signal_filter(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="continuous_leaders_signal_filter_demo", signal_type="momentum", lookback_days=3)

        response = self.client.get(
            "/dashboard/continuous-leaders?lookback_runs=3&continuous_signal=BUY&min_signal_strength=1"
        )

        self.assertEqual(200, response.status_code)
        self.assertIn("Signal", response.text)
        self.assertIn("Min Strength", response.text)

    def test_dashboard_continuous_leaders_export_csv_returns_rows(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="continuous_leaders_export_demo", signal_type="momentum", lookback_days=3)

        response = self.client.get("/dashboard/continuous-leaders/export?lookback_runs=3")

        self.assertEqual(200, response.status_code)
        self.assertIn("text/csv", response.headers.get("content-type", ""))
        self.assertIn("continuous_leaders_3runs.csv", response.headers.get("content-disposition", ""))
        self.assertIn(
            "ticker,name,market,hits,runs,score,signal_label,signal_strength,conviction_bucket,position_size_hint,entry_style,execution_tags,percentile,target_horizon_days,expected_drawdown_20d,model_reward_risk_ratio,trade_date,continuous_state",
            response.text,
        )

        filtered = self.client.get(
            "/dashboard/continuous-leaders/export?lookback_runs=3&continuous_signal=BUY&min_signal_strength=1"
        )
        self.assertEqual(200, filtered.status_code)
        self.assertIn("signal_label", filtered.text)

    def test_screener_supports_execution_tag_filters(self) -> None:
        from sqlalchemy import select

        from app.core.db import SessionLocal
        from app.models.tables import ModelRun, Prediction
        from app.services.dataset_build import build_dataset
        from app.services.repository import PredictionTradePlanRepository, SymbolRepository
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="screener_execution_tag_demo", signal_type="momentum", lookback_days=3)
        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            aapl = symbol_repo.get_by_ticker("AAPL")
            msft = symbol_repo.get_by_ticker("MSFT")
            model_run = db.scalar(select(ModelRun).order_by(ModelRun.id.desc()))
            predictions = list(db.execute(select(Prediction).where(Prediction.model_run_id == model_run.id)).scalars())
            rows = []
            for prediction in predictions:
                if prediction.symbol_id == aapl.id:
                    rows.append({"symbol_id": prediction.symbol_id, "trade_date": prediction.trade_date, "execution_tags": ["gap-risk"]})
                elif prediction.symbol_id == msft.id:
                    rows.append({"symbol_id": prediction.symbol_id, "trade_date": prediction.trade_date, "execution_tags": ["earnings-soon"]})
            PredictionTradePlanRepository(db).replace_for_model_run(model_run.id, rows)

        include_response = self.client.get("/screeners?universe=synced&execution_tag_filter=gap-risk")
        self.assertEqual(200, include_response.status_code)
        self.assertIn("Risk Overview", include_response.text)
        self.assertIn("execution_tag_filter=gap-risk", include_response.text)
        self.assertIn("gap-risk", include_response.text)
        self.assertIn("快捷标签", self.client.get("/screeners?lang=zh").text)
        self.assertIn("/insights/AAPL?lang=en", include_response.text)
        self.assertNotIn("/insights/MSFT?lang=en", include_response.text)

        exclude_response = self.client.get("/screeners?universe=synced&exclude_execution_tag_filter=gap-risk")
        self.assertEqual(200, exclude_response.status_code)
        self.assertIn("exclude_execution_tag_filter=gap-risk", exclude_response.text)
        self.assertNotIn("/insights/AAPL?lang=en", exclude_response.text)

    def test_dashboard_continuous_leaders_supports_execution_tag_filters(self) -> None:
        from sqlalchemy import select

        from app.core.db import SessionLocal
        from app.models.tables import ModelRun, Prediction
        from app.services.dataset_build import build_dataset
        from app.services.repository import PredictionTradePlanRepository, SymbolRepository
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="continuous_execution_tag_demo", signal_type="momentum", lookback_days=3)
        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            aapl = symbol_repo.get_by_ticker("AAPL")
            msft = symbol_repo.get_by_ticker("MSFT")
            model_run = db.scalar(select(ModelRun).order_by(ModelRun.id.desc()))
            predictions = list(db.execute(select(Prediction).where(Prediction.model_run_id == model_run.id)).scalars())
            rows = []
            for prediction in predictions:
                if prediction.symbol_id == aapl.id:
                    rows.append({"symbol_id": prediction.symbol_id, "trade_date": prediction.trade_date, "execution_tags": ["gap-risk"]})
                elif prediction.symbol_id == msft.id:
                    rows.append({"symbol_id": prediction.symbol_id, "trade_date": prediction.trade_date, "execution_tags": ["earnings-soon"]})
            PredictionTradePlanRepository(db).replace_for_model_run(model_run.id, rows)

        include_response = self.client.get("/dashboard/continuous-leaders?lookback_runs=3&execution_tag_filter=gap-risk")
        self.assertEqual(200, include_response.status_code)
        self.assertIn("Risk Overview", include_response.text)
        self.assertIn("execution_tag_filter=gap-risk", include_response.text)
        self.assertIn("Quick Tags", include_response.text)
        self.assertIn("gap-risk", include_response.text)
        self.assertIn("/insights/AAPL?lang=en", include_response.text)
        self.assertNotIn("/insights/MSFT?lang=en", include_response.text)

        exclude_response = self.client.get("/dashboard/continuous-leaders?lookback_runs=3&exclude_execution_tag_filter=gap-risk")
        self.assertEqual(200, exclude_response.status_code)
        self.assertIn("exclude_execution_tag_filter=gap-risk", exclude_response.text)
        self.assertIn("earnings-soon", exclude_response.text)
        self.assertIn("/insights/MSFT?lang=en", exclude_response.text)
        self.assertNotIn("/insights/AAPL?lang=en", exclude_response.text)

    def test_dashboard_concept_detail_adds_tickers_to_watchlist_and_syncs(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer
        from app.core.db import SessionLocal
        from app.services.openbb_client import OpenBBClient
        from app.services.repository import ConceptSnapshotRepository, SymbolRepository, WatchlistRepository

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="concept_watchlist_demo", signal_type="momentum", lookback_days=3)
        with SessionLocal() as db:
            symbol = SymbolRepository(db).get_by_ticker("AAPL")
            self.assertIsNotNone(symbol)
            ConceptSnapshotRepository(db).upsert_snapshot(
                symbol_id=symbol.id,
                concept_name="Consumer Electronics",
                concept_code="C001",
                as_of_date="2026-04-03",
                source="test",
            )

        def fake_fetch(self, request) -> list[dict]:
            return [
                {
                    "date": "2026-04-01",
                    "symbol": request.ticker,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 100000,
                },
                {
                    "date": "2026-04-02",
                    "symbol": request.ticker,
                    "open": 100.5,
                    "high": 102.0,
                    "low": 100.0,
                    "close": 101.2,
                    "volume": 120000,
                },
            ]

        with patch.object(OpenBBClient, "fetch_historical_prices", new=fake_fetch):
            response = self.client.post(
                "/dashboard/concepts/consumer-electronics/watchlist",
                data={
                    "tickers_csv": "AAPL",
                    "auto_enable_sync": "1",
                    "sync_after_add": "1",
                },
                follow_redirects=True,
            )

        self.assertEqual(200, response.status_code)
        self.assertIn("Added 1 concept stock(s) to watchlist", response.text)
        self.assertIn("Sync enabled for 1", response.text)
        self.assertIn("Synced 1/1", response.text)

        with SessionLocal() as db:
            watchlist = WatchlistRepository(db).get_or_create_default()
            watchlist_map = WatchlistRepository(db).list_ticker_map(watchlist.id)
            self.assertIn("AAPL", watchlist_map)
            self.assertEqual(1, watchlist_map["AAPL"]["sync_enabled"])

    def test_dashboard_concept_detail_single_ticker_action_adds_watchlist_item(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer
        from app.core.db import SessionLocal
        from app.services.repository import ConceptSnapshotRepository, SymbolRepository, WatchlistRepository

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="concept_single_action_demo", signal_type="momentum", lookback_days=3)
        with SessionLocal() as db:
            symbol = SymbolRepository(db).get_by_ticker("AAPL")
            self.assertIsNotNone(symbol)
            ConceptSnapshotRepository(db).upsert_snapshot(
                symbol_id=symbol.id,
                concept_name="Consumer Electronics",
                concept_code="C001",
                as_of_date="2026-04-03",
                source="test",
            )

        response = self.client.post(
            "/dashboard/concepts/consumer-electronics/ticker-action",
            data={"ticker": "AAPL", "action": "add"},
            follow_redirects=True,
        )

        self.assertEqual(200, response.status_code)
        self.assertIn("Added AAPL to watchlist", response.text)
        self.assertIn("Actions", response.text)

        with SessionLocal() as db:
            watchlist = WatchlistRepository(db).get_or_create_default()
            watchlist_map = WatchlistRepository(db).list_ticker_map(watchlist.id)
            self.assertIn("AAPL", watchlist_map)

    def test_dashboard_concept_detail_shows_sortable_columns_and_five_day_metric(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer
        from app.core.db import SessionLocal
        from app.services.repository import ConceptSnapshotRepository, SymbolRepository

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="concept_sort_demo", signal_type="momentum", lookback_days=3)
        with SessionLocal() as db:
            symbol = SymbolRepository(db).get_by_ticker("AAPL")
            self.assertIsNotNone(symbol)
            ConceptSnapshotRepository(db).upsert_snapshot(
                symbol_id=symbol.id,
                concept_name="Consumer Electronics",
                concept_code="C001",
                as_of_date="2026-04-03",
                source="test",
            )

        response = self.client.get("/dashboard/concepts/consumer-electronics?sort_by=five_day&sort_order=desc")

        self.assertEqual(200, response.status_code)
        self.assertIn("5D %", response.text)
        self.assertIn("Watchlist", response.text)
        self.assertIn("sort_by=score", response.text)
        self.assertIn("sort_by=five_day", response.text)

    def test_dashboard_concept_detail_add_top_n_to_watchlist(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer
        from app.core.db import SessionLocal
        from app.services.openbb_client import OpenBBClient
        from app.services.repository import ConceptSnapshotRepository, SymbolRepository, WatchlistRepository

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="concept_topn_demo", signal_type="momentum", lookback_days=3)
        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            aapl = symbol_repo.get_by_ticker("AAPL")
            msft = symbol_repo.get_by_ticker("MSFT")
            self.assertIsNotNone(aapl)
            self.assertIsNotNone(msft)
            repo = ConceptSnapshotRepository(db)
            repo.upsert_snapshot(symbol_id=aapl.id, concept_name="Consumer Electronics", concept_code="C001", as_of_date="2026-04-03", source="test")
            repo.upsert_snapshot(symbol_id=msft.id, concept_name="Consumer Electronics", concept_code="C001", as_of_date="2026-04-03", source="test")

        def fake_fetch(self, request) -> list[dict]:
            return [
                {
                    "date": "2026-04-01",
                    "symbol": request.ticker,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 100000,
                },
                {
                    "date": "2026-04-02",
                    "symbol": request.ticker,
                    "open": 100.5,
                    "high": 102.0,
                    "low": 100.0,
                    "close": 101.2,
                    "volume": 120000,
                },
            ]

        with patch.object(OpenBBClient, "fetch_historical_prices", new=fake_fetch):
            response = self.client.post(
                "/dashboard/concepts/consumer-electronics/watchlist-top",
                data={
                    "tickers_csv": "AAPL,MSFT",
                    "top_n": "1",
                    "auto_enable_sync": "1",
                    "sync_after_add": "1",
                },
                follow_redirects=True,
            )

        self.assertEqual(200, response.status_code)
        self.assertIn("Added top 1 concept stock(s) to watchlist", response.text)
        self.assertIn("Sync enabled for 1", response.text)
        self.assertIn("Synced 1/1", response.text)

        with SessionLocal() as db:
            watchlist = WatchlistRepository(db).get_or_create_default()
            watchlist_map = WatchlistRepository(db).list_ticker_map(watchlist.id)
            self.assertIn("AAPL", watchlist_map)
            self.assertNotIn("MSFT", watchlist_map)

    def test_dashboard_concept_detail_shows_top_movers_comparison(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer
        from app.core.db import SessionLocal
        from app.services.repository import ConceptSnapshotRepository, SymbolRepository

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="concept_compare_demo", signal_type="momentum", lookback_days=3)
        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            aapl = symbol_repo.get_by_ticker("AAPL")
            msft = symbol_repo.get_by_ticker("MSFT")
            repo = ConceptSnapshotRepository(db)
            repo.upsert_snapshot(symbol_id=aapl.id, concept_name="Consumer Electronics", concept_code="C001", as_of_date="2026-04-03", source="test")
            repo.upsert_snapshot(symbol_id=msft.id, concept_name="Consumer Electronics", concept_code="C001", as_of_date="2026-04-03", source="test")

        response = self.client.get("/dashboard/concepts/consumer-electronics")

        self.assertEqual(200, response.status_code)
        self.assertIn("Top Movers Comparison", response.text)
        self.assertIn("score", response.text)
        self.assertIn("20D", response.text)
        self.assertIn("Pct ", response.text)
        self.assertIn("R/R ", response.text)
        self.assertIn("Top by Model", response.text)
        self.assertIn("Top by 20D", response.text)
        self.assertIn("Ready First", response.text)
        self.assertIn("comparison_sort=momentum_20d", response.text)
        self.assertIn("price signal sparkline", response.text)
        self.assertIn("<title>", response.text)
        self.assertIn("Concept Strength", response.text)
        self.assertIn("Breadth", response.text)
        self.assertIn("20D %", response.text)

    def test_dashboard_concept_detail_supports_chinese_language(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer
        from app.core.db import SessionLocal
        from app.services.repository import ConceptSnapshotRepository, SymbolRepository

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="concept_zh_demo", signal_type="momentum", lookback_days=3)
        with SessionLocal() as db:
            symbol = SymbolRepository(db).get_by_ticker("AAPL")
            self.assertIsNotNone(symbol)
            ConceptSnapshotRepository(db).upsert_snapshot(
                symbol_id=symbol.id,
                concept_name="Consumer Electronics",
                concept_code="C001",
                as_of_date="2026-04-03",
                source="test",
            )

        response = self.client.get("/dashboard/concepts/consumer-electronics?lang=zh")

        self.assertEqual(200, response.status_code)
        self.assertIn("概念详情", response.text)
        self.assertIn("强势股对比", response.text)
        self.assertIn("将概念股加入自选", response.text)
        self.assertIn("股票明细", response.text)
        self.assertIn("全部信号", response.text)
        self.assertIn("买点", response.text)
        self.assertIn("最低强度", response.text)
        self.assertIn("买点股数量", response.text)
        self.assertIn("最强信号强度", response.text)
        self.assertIn("Pct ", response.text)
        self.assertIn("R/R ", response.text)

    def test_dashboard_concept_detail_supports_signal_strength_filter(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer
        from app.core.db import SessionLocal
        from app.services.repository import ConceptSnapshotRepository, SymbolRepository

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="concept_signal_strength_demo", signal_type="momentum", lookback_days=3)
        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            aapl = symbol_repo.get_by_ticker("AAPL")
            msft = symbol_repo.get_by_ticker("MSFT")
            repo = ConceptSnapshotRepository(db)
            repo.upsert_snapshot(symbol_id=aapl.id, concept_name="Consumer Electronics", concept_code="C001", as_of_date="2026-04-03", source="test")
            repo.upsert_snapshot(symbol_id=msft.id, concept_name="Consumer Electronics", concept_code="C001", as_of_date="2026-04-03", source="test")

        response = self.client.get("/dashboard/concepts/consumer-electronics?signal_filter=BUY&min_signal_strength=1")

        self.assertEqual(200, response.status_code)
        self.assertIn("Signal Filter", response.text)
        self.assertIn("Min Strength", response.text)

    def test_insight_model_output_endpoint_and_page(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer
        from app.core.db import SessionLocal
        from app.services.repository import FundamentalSnapshotRepository, SymbolRepository

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="insight_model_demo", signal_type="momentum", lookback_days=3)
        with SessionLocal() as db:
            symbol = SymbolRepository(db).get_by_ticker("AAPL")
            self.assertIsNotNone(symbol)
            FundamentalSnapshotRepository(db).upsert_snapshot(
                symbol_id=symbol.id,
                report_date="2026-04-03",
                source="test",
                pe_ttm=22.5,
                roe_avg_3y=18.4,
                net_profit_yoy=24.0,
                revenue_yoy=12.0,
                debt_to_assets=42.0,
            )

        api_response = self.client.get("/insights/AAPL/model-output?lang=en")
        self.assertEqual(200, api_response.status_code)
        payload = api_response.json()
        self.assertEqual("AAPL", payload["ticker"])
        self.assertIn("model_run", payload)
        self.assertEqual("insight_model_demo", payload["model_run"]["name"])
        self.assertIn("summary_text", payload)
        self.assertTrue(payload["summary_text"])
        self.assertIn("confidence", payload)
        self.assertIn("signal_label", payload)
        self.assertIn("signal_strength", payload)
        self.assertIn("conviction_bucket", payload)
        self.assertTrue(payload["conviction_bucket"])
        self.assertIn("position_size_hint", payload)
        self.assertTrue(payload["position_size_hint"])
        self.assertIn("entry_style", payload)
        self.assertTrue(payload["entry_style"])
        self.assertIn("bullish_prob", payload)
        self.assertIn("bearish_prob", payload)
        self.assertIn("expected_return_5d", payload)
        self.assertIn("expected_return_20d", payload)
        self.assertIn("expected_drawdown_20d", payload)
        self.assertIn("model_reward_risk_ratio", payload)
        self.assertIn("target_horizon_days", payload)
        self.assertIn("percentile", payload)
        self.assertIn("regime_label", payload)
        self.assertIn("risk_score", payload)
        self.assertIn("drivers", payload)
        self.assertIn("positive", payload["drivers"])
        self.assertIn("risks", payload["drivers"])
        self.assertTrue(payload["drivers"]["positive"])
        self.assertIn("fundamentals", payload)
        self.assertEqual(22.5, payload["fundamentals"]["pe_ttm"])
        self.assertIn("feature_contributions", payload)
        self.assertIn("positive", payload["feature_contributions"])
        self.assertTrue(payload["feature_contributions"]["positive"])
        self.assertIsNotNone(payload["expected_drawdown_20d"])
        self.assertIsNotNone(payload["model_reward_risk_ratio"])
        self.assertIsNotNone(payload["target_horizon_days"])
        self.assertTrue(
            any(
                item["feature_name"] == "recent_daily_return"
                for item in payload["feature_contributions"]["positive"] + payload["feature_contributions"]["negative"]
            )
        )
        self.assertTrue(
            any(
                item["feature_name"].startswith("lag_return_")
                for item in payload["feature_contributions"]["positive"] + payload["feature_contributions"]["negative"]
            )
        )
        self.assertTrue(
            any(
                item["feature_name"] == "price_vs_ma20"
                for item in payload["feature_contributions"]["positive"] + payload["feature_contributions"]["negative"]
            )
        )
        self.assertTrue(
            any(
                item["feature_name"] == "volume_ratio_20d"
                for item in payload["feature_contributions"]["positive"] + payload["feature_contributions"]["negative"]
            )
        )

        chart_response = self.client.get("/insights/AAPL/chart-data?lang=en")
        self.assertEqual(200, chart_response.status_code)
        chart_payload = chart_response.json()
        self.assertEqual("AAPL", chart_payload["ticker"])
        self.assertIn("candles", chart_payload)
        self.assertIn("signals", chart_payload)
        self.assertTrue(chart_payload["candles"])

        page_response = self.client.get("/insights/AAPL?lang=en")
        self.assertEqual(200, page_response.status_code)
        self.assertIn("Model Output", page_response.text)
        self.assertIn("Model Summary", page_response.text)
        self.assertIn("insight_model_demo", page_response.text)
        self.assertIn("Bullish Probability", page_response.text)
        self.assertIn("Expected 5D Return", page_response.text)
        self.assertIn("Expected 20D Drawdown", page_response.text)
        self.assertIn("Model Reward / Risk", page_response.text)
        self.assertIn("Conviction", page_response.text)
        self.assertIn("Position Size Hint", page_response.text)
        self.assertIn("Entry Style", page_response.text)
        self.assertIn("Regime", page_response.text)
        self.assertIn("Risk Score", page_response.text)
        self.assertIn("Top Drivers", page_response.text)
        self.assertIn("Feature Contributions", page_response.text)
        self.assertIn("Positive Drivers", page_response.text)
        self.assertIn("Recent Daily Return", page_response.text)
        self.assertIn("Lagged Return", page_response.text)
        self.assertIn("Price vs MA20", page_response.text)
        self.assertIn("Volume Ratio (20D)", page_response.text)
        self.assertIn("PE TTM stays reasonable", page_response.text)
        self.assertIn("interactive-chart", page_response.text)

    def test_insight_page_renders_when_model_output_is_missing(self) -> None:
        from app.core.config import get_settings
        from app.core.db import SessionLocal
        from app.models.schema import SymbolCreate
        from app.services.repository import SymbolRepository

        settings = get_settings()
        path = settings.normalized_data_dir / "0100.HK.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "date,symbol,open,high,low,close,volume,adj_close,dividend,split_ratio\n"
            "2026-04-01,0100.HK,1160,1221,1072,1084,1634305,1084,,\n"
            "2026-04-02,0100.HK,1100,1124,992,1010,1297992,1010,,\n",
            encoding="utf-8",
        )

        with SessionLocal() as db:
            SymbolRepository(db).get_or_create_symbol(
                SymbolCreate(ticker="0100.HK", name="MINIMAX-W", market="HK", exchange="HKEX")
            )

        page_response = self.client.get("/insights/00100.HK?lang=en")
        self.assertEqual(200, page_response.status_code)
        self.assertIn("Model Output", page_response.text)
        self.assertIn("No trained model output is available for this stock yet.", page_response.text)
        self.assertIn("MINIMAX-W", page_response.text)

    def test_import_model_output_job_populates_prediction_details(self) -> None:
        csv_path = self.temp_path / "external_model.csv"
        csv_path.write_text(
            "ticker,trade_date,score,confidence,bullish_prob,bearish_prob,expected_return_5d,expected_return_20d,expected_drawdown_20d,model_reward_risk_ratio,target_horizon_days,percentile,signal_label,signal_strength,conviction_bucket,position_size_hint,entry_style,summary_text\n"
            "AAPL,2026-04-03,0.26,81,72.0,28.0,4.2,9.5,6.5,1.46,10,91.2,Buy,78,High Conviction,Aggressive,Breakout,Imported external model signal.\n",
            encoding="utf-8",
        )

        response = self.client.post(
            "/jobs/import-model-output",
            data={
                "csv_path": str(csv_path),
                "run_name": "qlib_import_demo",
                "model_type": "qlib_external",
                "market": "US",
                "universe": "imported_demo",
            },
        )
        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("success", payload["status"])
        self.assertEqual(1, payload["predictions_written"])
        self.assertEqual(1, payload["details_written"])

        api_response = self.client.get("/insights/AAPL/model-output?lang=en")
        self.assertEqual(200, api_response.status_code)
        model_output = api_response.json()
        self.assertEqual("qlib_import_demo", model_output["model_run"]["name"])
        self.assertEqual("Aggressive", model_output["position_size_hint"])
        self.assertEqual("Breakout", model_output["entry_style"])
        self.assertEqual("High Conviction", model_output["conviction_bucket"])
        self.assertEqual("Imported external model signal.", model_output["summary_text"])

    def test_qlib_prediction_adapter_imports_rows(self) -> None:
        from app.services.qlib_prediction_adapter import QlibPredictionAdapter

        rows = [
            {
                "ticker": "ASTS",
                "name": "AST SpaceMobile",
                "trade_date": "2026-04-03",
                "score": 0.19,
                "confidence": 74,
                "bullish_prob": 68.0,
                "bearish_prob": 32.0,
                "expected_return_5d": 3.5,
                "expected_return_20d": 8.4,
                "expected_drawdown_20d": 6.2,
                "model_reward_risk_ratio": 1.35,
                "target_horizon_days": 10,
                "percentile": 84.2,
                "signal_label": "Buy",
                "signal_strength": 72,
                "conviction_bucket": "High Conviction",
                "position_size_hint": "Aggressive",
                "entry_style": "Breakout",
                "summary_text": "Imported from qlib adapter.",
            }
        ]

        result = QlibPredictionAdapter().import_prediction_rows(
            rows,
            run_name="qlib_adapter_demo",
            model_type="qlib_external",
            market="US",
            universe="adapter_demo",
        )
        self.assertEqual(1, result["predictions_written"])
        self.assertEqual(1, result["details_written"])

        api_response = self.client.get("/insights/ASTS/model-output?lang=en")
        self.assertEqual(200, api_response.status_code)
        payload = api_response.json()
        self.assertEqual("qlib_adapter_demo", payload["model_run"]["name"])
        self.assertEqual("Aggressive", payload["position_size_hint"])
        self.assertEqual("Breakout", payload["entry_style"])
        self.assertEqual("Imported from qlib adapter.", payload["summary_text"])

    def test_qlib_predictor_imports_prediction_csv(self) -> None:
        from app.services.qlib_predictor import QlibPredictor

        qlib_dir = self.temp_path / "data" / "qlib"
        qlib_dir.mkdir(parents=True, exist_ok=True)
        (qlib_dir / "calendar").mkdir(parents=True, exist_ok=True)

        csv_path = self.temp_path / "qlib_predictions.csv"
        csv_path.write_text(
            "ticker,name,trade_date,score,confidence,bullish_prob,bearish_prob,expected_return_5d,expected_return_20d,expected_drawdown_20d,model_reward_risk_ratio,target_horizon_days,percentile,signal_label,signal_strength,conviction_bucket,position_size_hint,entry_style,summary_text\n"
            "RKLB,Rocket Lab,2026-04-03,0.17,73,66.0,34.0,3.1,7.2,6.0,1.20,10,82.4,Buy,66,Medium Conviction,Standard,Pullback,Imported through qlib predictor.\n",
            encoding="utf-8",
        )

        result = QlibPredictor().predict(
            run_name="qlib_predictor_demo",
            market="US",
            universe="predictor_demo",
            predictions_csv=csv_path,
        )
        self.assertEqual("import_csv", result["mode"])
        self.assertEqual(1, result["predictions_written"])

        api_response = self.client.get("/insights/RKLB/model-output?lang=en")
        self.assertEqual(200, api_response.status_code)
        payload = api_response.json()
        self.assertEqual("qlib_predictor_demo", payload["model_run"]["name"])
        self.assertEqual("Standard", payload["position_size_hint"])
        self.assertEqual("Pullback", payload["entry_style"])
        self.assertEqual("Imported through qlib predictor.", payload["summary_text"])

    def test_qlib_predictor_manifest_exposes_expected_schema(self) -> None:
        from app.services.qlib_predictor import QlibPredictor

        qlib_dir = self.temp_path / "data" / "qlib"
        qlib_dir.mkdir(parents=True, exist_ok=True)
        (qlib_dir / "calendar").mkdir(parents=True, exist_ok=True)

        manifest = QlibPredictor().build_native_inference_manifest(
            run_name="qlib_native_demo",
            market="US",
            universe="native_demo",
        )

        self.assertEqual("native_inference", manifest["mode"])
        self.assertEqual("qlib_native_demo", manifest["run_name"])
        self.assertIn("expected_schema", manifest)
        self.assertIn("required", manifest["expected_schema"])
        self.assertIn("ticker", manifest["expected_schema"]["required"])
        self.assertIn("fieldnames", manifest["expected_schema"])
        self.assertIn("entry_style", manifest["expected_schema"]["fieldnames"])
        self.assertIn("accepted_artifact_layouts", manifest)
        self.assertEqual("directory", manifest["accepted_artifact_layouts"][0]["type"])
        self.assertIn("predictions.csv", manifest["accepted_artifact_layouts"][0]["required_files"])
        self.assertIn("predictions.json", manifest["accepted_artifact_layouts"][0]["accepted_prediction_files"])
        self.assertIn("predictions.jsonl", manifest["accepted_artifact_layouts"][0]["accepted_prediction_files"])
        self.assertIn("native_result.json", manifest["accepted_artifact_layouts"][0]["accepted_prediction_files"])
        self.assertIn("manifest.json", manifest["accepted_artifact_layouts"][0]["optional_files"])
        self.assertIn("explanations.csv", manifest["accepted_artifact_layouts"][0]["optional_files"])
        self.assertIn("features.csv", manifest["accepted_artifact_layouts"][0]["optional_files"])
        self.assertIn("chart_signals.csv", manifest["accepted_artifact_layouts"][0]["optional_files"])
        self.assertIn("*.json", manifest["accepted_artifact_layouts"][1]["accepted_filenames"])
        self.assertIn("*.jsonl", manifest["accepted_artifact_layouts"][1]["accepted_filenames"])
        self.assertIn("accepted_manifest_fields", manifest)
        self.assertIn("run_name", manifest["accepted_manifest_fields"])
        self.assertIn("accepted_explanation_schema", manifest)
        self.assertIn("feature_name", manifest["accepted_explanation_schema"]["required"])
        self.assertIn("accepted_chart_signal_schema", manifest)
        self.assertIn("signal_label", manifest["accepted_chart_signal_schema"]["optional"])
        self.assertIn("accepted_trade_plan_schema", manifest)
        self.assertIn("entry_low", manifest["accepted_trade_plan_schema"]["optional"])
        self.assertIn("stop_type", manifest["accepted_trade_plan_schema"]["optional"])
        self.assertIn("trailing_stop_pct", manifest["accepted_trade_plan_schema"]["optional"])
        self.assertIn("invalidation_reason", manifest["accepted_trade_plan_schema"]["optional"])
        self.assertIn("execution_tags", manifest["accepted_trade_plan_schema"]["optional"])
        self.assertIn("accepted_native_result_schema", manifest)
        self.assertIn("predictions", manifest["accepted_native_result_schema"]["required"])

    def test_qlib_predictor_native_inference_stub_is_explicit(self) -> None:
        from app.services.qlib_predictor import QlibPredictor

        qlib_dir = self.temp_path / "data" / "qlib"
        qlib_dir.mkdir(parents=True, exist_ok=True)
        (qlib_dir / "calendar").mkdir(parents=True, exist_ok=True)

        predictor = QlibPredictor()
        with patch.object(predictor, "is_available", return_value=True):
            with self.assertRaises(RuntimeError) as exc:
                predictor.predict(
                    run_name="qlib_native_stub_demo",
                    market="US",
                    universe="native_demo",
                )

        self.assertIn("Native Qlib inference is not wired in yet", str(exc.exception))

    def test_qlib_predictor_reads_predictions_csv_from_artifact_directory(self) -> None:
        from app.services.qlib_predictor import QlibPredictor

        qlib_dir = self.temp_path / "data" / "qlib"
        qlib_dir.mkdir(parents=True, exist_ok=True)
        (qlib_dir / "calendar").mkdir(parents=True, exist_ok=True)

        artifact_dir = self.temp_path / "qlib_artifact"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        csv_path = artifact_dir / "predictions.csv"
        csv_path.write_text(
            "ticker,name,trade_date,score,confidence,bullish_prob,bearish_prob,expected_return_5d,expected_return_20d,expected_drawdown_20d,model_reward_risk_ratio,target_horizon_days,percentile,signal_label,signal_strength,conviction_bucket,position_size_hint,entry_style,summary_text\n"
            "ASTS,AST SpaceMobile,2026-04-03,0.23,79,71.0,29.0,3.9,9.1,5.8,1.57,10,88.4,Buy,78,High Conviction,Aggressive,Breakout,Imported from native artifact layout.\n",
            encoding="utf-8",
        )

        result = QlibPredictor().predict(
            run_name="qlib_artifact_demo",
            market="US",
            universe="artifact_demo",
            artifact_path=str(artifact_dir),
        )
        self.assertEqual("native_inference", result["mode"])
        self.assertEqual(1, result["predictions_written"])
        self.assertEqual(1, result["details_written"])

        api_response = self.client.get("/insights/ASTS/model-output?lang=en")
        self.assertEqual(200, api_response.status_code)
        payload = api_response.json()
        self.assertEqual("qlib_artifact_demo", payload["model_run"]["name"])
        self.assertEqual("Aggressive", payload["position_size_hint"])
        self.assertEqual("Breakout", payload["entry_style"])
        self.assertEqual("Imported from native artifact layout.", payload["summary_text"])

    def test_qlib_predictor_reads_predictions_json_from_artifact_directory(self) -> None:
        from app.services.qlib_predictor import QlibPredictor

        qlib_dir = self.temp_path / "data" / "qlib"
        qlib_dir.mkdir(parents=True, exist_ok=True)
        (qlib_dir / "calendar").mkdir(parents=True, exist_ok=True)

        artifact_dir = self.temp_path / "qlib_artifact_json"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "predictions.json").write_text(
            json.dumps(
                [
                    {
                        "ticker": "AAPL",
                        "name": "Apple Inc.",
                        "trade_date": "2026-04-03",
                        "score": 0.31,
                        "confidence": 81,
                        "signal_label": "Buy",
                        "signal_strength": 74,
                        "conviction_bucket": "High Conviction",
                        "position_size_hint": "Aggressive",
                        "entry_style": "Breakout",
                        "summary_text": "Imported from structured Qlib JSON.",
                    }
                ]
            ),
            encoding="utf-8",
        )

        result = QlibPredictor().predict(
            run_name="qlib_artifact_json_demo",
            market="US",
            universe="artifact_json_demo",
            artifact_path=str(artifact_dir),
        )
        self.assertEqual("native_inference", result["mode"])
        self.assertEqual(1, result["predictions_written"])
        self.assertEqual(1, result["details_written"])

        api_response = self.client.get("/insights/AAPL/model-output?lang=en")
        self.assertEqual(200, api_response.status_code)
        payload = api_response.json()
        self.assertEqual("qlib_artifact_json_demo", payload["model_run"]["name"])
        self.assertEqual("Aggressive", payload["position_size_hint"])
        self.assertEqual("Breakout", payload["entry_style"])
        self.assertEqual("Imported from structured Qlib JSON.", payload["summary_text"])

    def test_qlib_predictor_reads_predictions_jsonl_from_artifact_directory(self) -> None:
        from app.services.qlib_predictor import QlibPredictor

        qlib_dir = self.temp_path / "data" / "qlib"
        qlib_dir.mkdir(parents=True, exist_ok=True)
        (qlib_dir / "calendar").mkdir(parents=True, exist_ok=True)

        artifact_dir = self.temp_path / "qlib_artifact_jsonl"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "predictions.jsonl").write_text(
            '\n'.join(
                [
                    json.dumps(
                        {
                            "ticker": "RKLB",
                            "name": "Rocket Lab",
                            "trade_date": "2026-04-03",
                            "score": 0.27,
                            "signal_label": "Watch",
                            "signal_strength": 58,
                            "position_size_hint": "Starter",
                            "entry_style": "Wait",
                            "summary_text": "Imported from Qlib JSONL.",
                        }
                    )
                ]
            ),
            encoding="utf-8",
        )

        result = QlibPredictor().predict(
            run_name="qlib_artifact_jsonl_demo",
            market="US",
            universe="artifact_jsonl_demo",
            artifact_path=str(artifact_dir),
        )
        self.assertEqual("native_inference", result["mode"])
        self.assertEqual(1, result["predictions_written"])

        api_response = self.client.get("/insights/RKLB/model-output?lang=en")
        self.assertEqual(200, api_response.status_code)
        payload = api_response.json()
        self.assertEqual("qlib_artifact_jsonl_demo", payload["model_run"]["name"])
        self.assertEqual("Starter", payload["position_size_hint"])
        self.assertEqual("Wait", payload["entry_style"])
        self.assertEqual("Imported from Qlib JSONL.", payload["summary_text"])

    def test_qlib_predictor_imports_native_result_bundle(self) -> None:
        from app.services.qlib_predictor import QlibPredictor
        from app.services.sample_data import seed_sample_data

        qlib_dir = self.temp_path / "data" / "qlib"
        qlib_dir.mkdir(parents=True, exist_ok=True)
        (qlib_dir / "calendar").mkdir(parents=True, exist_ok=True)
        seed_sample_data()

        artifact_dir = self.temp_path / "qlib_native_bundle"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "native_result.json").write_text(
            json.dumps(
                {
                    "manifest": {
                        "run_name": "qlib_native_bundle_demo",
                        "market": "US",
                        "universe": "bundle_demo",
                        "model_type": "qlib_native_bundle",
                    },
                    "predictions": [
                        {
                            "ticker": "AAPL",
                            "name": "Apple Inc.",
                            "trade_date": "2026-04-03",
                            "score": 0.33,
                            "signal_label": "Buy",
                            "signal_strength": 77,
                            "position_size_hint": "Aggressive",
                            "entry_style": "Breakout",
                            "summary_text": "Imported from native result bundle.",
                        }
                    ],
                    "explanations": [
                        {
                            "ticker": "AAPL",
                            "trade_date": "2026-04-03",
                            "feature_name": "momentum_20",
                            "contribution": 0.22,
                            "direction": "positive",
                            "display_order": 1,
                        }
                    ],
                    "chart_signals": [
                        {
                            "ticker": "AAPL",
                            "trade_date": "2026-04-02",
                            "signal_label": "Pullback",
                            "signal_strength": 68,
                            "note": "Bundle-driven pullback marker.",
                        }
                    ],
                    "trade_plan": [
                        {
                            "ticker": "AAPL",
                            "trade_date": "2026-04-03",
                            "entry_low": 188.0,
                            "entry_high": 190.0,
                            "risk_level": 184.5,
                            "execution_tags": ["gap-risk"],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        result = QlibPredictor().predict(
            run_name="ignored_bundle_name",
            market="US",
            universe="ignored_bundle_universe",
            artifact_path=str(artifact_dir),
        )
        self.assertEqual("native_inference", result["mode"])
        self.assertEqual(1, result["predictions_written"])
        self.assertEqual(1, result["explanations_written"])
        self.assertEqual(1, result["chart_signals_written"])
        self.assertEqual(1, result["trade_plans_written"])

        model_output_response = self.client.get("/insights/AAPL/model-output?lang=en")
        self.assertEqual(200, model_output_response.status_code)
        model_payload = model_output_response.json()
        self.assertEqual("qlib_native_bundle_demo", model_payload["model_run"]["name"])
        self.assertEqual("Imported from native result bundle.", model_payload["summary_text"])
        self.assertEqual("Aggressive", model_payload["position_size_hint"])
        self.assertTrue(
            any(
                item["feature_name"] == "momentum_20"
                for item in model_payload["feature_contributions"]["positive"] + model_payload["feature_contributions"]["negative"]
            )
        )

        chart_response = self.client.get("/insights/AAPL/chart-data?lang=en")
        self.assertEqual(200, chart_response.status_code)
        chart_payload = chart_response.json()
        self.assertTrue(any(item["note"] == "Bundle-driven pullback marker." for item in chart_payload["signals"]))

    def test_qlib_predictor_reads_artifact_manifest_metadata(self) -> None:
        from app.services.qlib_predictor import QlibPredictor

        qlib_dir = self.temp_path / "data" / "qlib"
        qlib_dir.mkdir(parents=True, exist_ok=True)
        (qlib_dir / "calendar").mkdir(parents=True, exist_ok=True)

        artifact_dir = self.temp_path / "qlib_artifact_with_manifest"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "predictions.csv").write_text(
            "ticker,name,trade_date,score,summary_text\n"
            "RKLB,Rocket Lab,2026-04-03,0.21,Imported with manifest metadata.\n",
            encoding="utf-8",
        )
        (artifact_dir / "manifest.json").write_text(
            '{"run_name":"qlib_manifest_demo","market":"US","universe":"space","model_type":"qlib_native_manifest","target_horizon_days":15}',
            encoding="utf-8",
        )

        result = QlibPredictor().predict(
            run_name="fallback_name",
            market="CN",
            universe="fallback_universe",
            artifact_path=str(artifact_dir),
        )
        self.assertEqual("native_inference", result["mode"])
        self.assertEqual("qlib_manifest_demo", result["run_name"])
        self.assertIn("artifact_manifest", result)
        self.assertEqual("space", result["artifact_manifest"]["universe"])

        api_response = self.client.get("/insights/RKLB/model-output?lang=en")
        self.assertEqual(200, api_response.status_code)
        payload = api_response.json()
        self.assertEqual("qlib_manifest_demo", payload["model_run"]["name"])
        self.assertEqual("Imported with manifest metadata.", payload["summary_text"])

    def test_qlib_predictor_imports_artifact_explanations(self) -> None:
        from app.services.qlib_predictor import QlibPredictor

        qlib_dir = self.temp_path / "data" / "qlib"
        qlib_dir.mkdir(parents=True, exist_ok=True)
        (qlib_dir / "calendar").mkdir(parents=True, exist_ok=True)

        artifact_dir = self.temp_path / "qlib_artifact_with_explanations"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "predictions.csv").write_text(
            "ticker,name,trade_date,score,summary_text\n"
            "ASTS,AST SpaceMobile,2026-04-03,0.26,Imported with explanations.\n",
            encoding="utf-8",
        )
        (artifact_dir / "explanations.csv").write_text(
            "ticker,trade_date,feature_name,feature_value,contribution,direction,display_order\n"
            "ASTS,2026-04-03,momentum_20,0.14,0.19,positive,1\n"
            "ASTS,2026-04-03,volatility_20,0.08,-0.05,negative,2\n",
            encoding="utf-8",
        )

        result = QlibPredictor().predict(
            run_name="qlib_explanations_demo",
            market="US",
            universe="artifact_explanations",
            artifact_path=str(artifact_dir),
        )
        self.assertEqual("native_inference", result["mode"])
        self.assertEqual(2, result["explanations_written"])

        api_response = self.client.get("/insights/ASTS/model-output?lang=en")
        self.assertEqual(200, api_response.status_code)
        payload = api_response.json()
        self.assertEqual("qlib_explanations_demo", payload["model_run"]["name"])
        self.assertTrue(
            any(
                item["feature_name"] == "momentum_20"
                for item in payload["feature_contributions"]["positive"] + payload["feature_contributions"]["negative"]
            )
        )

    def test_qlib_predictor_imports_artifact_chart_signals(self) -> None:
        from app.services.qlib_predictor import QlibPredictor
        from app.services.sample_data import seed_sample_data

        qlib_dir = self.temp_path / "data" / "qlib"
        qlib_dir.mkdir(parents=True, exist_ok=True)
        (qlib_dir / "calendar").mkdir(parents=True, exist_ok=True)
        seed_sample_data()

        artifact_dir = self.temp_path / "qlib_artifact_with_chart_signals"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "predictions.csv").write_text(
            "ticker,name,trade_date,score,summary_text\n"
            "AAPL,Apple Inc.,2026-04-03,0.26,Imported with chart signals.\n",
            encoding="utf-8",
        )
        (artifact_dir / "chart_signals.csv").write_text(
            "ticker,trade_date,score,rank_value,signal_label,signal_strength,note\n"
            "AAPL,2026-04-02,0.11,2,Pullback,68,Preferred pullback entry.\n",
            encoding="utf-8",
        )

        result = QlibPredictor().predict(
            run_name="qlib_chart_signals_demo",
            market="US",
            universe="artifact_chart_signals",
            artifact_path=str(artifact_dir),
        )
        self.assertEqual("native_inference", result["mode"])
        self.assertEqual(1, result["chart_signals_written"])

        chart_response = self.client.get("/insights/AAPL/chart-data?lang=en")
        self.assertEqual(200, chart_response.status_code)
        payload = chart_response.json()
        self.assertTrue(
            any(
                item["label"] == "Pullback" and item["note"] == "Preferred pullback entry."
                for item in payload["signals"]
            )
        )

    def test_qlib_predictor_imports_artifact_trade_plan(self) -> None:
        from app.services.qlib_predictor import QlibPredictor
        from app.services.sample_data import seed_sample_data

        qlib_dir = self.temp_path / "data" / "qlib"
        qlib_dir.mkdir(parents=True, exist_ok=True)
        (qlib_dir / "calendar").mkdir(parents=True, exist_ok=True)
        seed_sample_data()

        artifact_dir = self.temp_path / "qlib_artifact_with_trade_plan"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "predictions.csv").write_text(
            "ticker,name,trade_date,score,summary_text\n"
            "AAPL,Apple Inc.,2026-04-03,0.29,Imported with trade plan.\n",
            encoding="utf-8",
        )
        (artifact_dir / "trade_plan.json").write_text(
            '[{"ticker":"AAPL","trade_date":"2026-04-03","entry_low":188.0,"entry_high":190.0,"breakout_level":193.0,"take_profit_low":198.0,"take_profit_high":202.0,"risk_level":184.5,"support_level":186.0,"resistance_level":193.0,"stop_type":"trailing","trailing_stop_pct":6.5,"invalidation_reason":"Break below the post-breakout low.","execution_tags":["gap-risk","earnings-soon"],"note":"Model-generated execution plan."}]',
            encoding="utf-8",
        )

        result = QlibPredictor().predict(
            run_name="qlib_trade_plan_demo",
            market="US",
            universe="artifact_trade_plan",
            artifact_path=str(artifact_dir),
        )
        self.assertEqual("native_inference", result["mode"])
        self.assertEqual(1, result["trade_plans_written"])

        summary_response = self.client.get("/insights/AAPL/summary?lang=en")
        self.assertEqual(200, summary_response.status_code)
        payload = summary_response.json()
        self.assertEqual(188.0, payload["entry_zone"]["low"])
        self.assertEqual(190.0, payload["entry_zone"]["high"])
        self.assertEqual(193.0, payload["breakout_level"])
        self.assertEqual(198.0, payload["take_profit_zone"]["low"])
        self.assertEqual(202.0, payload["take_profit_zone"]["high"])
        self.assertEqual(184.5, payload["risk_level"])

        model_output_response = self.client.get("/insights/AAPL/model-output?lang=en")
        self.assertEqual(200, model_output_response.status_code)
        model_payload = model_output_response.json()
        self.assertIn("trade_plan", model_payload)
        self.assertEqual("trailing", model_payload["trade_plan"]["stop_type"])
        self.assertEqual(6.5, model_payload["trade_plan"]["trailing_stop_pct"])
        self.assertEqual("Break below the post-breakout low.", model_payload["trade_plan"]["invalidation_reason"])
        self.assertEqual(["gap-risk", "earnings-soon"], model_payload["trade_plan"]["execution_tags"])

        page_response = self.client.get("/insights/AAPL?lang=en")
        self.assertEqual(200, page_response.status_code)
        self.assertIn("Stop Type", page_response.text)
        self.assertIn("Trailing Stop", page_response.text)
        self.assertIn("Invalidation", page_response.text)
        self.assertIn("Execution Tags", page_response.text)
        self.assertIn("gap-risk", page_response.text)

        from app.core.db import SessionLocal
        from app.services.repository import ConceptSnapshotRepository, SymbolRepository, WatchlistRepository

        with SessionLocal() as db:
            symbol = SymbolRepository(db).get_by_ticker("AAPL")
            watchlist_repo = WatchlistRepository(db)
            watchlist = watchlist_repo.get_or_create_default()
            if symbol is not None and "AAPL" not in watchlist_repo.list_ticker_map(watchlist.id):
                watchlist_repo.add_symbol(watchlist.id, symbol.id)
            if symbol is not None:
                ConceptSnapshotRepository(db).upsert_snapshot(
                    symbol_id=symbol.id,
                    concept_name="Consumer Electronics",
                    concept_code="C001",
                    as_of_date="2026-04-03",
                    source="test",
                )

        screener_export = self.client.get("/screeners/export")
        self.assertEqual(200, screener_export.status_code)
        self.assertIn("model_execution_tags", screener_export.text)
        self.assertIn("gap-risk;earnings-soon", screener_export.text)

        continuous_export = self.client.get("/dashboard/continuous-leaders/export?lookback_runs=3")
        self.assertEqual(200, continuous_export.status_code)
        self.assertIn("execution_tags", continuous_export.text)
        self.assertIn("gap-risk;earnings-soon", continuous_export.text)

        heatmap_page = self.client.get("/dashboard/market/heatmap?signal_filter=BUY&min_signal_strength=1")
        self.assertEqual(200, heatmap_page.status_code)
        self.assertIn("gap-risk", heatmap_page.text)

        concept_page = self.client.get("/dashboard/market/concepts?signal_filter=BUY&min_signal_strength=1")
        self.assertEqual(200, concept_page.status_code)
        self.assertIn("gap-risk", concept_page.text)

        concept_export = self.client.get("/dashboard/market/concepts/export")
        self.assertEqual(200, concept_export.status_code)
        self.assertIn("execution_tags", concept_export.text)
        self.assertIn("gap-risk", concept_export.text)
        self.assertIn("earnings-soon", concept_export.text)

    def test_watchlist_page_adds_symbols_and_links_to_insight(self) -> None:
        from app.services.sample_data import seed_sample_data

        seed_sample_data()

        add_response = self.client.post(
            "/watchlist/add",
            data={"ticker": "ASTS", "name": "AST SpaceMobile", "market": "US", "sync_after_add": ""},
            follow_redirects=True,
        )
        self.assertEqual(200, add_response.status_code)
        self.assertIn("Added ASTS to your watchlist.", add_response.text)
        self.assertIn("/watchlist/open/1", add_response.text)
        self.assertIn("Open Insight", add_response.text)
        self.assertIn("ASTS", add_response.text)

        page_response = self.client.get("/watchlist")
        self.assertEqual(200, page_response.status_code)
        self.assertIn("Follow Stocks Across Markets", page_response.text)
        self.assertIn("China A-Shares", page_response.text)
        self.assertIn("Hong Kong Stocks", page_response.text)
        self.assertIn("Sync Enabled Stocks", page_response.text)

        toggle_response = self.client.post(
            "/watchlist/toggle-sync",
            data={"item_id": "1", "enabled": "1"},
            follow_redirects=True,
        )
        self.assertEqual(200, toggle_response.status_code)
        self.assertIn("Sync setting updated.", toggle_response.text)
        self.assertIn("Disable Sync", toggle_response.text)

        extra_response = self.client.post(
            "/watchlist/add",
            data={"ticker": "TSLA", "name": "Tesla", "market": "US", "sync_after_add": ""},
            follow_redirects=True,
        )
        self.assertEqual(200, extra_response.status_code)

        waiting_toggle = self.client.post(
            "/watchlist/toggle-sync",
            data={"item_id": "2", "enabled": "1"},
            follow_redirects=True,
        )
        self.assertEqual(200, waiting_toggle.status_code)

        waiting_response = self.client.get("/watchlist/open/2", follow_redirects=True)
        self.assertEqual(200, waiting_response.status_code)
        self.assertIn("Still Sync, Please wait", waiting_response.text)

    def test_watchlist_supports_execution_tag_filters(self) -> None:
        from sqlalchemy import select

        from app.core.db import SessionLocal
        from app.models.tables import ModelRun, Prediction
        from app.services.dataset_build import build_dataset
        from app.services.repository import PredictionTradePlanRepository, SymbolRepository, WatchlistRepository
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="watchlist_execution_tag_demo", signal_type="momentum", lookback_days=3)
        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            aapl = symbol_repo.get_by_ticker("AAPL")
            msft = symbol_repo.get_by_ticker("MSFT")
            watchlist_repo = WatchlistRepository(db)
            watchlist = watchlist_repo.get_or_create_default()
            watchlist_repo.add_symbol(watchlist.id, aapl.id)
            watchlist_repo.add_symbol(watchlist.id, msft.id)
            model_run = db.scalar(select(ModelRun).order_by(ModelRun.id.desc()))
            predictions = list(db.execute(select(Prediction).where(Prediction.model_run_id == model_run.id)).scalars())
            rows = []
            for prediction in predictions:
                if prediction.symbol_id == aapl.id:
                    rows.append({"symbol_id": prediction.symbol_id, "trade_date": prediction.trade_date, "execution_tags": ["gap-risk"]})
                elif prediction.symbol_id == msft.id:
                    rows.append({"symbol_id": prediction.symbol_id, "trade_date": prediction.trade_date, "execution_tags": ["earnings-soon"]})
            PredictionTradePlanRepository(db).replace_for_model_run(model_run.id, rows)

        include_response = self.client.get("/watchlist?execution_tag_filter=gap-risk")
        self.assertIn("Quick Tags", include_response.text)
        self.assertIn("Clear Tags", include_response.text)
        self.assertIn("execution-tag-options", include_response.text)
        self.assertIn("Risk Overview", include_response.text)
        self.assertEqual(200, include_response.status_code)
        self.assertIn("Execution Filters", include_response.text)
        self.assertIn("gap-risk", include_response.text)
        self.assertIn("/watchlist/open/1", include_response.text)
        self.assertNotIn("/watchlist/open/2", include_response.text)

        exclude_response = self.client.get("/watchlist?exclude_execution_tag_filter=gap-risk")
        self.assertEqual(200, exclude_response.status_code)
        self.assertIn("Exclude Tag", exclude_response.text)
        self.assertIn("earnings-soon", exclude_response.text)
        self.assertIn("/watchlist/open/2", exclude_response.text)
        self.assertNotIn("/watchlist/open/1", exclude_response.text)

    def test_watchlist_suggestions_and_cn_name_autofill(self) -> None:
        suggest_response = self.client.get("/watchlist/suggest?q=6005&market=CN")
        self.assertEqual(200, suggest_response.status_code)
        suggestions = suggest_response.json()
        self.assertTrue(any(item["name"] == "贵州茅台" for item in suggestions))
        self.assertTrue(any(item.get("exchange") == "SSE" for item in suggestions))

        add_response = self.client.post(
            "/watchlist/add",
            data={"ticker": "600519.SH", "market": "CN", "name": "", "sync_after_add": ""},
            follow_redirects=True,
        )
        self.assertEqual(200, add_response.status_code)
        self.assertIn("Added 600519.SS to your watchlist.", add_response.text)
        self.assertIn("贵州茅台", add_response.text)

    def test_watchlist_market_order_is_cn_hk_us(self) -> None:
        from app.core.db import SessionLocal
        from app.models.schema import SymbolCreate
        from app.services.repository import SymbolRepository, WatchlistRepository

        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            watchlist_repo = WatchlistRepository(db)
            watchlist = watchlist_repo.get_or_create_default()

            cn = symbol_repo.get_or_create_symbol(SymbolCreate(ticker="600519.SS", name="贵州茅台", market="CN", exchange="SSE"))
            hk = symbol_repo.get_or_create_symbol(SymbolCreate(ticker="0700.HK", name="腾讯控股", market="HK", exchange="HKEX"))
            us = symbol_repo.get_or_create_symbol(SymbolCreate(ticker="ASTS", name="AST SpaceMobile", market="US", exchange="NASDAQ"))

            for symbol in (us, hk, cn):
                watchlist_repo.add_symbol(watchlist.id, symbol.id)

            ordered = watchlist_repo.list_items(watchlist.id)

        self.assertEqual(["CN", "HK", "US"], [item["market"] for item in ordered])

    def test_watchlist_page_shows_market_sections(self) -> None:
        from app.core.db import SessionLocal
        from app.models.schema import SymbolCreate
        from app.services.repository import SymbolRepository, WatchlistRepository

        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            watchlist_repo = WatchlistRepository(db)
            watchlist = watchlist_repo.get_or_create_default()
            for payload in (
                SymbolCreate(ticker="600519.SS", name="贵州茅台", market="CN", exchange="SSE"),
                SymbolCreate(ticker="0700.HK", name="腾讯控股", market="HK", exchange="HKEX"),
                SymbolCreate(ticker="ASTS", name="AST SpaceMobile", market="US", exchange="NASDAQ"),
            ):
                symbol = symbol_repo.get_or_create_symbol(payload)
                watchlist_repo.add_symbol(watchlist.id, symbol.id)

        response = self.client.get("/watchlist")
        self.assertEqual(200, response.status_code)
        self.assertIn("A股", response.text)
        self.assertIn("港股", response.text)
        self.assertIn("美股", response.text)

    def test_screener_page_filters_watchlist_rules(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="screener_model_context", signal_type="momentum", lookback_days=3)
        self.client.post(
            "/watchlist/add",
            data={"ticker": "ASTS", "name": "AST SpaceMobile", "market": "US", "sync_after_add": ""},
            follow_redirects=True,
        )
        self.client.post(
            "/watchlist/add",
            data={"ticker": "AAPL", "name": "Apple Inc.", "market": "US", "sync_after_add": ""},
            follow_redirects=True,
        )

        response = self.client.get(
            "/screeners",
            params={
                "universe": "watchlist",
                "market": "US",
                "min_trend_score": 1,
                "action_filter": "ALL",
                "min_volume_ratio": 0,
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertIn("Rule-Based Stock Selection", response.text)
        self.assertIn("Open Watchlist", response.text)
        self.assertIn("ASTS", response.text)
        self.assertIn("Open Insight", response.text)
        self.assertIn("In Watchlist", response.text)
        self.assertIn("Model", response.text)
        self.assertIn("model:", response.text)
        self.assertIn("recent move", response.text)
        self.assertTrue("Strong" in response.text or "Positive" in response.text or "Weak" in response.text)
        self.assertIn("Details", response.text)

    def test_watchlist_page_renders_decision_console(self) -> None:
        self.client.post(
            "/watchlist/add",
            data={"ticker": "ASTS", "name": "AST SpaceMobile", "market": "US", "sync_after_add": ""},
            follow_redirects=True,
        )

        with patch(
            "app.api.routes.watchlist.safe_symbol_analysis",
            return_value={"decision": "BUY", "confidence": 82, "score": 6},
        ), patch(
            "app.api.routes.watchlist.build_symbol_decision_brief",
            return_value={
                "headline": "ASTS remains constructive with bullish support",
                "summary": "daily BUY | multi-timeframe aligned",
            },
        ):
            response = self.client.get("/watchlist?mode=premarket")

        self.assertEqual(200, response.status_code)
        self.assertIn("Decision Console", response.text)
        self.assertIn("watchlist-analysis-panels", response.text)
        self.assertIn("watchlist-table-panel", response.text)
        self.assertIn("Premarket", response.text)

    def test_watchlist_analysis_fragment_renders_decision_console(self) -> None:
        self.client.post(
            "/watchlist/add",
            data={"ticker": "ASTS", "name": "AST SpaceMobile", "market": "US", "sync_after_add": ""},
            follow_redirects=True,
        )

        with patch(
            "app.api.routes.watchlist.safe_symbol_analysis",
            return_value={"decision": "BUY", "confidence": 82, "score": 6},
        ), patch(
            "app.api.routes.watchlist.build_symbol_decision_brief",
            return_value={
                "headline": "ASTS remains constructive with bullish support",
                "summary": "daily BUY | multi-timeframe aligned",
            },
        ):
            response = self.client.get("/watchlist/analysis-fragment?mode=premarket")

        self.assertEqual(200, response.status_code)
        self.assertIn("High Priority", response.text)
        self.assertIn("Decision", response.text)
        self.assertIn("ASTS remains constructive with bullish support", response.text)
        self.assertIn("AI Briefs", response.text)

    def test_watchlist_table_fragment_renders_saved_stocks(self) -> None:
        self.client.post(
            "/watchlist/add",
            data={"ticker": "ASTS", "name": "AST SpaceMobile", "market": "US", "sync_after_add": ""},
            follow_redirects=True,
        )

        response = self.client.get("/watchlist/table-fragment?mode=monitor")

        self.assertEqual(200, response.status_code)
        self.assertIn("Saved Stocks", response.text)
        self.assertIn("ASTS", response.text)
        self.assertIn("Open Insight", response.text)

    def test_watchlist_analysis_fragment_is_cached_between_requests(self) -> None:
        self.client.post(
            "/watchlist/add",
            data={"ticker": "ASTS", "name": "AST SpaceMobile", "market": "US", "sync_after_add": ""},
            follow_redirects=True,
        )

        with patch(
            "app.api.routes.watchlist.safe_symbol_analysis",
            return_value={"decision": "BUY", "confidence": 82, "score": 6},
        ) as analysis_mock, patch(
            "app.api.routes.watchlist.build_symbol_decision_brief",
            return_value={
                "headline": "ASTS remains constructive with bullish support",
                "summary": "daily BUY | multi-timeframe aligned",
            },
        ):
            first = self.client.get("/watchlist/analysis-fragment?mode=monitor&analysis_limit=1")
            second = self.client.get("/watchlist/analysis-fragment?mode=monitor&analysis_limit=1")

        self.assertEqual(200, first.status_code)
        self.assertEqual(200, second.status_code)
        self.assertEqual(1, analysis_mock.call_count)

    def test_watchlist_cache_is_cleared_after_add(self) -> None:
        self.client.post(
            "/watchlist/add",
            data={"ticker": "ASTS", "name": "AST SpaceMobile", "market": "US", "sync_after_add": ""},
            follow_redirects=True,
        )

        first = self.client.get("/watchlist/table-fragment?mode=monitor")
        self.assertEqual(200, first.status_code)
        self.assertIn("ASTS", first.text)

        self.client.post(
            "/watchlist/add",
            data={"ticker": "AAPL", "name": "Apple", "market": "US", "sync_after_add": ""},
            follow_redirects=True,
        )

        second = self.client.get("/watchlist/table-fragment?mode=monitor")
        self.assertEqual(200, second.status_code)
        self.assertIn("ASTS", second.text)
        self.assertIn("AAPL", second.text)

    def test_watchlist_uses_batched_prediction_queries(self) -> None:
        from app.services.runtime_cache import clear_namespace

        self.client.post(
            "/watchlist/add",
            data={"ticker": "ASTS", "name": "AST SpaceMobile", "market": "US", "sync_after_add": ""},
            follow_redirects=True,
        )
        clear_namespace("watchlist_items")
        clear_namespace("watchlist_table_fragment")

        with patch(
            "app.api.routes.watchlist.PredictionRepository.get_latest_model_outputs_for_tickers",
            return_value={
                "ASTS": {
                    "ticker": "ASTS",
                    "score": 0.31,
                    "confidence": 88,
                    "signal_label": "Buy",
                    "signal_strength": 79,
                }
            },
        ) as batch_predictions, patch(
            "app.api.routes.watchlist.PredictionTradePlanRepository.get_latest_for_tickers",
            return_value={"ASTS": {"execution_tags": ["gap-risk"]}},
        ) as batch_trade_plans, patch(
            "app.api.routes.watchlist.PredictionRepository.get_latest_model_output_for_ticker",
            side_effect=AssertionError("single prediction lookup should not be used"),
        ), patch(
            "app.api.routes.watchlist.PredictionTradePlanRepository.get_latest_for_ticker",
            side_effect=AssertionError("single trade plan lookup should not be used"),
        ):
            response = self.client.get("/watchlist/table-fragment?mode=monitor")

        self.assertEqual(200, response.status_code)
        self.assertIn("ASTS", response.text)
        batch_predictions.assert_called_once()
        batch_trade_plans.assert_called_once()

    def test_dashboard_read_repository_loads_summary_snapshot(self) -> None:
        self._seed_symbol("ASTS", "AST SpaceMobile", "US", "NASDAQ")

        from app.core.db import SessionLocal
        from app.services.repository import ModelRunRepository, PredictionDetailRepository, PredictionWriteRepository, SymbolRepository

        with SessionLocal() as db:
            symbol = SymbolRepository(db).get_by_ticker("ASTS")
            self.assertIsNotNone(symbol)
            run = ModelRunRepository(db).create_run(
                name="test-run",
                model_type="xgboost",
                market="US",
                universe="watchlist",
                train_start="2026-01-01",
                train_end="2026-03-31",
                test_start="2026-04-01",
                test_end="2026-04-10",
                config={"top_n": 5},
                artifact_path=None,
                status="success",
            )
            PredictionWriteRepository(db).replace_for_model_run(
                run.id,
                [
                    {
                        "symbol_id": symbol.id,
                        "trade_date": "2026-04-10",
                        "score": 0.21,
                        "rank_value": 1,
                    }
                ],
            )
            PredictionDetailRepository(db).replace_for_model_run(
                run.id,
                [
                    {
                        "symbol_id": symbol.id,
                        "trade_date": "2026-04-10",
                        "confidence": 77,
                        "signal_label": "Buy",
                        "signal_strength": 68,
                        "summary_text": "Constructive setup",
                    }
                ],
            )
            snapshot = DashboardReadRepository(db).load_summary_snapshot()

        self.assertIn("latest_signals", snapshot)
        self.assertIn("sync_states", snapshot)
        self.assertIn("recent_jobs", snapshot)
        self.assertIn("recent_model_runs", snapshot)
        self.assertIn("latest_backtest", snapshot)
        self.assertIn("latest_backtest_curve", snapshot)
        self.assertIn("concept_summary", snapshot)

    def test_watchlist_page_limits_full_analysis_to_top_candidates(self) -> None:
        self.client.post(
            "/watchlist/add",
            data={"ticker": "ASTS", "name": "AST SpaceMobile", "market": "US", "sync_after_add": ""},
            follow_redirects=True,
        )
        self.client.post(
            "/watchlist/add",
            data={"ticker": "AAPL", "name": "Apple", "market": "US", "sync_after_add": ""},
            follow_redirects=True,
        )

        model_outputs = {
            "ASTS": {"ticker": "ASTS", "score": 0.31, "confidence": 88, "signal_label": "Buy", "signal_strength": 79},
            "AAPL": {"ticker": "AAPL", "score": 0.04, "confidence": 52, "signal_label": "Watch", "signal_strength": 22},
        }

        with patch(
            "app.api.routes.watchlist.PredictionRepository.get_latest_model_output_for_ticker",
            side_effect=lambda ticker: model_outputs.get(ticker),
        ), patch(
            "app.api.routes.watchlist.safe_symbol_analysis",
            return_value={"decision": "BUY", "confidence": 82, "score": 6},
        ) as analysis_mock, patch(
            "app.api.routes.watchlist.build_symbol_decision_brief",
            return_value={
                "headline": "ASTS remains constructive with bullish support",
                "summary": "daily BUY | multi-timeframe aligned",
            },
        ):
            response = self.client.get("/watchlist/analysis-fragment?mode=monitor&analysis_limit=1")

        self.assertEqual(200, response.status_code)
        self.assertEqual(1, analysis_mock.call_count)
        self.assertIn("ASTS remains constructive with bullish support", response.text)

    def test_watchlist_page_renders_ai_briefs_for_top_ranked_names(self) -> None:
        self.client.post(
            "/watchlist/add",
            data={"ticker": "ASTS", "name": "AST SpaceMobile", "market": "US", "sync_after_add": ""},
            follow_redirects=True,
        )

        with patch(
            "app.api.routes.watchlist.safe_symbol_analysis",
            return_value={"decision": "BUY", "confidence": 82, "score": 6},
        ), patch(
            "app.api.routes.watchlist.build_symbol_decision_brief",
            return_value={
                "headline": "ASTS remains constructive with bullish support",
                "summary": "daily BUY | multi-timeframe aligned",
            },
        ), patch(
            "app.api.routes.watchlist.AIAnalysisService.analyze_symbol",
            return_value={"headline": "AI sees a constructive trend-following setup"},
        ):
            response = self.client.get("/watchlist/analysis-fragment?mode=monitor&analysis_limit=1&ai_analysis_limit=1")

        self.assertEqual(200, response.status_code)
        self.assertIn("AI Briefs", response.text)
        self.assertIn("AI sees a constructive trend-following setup", response.text)

    def test_portfolio_page_can_add_position_and_render_ai_fields(self) -> None:
        self._seed_symbol("ASTS", "AST SpaceMobile", "US", "NASDAQ")

        add_response = self.client.post(
            "/portfolio/add",
            data={
                "ticker": "ASTS",
                "name": "AST SpaceMobile",
                "market": "US",
                "quantity": "100",
                "cost_basis": "18.5",
                "note": "swing",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, add_response.status_code)

        with patch(
            "app.api.routes.portfolio.safe_symbol_analysis",
            return_value={"latest_close": 21.0, "decision": "BUY", "confidence": 80, "score": 6},
        ), patch(
            "app.api.routes.portfolio.AIAnalysisService.analyze_symbol",
            return_value={
                "headline": "ASTS remains constructive",
                "verdict": "BUY",
                "strategy": "进攻/顺势跟踪",
            },
        ):
            response = self.client.get("/portfolio")

        self.assertEqual(200, response.status_code)
        self.assertIn("持仓", response.text)
        self.assertIn("ASTS", response.text)
        self.assertIn("ASTS remains constructive", response.text)
        self.assertIn("进攻/顺势跟踪", response.text)

    def test_portfolio_export_and_import_round_trip(self) -> None:
        add_response = self.client.post(
            "/portfolio/add",
            data={
                "ticker": "ASTS",
                "name": "AST SpaceMobile",
                "market": "US",
                "quantity": "100",
                "cost_basis": "18.5",
                "note": "swing",
            },
            follow_redirects=False,
        )
        self.assertEqual(303, add_response.status_code)

        export_response = self.client.get("/portfolio/export")
        self.assertEqual(200, export_response.status_code)
        self.assertIn("text/csv", export_response.headers.get("content-type", ""))
        self.assertIn("ASTS", export_response.text)

        remove_response = self.client.post("/portfolio/remove", data={"ticker": "ASTS"}, follow_redirects=False)
        self.assertEqual(303, remove_response.status_code)

        import_response = self.client.post(
            "/portfolio/import",
            data={"csv_text": export_response.text},
            follow_redirects=False,
        )
        self.assertEqual(303, import_response.status_code)

        with patch(
            "app.api.routes.portfolio.safe_symbol_analysis",
            return_value={"latest_close": 21.0, "decision": "BUY", "confidence": 80, "score": 6},
        ), patch(
            "app.api.routes.portfolio.AIAnalysisService.analyze_symbol",
            return_value={"headline": "ASTS remains constructive", "verdict": "BUY", "strategy": "进攻/顺势跟踪"},
        ):
            page = self.client.get("/portfolio")

        self.assertEqual(200, page.status_code)
        self.assertIn("ASTS", page.text)

    def test_notification_settings_page_renders_channel_status(self) -> None:
        response = self.client.get("/settings/notifications")

        self.assertEqual(200, response.status_code)
        self.assertIn("通知配置", response.text)
        self.assertIn("PQW_WECHAT_WEBHOOK_URL", response.text)
        self.assertIn("PQW_TELEGRAM_BOT_TOKEN", response.text)

    def test_push_notification_service_supports_telegram(self) -> None:
        os.environ["PQW_TELEGRAM_BOT_TOKEN"] = "bot-token"
        os.environ["PQW_TELEGRAM_CHAT_ID"] = "123456"
        from app.core.config import reset_settings_cache
        from app.services.push_notifications import PushNotificationService

        reset_settings_cache()
        with patch("app.services.push_notifications.httpx.post") as mocked_post:
            mocked_post.return_value = MagicMock(status_code=200, raise_for_status=lambda: None)
            result = PushNotificationService().send_text(
                title="AI 每日决策面板",
                body="测试消息",
                channels=["telegram"],
            )

        self.assertEqual("success", result["status"])
        self.assertEqual(["telegram"], result["sent"])
        call = mocked_post.call_args
        self.assertIsNotNone(call)
        self.assertIn("api.telegram.org", call.args[0])

    def test_screener_page_shows_add_to_watchlist_for_untracked_results(self) -> None:
        from app.services.sample_data import seed_sample_data

        seed_sample_data()

        response = self.client.get(
            "/screeners",
            params={
                "universe": "synced",
                "market": "US",
                "min_trend_score": 1,
                "action_filter": "ALL",
                "min_volume_ratio": 0,
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertIn("Add To Watchlist", response.text)
        self.assertIn("Add Current Results To Watchlist", response.text)
        self.assertIn("Auto-enable Sync for added stocks", response.text)

    def test_screener_page_supports_chinese_language(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="screener_model_context_zh", signal_type="momentum", lookback_days=3)

        response = self.client.get(
            "/screeners",
            params={
                "lang": "zh",
                "universe": "synced",
                "market": "US",
                "min_trend_score": 1,
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertIn("基于规则的选股", response.text)
        self.assertIn("语言", response.text)
        self.assertIn("将当前结果加入自选", response.text)
        self.assertIn("打开分析页", response.text)
        self.assertTrue("强" in response.text or "偏强" in response.text or "偏弱" in response.text)
        self.assertIn("展开", response.text)
        self.assertIn("展开", response.text)
        self.assertIn("模型信号", response.text)
        self.assertIn("最低信号强度", response.text)

    def test_screener_page_supports_snapshot_persistence_filter(self) -> None:
        from app.services.dataset_build import build_dataset
        from app.services.sample_data import seed_sample_data
        from app.services.trainer import SignalTrainer

        seed_sample_data()
        build_dataset(normalize_only=True)
        SignalTrainer().train(run_name="snapshot_filter_one", signal_type="momentum", lookback_days=3)
        SignalTrainer().train(run_name="snapshot_filter_two", signal_type="momentum", lookback_days=4)

        self.client.post(
            "/watchlist/add",
            data={"ticker": "ASTS", "name": "AST SpaceMobile", "market": "US", "sync_after_add": ""},
            follow_redirects=True,
        )

        response = self.client.get(
            "/screeners",
            params={
                "universe": "watchlist",
                "market": "US",
                "min_trend_score": 1,
                "action_filter": "ALL",
                "min_volume_ratio": 0,
                "recent_snapshot_runs": 2,
                "min_snapshot_hits": 1,
                "sort_by": "snapshot_hits",
                "sort_order": "desc",
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertIn("Hits", response.text)
        self.assertIn("/2", response.text)
        self.assertIn("ASTS", response.text)

    def test_screener_service_filters_and_sorts_by_model_signal(self) -> None:
        from app.services.screener import ScreenerService

        service = ScreenerService()
        rows = [
            {"ticker": "AAA", "model_signal_label": "buy", "model_signal_strength": 82},
            {"ticker": "BBB", "model_signal_label": "watch", "model_signal_strength": 45},
            {"ticker": "CCC", "model_signal_label": "buy", "model_signal_strength": 28},
        ]

        filtered = service._apply_model_signal_filter(
            rows,
            model_signal_filter="BUY",
            min_model_signal_strength=30,
        )

        self.assertEqual(["AAA"], [row["ticker"] for row in filtered])

        sorted_rows = service._sort_results(
            rows,
            sort_by="model_signal_strength",
            sort_order="desc",
        )
        self.assertEqual(["AAA", "BBB", "CCC"], [row["ticker"] for row in sorted_rows])

    def test_cn_growth_value_template_uses_fundamental_rules(self) -> None:
        from app.core.db import SessionLocal
        from app.models.schema import SymbolCreate
        from app.services.repository import FundamentalSnapshotRepository, SymbolRepository, WatchlistRepository

        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            watchlist_repo = WatchlistRepository(db)
            watchlist = watchlist_repo.get_or_create_default()

            winner = symbol_repo.get_or_create_symbol(
                SymbolCreate(ticker="600519.SS", name="贵州茅台", market="CN", exchange="SSE")
            )
            loser = symbol_repo.get_or_create_symbol(
                SymbolCreate(ticker="000001.SZ", name="平安银行", market="CN", exchange="SZSE")
            )
            watchlist_repo.add_symbol(watchlist.id, winner.id)
            watchlist_repo.add_symbol(watchlist.id, loser.id)

            fundamentals = FundamentalSnapshotRepository(db)
            fundamentals.upsert_snapshot(
                symbol_id=winner.id,
                report_date="2026-03-31",
                source="tushare",
                listing_date="2001-08-27",
                pe_ttm=24.3,
                market_cap=1800000000000.0,
                roe_avg_3y=28.5,
                net_profit_yoy=21.2,
                revenue_yoy=11.8,
            )
            fundamentals.upsert_snapshot(
                symbol_id=loser.id,
                report_date="2026-03-31",
                source="tushare",
                listing_date="1991-04-03",
                pe_ttm=6.2,
                market_cap=110000000000.0,
                roe_avg_3y=9.3,
                net_profit_yoy=6.8,
                revenue_yoy=4.1,
            )

        response = self.client.get(
            "/screeners",
            params={
                "lang": "zh",
                "model_template": "cn_growth_value",
                "universe": "watchlist",
                "market": "CN",
                "min_trend_score": 0,
                "min_listing_days": 365,
                "pe_min": 0,
                "pe_max": 30,
                "min_roe_avg_3y": 12,
                "min_net_profit_yoy": 20,
                "exclude_bottom_market_cap_pct": 10,
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertIn("高成长低估值", response.text)
        self.assertIn("贵州茅台", response.text)
        self.assertNotIn("平安银行", response.text)

    def test_cn_high_roe_steady_growth_template_filters_on_revenue_and_leverage(self) -> None:
        from app.core.db import SessionLocal
        from app.models.schema import SymbolCreate
        from app.services.repository import FundamentalSnapshotRepository, SymbolRepository, WatchlistRepository

        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            watchlist_repo = WatchlistRepository(db)
            watchlist = watchlist_repo.get_or_create_default()

            winner = symbol_repo.get_or_create_symbol(
                SymbolCreate(ticker="603288.SS", name="海天味业", market="CN", exchange="SSE")
            )
            loser = symbol_repo.get_or_create_symbol(
                SymbolCreate(ticker="300750.SZ", name="宁德时代", market="CN", exchange="SZSE")
            )
            watchlist_repo.add_symbol(watchlist.id, winner.id)
            watchlist_repo.add_symbol(watchlist.id, loser.id)

            fundamentals = FundamentalSnapshotRepository(db)
            fundamentals.upsert_snapshot(
                symbol_id=winner.id,
                report_date="2026-03-31",
                source="tushare",
                listing_date="2014-02-11",
                pe_ttm=35.0,
                market_cap=220000000000.0,
                roe_avg_3y=23.0,
                net_profit_yoy=12.5,
                revenue_yoy=11.2,
                debt_to_assets=28.0,
            )
            fundamentals.upsert_snapshot(
                symbol_id=loser.id,
                report_date="2026-03-31",
                source="tushare",
                listing_date="2018-06-11",
                pe_ttm=32.0,
                market_cap=980000000000.0,
                roe_avg_3y=18.4,
                net_profit_yoy=14.6,
                revenue_yoy=8.1,
                debt_to_assets=71.0,
            )

        response = self.client.get(
            "/screeners",
            params={
                "lang": "zh",
                "model_template": "cn_high_roe_steady_growth",
                "universe": "watchlist",
                "market": "CN",
                "min_trend_score": 0,
                "min_listing_days": 730,
                "pe_min": 0,
                "pe_max": 60,
                "min_roe_avg_3y": 15,
                "min_net_profit_yoy": 10,
                "min_revenue_yoy": 10,
                "max_debt_to_assets": 65,
                "exclude_bottom_market_cap_pct": 0,
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertIn("高ROE稳增长", response.text)
        self.assertIn("海天味业", response.text)
        self.assertNotIn("宁德时代", response.text)

    def test_cn_low_valuation_high_dividend_template_filters_on_dividend(self) -> None:
        from app.core.db import SessionLocal
        from app.models.schema import SymbolCreate
        from app.services.repository import FundamentalSnapshotRepository, SymbolRepository, WatchlistRepository

        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            watchlist_repo = WatchlistRepository(db)
            watchlist = watchlist_repo.get_or_create_default()

            winner = symbol_repo.get_or_create_symbol(
                SymbolCreate(ticker="601288.SS", name="农业银行", market="CN", exchange="SSE")
            )
            loser = symbol_repo.get_or_create_symbol(
                SymbolCreate(ticker="600519.SS", name="贵州茅台", market="CN", exchange="SSE")
            )
            watchlist_repo.add_symbol(watchlist.id, winner.id)
            watchlist_repo.add_symbol(watchlist.id, loser.id)

            fundamentals = FundamentalSnapshotRepository(db)
            fundamentals.upsert_snapshot(
                symbol_id=winner.id,
                report_date="2026-03-31",
                source="tushare",
                listing_date="2010-07-15",
                pe_ttm=7.1,
                dividend_yield=5.8,
                market_cap=1300000000000.0,
                roe_avg_3y=11.4,
                net_profit_yoy=2.0,
                revenue_yoy=1.2,
                debt_to_assets=63.0,
            )
            fundamentals.upsert_snapshot(
                symbol_id=loser.id,
                report_date="2026-03-31",
                source="tushare",
                listing_date="2001-08-27",
                pe_ttm=24.3,
                dividend_yield=1.7,
                market_cap=1800000000000.0,
                roe_avg_3y=28.5,
                net_profit_yoy=21.2,
                revenue_yoy=11.8,
                debt_to_assets=34.0,
            )

        response = self.client.get(
            "/screeners",
            params={
                "lang": "zh",
                "model_template": "cn_low_valuation_high_dividend",
                "universe": "watchlist",
                "market": "CN",
                "min_trend_score": 0,
                "min_listing_days": 1095,
                "pe_min": 0,
                "pe_max": 20,
                "min_roe_avg_3y": 10,
                "min_net_profit_yoy": 0,
                "min_revenue_yoy": 0,
                "max_debt_to_assets": 70,
                "min_dividend_yield": 3,
                "exclude_bottom_market_cap_pct": 0,
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertIn("低估值高分红", response.text)
        self.assertIn("农业银行", response.text)
        self.assertNotIn("贵州茅台", response.text)

    def test_screener_can_save_strategy_preset(self) -> None:
        response = self.client.post(
            "/screeners/save",
            data={
                "preset_name": "My Dividend Picks",
                "model_template": "cn_low_valuation_high_dividend",
                "universe": "watchlist",
                "market": "CN",
                "min_trend_score": "0",
                "action_filter": "ALL",
                "min_volume_ratio": "0",
                "min_listing_days": "1095",
                "pe_min": "0",
                "pe_max": "20",
                "min_roe_avg_3y": "10",
                "min_net_profit_yoy": "0",
                "min_revenue_yoy": "0",
                "max_debt_to_assets": "70",
                "min_dividend_yield": "3",
                "exclude_bottom_market_cap_pct": "10",
            },
            follow_redirects=True,
        )

        self.assertEqual(200, response.status_code)
        self.assertIn("Saved strategy: My Dividend Picks", response.text)
        self.assertIn("My Dividend Picks", response.text)
        self.assertIn("cn_low_valuation_high_dividend", str(response.request.url))

    def test_screener_can_delete_strategy_preset(self) -> None:
        self.client.post(
            "/screeners/save",
            data={
                "preset_name": "Delete Me",
                "model_template": "cn_growth_value",
                "universe": "watchlist",
                "market": "CN",
                "min_trend_score": "0",
                "action_filter": "ALL",
                "min_volume_ratio": "0",
                "min_listing_days": "365",
                "pe_min": "0",
                "pe_max": "30",
                "min_roe_avg_3y": "12",
                "min_net_profit_yoy": "20",
                "min_revenue_yoy": "0",
                "max_debt_to_assets": "100",
                "min_dividend_yield": "0",
                "exclude_bottom_market_cap_pct": "10",
            },
            follow_redirects=True,
        )

        response = self.client.post(
            "/screeners/delete",
            data={"preset_name": "Delete Me"},
            follow_redirects=True,
        )

        self.assertEqual(200, response.status_code)
        self.assertIn("Deleted strategy: Delete Me", response.text)
        self.assertNotIn(">Delete Me</td>", response.text)

    def test_screener_export_csv_returns_rows(self) -> None:
        from app.core.db import SessionLocal
        from app.models.schema import SymbolCreate
        from app.services.repository import FundamentalSnapshotRepository, SymbolRepository, WatchlistRepository

        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            watchlist_repo = WatchlistRepository(db)
            watchlist = watchlist_repo.get_or_create_default()
            symbol = symbol_repo.get_or_create_symbol(
                SymbolCreate(ticker="601288.SS", name="农业银行", market="CN", exchange="SSE")
            )
            watchlist_repo.add_symbol(watchlist.id, symbol.id)
            FundamentalSnapshotRepository(db).upsert_snapshot(
                symbol_id=symbol.id,
                report_date="2026-03-31",
                source="tushare",
                listing_date="2010-07-15",
                pe_ttm=7.1,
                dividend_yield=5.8,
                market_cap=1300000000000.0,
                roe_avg_3y=11.4,
                net_profit_yoy=2.0,
                revenue_yoy=1.2,
                debt_to_assets=63.0,
            )

        response = self.client.get(
            "/screeners/export",
            params={
                "model_template": "cn_low_valuation_high_dividend",
                "universe": "watchlist",
                "market": "CN",
                "min_trend_score": 0,
                "min_listing_days": 1095,
                "pe_min": 0,
                "pe_max": 20,
                "min_roe_avg_3y": 10,
                "min_net_profit_yoy": 0,
                "min_revenue_yoy": 0,
                "max_debt_to_assets": 70,
                "min_dividend_yield": 3,
                "exclude_bottom_market_cap_pct": 0,
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertIn("text/csv", response.headers.get("content-type", ""))
        self.assertIn("attachment; filename=cn_low_valuation_high_dividend_screener.csv", response.headers.get("content-disposition", ""))
        self.assertIn("ticker,name,market", response.text)
        self.assertIn("model_signal_label,model_signal_strength", response.text)
        self.assertIn("model_conviction_bucket", response.text)
        self.assertIn("model_position_size_hint", response.text)
        self.assertIn("model_entry_style", response.text)
        self.assertIn("model_execution_tags", response.text)
        self.assertIn("model_percentile,model_horizon_days,model_reward_risk_ratio,model_expected_drawdown_20d", response.text)
        self.assertIn("601288.SS", response.text)

    def test_saved_strategy_shows_hit_count(self) -> None:
        self.client.post(
            "/screeners/save",
            data={
                "preset_name": "Hit Count Demo",
                "model_template": "technical_momentum",
                "universe": "watchlist",
                "market": "ALL",
                "min_trend_score": "60",
                "action_filter": "ALL",
                "min_volume_ratio": "0",
                "min_listing_days": "365",
                "pe_min": "0",
                "pe_max": "30",
                "min_roe_avg_3y": "12",
                "min_net_profit_yoy": "20",
                "min_revenue_yoy": "0",
                "max_debt_to_assets": "100",
                "min_dividend_yield": "0",
                "exclude_bottom_market_cap_pct": "10",
            },
            follow_redirects=True,
        )

        response = self.client.get("/screeners")
        self.assertEqual(200, response.status_code)
        self.assertIn("Hits", response.text)
        self.assertIn("Hit Count Demo", response.text)

    def test_screener_sort_by_dividend_desc_orders_results(self) -> None:
        from app.core.db import SessionLocal
        from app.models.schema import SymbolCreate
        from app.services.repository import FundamentalSnapshotRepository, SymbolRepository, WatchlistRepository

        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            watchlist_repo = WatchlistRepository(db)
            watchlist = watchlist_repo.get_or_create_default()
            first = symbol_repo.get_or_create_symbol(SymbolCreate(ticker="601288.SS", name="农业银行", market="CN", exchange="SSE"))
            second = symbol_repo.get_or_create_symbol(SymbolCreate(ticker="601398.SS", name="工商银行", market="CN", exchange="SSE"))
            watchlist_repo.add_symbol(watchlist.id, first.id)
            watchlist_repo.add_symbol(watchlist.id, second.id)
            repo = FundamentalSnapshotRepository(db)
            repo.upsert_snapshot(
                symbol_id=first.id,
                report_date="2026-03-31",
                source="tushare",
                listing_date="2010-07-15",
                pe_ttm=7.1,
                dividend_yield=5.8,
                market_cap=1300000000000.0,
                roe_avg_3y=11.4,
                net_profit_yoy=2.0,
                revenue_yoy=1.2,
                debt_to_assets=63.0,
            )
            repo.upsert_snapshot(
                symbol_id=second.id,
                report_date="2026-03-31",
                source="tushare",
                listing_date="2006-10-27",
                pe_ttm=6.8,
                dividend_yield=4.2,
                market_cap=1700000000000.0,
                roe_avg_3y=11.0,
                net_profit_yoy=1.5,
                revenue_yoy=1.0,
                debt_to_assets=61.0,
            )

        response = self.client.get(
            "/screeners",
            params={
                "model_template": "cn_low_valuation_high_dividend",
                "universe": "watchlist",
                "market": "CN",
                "min_trend_score": 0,
                "min_listing_days": 1095,
                "pe_min": 0,
                "pe_max": 20,
                "min_roe_avg_3y": 10,
                "min_net_profit_yoy": 0,
                "min_revenue_yoy": 0,
                "max_debt_to_assets": 70,
                "min_dividend_yield": 3,
                "exclude_bottom_market_cap_pct": 0,
                "sort_by": "dividend_yield",
                "sort_order": "desc",
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertLess(response.text.index("601288.SS"), response.text.index("601398.SS"))

    def test_screener_can_add_result_to_watchlist(self) -> None:
        from app.core.db import SessionLocal
        from app.services.repository import WatchlistRepository

        response = self.client.post(
            "/screeners/add-to-watchlist",
            data={
                "ticker": "ASTS",
                "name": "AST SpaceMobile",
                "symbol_market": "US",
                "model_template": "technical_momentum",
                "universe": "watchlist",
                "market": "ALL",
                "min_trend_score": "60",
                "action_filter": "ALL",
                "min_volume_ratio": "0",
                "min_listing_days": "365",
                "pe_min": "0",
                "pe_max": "30",
                "min_roe_avg_3y": "12",
                "min_net_profit_yoy": "20",
                "min_revenue_yoy": "0",
                "max_debt_to_assets": "100",
                "min_dividend_yield": "0",
                "exclude_bottom_market_cap_pct": "10",
                "sort_by": "default",
                "sort_order": "desc",
            },
            follow_redirects=True,
        )

        self.assertEqual(200, response.status_code)
        self.assertIn("Added ASTS to watchlist", response.text)

        with SessionLocal() as db:
            watchlist = WatchlistRepository(db).get_or_create_default()
            watchlist_map = WatchlistRepository(db).list_ticker_map(watchlist.id)

        self.assertIn("ASTS", watchlist_map)
        self.assertEqual("AST SpaceMobile", watchlist_map["ASTS"]["name"])

    def test_technical_screener_name_prefers_symbol_name(self) -> None:
        from unittest.mock import patch

        from app.core.db import SessionLocal
        from app.models.schema import SymbolCreate
        from app.services.repository import SymbolRepository, WatchlistRepository

        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            watchlist_repo = WatchlistRepository(db)
            watchlist = watchlist_repo.get_or_create_default()
            symbol_repo.get_or_create_symbol(
                SymbolCreate(ticker="RKLB", name="Rocket Lab Corporation", market="US", exchange="NASDAQ")
            )
            symbol = symbol_repo.get_by_ticker("RKLB")
            watchlist_repo.add_symbol(watchlist.id, symbol.id)

        with patch(
            "app.services.screener.InsightEngine.get_insight",
            return_value={
                "ticker": "RKLB",
                "as_of_date": "2026-04-03",
                "trend_score": 72,
                "action_label": "wait_for_breakout",
                "action_summary": "Wait for breakout",
                "latest_close": 24.5,
                "momentum_5": 6.1,
                "momentum_20": 18.4,
                "volume_ratio": 1.6,
                "distance_to_breakout_pct": 2.4,
            },
        ):
            response = self.client.get(
                "/screeners",
                params={
                    "model_template": "technical_momentum",
                    "universe": "watchlist",
                    "market": "US",
                    "min_trend_score": "1",
                    "action_filter": "ALL",
                    "min_volume_ratio": "0",
                },
            )

        self.assertEqual(200, response.status_code)
        self.assertIn("Rocket Lab Corporation", response.text)

    def test_screener_can_bulk_add_results_to_watchlist(self) -> None:
        from app.core.db import SessionLocal
        from app.services.repository import WatchlistRepository
        from app.services.sample_data import seed_sample_data

        seed_sample_data()

        response = self.client.post(
            "/screeners/add-all-to-watchlist",
            data={
                "model_template": "technical_momentum",
                "universe": "synced",
                "market": "US",
                "min_trend_score": "1",
                "action_filter": "ALL",
                "min_volume_ratio": "0",
                "min_listing_days": "365",
                "pe_min": "0",
                "pe_max": "30",
                "min_roe_avg_3y": "12",
                "min_net_profit_yoy": "20",
                "min_revenue_yoy": "0",
                "max_debt_to_assets": "100",
                "min_dividend_yield": "0",
                "exclude_bottom_market_cap_pct": "10",
                "sort_by": "default",
                "sort_order": "desc",
            },
            follow_redirects=True,
        )

        self.assertEqual(200, response.status_code)
        self.assertIn("Added", response.text)
        self.assertIn("screener results to watchlist", response.text)

        with SessionLocal() as db:
            watchlist = WatchlistRepository(db).get_or_create_default()
            watchlist_map = WatchlistRepository(db).list_ticker_map(watchlist.id)

        self.assertIn("ASTS", watchlist_map)
        self.assertIn("AAPL", watchlist_map)
        self.assertIn("MSFT", watchlist_map)

    def test_screener_can_bulk_add_top_n_and_enable_sync(self) -> None:
        from app.core.db import SessionLocal
        from app.services.repository import WatchlistRepository
        from app.services.sample_data import seed_sample_data

        seed_sample_data()

        response = self.client.post(
            "/screeners/add-all-to-watchlist",
            data={
                "model_template": "technical_momentum",
                "universe": "synced",
                "market": "US",
                "min_trend_score": "1",
                "action_filter": "ALL",
                "min_volume_ratio": "0",
                "min_listing_days": "365",
                "pe_min": "0",
                "pe_max": "30",
                "min_roe_avg_3y": "12",
                "min_net_profit_yoy": "20",
                "min_revenue_yoy": "0",
                "max_debt_to_assets": "100",
                "min_dividend_yield": "0",
                "exclude_bottom_market_cap_pct": "10",
                "sort_by": "trend_score",
                "sort_order": "desc",
                "bulk_top_n": "1",
                "auto_enable_sync": "1",
            },
            follow_redirects=True,
        )

        self.assertEqual(200, response.status_code)
        self.assertIn("Added 1 screener results to watchlist", response.text)
        self.assertIn("Sync enabled for 1", response.text)
        self.assertIn("Open Watchlist", response.text)
        self.assertIn("Review Sync Settings", response.text)

        with SessionLocal() as db:
            watchlist = WatchlistRepository(db).get_or_create_default()
            watchlist_map = WatchlistRepository(db).list_ticker_map(watchlist.id)

        self.assertEqual(1, len(watchlist_map))
        only_item = next(iter(watchlist_map.values()))
        self.assertEqual(1, only_item["sync_enabled"])

        screener_response = self.client.get(
            "/screeners",
            params={
                "universe": "watchlist",
                "market": "US",
                "min_trend_score": "1",
                "action_filter": "ALL",
                "min_volume_ratio": "0",
            },
        )
        self.assertEqual(200, screener_response.status_code)
        self.assertIn("Sync On", screener_response.text)
        self.assertIn("Ready", screener_response.text)

    def test_screener_sync_now_updates_watchlist_item(self) -> None:
        from app.core.db import SessionLocal
        from app.models.schema import SymbolCreate
        from app.services.repository import PriceSyncStateRepository, SymbolRepository, WatchlistRepository

        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            watchlist_repo = WatchlistRepository(db)
            watchlist = watchlist_repo.get_or_create_default()
            symbol = symbol_repo.get_or_create_symbol(
                SymbolCreate(ticker="TSLA", name="Tesla", market="US")
            )
            item = watchlist_repo.add_symbol(watchlist.id, symbol.id)

        with patch("app.api.routes.screener.sync_market_data", return_value=[{"ticker": "TSLA", "status": "success", "rows": 125}]):
            response = self.client.post(
                "/screeners/sync-symbol",
                data={
                    "ticker": "TSLA",
                    "item_id": str(item.id),
                    "model_template": "technical_momentum",
                    "universe": "watchlist",
                    "market": "US",
                    "min_trend_score": "1",
                    "action_filter": "ALL",
                    "min_volume_ratio": "0",
                    "min_listing_days": "365",
                    "pe_min": "0",
                    "pe_max": "30",
                    "min_roe_avg_3y": "12",
                    "min_net_profit_yoy": "20",
                    "min_revenue_yoy": "0",
                    "max_debt_to_assets": "100",
                    "min_dividend_yield": "0",
                    "exclude_bottom_market_cap_pct": "10",
                    "sort_by": "default",
                    "sort_order": "desc",
                },
                follow_redirects=True,
            )

        self.assertEqual(200, response.status_code)
        self.assertIn("Synced TSLA", response.text)
        self.assertIn("Open Watchlist", response.text)

        with SessionLocal() as db:
            watchlist = WatchlistRepository(db).get_or_create_default()
            watchlist_map = WatchlistRepository(db).list_ticker_map(watchlist.id)
            state = PriceSyncStateRepository(db).get_state_for_ticker("TSLA")

        self.assertEqual(1, watchlist_map["TSLA"]["sync_enabled"])
        self.assertIsNone(state)

    def test_screener_can_sync_top_results(self) -> None:
        from app.services.sample_data import seed_sample_data

        seed_sample_data()
        self.client.post(
            "/watchlist/add",
            data={"ticker": "ASTS", "name": "AST SpaceMobile", "market": "US", "sync_after_add": ""},
            follow_redirects=True,
        )

        with patch("app.api.routes.screener.sync_market_data", return_value=[{"ticker": "ASTS", "status": "success", "rows": 125}]):
            response = self.client.post(
                "/screeners/sync-top-results",
                data={
                    "lang": "en",
                    "model_template": "technical_momentum",
                    "universe": "watchlist",
                    "market": "US",
                    "min_trend_score": "1",
                    "action_filter": "ALL",
                    "min_volume_ratio": "0",
                    "min_listing_days": "365",
                    "pe_min": "0",
                    "pe_max": "30",
                    "min_roe_avg_3y": "12",
                    "min_net_profit_yoy": "20",
                    "min_revenue_yoy": "0",
                    "max_debt_to_assets": "100",
                    "min_dividend_yield": "0",
                    "exclude_bottom_market_cap_pct": "10",
                    "sort_by": "default",
                    "sort_order": "desc",
                    "sync_top_n": "1",
                },
                follow_redirects=True,
            )

        self.assertEqual(200, response.status_code)
        self.assertIn("Synced 1/1 screener results", response.text)

    def test_sync_cn_fundamentals_job_writes_snapshots(self) -> None:
        from app.core.db import SessionLocal
        from app.services.repository import FundamentalSnapshotRepository
        from app.services.tushare_client import TushareClient

        os.environ["PQW_TUSHARE_TOKEN"] = "test-token"

        fake_rows = [
            CNFundamentalRow(
                ticker="600519.SH",
                report_date="2026-03-31",
                listing_date="2001-08-27",
                pe_ttm=24.3,
                market_cap=1800000000000.0,
                roe_avg_3y=28.5,
                net_profit_yoy=21.2,
                revenue_yoy=11.8,
                debt_to_assets=34.0,
                raw_data={"source": "mock"},
            )
        ]

        with patch.object(TushareClient, "is_configured", return_value=True):
            with patch.object(TushareClient, "fetch_cn_growth_value_candidates", return_value=fake_rows):
                response = self.client.post(
                    "/jobs/sync-cn-fundamentals",
                    data={"tickers": "600519.SH"},
                )

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("success", payload["status"])
        self.assertEqual(1, payload["rows_written"])

        with SessionLocal() as db:
            latest = FundamentalSnapshotRepository(db).get_latest_for_ticker("600519.SS")

        self.assertIsNotNone(latest)
        self.assertEqual(24.3, latest["pe_ttm"])
        self.assertEqual(28.5, latest["roe_avg_3y"])

    def test_sync_global_fundamentals_job_writes_snapshots(self) -> None:
        from app.core.db import SessionLocal
        from app.services.openbb_client import OpenBBClient
        from app.services.repository import FundamentalSnapshotRepository

        def fake_snapshot(self, ticker: str):
            payload = {
                "ASTS": {
                    "report_date": "2026-04-03",
                    "name": "AST SpaceMobile",
                    "exchange": "NASDAQ",
                    "listing_date": "2021-04-07",
                    "pe_ttm": 22.4,
                    "dividend_yield": None,
                    "market_cap": 5200000000.0,
                    "roe_avg_3y": 9.8,
                    "net_profit_yoy": 18.5,
                    "revenue_yoy": 32.1,
                    "debt_to_assets": 28.0,
                    "raw_data": {"provider": "mock"},
                },
                "0700.HK": {
                    "report_date": "2026-04-02",
                    "name": "腾讯控股",
                    "exchange": "HKEX",
                    "listing_date": "2004-06-16",
                    "pe_ttm": 19.7,
                    "dividend_yield": 1.2,
                    "market_cap": 3200000000000.0,
                    "roe_avg_3y": 21.3,
                    "net_profit_yoy": 14.2,
                    "revenue_yoy": 11.0,
                    "debt_to_assets": 41.0,
                    "raw_data": {"provider": "mock"},
                },
            }
            return payload.get(ticker.upper())

        with patch.object(OpenBBClient, "fetch_fundamental_snapshot", new=fake_snapshot):
            response = self.client.post(
                "/jobs/sync-global-fundamentals",
                data={"tickers": "ASTS,0700.HK"},
            )

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("success", payload["status"])
        self.assertEqual(2, payload["rows_written"])

        with SessionLocal() as db:
            us_latest = FundamentalSnapshotRepository(db).get_latest_for_ticker("ASTS")
            hk_latest = FundamentalSnapshotRepository(db).get_latest_for_ticker("0700.HK")

        self.assertIsNotNone(us_latest)
        self.assertEqual(22.4, us_latest["pe_ttm"])
        self.assertEqual("US", us_latest["market"])
        self.assertIsNotNone(hk_latest)
        self.assertEqual(1.2, hk_latest["dividend_yield"])
        self.assertEqual("HK", hk_latest["market"])

    def test_global_growth_value_template_uses_us_hk_snapshots(self) -> None:
        from app.core.db import SessionLocal
        from app.models.schema import SymbolCreate
        from app.services.repository import FundamentalSnapshotRepository, SymbolRepository, WatchlistRepository

        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            watchlist_repo = WatchlistRepository(db)
            watchlist = watchlist_repo.get_or_create_default()

            us_symbol = symbol_repo.get_or_create_symbol(
                SymbolCreate(ticker="ASTS", name="AST SpaceMobile", market="US", exchange="NASDAQ")
            )
            hk_symbol = symbol_repo.get_or_create_symbol(
                SymbolCreate(ticker="0700.HK", name="腾讯控股", market="HK", exchange="HKEX")
            )
            weak_symbol = symbol_repo.get_or_create_symbol(
                SymbolCreate(ticker="PL", name="Planet Labs", market="US", exchange="NYSE")
            )

            for symbol in (us_symbol, hk_symbol, weak_symbol):
                watchlist_repo.add_symbol(watchlist.id, symbol.id)

            repo = FundamentalSnapshotRepository(db)
            repo.upsert_snapshot(
                symbol_id=us_symbol.id,
                report_date="2026-04-03",
                source="yfinance_fundamentals",
                listing_date="2021-04-07",
                pe_ttm=22.4,
                market_cap=5200000000.0,
                roe_avg_3y=9.8,
                net_profit_yoy=18.5,
                revenue_yoy=32.1,
                debt_to_assets=28.0,
            )
            repo.upsert_snapshot(
                symbol_id=hk_symbol.id,
                report_date="2026-04-02",
                source="yfinance_fundamentals",
                listing_date="2004-06-16",
                pe_ttm=19.7,
                market_cap=3200000000000.0,
                roe_avg_3y=21.3,
                net_profit_yoy=14.2,
                revenue_yoy=11.0,
                debt_to_assets=41.0,
            )
            repo.upsert_snapshot(
                symbol_id=weak_symbol.id,
                report_date="2026-04-03",
                source="yfinance_fundamentals",
                listing_date="2021-12-08",
                pe_ttm=88.0,
                market_cap=2100000000.0,
                roe_avg_3y=1.1,
                net_profit_yoy=-12.0,
                revenue_yoy=3.0,
                debt_to_assets=82.0,
            )

        response = self.client.get(
            "/screeners",
            params={
                "model_template": "global_growth_value",
                "universe": "watchlist",
                "market": "ALL",
                "min_trend_score": 0,
                "min_listing_days": 365,
                "pe_min": 0,
                "pe_max": 35,
                "min_roe_avg_3y": 8,
                "min_net_profit_yoy": 10,
                "min_revenue_yoy": 8,
                "max_debt_to_assets": 75,
                "exclude_bottom_market_cap_pct": 0,
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertIn("AST SpaceMobile", response.text)
        self.assertIn("腾讯控股", response.text)
        self.assertNotIn("Planet Labs", response.text)

    def test_tushare_client_maps_stock_daily_and_fina_fields(self) -> None:
        from app.core.config import reset_settings_cache
        from app.services.tushare_client import TushareClient

        os.environ["PQW_TUSHARE_TOKEN"] = "test-token"
        reset_settings_cache()

        class FakePro:
            def stock_basic(self, **kwargs):
                self.stock_kwargs = kwargs
                return pd.DataFrame(
                    [
                        {
                            "ts_code": "600519.SH",
                            "name": "贵州茅台",
                            "exchange": "SSE",
                            "list_date": "20010827",
                        }
                    ]
                )

            def daily_basic(self, **kwargs):
                self.daily_kwargs = kwargs
                return pd.DataFrame(
                    [
                        {"ts_code": "600519.SH", "trade_date": "20260402", "pe_ttm": 24.3, "total_mv": 180000000.0},
                        {"ts_code": "600519.SH", "trade_date": "20260401", "pe_ttm": 24.1, "total_mv": 179000000.0},
                    ]
                )

            def fina_indicator(self, **kwargs):
                self.fina_kwargs = kwargs
                return pd.DataFrame(
                    [
                        {
                            "ts_code": "600519.SH",
                            "end_date": "20251231",
                            "roe": 29.0,
                            "roe_avg": 28.0,
                            "netprofit_yoy": 18.0,
                            "q_netprofit_yoy": 21.2,
                            "q_dtprofit_yoy": 19.3,
                            "q_sales_yoy": 11.8,
                            "tr_yoy": 10.4,
                            "debt_asset_ratio": 34.0,
                        },
                        {
                            "ts_code": "600519.SH",
                            "end_date": "20241231",
                            "roe": 27.0,
                            "roe_avg": 26.0,
                            "netprofit_yoy": 16.0,
                            "q_netprofit_yoy": 18.1,
                            "q_dtprofit_yoy": 17.3,
                            "q_sales_yoy": 10.8,
                            "tr_yoy": 9.4,
                            "debt_asset_ratio": 33.0,
                        },
                        {
                            "ts_code": "600519.SH",
                            "end_date": "20231231",
                            "roe": 25.0,
                            "roe_avg": 24.0,
                            "netprofit_yoy": 14.0,
                            "q_netprofit_yoy": 16.1,
                            "q_dtprofit_yoy": 15.3,
                            "q_sales_yoy": 9.8,
                            "tr_yoy": 8.4,
                            "debt_asset_ratio": 32.0,
                        },
                    ]
                )

        fake_pro = FakePro()

        with patch("tushare.pro_api", return_value=fake_pro):
            rows = TushareClient().fetch_cn_growth_value_candidates(["600519.SH"])

        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual("600519.SS", row.ticker)
        self.assertEqual("贵州茅台", row.name)
        self.assertEqual("SSE", row.exchange)
        self.assertEqual("2001-08-27", row.listing_date)
        self.assertEqual("2026-04-02", row.report_date)
        self.assertEqual(24.3, row.pe_ttm)
        self.assertEqual(1800000000000.0, row.market_cap)
        self.assertEqual(26.0, row.roe_avg_3y)
        self.assertEqual(21.2, row.net_profit_yoy)
        self.assertEqual(11.8, row.revenue_yoy)
        self.assertEqual(34.0, row.debt_to_assets)

    def test_watchlist_add_and_sync_now_works(self) -> None:
        from app.services.openbb_client import OpenBBClient

        def fake_fetch(self, request) -> list[dict]:
            symbol = request.ticker
            return [
                {
                    "date": "2026-04-01",
                    "symbol": symbol,
                    "open": 10.0,
                    "high": 10.5,
                    "low": 9.8,
                    "close": 10.2,
                    "volume": 120000,
                },
                {
                    "date": "2026-04-02",
                    "symbol": symbol,
                    "open": 10.2,
                    "high": 10.8,
                    "low": 10.0,
                    "close": 10.7,
                    "volume": 140000,
                },
            ]

        with patch.object(OpenBBClient, "fetch_historical_prices", new=fake_fetch):
            add_response = self.client.post(
                "/watchlist/add",
                data={"ticker": "0700.HK", "market": "HK", "name": "", "sync_after_add": "true"},
                follow_redirects=True,
            )
            table_response = self.client.get("/watchlist/table-fragment?mode=monitor")

        self.assertEqual(200, add_response.status_code)
        self.assertIn("Added 0700.HK and synced 2 rows.", add_response.text)
        self.assertEqual(200, table_response.status_code)
        self.assertIn("腾讯控股", table_response.text)

    def test_refresh_existing_watchlist_metadata_repairs_old_rows(self) -> None:
        from app.core.db import SessionLocal
        from app.models.schema import SymbolCreate
        from app.services.openbb_client import OpenBBClient
        from app.services.repository import SymbolRepository, WatchlistRepository

        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            watchlist_repo = WatchlistRepository(db)
            watchlist = watchlist_repo.get_or_create_default()
            symbol = symbol_repo.get_or_create_symbol(SymbolCreate(ticker="0883.HK", name=None, market="HK"))
            watchlist_repo.add_symbol(watchlist.id, symbol.id)

        with patch.object(
            OpenBBClient,
            "fetch_symbol_profile",
            return_value={"ticker": "0883.HK", "name": "中国海洋石油", "exchange": "HKEX"},
        ):
            response = self.client.post("/watchlist/refresh-metadata", follow_redirects=True)
        self.assertEqual(200, response.status_code)
        self.assertIn("Updated metadata for 1 existing watchlist stock(s).", response.text)
        self.assertIn("中国海洋石油", response.text)

    def test_refresh_existing_watchlist_metadata_uses_live_profile_for_us_stock(self) -> None:
        from app.core.db import SessionLocal
        from app.models.schema import SymbolCreate
        from app.services.openbb_client import OpenBBClient
        from app.services.repository import SymbolRepository, WatchlistRepository

        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            watchlist_repo = WatchlistRepository(db)
            watchlist = watchlist_repo.get_or_create_default()
            symbol = symbol_repo.get_or_create_symbol(SymbolCreate(ticker="RKLB", name=None, market="US"))
            watchlist_repo.add_symbol(watchlist.id, symbol.id)

        with patch.object(
            OpenBBClient,
            "fetch_symbol_profile",
            return_value={"ticker": "RKLB", "name": "Rocket Lab USA, Inc.", "exchange": "NASDAQ"},
        ):
            response = self.client.post("/watchlist/refresh-metadata", follow_redirects=True)

        self.assertEqual(200, response.status_code)
        self.assertIn("Rocket Lab USA, Inc.", response.text)
        self.assertIn("NASDAQ", response.text)

    def test_hk_symbol_alias_00100_maps_to_minimax(self) -> None:
        from app.services.symbol_catalog import infer_symbol_record

        record = infer_symbol_record("00100.HK", "HK")

        self.assertIsNotNone(record)
        self.assertEqual("MINIMAX-W", record["name"])
        self.assertEqual("0100.HK", record["ticker"])

    def test_symbol_history_alias_reads_00100_from_0100_file(self) -> None:
        from app.core.config import get_settings
        from app.services.symbol_details import SymbolDataService

        settings = get_settings()
        path = settings.normalized_data_dir / "0100.HK.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "date,symbol,open,high,low,close,volume,adj_close,dividend,split_ratio\n"
            "2026-03-12,0100.HK,1160,1221,1072,1084,1634305,1084,,\n"
            "2026-03-13,0100.HK,1100,1124,992,1010,1297992,1010,,\n",
            encoding="utf-8",
        )

        rows = SymbolDataService().get_history("00100.HK", limit=10)

        self.assertEqual(2, len(rows))
        self.assertEqual("2026-03-13", rows[-1]["date"])
        self.assertEqual(1010.0, rows[-1]["close"])

    def test_hk_sync_retries_with_five_digit_alias(self) -> None:
        from app.core.db import SessionLocal
        from app.models.schema import SymbolCreate
        from app.services.openbb_client import OpenBBClient
        from app.services.market_sync import sync_market_data
        from app.services.repository import SymbolRepository

        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            symbol_repo.get_or_create_symbol(SymbolCreate(ticker="0100.HK", name="MINIMAX-W", market="HK"))

        def fake_fetch(self, request):
            if request.ticker == "0100.HK":
                return [
                    {
                        "date": "2026-04-02",
                        "symbol": request.ticker,
                        "open": 100.0,
                        "high": 101.0,
                        "low": 98.0,
                        "close": 99.0,
                        "volume": 1000,
                    }
                ]
            if request.ticker == "00100.HK":
                return [
                    {
                        "date": "2026-04-01",
                        "symbol": request.ticker,
                        "open": 95.0,
                        "high": 99.0,
                        "low": 94.0,
                        "close": 97.0,
                        "volume": 900,
                    },
                    {
                        "date": "2026-04-02",
                        "symbol": request.ticker,
                        "open": 100.0,
                        "high": 101.0,
                        "low": 98.0,
                        "close": 99.0,
                        "volume": 1000,
                    },
                    {
                        "date": "2026-04-03",
                        "symbol": request.ticker,
                        "open": 101.0,
                        "high": 105.0,
                        "low": 100.0,
                        "close": 104.0,
                        "volume": 1100,
                    },
                ]
            return []

        with patch.object(OpenBBClient, "fetch_historical_prices", new=fake_fetch):
            results = sync_market_data(tickers=["0100.HK"], start_date="2026-04-01", provider="yfinance")

        self.assertEqual("success", results[0]["status"])
        self.assertEqual(3, results[0]["rows"])
        self.assertEqual("00100.HK", results[0]["provider_ticker"])

    def test_openbb_permission_error_falls_back_to_yfinance_history(self) -> None:
        from app.services.openbb_client import HistoricalPriceRequest, OpenBBClient

        client = OpenBBClient()
        request = HistoricalPriceRequest(ticker="600000.SS", start_date="2026-04-01", provider="yfinance")

        with patch.object(OpenBBClient, "_load_openbb", return_value=None), patch.object(
            OpenBBClient,
            "_fetch_with_yfinance",
            return_value=[
                {
                    "date": "2026-04-01",
                    "symbol": "600000.SS",
                    "open": 10.0,
                    "high": 10.2,
                    "low": 9.8,
                    "close": 10.1,
                    "volume": 1000,
                    "adj_close": 10.1,
                    "dividend": None,
                    "split_ratio": None,
                }
            ],
        ):
            rows = client.fetch_historical_prices(request)

        self.assertEqual(1, len(rows))
        self.assertEqual("yfinance", client.last_source_used)
        self.assertEqual("2026-04-01", rows[0]["date"])

    def test_cn_history_prefers_akshare_before_yfinance(self) -> None:
        from app.services.openbb_client import HistoricalPriceRequest, OpenBBClient

        client = OpenBBClient()
        request = HistoricalPriceRequest(ticker="600000.SS", start_date="2026-04-01", provider="yfinance")

        with patch.object(
            OpenBBClient,
            "_fetch_with_akshare",
            return_value=[
                {
                    "date": "2026-04-01",
                    "symbol": "600000.SS",
                    "open": 10.0,
                    "high": 10.2,
                    "low": 9.8,
                    "close": 10.1,
                    "volume": 1000,
                    "adj_close": 10.1,
                    "dividend": None,
                    "split_ratio": None,
                }
            ],
        ) as akshare_mock, patch.object(
            OpenBBClient,
            "_fetch_with_yfinance",
            return_value=[],
        ) as yfinance_mock:
            rows = client.fetch_historical_prices(request)

        self.assertEqual(1, len(rows))
        self.assertEqual("akshare", client.last_source_used)
        akshare_mock.assert_called_once()
        yfinance_mock.assert_not_called()

    def test_cn_history_prefers_tushare_before_other_sources(self) -> None:
        from app.services.openbb_client import HistoricalPriceRequest, OpenBBClient

        client = OpenBBClient()
        request = HistoricalPriceRequest(ticker="600000.SS", start_date="2026-04-01", provider="yfinance")

        with patch(
            "app.services.openbb_client.TushareClient.fetch_cn_daily_history",
            return_value=[
                {
                    "date": "2026-04-01",
                    "symbol": "600000.SS",
                    "open": 10.0,
                    "high": 10.2,
                    "low": 9.8,
                    "close": 10.1,
                    "volume": 1000,
                    "adj_close": 10.1,
                    "dividend": None,
                    "split_ratio": None,
                }
            ],
        ) as tushare_mock, patch.object(
            OpenBBClient,
            "_fetch_with_akshare",
            return_value=[],
        ) as akshare_mock, patch.object(
            OpenBBClient,
            "_fetch_with_yfinance",
            return_value=[],
        ) as yfinance_mock:
            rows = client.fetch_historical_prices(request)

        self.assertEqual(1, len(rows))
        self.assertEqual("tushare", client.last_source_used)
        tushare_mock.assert_called_once()
        akshare_mock.assert_not_called()
        yfinance_mock.assert_not_called()

    def test_cn_history_falls_back_to_baostock_before_yfinance(self) -> None:
        from app.services.openbb_client import HistoricalPriceRequest, OpenBBClient

        client = OpenBBClient()
        request = HistoricalPriceRequest(ticker="600000.SS", start_date="2026-04-01", provider="yfinance")

        with patch(
            "app.services.openbb_client.TushareClient.fetch_cn_daily_history",
            return_value=[],
        ), patch.object(
            OpenBBClient,
            "_fetch_with_akshare",
            return_value=[],
        ), patch.object(
            OpenBBClient,
            "_fetch_with_baostock",
            return_value=[
                {
                    "date": "2026-04-01",
                    "symbol": "600000.SS",
                    "open": 10.0,
                    "high": 10.2,
                    "low": 9.8,
                    "close": 10.1,
                    "volume": 1000,
                    "adj_close": 10.1,
                    "dividend": None,
                    "split_ratio": None,
                }
            ],
        ) as baostock_mock, patch.object(
            OpenBBClient,
            "_fetch_with_yfinance",
            return_value=[],
        ) as yfinance_mock:
            rows = client.fetch_historical_prices(request)

        self.assertEqual(1, len(rows))
        self.assertEqual("baostock", client.last_source_used)
        baostock_mock.assert_called_once()
        yfinance_mock.assert_not_called()

    def test_cn_history_falls_back_to_eastmoney_before_akshare(self) -> None:
        from app.services.openbb_client import HistoricalPriceRequest, OpenBBClient

        client = OpenBBClient()
        request = HistoricalPriceRequest(ticker="600000.SS", start_date="2026-04-01", provider="yfinance")

        with patch(
            "app.services.openbb_client.TushareClient.fetch_cn_daily_history",
            return_value=[],
        ), patch.object(
            OpenBBClient,
            "_fetch_with_eastmoney_cn",
            return_value=[
                {
                    "date": "2026-04-01",
                    "symbol": "600000.SS",
                    "open": 10.0,
                    "high": 10.2,
                    "low": 9.8,
                    "close": 10.1,
                    "volume": 1000,
                    "adj_close": 10.1,
                    "dividend": None,
                    "split_ratio": None,
                }
            ],
        ) as eastmoney_mock, patch.object(
            OpenBBClient,
            "_fetch_with_akshare",
            return_value=[],
        ) as akshare_mock:
            rows = client.fetch_historical_prices(request)

        self.assertEqual(1, len(rows))
        self.assertEqual("eastmoney", client.last_source_used)
        eastmoney_mock.assert_called_once()
        akshare_mock.assert_not_called()

    def test_baostock_code_mapping_supports_cn_tickers(self) -> None:
        from app.services.openbb_client import OpenBBClient

        client = OpenBBClient()

        self.assertEqual("sh.600000", client._to_baostock_code("600000.SS"))
        self.assertEqual("sh.600000", client._to_baostock_code("600000.SH"))
        self.assertEqual("sz.000001", client._to_baostock_code("000001.SZ"))

    def test_cn_ticker_format_supports_beijing_exchange(self) -> None:
        from app.services.openbb_client import OpenBBClient
        from app.services.ticker_format import normalize_ticker_for_market

        self.assertEqual("920000.BJ", normalize_ticker_for_market("920000", "CN"))
        self.assertEqual("920000.BJ", normalize_ticker_for_market("920000.BJ", "CN"))
        self.assertEqual("CN", OpenBBClient()._infer_market("920000.BJ"))

    def test_tradingview_client_maps_cn_and_us_markets(self) -> None:
        from app.services.tradingview_client import TradingViewClient

        client = TradingViewClient()

        cn_config = client._resolve_market_config(ticker="600000.SS", market="CN", exchange="SSE")
        us_config = client._resolve_market_config(ticker="AAPL", market="US", exchange="NASDAQ")

        self.assertIsNotNone(cn_config)
        self.assertEqual("china", cn_config.screener)
        self.assertEqual("SSE", cn_config.exchange)
        self.assertIsNotNone(us_config)
        self.assertEqual("america", us_config.screener)
        self.assertEqual("NASDAQ", us_config.exchange)

    def test_tradingview_client_falls_back_to_curl_scanner(self) -> None:
        from app.services.tradingview_client import TradingViewClient

        client = TradingViewClient()

        class FakeCompleted:
            returncode = 0
            stderr = ""
            stdout = (
                '{"data":[{"s":"NASDAQ:AAPL","d":['
                "-0.09,-0.17,-0.26,46.88,54.4,0,0,0,0,0,0,0,0,0,0,0,0,0,2.0,1.0,-1.67,-2.66,0,0,0,0,0,0,0,0,253.5,"
                "0,0,250,0,249,0,248,0,247,0,246,0,245,0,1,0,1,0,1,0,"
                "0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,252,0,240,260,0,0,0,0,0"
                ']}]}'
            )

        with patch(
            "app.services.tradingview_client.subprocess.run",
            return_value=FakeCompleted(),
        ), patch(
            "app.services.tradingview_client.TradingViewClient._resolve_market_config",
            return_value=client._resolve_market_config(ticker="AAPL", market="US", exchange="NASDAQ"),
        ), patch(
            "tradingview_ta.TA_Handler.get_analysis",
            side_effect=RuntimeError("primary path failed"),
        ):
            payload = client.get_technical_rating(ticker="AAPL", market="US", exchange="NASDAQ", interval="1d")

        self.assertEqual("success", payload["status"])
        self.assertEqual("tradingview_curl", payload["source"])
        self.assertIn("recommendation", payload)

    def test_symbol_technical_rating_endpoint_returns_tradingview_payload(self) -> None:
        self._seed_symbol("600000.SS", "浦发银行", "CN", "SSE")

        with patch(
            "app.api.routes.symbols.TradingViewClient.get_technical_rating",
            return_value={
                "ticker": "600000.SS",
                "market": "CN",
                "exchange": "SSE",
                "interval": "1d",
                "status": "success",
                "recommendation": "BUY",
                "source": "tradingview_ta",
            },
        ):
            response = self.client.get("/symbols/600000.SS/technical-rating")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("BUY", payload["recommendation"])
        self.assertEqual("tradingview_ta", payload["source"])

    def test_symbol_multi_timeframe_analysis_endpoint_returns_alignment(self) -> None:
        self._seed_symbol("600000.SS", "浦发银行", "CN", "SSE")

        with patch(
            "app.api.routes.symbols.TradingViewClient.get_multi_timeframe_analysis",
            return_value={
                "ticker": "600000.SS",
                "status": "success",
                "alignment": "bullish_alignment",
                "bullish_count": 4,
                "bearish_count": 0,
                "neutral_count": 1,
                "ratings": {
                    "1w": {"recommendation": "BUY"},
                    "1d": {"recommendation": "STRONG_BUY"},
                    "4h": {"recommendation": "BUY"},
                    "1h": {"recommendation": "BUY"},
                    "15m": {"recommendation": "NEUTRAL"},
                },
            },
        ):
            response = self.client.get("/symbols/600000.SS/multi-timeframe-analysis")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("bullish_alignment", payload["alignment"])
        self.assertEqual(4, payload["bullish_count"])

    def test_symbol_bollinger_band_analysis_endpoint_returns_local_band_payload(self) -> None:
        self._seed_symbol("600013.SS", "布林测试股", "CN", "SSE")
        self._write_price_history("600013.SS", self._build_squeeze_cn_history())

        response = self.client.get("/symbols/600013.SS/bollinger-band-analysis")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("success", payload["status"])
        self.assertIn("rating", payload)
        self.assertIn("bandwidth_pct", payload)
        self.assertIn("squeeze", payload)

    def test_symbol_candlestick_patterns_endpoint_returns_detected_patterns(self) -> None:
        self._seed_symbol("600011.SS", "吞没测试股", "CN", "SSE")
        self._write_price_history("600011.SS", self._build_bullish_engulfing_history())

        response = self.client.get("/symbols/600011.SS/candlestick-patterns")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("success", payload["status"])
        self.assertIn("看涨吞没", payload["patterns"])

    def test_symbol_combined_analysis_endpoint_returns_decision_payload(self) -> None:
        self._seed_symbol("600000.SS", "浦发银行", "CN", "SSE")

        with patch(
            "app.api.routes.symbols.TradingViewClient.get_technical_rating",
            return_value={"ticker": "600000.SS", "status": "success", "recommendation": "BUY"},
        ), patch(
            "app.api.routes.symbols.TradingViewClient.get_multi_timeframe_analysis",
            return_value={
                "ticker": "600000.SS",
                "status": "success",
                "alignment": "bullish_alignment",
                "bullish_count": 4,
                "bearish_count": 0,
                "neutral_count": 1,
                "ratings": {"1d": {"recommendation": "BUY"}},
            },
        ), patch(
            "app.api.routes.symbols.TechnicalPatternService.get_bollinger_band_analysis",
            return_value={"ticker": "600000.SS", "status": "success", "signal": "upper_band_strength", "squeeze": True},
        ), patch(
            "app.api.routes.symbols.TechnicalPatternService.get_candlestick_patterns",
            return_value={"ticker": "600000.SS", "status": "success", "patterns": ["看涨吞没"]},
        ):
            response = self.client.get("/symbols/600000.SS/combined-analysis")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("success", payload["status"])
        self.assertIn(payload["decision"], {"BUY", "STRONG BUY"})
        self.assertTrue(payload["reasons"])

    def test_symbol_page_renders_combined_analysis_sections(self) -> None:
        self._seed_symbol("600000.SS", "浦发银行", "CN", "SSE")
        self._write_price_history("600000.SS", self._build_squeeze_cn_history())

        response = self.client.get("/symbols/600000.SS")

        self.assertEqual(200, response.status_code)
        self.assertIn("Combined Analysis", response.text)
        self.assertIn("TradingView Multi-Timeframe", response.text)
        self.assertIn("Bollinger Band", response.text)
        self.assertIn("Candlestick Patterns", response.text)
        self.assertIn("News Sentiment", response.text)
        self.assertIn("Loading multi-timeframe analysis", response.text)
        self.assertIn("Loading sentiment brief", response.text)
        self.assertIn("Decision Brief", response.text)
        self.assertIn("/symbols/600000.SS/page-bundle", response.text)

    def test_symbol_page_bundle_endpoint_returns_combined_sections(self) -> None:
        self._seed_symbol("600000.SS", "浦发银行", "CN", "SSE")

        with patch(
            "app.api.routes.symbols.safe_symbol_analysis",
            return_value={
                "decision": "BUY",
                "confidence": 80,
                "score": 6,
                "technical_rating": {"recommendation": "BUY"},
                "multi_timeframe": {
                    "alignment": "bullish_alignment",
                    "bullish_count": 4,
                    "bearish_count": 0,
                    "neutral_count": 1,
                    "ratings": {"1d": {"recommendation": "BUY"}},
                },
                "bollinger_band": {"rating": 2, "signal": "upper_band_strength", "bandwidth_pct": 7.4, "band_position_pct": 84.0, "squeeze": True},
                "candlestick_patterns": {"patterns": ["三连阳", "看涨吞没"]},
                "reasons": ["Momentum and breadth are aligned."],
            },
        ), patch(
            "app.api.routes.symbols.build_symbol_decision_brief",
            return_value={
                "headline": "Decision brief headline",
                "summary": "Decision brief summary",
                "sentiment": "bullish",
                "urgency": "monitor",
            },
        ), patch(
            "app.api.routes.symbols.build_symbol_news_sentiment_brief",
            return_value={
                "sentiment": "bullish",
                "urgency": "monitor",
                "headlines": ["China banks track policy easing hopes"],
                "summary": "Signals remain constructive.",
            },
        ), patch(
            "app.api.routes.symbols.MarketNewsService.fetch_symbol_headlines",
            return_value=[{"source": "Reuters Markets", "title": "China banks track policy easing hopes", "link": "https://example.com/news", "published_at": "2026-04-09T09:00:00+08:00"}],
        ):
            response = self.client.get("/symbols/600000.SS/page-bundle")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("success", payload["status"])
        self.assertEqual("BUY", payload["combined"]["decision"])
        self.assertEqual("Decision brief headline", payload["decision_brief"]["headline"])
        self.assertEqual("China banks track policy easing hopes", payload["news_feed"][0]["title"])

    def test_symbol_ai_analysis_endpoint_returns_local_dashboard(self) -> None:
        self._seed_symbol("600000.SS", "浦发银行", "CN", "SSE")

        with patch(
            "app.api.routes.symbols.safe_symbol_analysis",
            return_value={
                "decision": "BUY",
                "confidence": 80,
                "score": 6,
                "reasons": ["Momentum and breadth are aligned."],
                "technical_rating": {"recommendation": "BUY"},
                "multi_timeframe": {"alignment": "bullish_alignment", "ratings": {"1d": {"recommendation": "BUY"}}},
            },
        ), patch(
            "app.services.ai_analysis.InsightEngine.get_insight",
            return_value={
                "ticker": "600000.SS",
                "trend_label": "bullish",
                "entry_zone": {"low": 10.2, "high": 10.5},
                "take_profit_zone": {"low": 11.2, "high": 11.8},
                "risk_level": 9.8,
                "distance_to_breakout_pct": 2.4,
                "volume_ratio": 1.2,
            },
        ), patch(
            "app.services.ai_analysis.MarketNewsService.fetch_symbol_headlines",
            return_value=[{"source": "Reuters Markets", "title": "Bank shares steady after policy signal", "published_at": "2026-04-09T09:00:00+08:00"}],
        ):
            response = self.client.get("/symbols/600000.SS/ai-analysis?lang=zh")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("success", payload["status"])
        self.assertEqual("local", payload["source"])
        self.assertEqual("BUY", payload["verdict"])
        self.assertIn("buy_zone", payload)
        self.assertTrue(payload["checklist"])

    def test_symbol_page_renders_ai_analysis_panel(self) -> None:
        self._seed_symbol("600000.SS", "浦发银行", "CN", "SSE")

        response = self.client.get("/symbols/600000.SS")

        self.assertEqual(200, response.status_code)
        self.assertIn("AI Analysis", response.text)
        self.assertIn("/symbols/600000.SS/ai-analysis", response.text)

    def test_symbol_page_bundle_is_cached_between_requests(self) -> None:
        self._seed_symbol("600000.SS", "浦发银行", "CN", "SSE")
        self._write_price_history("600000.SS", self._build_squeeze_cn_history())

        with patch(
            "app.api.routes.symbols.safe_symbol_analysis",
            return_value={
                "decision": "BUY",
                "confidence": 80,
                "score": 6,
                "technical_rating": {"recommendation": "BUY"},
                "multi_timeframe": {"alignment": "bullish_alignment", "ratings": {"1d": {"recommendation": "BUY"}}},
                "bollinger_band": {"signal": "upper_band_strength"},
                "candlestick_patterns": {"patterns": ["看涨吞没"]},
                "reasons": ["Momentum and breadth are aligned."],
            },
        ) as analysis_mock, patch(
            "app.api.routes.symbols.build_symbol_decision_brief",
            return_value={
                "headline": "Momentum still supports the trend",
                "summary": "daily BUY | multi-timeframe aligned",
            },
        ) as brief_mock, patch(
            "app.api.routes.symbols.build_symbol_news_sentiment_brief",
            return_value={
                "sentiment": "bullish",
                "urgency": "monitor",
                "headlines": ["Momentum still supports the trend"],
                "summary": "Signals remain constructive.",
            },
        ) as news_brief_mock, patch(
            "app.api.routes.symbols.MarketNewsService.fetch_symbol_headlines",
            return_value=[],
        ) as headlines_mock:
            first = self.client.get("/symbols/600000.SS/page-bundle")
            second = self.client.get("/symbols/600000.SS/page-bundle")

        self.assertEqual(200, first.status_code)
        self.assertEqual(200, second.status_code)
        self.assertEqual(1, analysis_mock.call_count)
        self.assertEqual(1, brief_mock.call_count)
        self.assertEqual(1, news_brief_mock.call_count)
        self.assertEqual(1, headlines_mock.call_count)

    def test_symbol_decision_brief_endpoint_returns_summary(self) -> None:
        self._seed_symbol("600000.SS", "浦发银行", "CN", "SSE")

        with patch(
            "app.api.routes.symbols.TradingViewClient.get_technical_rating",
            return_value={"ticker": "600000.SS", "status": "success", "recommendation": "BUY"},
        ), patch(
            "app.api.routes.symbols.TradingViewClient.get_multi_timeframe_analysis",
            return_value={
                "ticker": "600000.SS",
                "status": "success",
                "alignment": "bullish_alignment",
                "bullish_count": 4,
                "bearish_count": 0,
                "neutral_count": 1,
                "ratings": {"1d": {"recommendation": "BUY"}},
            },
        ), patch(
            "app.api.routes.symbols.TechnicalPatternService.get_bollinger_band_analysis",
            return_value={"ticker": "600000.SS", "status": "success", "signal": "upper_band_strength", "squeeze": True},
        ), patch(
            "app.api.routes.symbols.TechnicalPatternService.get_candlestick_patterns",
            return_value={"ticker": "600000.SS", "status": "success", "patterns": ["看涨吞没"]},
        ):
            response = self.client.get("/symbols/600000.SS/decision-brief")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("success", payload["status"])
        self.assertIn(payload["sentiment"], {"bullish", "neutral", "bearish"})
        self.assertTrue(payload["headline"])

    def test_symbol_news_sentiment_endpoint_returns_local_brief(self) -> None:
        self._seed_symbol("600000.SS", "浦发银行", "CN", "SSE")

        with patch(
            "app.api.routes.symbols.TradingViewClient.get_technical_rating",
            return_value={"ticker": "600000.SS", "status": "success", "recommendation": "BUY"},
        ), patch(
            "app.api.routes.symbols.TradingViewClient.get_multi_timeframe_analysis",
            return_value={"ticker": "600000.SS", "status": "success", "alignment": "bullish_alignment", "ratings": {"1d": {"recommendation": "BUY"}}},
        ), patch(
            "app.api.routes.symbols.TechnicalPatternService.get_bollinger_band_analysis",
            return_value={"ticker": "600000.SS", "status": "success", "signal": "upper_band_strength", "squeeze": True},
        ), patch(
            "app.api.routes.symbols.TechnicalPatternService.get_candlestick_patterns",
            return_value={"ticker": "600000.SS", "status": "success", "patterns": ["看涨吞没"]},
        ):
            response = self.client.get("/symbols/600000.SS/news-sentiment")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("success", payload["status"])
        self.assertTrue(payload["headlines"])
        self.assertIn(payload["sentiment"], {"bullish", "neutral", "bearish"})

    def test_symbol_news_feed_endpoint_returns_items(self) -> None:
        self._seed_symbol("600000.SS", "浦发银行", "CN", "SSE")

        with patch(
            "app.api.routes.symbols.MarketNewsService.fetch_symbol_headlines",
            return_value=[
                {"source": "Reuters Markets", "title": "Bank stocks steady as yields cool", "link": "https://example.com/reuters", "published_at": "2026-04-09T09:00:00+08:00"}
            ],
        ):
            response = self.client.get("/symbols/600000.SS/news-feed")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("success", payload["status"])
        self.assertEqual("Reuters Markets", payload["items"][0]["source"])

    def test_hk_history_falls_back_to_akshare_when_yfinance_is_too_short(self) -> None:
        from app.services.openbb_client import HistoricalPriceRequest, OpenBBClient

        client = OpenBBClient()

        def fake_yfinance(request):
            return [
                {
                    "date": "2026-04-02",
                    "symbol": request.ticker,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 98.0,
                    "close": 99.0,
                    "volume": 1000,
                }
            ]

        def fake_akshare(request):
            return [
                {
                    "date": "2026-04-01",
                    "symbol": request.ticker,
                    "open": 95.0,
                    "high": 99.0,
                    "low": 94.0,
                    "close": 97.0,
                    "volume": 900,
                },
                {
                    "date": "2026-04-02",
                    "symbol": request.ticker,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 98.0,
                    "close": 99.0,
                    "volume": 1000,
                },
                {
                    "date": "2026-04-03",
                    "symbol": request.ticker,
                    "open": 101.0,
                    "high": 105.0,
                    "low": 100.0,
                    "close": 104.0,
                    "volume": 1100,
                },
            ]

        with patch.object(OpenBBClient, "_fetch_with_yfinance", side_effect=fake_yfinance):
            with patch.object(OpenBBClient, "_fetch_with_akshare", side_effect=fake_akshare):
                rows = client.fetch_historical_prices(
                    HistoricalPriceRequest(
                        ticker="0100.HK",
                        start_date="2025-01-01",
                        provider="yfinance",
                    )
                )

        self.assertEqual(3, len(rows))
        self.assertEqual("akshare", client.last_source_used)

    def test_sync_market_data_merges_existing_rows_for_incremental_refresh(self) -> None:
        from app.services.market_sync import sync_market_data

        self._seed_symbol("600004.SS", "刷新缓存股", "CN", "SSE")
        self._write_price_history(
            "600004.SS",
            [
                {
                    "date": "2026-04-01",
                    "symbol": "600004.SS",
                    "open": 10.0,
                    "high": 10.3,
                    "low": 9.9,
                    "close": 10.2,
                    "volume": 1000,
                    "adj_close": 10.2,
                    "dividend": None,
                    "split_ratio": None,
                },
                {
                    "date": "2026-04-02",
                    "symbol": "600004.SS",
                    "open": 10.2,
                    "high": 10.4,
                    "low": 10.0,
                    "close": 10.3,
                    "volume": 1100,
                    "adj_close": 10.3,
                    "dividend": None,
                    "split_ratio": None,
                },
            ],
        )

        with patch(
            "app.services.openbb_client.OpenBBClient.fetch_historical_prices",
            return_value=[
                {
                    "date": "2026-04-02",
                    "symbol": "600004.SS",
                    "open": 10.25,
                    "high": 10.45,
                    "low": 10.05,
                    "close": 10.35,
                    "volume": 1200,
                    "adj_close": 10.35,
                    "dividend": None,
                    "split_ratio": None,
                },
                {
                    "date": "2026-04-03",
                    "symbol": "600004.SS",
                    "open": 10.4,
                    "high": 10.6,
                    "low": 10.2,
                    "close": 10.5,
                    "volume": 1300,
                    "adj_close": 10.5,
                    "dividend": None,
                    "split_ratio": None,
                },
            ],
        ):
            results = sync_market_data(
                tickers=["600004.SS"],
                start_date="2026-04-02",
                provider="yfinance",
                start_dates_by_ticker={"600004.SS": "2026-04-02"},
            )

        self.assertEqual("success", results[0]["status"])
        self.assertEqual(2, results[0]["rows"])
        self.assertEqual(3, results[0]["stored_rows"])
        merged = pd.read_csv(self.temp_path / "data" / "raw" / "600004.SS.csv")
        self.assertEqual(["2026-04-01", "2026-04-02", "2026-04-03"], merged["date"].tolist())
        self.assertEqual(10.35, round(float(merged.loc[merged["date"] == "2026-04-02", "close"].iloc[0]), 2))

    def test_hk_history_falls_back_to_stockanalysis_when_other_sources_are_too_short(self) -> None:
        from app.services.openbb_client import HistoricalPriceRequest, OpenBBClient

        client = OpenBBClient()

        def fake_yfinance(request):
            return [
                {
                    "date": "2026-04-02",
                    "symbol": request.ticker,
                    "open": 1021.0,
                    "high": 1030.0,
                    "low": 940.0,
                    "close": 949.5,
                    "volume": 1399397.0,
                }
            ]

        sample_df = pd.DataFrame(
            [
                {"Date": "Mar 13, 2026", "Open": 1100.0, "High": 1124.0, "Low": 992.0, "Close": 1010.0, "Volume": 1297992.0},
                {"Date": "Mar 12, 2026", "Open": 1160.0, "High": 1221.0, "Low": 1072.0, "Close": 1084.0, "Volume": 1634305.0},
                {"Date": "Mar 11, 2026", "Open": 1180.0, "High": 1320.0, "Low": 1101.0, "Close": 1141.0, "Volume": 2626687.0},
            ]
        )

        class FakeResponse:
            def read(self):
                return b"<html><body><table></table></body></html>"

        with patch.object(OpenBBClient, "_fetch_with_yfinance", side_effect=fake_yfinance):
            with patch.object(OpenBBClient, "_fetch_with_akshare", return_value=[]):
                with patch("app.services.openbb_client.urlopen", return_value=FakeResponse()):
                    with patch("pandas.read_html", return_value=[sample_df]):
                        rows = client.fetch_historical_prices(
                            HistoricalPriceRequest(
                                ticker="0100.HK",
                                start_date="2026-03-01",
                                provider="yfinance",
                            )
                        )

        self.assertEqual(3, len(rows))
        self.assertEqual("stockanalysis", client.last_source_used)
        self.assertEqual("2026-03-11", rows[0]["date"])
        self.assertEqual(1141.0, rows[0]["close"])

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

    def test_auto_analysis_settings_and_manual_run(self) -> None:
        from app.core.db import SessionLocal
        from app.services.ai_daily_report import load_ai_daily_report
        from app.services.openbb_client import OpenBBClient
        from app.services.repository import DataJobRepository

        add_response = self.client.post(
            "/watchlist/add",
            data={"ticker": "600000.SS", "name": "浦发银行", "market": "CN"},
            follow_redirects=True,
        )
        self.assertEqual(200, add_response.status_code)

        toggle_response = self.client.post(
            "/watchlist/toggle-sync",
            data={"item_id": "1", "enabled": "1"},
            follow_redirects=True,
        )
        self.assertEqual(200, toggle_response.status_code)

        config_response = self.client.post(
            "/jobs/auto-analysis/config",
            data={
                "enabled": "true",
                "interval_hours": "6",
                "provider": "yfinance",
                "start_date": "2026-04-01",
                "signal_type": "momentum",
                "lookback_days": "2",
                "top_n": "1",
                "sync_cn_concepts": "true",
            },
        )
        self.assertEqual(200, config_response.status_code)
        self.assertTrue(config_response.json()["config"]["enabled"])
        self.assertTrue(config_response.json()["config"]["sync_cn_concepts"])

        def fake_fetch(self, request) -> list[dict]:
            symbol = request.ticker
            return [
                {
                    "date": "2026-04-01",
                    "symbol": symbol,
                    "open": 80.0,
                    "high": 81.0,
                    "low": 79.5,
                    "close": 80.5,
                    "volume": 300000,
                },
                {
                    "date": "2026-04-02",
                    "symbol": symbol,
                    "open": 80.5,
                    "high": 82.0,
                    "low": 80.0,
                    "close": 81.6,
                    "volume": 320000,
                },
                {
                    "date": "2026-04-03",
                    "symbol": symbol,
                    "open": 81.6,
                    "high": 83.0,
                    "low": 81.0,
                    "close": 82.4,
                    "volume": 340000,
                },
            ]

        with patch.object(OpenBBClient, "fetch_historical_prices", new=fake_fetch), patch(
            "app.services.auto_analysis.PushNotificationService.available_channels",
            return_value=["wechat"],
        ), patch(
            "app.services.auto_analysis.PushNotificationService.send_text",
            return_value={"status": "success", "sent": ["wechat"], "failed": []},
        ):
            run_response = self.client.post("/jobs/run-watchlist-analysis")

        self.assertEqual(200, run_response.status_code)
        payload = run_response.json()
        self.assertEqual("success", payload["status"])
        self.assertEqual(["600000.SS"], payload["tickers"])
        self.assertGreater(payload["predictions_written"], 0)

        summary_response = self.client.get("/dashboard/summary")
        self.assertEqual(200, summary_response.status_code)
        summary = summary_response.json()
        self.assertTrue(summary["auto_analysis"]["enabled"])
        self.assertEqual(6, summary["auto_analysis"]["interval_hours"])
        self.assertTrue(summary["auto_analysis"]["sync_cn_concepts"])
        ai_report = load_ai_daily_report()
        self.assertIsNotNone(ai_report)
        self.assertEqual("success", ai_report["status"])

        with SessionLocal() as db:
            latest_job = DataJobRepository(db).list_recent_jobs(limit=1)[0]

        self.assertEqual("watchlist_auto_analysis", latest_job["job_type"])
        self.assertEqual("success", latest_job["status"])

    def test_close_review_settings_and_manual_run(self) -> None:
        from app.core.db import SessionLocal
        from app.services.repository import DataJobRepository

        config_response = self.client.post(
            "/jobs/close-review/config",
            data={
                "enabled": "true",
                "run_hour": "16",
                "run_minute": "0",
                "provider": "yfinance",
                "days_back": "7",
                "overlap_days": "3",
                "refresh_limit": "0",
                "stale_job_hours": "12",
            },
        )
        self.assertEqual(200, config_response.status_code)
        self.assertTrue(config_response.json()["config"]["enabled"])
        self.assertEqual(16, config_response.json()["config"]["run_hour"])

        with patch(
            "app.services.close_review_scheduler.refresh_cn_market_data_daily",
            return_value={"status": "success", "success_count": 12, "message": "ok"},
        ), patch(
            "app.services.close_review_scheduler.rebuild_technical_snapshots",
            return_value={"status": "success", "snapshots_rebuilt": 34, "message": "ok"},
        ), patch(
            "app.services.close_review_scheduler.auto_analysis_service.run_watchlist_analysis",
            return_value={"status": "success", "tickers": ["ASTS", "RKLB"]},
        ):
            run_response = self.client.post("/jobs/run-close-review")

        self.assertEqual(200, run_response.status_code)
        payload = run_response.json()
        self.assertEqual("success", payload["status"])
        self.assertEqual(12, payload["refresh_result"]["success_count"])

        status_response = self.client.get("/jobs/close-review")
        self.assertEqual(200, status_response.status_code)
        self.assertTrue(status_response.json()["enabled"])

        with SessionLocal() as db:
            latest_job = DataJobRepository(db).list_recent_jobs(limit=1)[0]
        self.assertEqual("cn_close_review", latest_job["job_type"])
        self.assertEqual("success", latest_job["status"])

    def test_close_review_refresh_falls_back_when_primary_result_fails(self) -> None:
        responses = [
            {"status": "failed", "success_count": 0, "failure_count": 20, "message": "primary failed"},
            {"status": "success", "success_count": 12, "failure_count": 0, "message": "fallback ok"},
        ]

        with patch(
            "app.services.close_review_scheduler.refresh_cn_market_data_daily",
            side_effect=responses,
        ) as mocked_refresh, patch(
            "app.services.close_review_scheduler.rebuild_technical_snapshots",
            return_value={"status": "success", "snapshots_rebuilt": 34, "message": "ok"},
        ), patch(
            "app.services.close_review_scheduler.auto_analysis_service.run_watchlist_analysis",
            return_value={"status": "success", "tickers": ["ASTS"]},
        ):
            response = self.client.post("/jobs/run-close-review")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertEqual("success", payload["status"])
        self.assertEqual("yfinance", payload["refresh_result"]["provider_used"])
        self.assertEqual(["tushare", "yfinance"], payload["refresh_result"]["providers_attempted"])
        self.assertEqual(2, mocked_refresh.call_count)

    def test_close_review_runs_auto_analysis_for_cn_only(self) -> None:
        with patch(
            "app.services.close_review_scheduler.refresh_cn_market_data_daily",
            return_value={"status": "success", "success_count": 12, "message": "ok"},
        ), patch(
            "app.services.close_review_scheduler.rebuild_technical_snapshots",
            return_value={"status": "success", "rows_written": 34, "message": "ok"},
        ), patch(
            "app.services.close_review_scheduler.auto_analysis_service.run_watchlist_analysis",
            return_value={"status": "success", "tickers": ["600330.SS", "300385.SZ"]},
        ) as mocked_run:
            response = self.client.post("/jobs/run-close-review")

        self.assertEqual(200, response.status_code)
        self.assertEqual("success", response.json()["status"])
        self.assertEqual(["CN"], mocked_run.call_args.kwargs["allowed_markets"])

    def test_auto_analysis_defaults_to_cn_market_scope(self) -> None:
        from app.services.auto_analysis import auto_analysis_service

        status = auto_analysis_service.get_status()

        self.assertEqual(["CN"], status["default_allowed_markets"])
        self.assertEqual("tushare", status["provider"])

    def test_auto_analysis_status_can_reuse_existing_db_session(self) -> None:
        from app.core.db import SessionLocal
        from app.services.auto_analysis import auto_analysis_service

        with SessionLocal() as db:
            status = auto_analysis_service.get_status(db=db)

        self.assertEqual(["CN"], status["default_allowed_markets"])

    def test_close_review_status_can_reuse_existing_db_session(self) -> None:
        from app.core.db import SessionLocal
        from app.services.close_review_scheduler import close_review_scheduler_service

        with SessionLocal() as db:
            status = close_review_scheduler_service.get_status(db=db)

        self.assertIn("enabled", status)
        self.assertIn("run_hour", status)
        self.assertEqual("tushare", status["provider"])

    def test_close_review_scheduler_can_retry_failed_runs_after_cooldown(self) -> None:
        from unittest.mock import patch

        from app.core.db import SessionLocal
        from app.services.close_review_scheduler import close_review_scheduler_service
        from app.services.repository import AppSettingRepository

        with SessionLocal() as db:
            AppSettingRepository(db).set(
                "close_review_scheduler_config",
                json.dumps(
                    {
                        "enabled": True,
                        "run_hour": 16,
                        "run_minute": 0,
                        "provider": "tushare",
                        "days_back": 7,
                        "overlap_days": 3,
                        "refresh_limit": 500,
                        "stale_job_hours": 12,
                        "retry_cooldown_minutes": 30,
                        "max_attempts_per_day": 3,
                        "last_run_date": None,
                        "last_attempt_date": "2026-04-10",
                        "last_attempt_at": "2026-04-10T16:00:00+08:00",
                        "last_attempt_count": 1,
                    }
                ),
            )

        fake_now = datetime.fromisoformat("2026-04-10T16:45:00+08:00")
        with patch("app.services.close_review_scheduler.sh_now", return_value=fake_now), patch(
            "app.services.close_review_scheduler.close_review_scheduler_service.run_close_review",
            return_value={"status": "success"},
        ) as mocked_run:
            result = close_review_scheduler_service.run_due_job()

        self.assertEqual({"status": "success"}, result)
        mocked_run.assert_called_once_with(trigger="scheduler")

    def test_close_review_scheduler_respects_retry_cooldown_and_attempt_cap(self) -> None:
        from unittest.mock import patch

        from app.core.db import SessionLocal
        from app.services.close_review_scheduler import close_review_scheduler_service
        from app.services.repository import AppSettingRepository

        with SessionLocal() as db:
            AppSettingRepository(db).set(
                "close_review_scheduler_config",
                json.dumps(
                    {
                        "enabled": True,
                        "run_hour": 16,
                        "run_minute": 0,
                        "provider": "tushare",
                        "days_back": 7,
                        "overlap_days": 3,
                        "refresh_limit": 500,
                        "stale_job_hours": 12,
                        "retry_cooldown_minutes": 60,
                        "max_attempts_per_day": 2,
                        "last_run_date": None,
                        "last_attempt_date": "2026-04-10",
                        "last_attempt_at": "2026-04-10T16:20:00+08:00",
                        "last_attempt_count": 1,
                    }
                ),
            )

        with patch(
            "app.services.close_review_scheduler.sh_now",
            return_value=datetime.fromisoformat("2026-04-10T16:45:00+08:00"),
        ), patch(
            "app.services.close_review_scheduler.close_review_scheduler_service.run_close_review"
        ) as mocked_run:
            result = close_review_scheduler_service.run_due_job()

        self.assertIsNone(result)
        mocked_run.assert_not_called()

        with SessionLocal() as db:
            AppSettingRepository(db).set(
                "close_review_scheduler_config",
                json.dumps(
                    {
                        "enabled": True,
                        "run_hour": 16,
                        "run_minute": 0,
                        "provider": "tushare",
                        "days_back": 7,
                        "overlap_days": 3,
                        "refresh_limit": 500,
                        "stale_job_hours": 12,
                        "retry_cooldown_minutes": 30,
                        "max_attempts_per_day": 2,
                        "last_run_date": None,
                        "last_attempt_date": "2026-04-10",
                        "last_attempt_at": "2026-04-10T15:00:00+08:00",
                        "last_attempt_count": 2,
                    }
                ),
            )

        with patch(
            "app.services.close_review_scheduler.sh_now",
            return_value=datetime.fromisoformat("2026-04-10T17:00:00+08:00"),
        ), patch(
            "app.services.close_review_scheduler.close_review_scheduler_service.run_close_review"
        ) as mocked_run:
            result = close_review_scheduler_service.run_due_job()

        self.assertIsNone(result)
        mocked_run.assert_not_called()

    def test_ai_daily_report_defaults_to_cn_market_scope(self) -> None:
        from app.services.ai_daily_report import build_ai_daily_report
        from types import SimpleNamespace

        fake_symbol_repo = SimpleNamespace(
            get_overview=lambda ticker: {"ticker": ticker, "name": ticker, "market": "CN" if ticker.endswith((".SS", ".SZ")) else "US"}
        )
        fake_prediction_repo = SimpleNamespace(
            list_latest_predictions_for_market=lambda market, limit=40: [
                {"ticker": "600330.SS", "name": "天通股份", "market": "CN", "score": 0.12, "signal_strength": 82, "confidence": 0.76},
                {"ticker": "AAPL", "name": "Apple", "market": "US", "score": 0.2, "signal_strength": 90, "confidence": 0.8},
            ],
            list_symbol_predictions=lambda *args, **kwargs: [],
        )
        fake_ai_service = SimpleNamespace(
            insight_engine=SimpleNamespace(
                get_insight=lambda ticker, **kwargs: {"trend_score": 72, "trend_label": "bullish", "setup_label": "pullback_buy", "confidence": 0.72, "explanation": ["ok"]}
                if ticker.endswith((".SS", ".SZ"))
                else None
            ),
            analyze_symbol=lambda **kwargs: {
                "headline": "ok",
                "verdict": "BUY",
                "confidence": 70,
                "strategy": "进攻/顺势跟踪",
                "buy_zone": {"low": 1, "high": 2},
                "stop_loss": 0.9,
                "take_profit": {"low": 3, "high": 4},
                "summary": "ok",
            },
        )

        with patch("app.services.ai_daily_report.SymbolRepository",
            return_value=fake_symbol_repo,
        ), patch(
            "app.services.ai_daily_report.PredictionRepository",
            return_value=fake_prediction_repo,
        ), patch(
            "app.services.ai_daily_report.AIAnalysisService",
            return_value=fake_ai_service,
        ):
            report = build_ai_daily_report(limit=8)

        self.assertTrue(report["rows"])
        self.assertEqual({"CN"}, {row.get("market") for row in report["rows"]})
        self.assertEqual("cn_full_market_top_picks", report["scope"])
        self.assertTrue(report["buy_the_dip_rows"])
        self.assertEqual("pullback_buy", report["buy_the_dip_rows"][0]["setup_label"])

    def test_ai_daily_report_can_reuse_existing_db_session(self) -> None:
        from app.core.db import SessionLocal
        from app.services.ai_daily_report import load_ai_daily_report, save_ai_daily_report

        payload = {
            "status": "success",
            "mood": "偏进攻",
            "headline": "session reuse",
            "strategy": {"headline": "-", "playbook": "-", "bullets": []},
            "rows": [],
            "buy_the_dip_rows": [],
        }
        with SessionLocal() as db:
            save_ai_daily_report(payload, db=db)
            loaded = load_ai_daily_report(db=db)

        self.assertEqual("session reuse", loaded["headline"])

    def test_ai_daily_report_custom_tickers_still_use_explicit_scope(self) -> None:
        from app.services.ai_daily_report import build_ai_daily_report
        from types import SimpleNamespace

        fake_watchlist_repo = SimpleNamespace(
            get_or_create_default=lambda: SimpleNamespace(id=1),
            list_items=lambda _watchlist_id: [
                {"ticker": "AAPL", "name": "Apple", "market": "US"},
                {"ticker": "600330.SS", "name": "天通股份", "market": "CN"},
            ],
        )
        fake_symbol_repo = SimpleNamespace(
            get_overview=lambda ticker: {"ticker": ticker, "name": ticker, "market": "CN" if ticker.endswith((".SS", ".SZ")) else "US"}
        )
        fake_prediction_repo = SimpleNamespace(list_symbol_predictions=lambda *args, **kwargs: [])
        fake_ai_service = SimpleNamespace(
            insight_engine=SimpleNamespace(get_insight=lambda *args, **kwargs: {"trend_score": 72, "trend_label": "bullish", "setup_label": "pullback_buy", "confidence": 0.72, "explanation": ["ok"]}),
            analyze_symbol=lambda **kwargs: {
                "headline": "ok",
                "verdict": "BUY",
                "confidence": 70,
                "strategy": "进攻/顺势跟踪",
                "buy_zone": {"low": 1, "high": 2},
                "stop_loss": 0.9,
                "take_profit": {"low": 3, "high": 4},
                "summary": "ok",
            },
        )

        with patch("app.services.ai_daily_report.WatchlistRepository", return_value=fake_watchlist_repo), patch(
            "app.services.ai_daily_report.SymbolRepository",
            return_value=fake_symbol_repo,
        ), patch(
            "app.services.ai_daily_report.PredictionRepository",
            return_value=fake_prediction_repo,
        ), patch(
            "app.services.ai_daily_report.AIAnalysisService",
            return_value=fake_ai_service,
        ):
            report = build_ai_daily_report(limit=8, tickers=["600330.SS"], markets=["CN"])

        self.assertEqual("custom_tickers", report["scope"])
        self.assertEqual(["600330.SS"], [row.get("ticker") for row in report["rows"]])

    def test_cleanup_stale_jobs_marks_old_running_jobs_failed(self) -> None:
        from datetime import datetime, timedelta, timezone

        from app.core.db import SessionLocal
        from app.models.tables import DataJob

        with SessionLocal() as db:
            db.add(
                DataJob(
                    job_type="watchlist_auto_analysis",
                    status="running",
                    started_at=(datetime.now(timezone.utc) - timedelta(hours=30)).replace(microsecond=0).isoformat(),
                    finished_at=None,
                    message=None,
                    params_json=None,
                )
            )
            db.commit()

        response = self.client.post("/jobs/cleanup-stale-jobs", data={"stale_job_hours": "12"})
        self.assertEqual(200, response.status_code)
        self.assertEqual("success", response.json()["status"])
        self.assertEqual(1, response.json()["cleaned_jobs"])

        with SessionLocal() as db:
            row = db.query(DataJob).order_by(DataJob.id.desc()).first()
            assert row is not None
            self.assertEqual("failed", row.status)
            self.assertIsNotNone(row.finished_at)
            self.assertIn("Manual cleanup closed a stale running job.", row.message or "")

    def test_prediction_replace_for_model_run_bulk_deletes_without_sqlite_variable_blowup(self) -> None:
        from app.core.db import SessionLocal
        from app.models.tables import Prediction
        from app.services.repository import ModelRunRepository, PredictionDetailRepository, PredictionWriteRepository
        from app.services.repository import SymbolRepository

        self._seed_symbol("AAPL", "Apple Inc.", "US", "NASDAQ")

        with SessionLocal() as db:
            model_run = ModelRunRepository(db).create_run(
                name="bulk_delete_test",
                model_type="local_baseline",
                market="US",
                universe="test",
                train_start="2026-01-01",
                train_end="2026-01-02",
                test_start="2026-01-01",
                test_end="2026-01-02",
                config={},
                artifact_path=None,
                status="running",
            )
            symbol = SymbolRepository(db).get_by_ticker("AAPL")
            assert symbol is not None
            prediction_rows = [
                {"symbol_id": symbol.id, "trade_date": f"2026-01-{day:02d}", "score": float(day), "rank_value": float(day)}
                for day in range(1, 29)
            ]
            PredictionWriteRepository(db).replace_for_model_run(model_run.id, prediction_rows)

            predictions = db.query(Prediction).filter(Prediction.model_run_id == model_run.id).all()
            detail_rows = [
                {"symbol_id": prediction.symbol_id, "trade_date": prediction.trade_date, "confidence": 0.7}
                for prediction in predictions
            ]
            PredictionDetailRepository(db).replace_for_model_run(model_run.id, detail_rows)

            rewritten_rows = [
                {"symbol_id": symbol.id, "trade_date": f"2026-02-{day:02d}", "score": float(day), "rank_value": float(day)}
                for day in range(1, 29)
            ]
            PredictionWriteRepository(db).replace_for_model_run(model_run.id, rewritten_rows)

            remaining = db.query(Prediction).filter(Prediction.model_run_id == model_run.id).count()
            self.assertEqual(len(rewritten_rows), remaining)

    def test_sqlite_engine_uses_wal_mode(self) -> None:
        from app.core.db import SessionLocal, engine
        from sqlalchemy import text

        with SessionLocal() as db:
            journal_mode = db.execute(text("PRAGMA journal_mode")).scalar()
        self.assertIn(str(journal_mode).lower(), {"wal"})
        self.assertEqual(20, engine.pool.size())

    def test_settings_resolve_database_url(self) -> None:
        from app.core.config import Settings

        sqlite_settings = Settings()
        self.assertTrue(sqlite_settings.resolved_database_url.startswith("sqlite:///"))

        pg_settings = Settings(database_url="postgresql+psycopg://user:pass@localhost:5432/ana")
        self.assertEqual("postgresql+psycopg://user:pass@localhost:5432/ana", pg_settings.resolved_database_url)

    def test_db_backend_detection_supports_sqlite_and_postgres(self) -> None:
        from app.core.db import _is_sqlite_url

        self.assertTrue(_is_sqlite_url("sqlite:////tmp/test.db"))
        self.assertFalse(_is_sqlite_url("postgresql+psycopg://user:pass@localhost:5432/ana"))

    def test_postgres_connect_args_use_tuned_defaults(self) -> None:
        from app.core.db import _build_postgresql_connect_args

        connect_args = _build_postgresql_connect_args()

        self.assertEqual(10, connect_args["connect_timeout"])
        self.assertEqual("pqw-app", connect_args["application_name"])
        self.assertIn("statement_timeout=60000", connect_args["options"])
        self.assertIn("idle_in_transaction_session_timeout=60000", connect_args["options"])

    def test_price_sync_state_upsert_retries_when_sqlite_is_locked(self) -> None:
        from app.core.db import SessionLocal
        from sqlalchemy.exc import OperationalError

        from app.services.repository import PriceSyncStateRepository, SymbolRepository

        self._seed_symbol("600000.SS", "浦发银行", "CN", "SSE")

        with SessionLocal() as db:
            symbol = SymbolRepository(db).get_by_ticker("600000.SS")
            assert symbol is not None
            original_commit = db.commit
            attempts = {"count": 0}

            def flaky_commit():
                attempts["count"] += 1
                if attempts["count"] == 1:
                    raise OperationalError("UPDATE price_sync_state", {}, Exception("database is locked"))
                return original_commit()

            with patch.object(db, "commit", side_effect=flaky_commit):
                row = PriceSyncStateRepository(db).upsert_state(
                    symbol_id=symbol.id,
                    provider="tushare",
                    last_synced_date="2026-04-10",
                    status="success",
                    message="ok",
                )

            self.assertEqual(symbol.id, row.symbol_id)
            self.assertEqual(2, attempts["count"])

    def test_prediction_repository_uses_latest_trade_date_per_market(self) -> None:
        from app.core.db import SessionLocal
        from app.models.tables import Prediction
        from app.services.repository import ModelRunRepository, PredictionRepository, SymbolRepository, utc_now_iso

        self._seed_symbol("600330.SS", "天通股份", "CN", "SSE")
        self._seed_symbol("AAPL", "Apple Inc.", "US", "NASDAQ")

        with SessionLocal() as db:
            run = ModelRunRepository(db).create_run(
                name="market_date_scope_test",
                model_type="local_baseline",
                market="ALL",
                universe="full_market",
                train_start="2026-04-01",
                train_end="2026-04-02",
                test_start="2026-04-01",
                test_end="2026-04-10",
                config={},
                artifact_path=None,
                status="success",
            )
            symbol_repo = SymbolRepository(db)
            cn_symbol = symbol_repo.get_by_ticker("600330.SS")
            us_symbol = symbol_repo.get_by_ticker("AAPL")
            assert cn_symbol is not None
            assert us_symbol is not None
            db.add(
                Prediction(
                    model_run_id=run.id,
                    symbol_id=cn_symbol.id,
                    trade_date="2026-04-09",
                    score=0.91,
                    rank_value=1,
                    created_at=utc_now_iso(),
                )
            )
            db.add(
                Prediction(
                    model_run_id=run.id,
                    symbol_id=us_symbol.id,
                    trade_date="2026-04-10",
                    score=0.88,
                    rank_value=2,
                    created_at=utc_now_iso(),
                )
            )
            db.commit()

            rows = PredictionRepository(db).list_latest_predictions_for_market("CN", limit=10)

        self.assertEqual(["600330.SS"], [row["ticker"] for row in rows])

    def test_openbb_client_yfinance_download_uses_timeout(self) -> None:
        from app.services.openbb_client import HistoricalPriceRequest, OpenBBClient

        class _Frame:
            empty = True

        with patch("yfinance.download", return_value=_Frame()) as mocked_download:
            rows = OpenBBClient()._fetch_with_yfinance(
                HistoricalPriceRequest(ticker="AAPL", start_date="2026-04-01", end_date="2026-04-10")
            )

        self.assertEqual([], rows)
        self.assertEqual(15, mocked_download.call_args.kwargs["timeout"])
        self.assertEqual(False, mocked_download.call_args.kwargs["threads"])

    def test_push_notification_service_sends_plain_text_to_telegram(self) -> None:
        from app.services.push_notifications import PushNotificationService

        service = PushNotificationService()
        service.settings.telegram_bot_token = "test-token"
        service.settings.telegram_chat_id = "12345"
        long_body = "x" * 5000

        with patch("httpx.post") as mocked_post:
            mocked_post.return_value = MagicMock(raise_for_status=lambda: None)
            service._send_telegram(title="Title", body=long_body)

        payload = mocked_post.call_args.kwargs["json"]
        self.assertEqual("12345", str(payload["chat_id"]))
        self.assertNotIn("parse_mode", payload)
        self.assertLessEqual(len(payload["text"]), service.TELEGRAM_MAX_TEXT)

    def test_database_migration_service_copies_sqlite_rows(self) -> None:
        source_path = self.temp_path / "storage" / "source.db"
        target_path = self.temp_path / "storage" / "target.db"

        from sqlalchemy import create_engine, text
        from app.models.base import Base
        from app.models import tables  # noqa: F401

        source_engine = create_engine(f"sqlite:///{source_path}", future=True)
        target_engine = create_engine(f"sqlite:///{target_path}", future=True)
        Base.metadata.create_all(bind=source_engine)
        Base.metadata.create_all(bind=target_engine)

        with source_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO symbols (id, ticker, name, market, exchange, sector, industry, is_active, created_at, updated_at)
                    VALUES (1, '600000.SS', 'PF Bank', 'CN', 'SSE', NULL, NULL, 1, '2026-04-10T09:00:00', '2026-04-10T09:00:00')
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES ('close_review_config', '{"enabled": true}', '2026-04-10T09:00:00')
                    """
                )
            )

        summary = DatabaseMigrationService().migrate(
            source_url=f"sqlite:///{source_path}",
            target_url=f"sqlite:///{target_path}",
            truncate_target=True,
        )

        self.assertEqual(1, summary.copied_rows.get("symbols"))
        self.assertEqual(1, summary.copied_rows.get("app_settings"))

        with target_engine.connect() as connection:
            symbol_count = connection.execute(text("SELECT COUNT(*) FROM symbols")).scalar_one()
            setting_value = connection.execute(
                text("SELECT value FROM app_settings WHERE key = 'close_review_config'")
            ).scalar_one()

        self.assertEqual(1, symbol_count)
        self.assertIn('"enabled": true', setting_value)

    def test_database_migration_validation_compares_table_counts(self) -> None:
        source_path = self.temp_path / "storage" / "validate_source.db"
        target_path = self.temp_path / "storage" / "validate_target.db"

        from sqlalchemy import create_engine, text
        from app.models.base import Base
        from app.models import tables  # noqa: F401

        source_engine = create_engine(f"sqlite:///{source_path}", future=True)
        target_engine = create_engine(f"sqlite:///{target_path}", future=True)
        Base.metadata.create_all(bind=source_engine)
        Base.metadata.create_all(bind=target_engine)

        with source_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO symbols (id, ticker, name, market, exchange, sector, industry, is_active, created_at, updated_at)
                    VALUES (1, '600000.SS', 'PF Bank', 'CN', 'SSE', NULL, NULL, 1, '2026-04-10T09:00:00', '2026-04-10T09:00:00')
                    """
                )
            )
        with target_engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO symbols (id, ticker, name, market, exchange, sector, industry, is_active, created_at, updated_at)
                    VALUES (1, '600000.SS', 'PF Bank', 'CN', 'SSE', NULL, NULL, 1, '2026-04-10T09:00:00', '2026-04-10T09:00:00')
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES ('close_review_config', '{"enabled": true}', '2026-04-10T09:00:00')
                    """
                )
            )

        summary = DatabaseMigrationService().validate(
            source_url=f"sqlite:///{source_path}",
            target_url=f"sqlite:///{target_path}",
        )

        self.assertFalse(summary.matches)
        symbol_comparison = next(item for item in summary.comparisons if item.table_name == "symbols")
        settings_comparison = next(item for item in summary.comparisons if item.table_name == "app_settings")
        self.assertTrue(symbol_comparison.matches)
        self.assertEqual(0, settings_comparison.source_count)
        self.assertEqual(1, settings_comparison.target_count)


if __name__ == "__main__":
    unittest.main()
