from __future__ import annotations


WORKSPACE_SIDEBAR_STYLE = """
          .sidebar {
            position:sticky;
            top:0;
            height:100vh;
            padding:18px 14px;
            background:rgba(7,16,24,0.92);
            border-right:1px solid var(--line);
            backdrop-filter:blur(18px);
          }
          .brand-tag { display:inline-flex; padding:5px 9px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:11px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; }
          .brand h1 { margin:10px 0 6px; font-size:24px; line-height:1.05; }
          .brand p { margin:0; color:var(--muted); font-size:13px; line-height:1.45; }
          .side-nav { display:grid; gap:7px; margin-top:18px; }
          .side-link {
            display:grid;
            gap:3px;
            padding:10px 12px;
            border:1px solid var(--line);
            border-radius:14px;
            background:rgba(17,28,40,0.68);
            text-decoration:none;
            color:inherit;
          }
          .side-link:hover { border-color:rgba(82,168,255,0.22); background:rgba(21,34,49,0.92); }
          .side-link.active { border-color:rgba(61,217,182,0.34); background:linear-gradient(180deg, rgba(61,217,182,0.18), rgba(82,168,255,0.10)); }
          .side-label { font-size:14px; font-weight:800; }
          .side-meta { font-size:11px; color:var(--muted); }
          .sidebar-foot { margin-top:16px; padding:12px; border:1px solid var(--line); border-radius:14px; background:rgba(17,28,40,0.68); color:var(--muted); font-size:12px; line-height:1.5; }
"""


WORKSPACE_COMPACT_STYLE = """
          .app { display:grid; grid-template-columns:260px minmax(0,1fr); min-height:100vh; }
          .main, .content { padding:20px 18px 28px; min-width:0; }
          .wrap { max-width:1108px; margin:0 auto; }
          .hero, .grid { gap:12px; margin-bottom:12px; }
          .card {
            background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94));
            border:1px solid var(--line);
            border-radius:18px;
            padding:16px;
            box-shadow:0 14px 28px rgba(0,0,0,0.18);
            margin-bottom:12px;
          }
          .eyebrow {
            display:inline-flex;
            padding:5px 8px;
            border-radius:999px;
            background:rgba(61,217,182,0.12);
            color:var(--accent);
            font-size:11px;
            font-weight:800;
            letter-spacing:0.05em;
            text-transform:uppercase;
            margin-bottom:10px;
          }
          .muted, .lead { color:var(--muted); line-height:1.45; font-size:13px; }
          .stack { display:grid; gap:10px; }
          .list-row { display:flex; justify-content:space-between; gap:12px; padding:10px 0; border-top:1px solid var(--line); }
          .list-row:first-child { border-top:none; padding-top:0; }
          input, textarea, select, button {
            width:100%;
            padding:9px 11px;
            border-radius:10px;
            border:1px solid var(--line);
            background:#0f1823;
            color:var(--ink);
            font:inherit;
          }
          textarea { min-height:124px; resize:vertical; }
          .table-wrap {
            width:100%;
            overflow-x:auto;
            border-radius:14px;
            border:1px solid var(--line);
            background:rgba(11,19,29,0.82);
          }
          table { width:100%; border-collapse:collapse; font-size:13px; }
          th, td { text-align:left; padding:9px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
          th { color:var(--muted); font-weight:700; }
          @media (max-width: 1120px) {
            .app { grid-template-columns:1fr; }
            .sidebar { position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }
          }
          @media (max-width: 720px) {
            .main, .content { padding:14px 10px 22px; }
            .card { border-radius:15px; padding:13px; }
            .hero, .grid { grid-template-columns:1fr !important; }
            .form-grid { grid-template-columns:1fr !important; }
          }
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
