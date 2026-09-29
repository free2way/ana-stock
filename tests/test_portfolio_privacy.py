from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.api.routes import portfolio


class PortfolioPrivacyTests(unittest.TestCase):
    def test_private_value_is_masked_by_default(self) -> None:
        rendered = portfolio._render_private_portfolio_value("$12,345.67 (8.2%)")

        self.assertIn("portfolio-private-mask", rendered)
        self.assertIn(">*****</span>", rendered)
        self.assertIn("portfolio-private-actual", rendered)
        self.assertIn("$12,345.67 (8.2%)", rendered)

    def test_portfolio_page_masks_cn_and_us_values_until_eye_toggle(self) -> None:
        rows = [
            {
                "ticker": "600519.SH",
                "name": "贵州茅台",
                "market": "CN",
                "quantity": 10.0,
                "cost_basis": 1500.0,
                "latest_price": 1600.0,
                "market_value": 16000.0,
                "pnl": 1000.0,
                "pnl_pct": 6.6667,
                "latest_price_missing": False,
                "ai_headline": "继续观察趋势",
                "ai_verdict": "持有",
                "ai_strategy": "按计划跟踪",
                "key_hint": "控制仓位",
                "target_weight_pct": 50.0,
                "target_weight_text": "50.0%",
                "target_weight_source": "test",
                "current_weight_pct": 88.4,
                "action_bucket": "HOLD",
                "action_bucket_key": "hold",
                "note": "",
            },
            {
                "ticker": "ASTS",
                "name": "AST SpaceMobile",
                "market": "US",
                "quantity": 100.0,
                "cost_basis": 18.5,
                "latest_price": 21.0,
                "market_value": 2100.0,
                "pnl": 250.0,
                "pnl_pct": 13.5135,
                "latest_price_missing": False,
                "ai_headline": "Protect profit",
                "ai_verdict": "BUY",
                "ai_strategy": "Trend follow",
                "key_hint": "Protect profit",
                "target_weight_pct": 50.0,
                "target_weight_text": "50.0%",
                "target_weight_source": "test",
                "current_weight_pct": 11.6,
                "action_bucket": "HOLD",
                "action_bucket_key": "hold",
                "note": "",
            },
        ]
        intelligence = {
            "market_rankings": [
                {"market": "CN", "weight_pct": 88.4, "market_value": 16000.0},
                {"market": "US", "weight_pct": 11.6, "market_value": 2100.0},
            ],
            "top_position": {
                "ticker": "600519.SH",
                "name": "贵州茅台",
                "market": "CN",
                "weight_pct": 88.4,
                "market_value": 16000.0,
            },
            "risk_posture": "均衡",
            "posture_summary": "测试组合",
            "risk_summary": "测试组合",
            "trim_candidates": 0,
            "exit_candidates": 0,
            "review_candidates": 0,
            "watch_items": [],
            "all_items": [],
            "sector_rankings": [],
            "total_market_value": 18100.0,
            "total_positions": 2,
            "concentration_pct": 88.4,
            "rebalance_alerts": 0,
            "top_market": "CN",
            "action_mix": {},
        }
        snapshot = {
            "payload": {
                "rows": rows,
                "totals": {"market_value": 18100.0, "cost": 16850.0},
                "intelligence": intelligence,
            }
        }
        watchlist_repo = MagicMock()
        watchlist_repo.get_or_create_default.return_value = SimpleNamespace(id=1)
        watchlist_repo.list_items.return_value = []

        def load_snapshot(_db: object, snapshot_name: str) -> dict | None:
            if snapshot_name == portfolio.SNAPSHOT_PORTFOLIO_WORKSPACE:
                return snapshot
            return None

        with (
            patch.object(portfolio, "is_authenticated", return_value=True),
            patch.object(portfolio, "resolve_request_lang", return_value="zh"),
            patch.object(portfolio, "load_latest_workspace_snapshot", side_effect=load_snapshot),
            patch.object(portfolio, "WatchlistRepository", return_value=watchlist_repo),
            patch.object(portfolio, "load_portfolio_positions", return_value=[]),
            patch.object(portfolio, "load_portfolio_trades", return_value=[]),
            patch.object(portfolio, "_load_portfolio_daily_change_pct", return_value=None),
        ):
            rendered = portfolio.portfolio_page(
                request=MagicMock(),
                message=None,
                sort_by="daily_change",
                sort_order="desc",
                action_focus="all",
                db=MagicMock(),
            )

        self.assertIn("A 股持仓", rendered)
        self.assertIn("美股持仓", rendered)
        self.assertIn('id="portfolio-private-values"', rendered)
        self.assertIn("data-portfolio-privacy-toggle", rendered)
        self.assertIn('aria-pressed="false"', rendered)
        self.assertIn(">*****</span>", rendered)
        self.assertIn(".portfolio-private-actual { display:none; }", rendered)
        self.assertIn("initializePortfolioPrivacy", rendered)
        self.assertIn("portfolio-values-visible", rendered)
        self.assertIn("¥16,000.00", rendered)
        self.assertIn("$2,100.00", rendered)


if __name__ == "__main__":
    unittest.main()
