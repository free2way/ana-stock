import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from fastapi.testclient import TestClient
from app.services.tushare_client import CNFundamentalRow


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
        self._login()

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
            "PQW_TUSHARE_TOKEN",
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

    def _login(self) -> None:
        response = self.client.post(
            "/login",
            data={"username": "admin", "password": "admin1234", "next": "/watchlist"},
            follow_redirects=False,
        )
        self.assertEqual(303, response.status_code)

    def test_login_page_and_protected_redirect(self) -> None:
        fresh_client = TestClient(self.client.app)
        try:
            login_page = fresh_client.get("/login")
            self.assertEqual(200, login_page.status_code)
            self.assertIn("admin1234", login_page.text)

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

        symbol_response = self.client.get("/symbols/AAPL")
        self.assertEqual(200, symbol_response.status_code)
        self.assertIn("AAPL", symbol_response.text)
        self.assertIn("Back to dashboard", symbol_response.text)

        data_sources_response = self.client.get("/dashboard/data-sources")
        self.assertEqual(200, data_sources_response.status_code)
        self.assertIn("text/html", data_sources_response.headers.get("content-type", ""))
        self.assertIn("Where This App Gets Data", data_sources_response.text)
        self.assertIn("Per Symbol Sync Source", data_sources_response.text)

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
        self.assertIn("bullish_prob", payload)
        self.assertIn("bearish_prob", payload)
        self.assertIn("expected_return_5d", payload)
        self.assertIn("expected_return_20d", payload)
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

        page_response = self.client.get("/insights/AAPL?lang=en")
        self.assertEqual(200, page_response.status_code)
        self.assertIn("Model Output", page_response.text)
        self.assertIn("Model Summary", page_response.text)
        self.assertIn("insight_model_demo", page_response.text)
        self.assertIn("Bullish Probability", page_response.text)
        self.assertIn("Expected 5D Return", page_response.text)
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

        self.assertEqual(200, add_response.status_code)
        self.assertIn("Added 0700.HK and synced 2 rows.", add_response.text)
        self.assertIn("腾讯控股", add_response.text)

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
        from app.services.openbb_client import OpenBBClient
        from app.services.repository import DataJobRepository

        add_response = self.client.post(
            "/watchlist/add",
            data={"ticker": "ASTS", "name": "AST SpaceMobile", "market": "US"},
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
            },
        )
        self.assertEqual(200, config_response.status_code)
        self.assertTrue(config_response.json()["config"]["enabled"])

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

        with patch.object(OpenBBClient, "fetch_historical_prices", new=fake_fetch):
            run_response = self.client.post("/jobs/run-watchlist-analysis")

        self.assertEqual(200, run_response.status_code)
        payload = run_response.json()
        self.assertEqual("success", payload["status"])
        self.assertEqual(["ASTS"], payload["tickers"])
        self.assertGreater(payload["predictions_written"], 0)

        summary_response = self.client.get("/dashboard/summary")
        self.assertEqual(200, summary_response.status_code)
        summary = summary_response.json()
        self.assertTrue(summary["auto_analysis"]["enabled"])
        self.assertEqual(6, summary["auto_analysis"]["interval_hours"])

        with SessionLocal() as db:
            latest_job = DataJobRepository(db).list_recent_jobs(limit=1)[0]

        self.assertEqual("watchlist_auto_analysis", latest_job["job_type"])
        self.assertEqual("success", latest_job["status"])


if __name__ == "__main__":
    unittest.main()
