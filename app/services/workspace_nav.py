from __future__ import annotations


WORKSPACE_SIDEBAR_STYLE = """
          .sidebar {
            position:sticky;
            top:0;
            height:100vh;
            padding:28px 20px;
            background:rgba(7,16,24,0.88);
            border-right:1px solid var(--line);
            backdrop-filter:blur(18px);
          }
          .brand-tag { display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; }
          .brand h1 { margin:12px 0 8px; font-size:28px; line-height:1.05; }
          .brand p { margin:0; color:var(--muted); font-size:14px; line-height:1.5; }
          .side-nav { display:grid; gap:10px; margin-top:24px; }
          .side-link {
            display:grid;
            gap:4px;
            padding:14px 16px;
            border:1px solid var(--line);
            border-radius:18px;
            background:rgba(17,28,40,0.68);
            text-decoration:none;
            color:inherit;
          }
          .side-link:hover { border-color:rgba(82,168,255,0.22); background:rgba(21,34,49,0.92); }
          .side-link.active { border-color:rgba(61,217,182,0.34); background:linear-gradient(180deg, rgba(61,217,182,0.18), rgba(82,168,255,0.10)); }
          .side-label { font-size:15px; font-weight:800; }
          .side-meta { font-size:12px; color:var(--muted); }
          .sidebar-foot { margin-top:24px; padding:16px; border:1px solid var(--line); border-radius:18px; background:rgba(17,28,40,0.68); color:var(--muted); font-size:13px; line-height:1.55; }
"""


def render_workspace_nav_html(*, lang: str, active_key: str | None = None, lookback_runs: int = 20) -> str:
    items = [
        ("home", f"/dashboard?lang={lang}&lookback_runs={lookback_runs}", "首页" if lang == "zh" else "Home", "工作台" if lang == "zh" else "Workspace"),
        ("watchlist", f"/watchlist?lang={lang}", "自选股" if lang == "zh" else "Watchlist", "观察池与同步" if lang == "zh" else "Tracking and sync"),
        ("portfolio", f"/portfolio?lang={lang}", "持仓" if lang == "zh" else "Portfolio", "组合风险与动作" if lang == "zh" else "Risk and actions"),
        ("screeners", f"/screeners?lang={lang}", "模型选股" if lang == "zh" else "Model Picks", "模板化选股" if lang == "zh" else "Template-driven screening"),
        ("market", f"/dashboard/market?lang={lang}&lookback_runs={lookback_runs}", "市场概览" if lang == "zh" else "Market", "热力与脉冲" if lang == "zh" else "Pulse and breadth"),
        ("social", f"/social-signals?lang={lang}", "社交信号" if lang == "zh" else "Social", "X观点验证" if lang == "zh" else "X idea validation"),
        ("ops", f"/dashboard/ops?lang={lang}&lookback_runs={lookback_runs}", "任务中心" if lang == "zh" else "Jobs", "自动任务与结果" if lang == "zh" else "Automation and results"),
        ("data", f"/dashboard/data-sources?lang={lang}", "数据状态" if lang == "zh" else "Data", "新鲜度与来源" if lang == "zh" else "Freshness and providers"),
        ("settings", f"/settings?lang={lang}", "设置" if lang == "zh" else "Settings", "通知与系统入口" if lang == "zh" else "Notifications and system entry"),
    ]
    return "".join(
        f"<a class='side-link{' active' if key == active_key else ''}' href='{href}'><span class='side-label'>{label}</span><span class='side-meta'>{meta}</span></a>"
        for key, href, label, meta in items
    )
