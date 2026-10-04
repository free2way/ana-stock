from __future__ import annotations

import ast
import copy
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.api.presentation.screener_pages import render_market_snapshot_page
from app.api.routes import screener as screener_routes
from app.services.stock_selection.market_snapshot_view import build_market_snapshot_view


def _payload() -> dict:
    return {
        "boards": [
            {
                "market": "CN",
                "title_zh": "A股强势",
                "title_en": "CN Momentum",
                "description_zh": "盘面候选",
                "description_en": "Market candidates",
                "rows": [
                    {
                        "ticker": "600001.SH",
                        "name": "示例股票",
                        "market": "CN",
                        "trend_score": 80,
                        "snapshot_score": 90,
                        "momentum_5": 5,
                        "volume_ratio": 2,
                    }
                ],
            },
            {
                "market": "US",
                "title_zh": "美股强势",
                "title_en": "US Momentum",
                "description_zh": "美股候选",
                "description_en": "US candidates",
                "rows": [{"ticker": "AAPL", "name": "Apple", "market": "US", "trend_score": 60}],
            },
        ]
    }


class MarketSnapshotDecouplingTests(unittest.TestCase):
    def test_domain_view_normalizes_filters_summarizes_and_does_not_mutate(self) -> None:
        payload = _payload()
        original = copy.deepcopy(payload)

        view = build_market_snapshot_view(
            payload,
            mode="bad-mode",
            market_filter="cn",
            history={"CN": [1, 3], "US": [2, 1]},
        )

        self.assertEqual(payload, original)
        self.assertEqual(view["mode"], "monitor")
        self.assertEqual(view["selected_market"], "CN")
        self.assertEqual(len(view["boards"]), 1)
        cn, us = view["market_summaries"]
        self.assertEqual(cn["heat"], 80.0)
        self.assertEqual(cn["delta"], 2)
        self.assertEqual(us["delta"], -1)

    def test_template_escapes_all_snapshot_and_form_values(self) -> None:
        payload = _payload()
        payload["boards"][0]["title_en"] = '<script>alert("title")</script>'
        payload["boards"][0]["rows"][0]["name"] = '<img src=x onerror="alert(1)">'
        payload["boards"][0]["rows"][0]["selection_reason"] = '"><svg onload=alert(2)>'
        view = build_market_snapshot_view(payload, mode="monitor", market_filter="CN")

        html = render_market_snapshot_page(
            lang="en",
            view=view,
            sentiment={"sentiment": "neutral"},
            watchlist_map={},
            message='<script>alert("message")</script>',
            nav_html="<a>nav</a>",
        )

        self.assertNotIn('<script>alert("title")</script>', html)
        self.assertNotIn('<img src=x onerror="alert(1)">', html)
        self.assertNotIn('<script>alert("message")</script>', html)
        self.assertNotIn('><svg onload=alert(2)>', html)
        self.assertIn("&lt;script&gt;alert", html)
        self.assertIn("Market Snapshot", html)

    def test_route_is_thin_and_has_no_inline_html(self) -> None:
        tree = ast.parse(inspect.getsource(screener_routes.market_snapshot_page))
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        self.assertFalse(any(isinstance(node, ast.JoinedStr) for node in ast.walk(tree)))
        self.assertIn("build_market_snapshot_view", calls)
        self.assertIn("render_market_snapshot_page", calls)

    def test_real_route_entry_loads_and_renders_cn_snapshot(self) -> None:
        repository = MagicMock()
        repository.get_or_create_default.return_value = SimpleNamespace(id=7)
        repository.list_ticker_map.return_value = {}
        with (
            patch.object(screener_routes, "is_authenticated", return_value=True),
            patch.object(screener_routes, "WatchlistRepository", return_value=repository),
            patch.object(
                screener_routes,
                "load_latest_workspace_snapshot",
                return_value={"payload": _payload()},
            ),
            patch.object(screener_routes, "load_market_snapshot_history", return_value={}),
            patch.object(
                screener_routes,
                "get_or_set",
                return_value={"sentiment": "bullish", "total_candidates": 1},
            ),
            patch.object(screener_routes, "render_workspace_nav_html", return_value="<a>nav</a>"),
        ):
            html = screener_routes.market_snapshot_page(
                MagicMock(),
                lang="zh",
                mode="monitor",
                market_filter="CN",
                db=MagicMock(),
            )

        self.assertIn("市场快照", html)
        self.assertIn("600001.SH", html)
        self.assertNotIn(">AAPL<", html)


if __name__ == "__main__":
    unittest.main()
