"""Exercise the HTTP route with real aggregation, presenters and templates."""

from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.dashboard import performance
from app.core.db import get_db_session


class DashboardPerformanceRouteTests(unittest.TestCase):
    def _render(self, *, market: str, lang: str, populated: bool):
        app = FastAPI()
        app.include_router(performance.router)
        db = MagicMock()
        app.dependency_overrides[get_db_session] = lambda: db
        run = {"id": 701, "name": "Fixture <model>", "market": market,
               "status": "success", "universe": "fixture"}
        summary = {
            "run": run, "pick_count": 2, "trade_dates": 1,
            "latest_trade_date": "2026-09-30",
            "windows": {5: {"count": 2, "avg_return": 1.0, "hit_rate": 50.0}},
            "rows": [
                {"ticker": "FIXTURE-A", "name": "Name <A>", "sector": "Sector A",
                 "regime_label": "risk_on", "return_5d": 4.0},
                {"ticker": "FIXTURE-B", "name": "Name B", "sector": "Sector A",
                 "regime_label": "risk_on", "return_5d": -2.0},
            ],
        }
        evaluation = {
            "snapshot_total": 3, "clean_snapshot_total": 2,
            "labeled_snapshot_total": 2,
            "windows": {action: {5: {"count": 2, "avg_return": 1.0, "hit_rate": 50.0}}
                        for action in ("buy_the_dip", "wait_for_breakout", "buy", "watch", "pullback", "breakout")},
        } if populated else {}
        original = deepcopy((summary, evaluation))
        with ExitStack() as stack:
            stack.enter_context(patch.object(performance, "is_authenticated", return_value=True))
            repo = stack.enter_context(patch.object(performance, "ModelRunRepository")).return_value
            repo.list_recent_runs.return_value = [run] if populated else []
            repo.get_run_by_id.return_value = SimpleNamespace(config_json="{}", artifact_path="")
            load_summary = stack.enter_context(patch.object(
                performance, "_build_model_run_performance_summary", return_value=summary
            ))
            stack.enter_context(patch.object(performance, "_build_watchlist_post_add_summary", return_value={}))
            stack.enter_context(patch.object(performance, "_build_recommendation_validation_summary", return_value={}))
            sources = [stack.enter_context(patch.object(performance, name, return_value=evaluation))
                       for name in ("build_next_tesla_evaluation", "build_technical_momentum_evaluation",
                                    "build_lightgbm_evaluation", "build_lightgbm_prediction_evaluation")]
            stack.enter_context(patch.object(performance, "list_latest_model_evaluations", return_value=[]))
            guidance = stack.enter_context(patch.object(
                performance, "load_model_selection_guidance_snapshot", return_value={}
            ))
            with TestClient(app) as client:
                response = client.get(f"/dashboard/model-performance?market={market}&lang={lang}")
            for source in sources:
                self.assertEqual(market, source.call_args.kwargs["market"])
                self.assertFalse(source.call_args.kwargs["allow_compute"])
            self.assertFalse(guidance.call_args.kwargs["allow_fallback"])
            for call in load_summary.call_args_list:
                self.assertEqual(market, call.kwargs["market"])
                self.assertFalse(call.kwargs["allow_compute"])
            self.assertEqual(1 if populated else 0, load_summary.call_count)
            self.assertEqual(1, repo.list_recent_runs.call_count)
        self.assertEqual(original, (summary, evaluation))
        return response

    def test_market_and_language_matrix_with_empty_and_populated_snapshots(self):
        for market in ("CN", "US", "ALL"):
            for lang in ("zh", "en"):
                for populated in (False, True):
                    with self.subTest(market=market, lang=lang, populated=populated):
                        response = self._render(market=market, lang=lang, populated=populated)
                        self.assertEqual(200, response.status_code)
                        self.assertIn("text/html", response.headers["content-type"])
                        self.assertIn("模型评测总览" if lang == "zh" else "Model Evaluation Overview", response.text)
                        self.assertIn("LightGBM", response.text)
                        self.assertNotIn("{{", response.text)
                        if populated:
                            self.assertIn("FIXTURE-A", response.text)
                            self.assertIn("Name &lt;A&gt;", response.text)
                            self.assertIn("1.00%", response.text)
                            self.assertIn("50.0%", response.text)
                        else:
                            self.assertIn("暂无可计算样本" if lang == "zh" else "No measurable samples yet", response.text)

    def test_unauthenticated_request_does_not_load_model_data(self):
        app = FastAPI()
        app.include_router(performance.router)
        app.dependency_overrides[get_db_session] = lambda: MagicMock()
        with patch.object(performance, "is_authenticated", return_value=False), patch.object(
            performance, "ModelRunRepository"
        ) as repo, TestClient(app) as client:
            response = client.get("/dashboard/model-performance", follow_redirects=False)
        self.assertIn(response.status_code, (302, 303, 307))
        self.assertIn("/login", response.headers["location"])
        repo.assert_not_called()
