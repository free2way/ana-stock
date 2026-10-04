from __future__ import annotations

import ast
import inspect
import unittest
from unittest.mock import MagicMock, patch

from app.api.presentation.factor_lab_pages import (
    render_factor_lab_page,
    render_factor_lab_run_detail_page,
)
from app.api.routes import screener as screener_routes


class FactorLabDecouplingTests(unittest.TestCase):
    def test_lab_template_escapes_strategy_factor_run_and_message(self) -> None:
        html = render_factor_lab_page(
            lang="en",
            strategies=[{"id": 'x"><script>', "name": "<b>strategy</b>"}],
            selected_strategy={
                "id": 'x"><script>',
                "name": "<img src=x onerror=alert(1)>",
                "description": "<svg onload=alert(2)>",
                "filters": [],
                "weights": {},
            },
            factor_defs=[
                {
                    "key": "factor",
                    "category": "technical",
                    "label_en": "<em>factor</em>",
                    "description_en": "<script>description</script>",
                }
            ],
            runs=[
                {
                    "id": 1,
                    "created_at": "<script>date</script>",
                    "payload": {"strategy": {"name": "<i>run</i>"}},
                }
            ],
            snapshot_ready=True,
            message="<script>message</script>",
            nav_html="<a>nav</a>",
        )

        self.assertNotIn("<script>message</script>", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;em&gt;factor&lt;/em&gt;", html)
        self.assertIn("Factor Lab", html)

    def test_detail_template_escapes_row_and_outcome_values(self) -> None:
        html = render_factor_lab_run_detail_page(
            lang="en",
            snapshot_id=9,
            snapshot={
                "payload": {
                    "strategy": {"id": "s", "name": "<script>name</script>"},
                    "rows": [
                        {
                            "ticker": "BAD/<script>",
                            "name": "<img src=x>",
                            "factor_scores": {"factor": 5},
                            "factor_values": {"factor": "<b>value</b>"},
                            "forward_outcome": {"return_1d_pct": "<svg>bad</svg>"},
                        }
                    ],
                }
            },
            factor_defs=[{"key": "factor", "label_en": "Factor"}],
            nav_html="<a>nav</a>",
        )

        self.assertNotIn("<script>name</script>", html)
        self.assertNotIn("<img src=x>", html)
        self.assertIn("&lt;b&gt;value&lt;/b&gt;", html)
        self.assertIn("&lt;svg&gt;bad&lt;/svg&gt;", html)

    def test_factor_routes_have_no_inline_html(self) -> None:
        for function in (
            screener_routes.factor_lab_page,
            screener_routes.factor_lab_run_detail_page,
        ):
            tree = ast.parse(inspect.getsource(function))
            self.assertFalse(any(isinstance(node, ast.JoinedStr) for node in ast.walk(tree)), function.__name__)

    def test_real_lab_route_loads_services_and_renders(self) -> None:
        strategy = {"id": "base", "name": "Base", "description": "desc", "source_params": {"market": "CN"}}
        with (
            patch.object(screener_routes, "is_authenticated", return_value=True),
            patch.object(screener_routes, "resolve_request_lang", return_value="en"),
            patch.object(screener_routes, "list_factor_strategies", return_value=[strategy]),
            patch.object(screener_routes, "get_factor_strategy", return_value=strategy),
            patch.object(screener_routes, "list_factor_definitions", return_value=[]),
            patch.object(screener_routes, "list_factor_experiment_runs", return_value=[]),
            patch.object(screener_routes, "exact_screener_snapshot_exists", return_value=True),
            patch.object(screener_routes, "render_workspace_nav_html", return_value="<a>nav</a>"),
        ):
            html = screener_routes.factor_lab_page(MagicMock(), lang="en", db=MagicMock())

        self.assertIn("Factor Lab", html)
        self.assertIn("Base", html)
        self.assertIn("Snapshot ready", html)


if __name__ == "__main__":
    unittest.main()
