from __future__ import annotations

import ast
from pathlib import Path
import re
import unittest

from app.api.presentation import dashboard_legacy


class DashboardTemplateBoundaryTests(unittest.TestCase):
    def test_dashboard_routes_contain_no_full_html_documents(self) -> None:
        route_root = Path(__file__).parents[1] / "app" / "api" / "routes" / "dashboard"
        offenders: list[str] = []
        for path in route_root.glob("*.py"):
            tree = ast.parse(path.read_text())
            for function in (
                node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ):
                if any(
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and "<!DOCTYPE html>" in node.value
                    for node in ast.walk(function)
                ):
                    offenders.append(f"{path.name}:{function.name}")

        self.assertEqual([], offenders)

    def test_legacy_template_fragments_are_complete_and_renderable(self) -> None:
        project_root = Path(__file__).parents[1]
        route_root = project_root / "app" / "api" / "routes" / "dashboard"
        template_root = project_root / "app" / "api" / "templates"
        migrated: list[str] = []

        for path in route_root.glob("*.py"):
            tree = ast.parse(path.read_text())
            for call in (
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "render_dashboard_legacy_page"
            ):
                self.assertTrue(call.args)
                self.assertIsInstance(call.args[0], ast.Constant)
                template_name = str(call.args[0].value)
                fragments_keyword = next(item for item in call.keywords if item.arg == "fragments")
                self.assertIsInstance(fragments_keyword.value, ast.List)
                fragment_count = len(fragments_keyword.value.elts)
                template_path = template_root / template_name
                self.assertTrue(template_path.exists(), template_name)
                referenced = {
                    int(value)
                    for value in re.findall(r"fragments\[(\d+)\]", template_path.read_text())
                }
                self.assertEqual(set(range(fragment_count)), referenced, template_name)
                rendered = dashboard_legacy.render_dashboard_legacy_page(
                    template_name,
                    fragments=[""] * fragment_count,
                )
                self.assertIn("<!DOCTYPE html>", rendered, template_name)
                migrated.append(template_name)

        # Winner traceback has graduated from the positional legacy adapter.
        named_pages = (
            "dashboard/winner_traceback.html",
            "dashboard/continuous_leaders.html",
            "dashboard/data_sources.html",
            "dashboard/ai_daily_report_message.html",
            "dashboard/ops_history.html",
            "dashboard/ops_job_detail.html",
            "dashboard/ops_jobs.html",
            "dashboard/ai_daily_report_history_detail.html",
        )
        for name in named_pages:
            self.assertTrue((template_root / name).exists())
            self.assertNotIn("fragments[", (template_root / name).read_text())
        self.assertGreaterEqual(len(migrated) + len(named_pages), 20)
        self.assertEqual(len(migrated), len(set(migrated)))
        self.assertNotIn("app.api.routes", Path(dashboard_legacy.__file__).read_text())


if __name__ == "__main__":
    unittest.main()
