from __future__ import annotations

import unittest
from unittest.mock import patch

from app.api.routes.dashboard import _render_dashboard_workspace
from app.api.routes.screener import _model_cell


class DashboardWorkflowHomeTests(unittest.TestCase):
    def test_model_cell_renders_observation_status_without_server_error(self) -> None:
        rendered = _model_cell(
            {
                "model_summary": "Technical Momentum",
                "model_activation_status": "observation_only",
            },
            "zh",
        )

        self.assertIn("模型仅观察", rendered)

    def test_home_prioritizes_decision_and_workflow_over_system_details(self) -> None:
        summary = {
            "generated_at": "2026-07-23 18:00",
            "auto_analysis": {"status": "success", "enabled": True},
            "market_context": {"risk_overview": {"top_tags": []}},
            "latest_model": {"name": "LightGBM", "status": "success", "finished_at": "2026-07-23 17:30"},
            "latest_signals": [],
        }
        pipeline_payload = {
            "trust_score": 72,
            "close_review_action_feed": {"summary": "复盘已生成", "actionable": [], "blocked": [], "risk_reduction": []},
        }
        with patch(
            "app.api.routes.dashboard.build_lightgbm_prediction_evaluation",
            return_value={"sample_count": 0, "windows": {}},
        ):
            rendered = _render_dashboard_workspace(
                lang="zh",
                session_mode="postmarket",
                lookback_runs=20,
                summary=summary,
                watchlist_rows=[],
                portfolio_rows=[],
                model_candidate_rows=[],
                portfolio_totals={"pnl_pct": 0},
                portfolio_meta={},
                pipeline_payload=pipeline_payload,
                recent_jobs=[],
                banner_html="",
                nlp_payload={},
            )

        self.assertIn("今日市场许可", rendered)
        self.assertIn("市场判断", rendered)
        self.assertIn("发现候选", rendered)
        self.assertIn("自选与执行", rendered)
        self.assertIn("持仓与复盘", rendered)
        self.assertIn("第一次使用：每天只做这四件事", rendered)
        self.assertIn("数据与模型状态", rendered)
        self.assertLess(rendered.index("今日市场许可"), rendered.index("数据与模型状态"))


if __name__ == "__main__":
    unittest.main()
