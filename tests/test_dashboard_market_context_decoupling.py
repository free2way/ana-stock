from __future__ import annotations

from pathlib import Path
import unittest

from app.api.routes.dashboard import _common
from app.services import dashboard_market_context
from app.services.stock_selection import reason_screen_policy


class DashboardMarketContextDecouplingTests(unittest.TestCase):
    def test_market_context_helpers_remain_compatibility_exports(self) -> None:
        self.assertIs(_common._build_market_context, dashboard_market_context._build_market_context)
        self.assertIs(_common._concept_slug, dashboard_market_context._concept_slug)
        self.assertIs(_common._concept_price_strength, dashboard_market_context._concept_price_strength)
        self.assertNotIn("app.api.routes", Path(dashboard_market_context.__file__).read_text())

    def test_concept_price_strength_uses_history_without_http_or_database(self) -> None:
        class FakeSymbolDataService:
            def get_history(self, ticker: str, *, limit: int) -> list[dict]:
                self.last_request = (ticker, limit)
                return [
                    {"close": float(index), "volume": 100.0 + index}
                    for index in range(1, 26)
                ]

        service = FakeSymbolDataService()
        result = dashboard_market_context._concept_price_strength(service, ["AAA"])

        self.assertEqual(("AAA", 25), service.last_request)
        self.assertGreater(result["avg_move_5d"], 0)
        self.assertGreater(result["avg_move_20d"], 0)
        self.assertEqual(100.0, result["breadth_pct"])
        self.assertGreater(result["turnover_ratio_20d"], 1.0)
        self.assertIsNotNone(result["flow_proxy_score"])

    def test_concept_slug_is_stable(self) -> None:
        self.assertEqual("ai-and-cloud", dashboard_market_context._concept_slug("AI & Cloud"))

    def test_reason_to_screener_policy_is_a_service_contract(self) -> None:
        self.assertIs(_common._reason_screen_params, reason_screen_policy._reason_screen_params)
        self.assertIs(_common._reason_screen_href, reason_screen_policy.reason_screen_href)

        params = reason_screen_policy._reason_screen_params(
            reason="too_far_from_pullback_zone",
            status=None,
            market="CN",
            lang="zh",
        )

        self.assertEqual("next_tesla_swing", params["model_template"])
        self.assertEqual("buy_the_dip", params["confluence_action_filter"])
        self.assertEqual("buy_the_dip", params["action_filter"])
        self.assertEqual("CN", params["market"])
        self.assertNotIn("app.api.routes", Path(reason_screen_policy.__file__).read_text())


if __name__ == "__main__":
    unittest.main()
