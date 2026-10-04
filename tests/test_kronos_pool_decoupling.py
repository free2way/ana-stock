from __future__ import annotations

import ast
import copy
import inspect
import unittest
from unittest.mock import MagicMock, patch

from app.api.presentation.screener_pages import render_kronos_validation_pool_page
from app.api.routes import screener as screener_routes
from app.services.stock_selection.kronos_pool import (
    kronos_decision_tone,
    prepare_kronos_validation_pool,
)


def _payload() -> dict:
    return {
        "status": "ready",
        "message": "fresh",
        "model_name": "kronos-test",
        "candidate_count": 4,
        "validated_count": 3,
        "updated_at": "2026-10-01T15:30:00+08:00",
        "rows": [
            {
                "ticker": "600001.SH",
                "name": "支持候选",
                "market": "CN",
                "kronos_status": "READY",
                "kronos_decision": "支持",
                "kronos_reason": "路径完整",
                "kronos_score": 20,
                "path_precheck": {"score": 80},
                "trade_readiness_score": 65,
            },
            {
                "ticker": "600002.SH",
                "name": "拒绝候选",
                "market": "CN",
                "kronos_status": "READY",
                "kronos_decision": "不支持",
                "kronos_reason": "回撤过高",
                "kronos_score": 99,
                "path_precheck": {"score": 99},
                "trade_readiness_score": 99,
            },
            {
                "ticker": "AAPL",
                "name": "Apple",
                "market": "US",
                "kronos_status": "READY",
                "kronos_decision": "Support",
                "kronos_score": 30,
            },
            {
                "ticker": "MSFT",
                "name": "Microsoft",
                "market": "US",
                "kronos_status": "NOT_CONFIGURED",
                "kronos_decision": "Pending",
            },
        ],
    }


class KronosPoolContractTests(unittest.TestCase):
    def test_chinese_rejection_is_not_misclassified_as_support(self) -> None:
        self.assertEqual(kronos_decision_tone("不支持"), "avoid")
        self.assertEqual(kronos_decision_tone("支持"), "support")

    def test_pool_filters_sorts_counts_and_does_not_mutate_source(self) -> None:
        payload = _payload()
        original = copy.deepcopy(payload)

        pool = prepare_kronos_validation_pool(payload, market="cn", status="ready")

        self.assertEqual(payload, original)
        self.assertEqual(pool["market_counts"], {"CN": 2, "US": 2})
        self.assertEqual(pool["status_counts"], {"READY": 3, "NOT_CONFIGURED": 1})
        self.assertEqual([row["ticker"] for row in pool["rows"]], ["600001.SH", "600002.SH"])
        self.assertEqual(pool["selected_market"], "CN")
        self.assertEqual(pool["selected_status"], "READY")

    def test_rendering_escapes_snapshot_content_and_marks_rejection(self) -> None:
        payload = _payload()
        payload["rows"][1]["name"] = '<script>alert("x")</script>'
        payload["rows"][1]["kronos_reason"] = '<img src=x onerror="alert(1)">'
        pool = prepare_kronos_validation_pool(payload, market="CN", status="READY")

        html = render_kronos_validation_pool_page(
            lang="zh",
            pool=pool,
            snapshot_created_at="2026-10-01",
            nav_html='<a href="/screeners">nav</a>',
        )

        self.assertNotIn('<script>alert("x")</script>', html)
        self.assertIn("&lt;script&gt;alert", html)
        self.assertNotIn('<img src=x onerror="alert(1)">', html)
        self.assertIn('decision-chip avoid">不支持', html)
        self.assertIn("market=CN", html)
        self.assertIn("status=READY", html)

    def test_route_has_no_inline_html_and_uses_domain_then_presenter(self) -> None:
        tree = ast.parse(inspect.getsource(screener_routes.kronos_validation_pool_page))
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

        self.assertFalse(any(isinstance(node, ast.JoinedStr) for node in ast.walk(tree)))
        self.assertIn("prepare_kronos_validation_pool", calls)
        self.assertIn("render_kronos_validation_pool_page", calls)

    @patch.object(screener_routes, "render_workspace_nav_html", return_value="<nav>workspace</nav>")
    @patch.object(screener_routes, "load_latest_kronos_validation")
    @patch.object(screener_routes, "resolve_request_lang", return_value="zh")
    @patch.object(screener_routes, "is_authenticated", return_value=True)
    def test_real_route_entry_renders_filtered_page(
        self,
        _authenticated: MagicMock,
        _resolve_lang: MagicMock,
        load_snapshot: MagicMock,
        _nav: MagicMock,
    ) -> None:
        load_snapshot.return_value = {"created_at": "2026-10-01", "payload": _payload()}

        html = screener_routes.kronos_validation_pool_page(
            MagicMock(),
            lang="zh",
            market="CN",
            status="READY",
            db=MagicMock(),
        )

        self.assertIn("Kronos 二次验证池", html)
        self.assertIn("600001.SH", html)
        self.assertIn("600002.SH", html)
        self.assertNotIn("AAPL", html)
        self.assertNotIn("MSFT", html)


if __name__ == "__main__":
    unittest.main()
