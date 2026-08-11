from __future__ import annotations

import unittest

from app.api.routes.screener import _normalize_screen_params, _template_default_min_trend_score
from app.services.model_selection_guidance import _recommendation_screener_href
from app.services.screener_snapshots import build_precompute_screener_params


class ScreenerDefaultTests(unittest.TestCase):
    def test_lightgbm_switch_uses_model_ranking_threshold(self) -> None:
        self.assertEqual(_template_default_min_trend_score("lightgbm_top_picks"), 10)

    def test_priority_model_link_preserves_lightgbm_threshold(self) -> None:
        href = _recommendation_screener_href(
            {"template": "lightgbm_top_picks"},
            target_market="CN",
        )
        self.assertIn("min_trend_score=10", href)

    def test_cn_precompute_can_exclude_cross_market_snapshots(self) -> None:
        params = build_precompute_screener_params(
            markets=["CN"],
            include_watchlist=False,
            include_all_market=False,
        )
        self.assertTrue(params)
        self.assertTrue(all(str(item.get("market")) == "CN" for item in params))

    def test_new_model_discovery_defaults_to_full_market(self) -> None:
        normalized = _normalize_screen_params({"model_template": "cn_volume_breakout"})
        self.assertEqual(normalized["universe"], "full_market")


if __name__ == "__main__":
    unittest.main()
