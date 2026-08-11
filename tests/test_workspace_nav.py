from __future__ import annotations

import unittest

from app.services.workspace_nav import render_workspace_nav_html


class WorkspaceNavigationTests(unittest.TestCase):
    def test_primary_navigation_follows_selection_workflow(self) -> None:
        rendered = render_workspace_nav_html(lang="zh", active_key="screeners")

        for label in ("今日决策", "市场判断", "发现候选", "自选与执行", "持仓与复盘"):
            self.assertIn(label, rendered)
        self.assertIn("workflow-link active", rendered)
        self.assertLess(rendered.index("市场判断"), rendered.index("发现候选"))
        self.assertLess(rendered.index("发现候选"), rendered.index("自选与执行"))

    def test_research_and_system_tools_are_collapsed_by_default(self) -> None:
        rendered = render_workspace_nav_html(lang="zh", active_key="home")

        self.assertIn("<details class='nav-tools'>", rendered)
        self.assertIn("研究与系统工具", rendered)
        self.assertIn("任务中心", rendered)
        self.assertIn("模型评测", rendered)

    def test_active_tool_opens_tool_group(self) -> None:
        rendered = render_workspace_nav_html(lang="en", active_key="ops")

        self.assertIn("<details class='nav-tools' open>", rendered)
        self.assertIn("Research & System Tools", rendered)
        self.assertIn("side-link active", rendered)


if __name__ == "__main__":
    unittest.main()
