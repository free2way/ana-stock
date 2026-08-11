from __future__ import annotations


WORKSPACE_SIDEBAR_STYLE = """
          .sidebar {
            position:sticky;
            top:0;
            height:100vh;
            padding:14px 10px;
            background:linear-gradient(180deg, rgba(5,10,16,0.98), rgba(8,14,21,0.96));
            border-right:1px solid var(--line);
            backdrop-filter:blur(18px);
            overflow-y:auto;
            scrollbar-gutter:stable;
          }
          .sidebar::-webkit-scrollbar { width:8px; }
          .sidebar::-webkit-scrollbar-track { background:transparent; }
          .sidebar::-webkit-scrollbar-thumb { background:#243548; border-radius:999px; }
          .brand { padding:4px 6px 10px; border-bottom:1px solid rgba(255,255,255,0.05); }
          .brand-tag { display:inline-flex; padding:4px 8px; border-radius:7px; background:rgba(61,217,182,0.11); color:var(--accent); font-size:10px; font-weight:900; letter-spacing:0.06em; text-transform:uppercase; }
          .brand h1 { margin:9px 0 5px; font-size:21px; line-height:1.08; letter-spacing:0; }
          .brand p { margin:0; color:var(--muted); font-size:12px; line-height:1.45; }
          .side-nav { display:grid; gap:8px; margin-top:14px; }
          .nav-section-label {
            padding:0 10px;
            color:var(--muted);
            font-size:10px;
            font-weight:900;
            letter-spacing:0.10em;
            text-transform:uppercase;
          }
          .nav-flow, .nav-tools-list { display:grid; gap:4px; }
          .side-link {
            position:relative;
            display:grid;
            gap:2px;
            padding:9px 10px 9px 12px;
            border:1px solid transparent;
            border-radius:8px;
            background:transparent;
            text-decoration:none;
            color:inherit;
            transition:background 140ms ease, border-color 140ms ease, transform 140ms ease;
          }
          .side-link::before {
            content:"";
            position:absolute;
            left:4px;
            top:10px;
            bottom:10px;
            width:2px;
            border-radius:999px;
            background:transparent;
          }
          .side-link:hover { border-color:rgba(255,255,255,0.06); background:rgba(21,34,49,0.62); transform:translateX(1px); }
          .side-link.active { border-color:rgba(61,217,182,0.24); background:linear-gradient(90deg, rgba(61,217,182,0.16), rgba(82,168,255,0.06)); }
          .side-link.active::before { background:var(--accent); }
          .side-link.workflow-link { background:rgba(255,255,255,0.018); }
          .side-link.workflow-link:hover { background:rgba(21,34,49,0.74); }
          .side-label { font-size:13px; font-weight:850; letter-spacing:0; }
          .side-meta { font-size:10.5px; color:var(--muted); line-height:1.3; }
          .nav-tools {
            margin-top:4px;
            border-top:1px solid rgba(255,255,255,0.07);
            padding-top:8px;
          }
          .nav-tools summary {
            display:flex;
            align-items:center;
            justify-content:space-between;
            gap:8px;
            padding:7px 10px;
            border-radius:8px;
            cursor:pointer;
            color:var(--muted);
            font-size:12px;
            font-weight:850;
            list-style:none;
          }
          .nav-tools summary::-webkit-details-marker { display:none; }
          .nav-tools summary::after { content:"+"; color:var(--accent); font-size:16px; line-height:1; }
          .nav-tools[open] summary::after { content:"−"; }
          .nav-tools summary:hover { background:rgba(21,34,49,0.56); color:var(--ink); }
          .nav-tools-list { margin-top:4px; }
          .nav-tools-list .side-link { padding-top:8px; padding-bottom:8px; }
          .sidebar-foot { margin-top:12px; padding:10px; border:1px solid rgba(255,255,255,0.06); border-radius:8px; background:rgba(17,28,40,0.48); color:var(--muted); font-size:11.5px; line-height:1.5; }
"""


WORKSPACE_COMPACT_STYLE = """
          .app { display:grid; grid-template-columns:248px minmax(0,1fr); min-height:100vh; }
          .main, .content { padding:18px 16px 26px; min-width:0; }
          .wrap { max-width:1180px; margin:0 auto; }
          .hero, .grid { gap:10px; margin-bottom:10px; }
          .app .topbar, .app .toolbar {
            display:flex;
            gap:8px;
            align-items:center;
            flex-wrap:wrap;
            margin-bottom:10px;
            min-height:34px;
          }
          .app .topbar a, .app .toolbar a {
            display:inline-flex;
            align-items:center;
            min-height:32px;
            padding:6px 9px;
            border-radius:8px;
            border:1px solid rgba(255,255,255,0.05);
            background:rgba(17,28,40,0.52);
            color:var(--accent);
            text-decoration:none;
            font-size:12px;
            font-weight:800;
          }
          .app .topbar a:hover, .app .toolbar a:hover { border-color:rgba(61,217,182,0.22); background:rgba(21,34,49,0.76); }
          .card {
            background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94));
            border:1px solid var(--line);
            border-radius:10px;
            padding:14px;
            box-shadow:0 10px 22px rgba(0,0,0,0.14);
            margin-bottom:10px;
          }
          .eyebrow {
            display:inline-flex;
            padding:4px 7px;
            border-radius:6px;
            background:rgba(61,217,182,0.10);
            color:var(--accent);
            font-size:10.5px;
            font-weight:900;
            letter-spacing:0.05em;
            text-transform:uppercase;
            margin-bottom:8px;
          }
          .muted, .lead { color:var(--muted); line-height:1.45; font-size:13px; }
          .app h1 { letter-spacing:0; }
          .stack { display:grid; gap:9px; }
          .list-row { display:flex; justify-content:space-between; gap:10px; padding:9px 0; border-top:1px solid var(--line); }
          .list-row:first-child { border-top:none; padding-top:0; }
          input, textarea, select, button {
            width:100%;
            padding:8px 10px;
            border-radius:8px;
            border:1px solid var(--line);
            background:#0f1823;
            color:var(--ink);
            font:inherit;
          }
          input:focus, textarea:focus, select:focus {
            outline:none;
            border-color:rgba(61,217,182,0.48);
            box-shadow:0 0 0 3px rgba(61,217,182,0.08);
          }
          button {
            cursor:pointer;
            transition:filter 140ms ease, transform 140ms ease;
          }
          button:hover { filter:brightness(1.04); transform:translateY(-1px); }
          textarea { min-height:124px; resize:vertical; }
          .app .pill, .app .default-chip, .app .linkbtn, .app .detail-link {
            border-radius:8px;
            font-size:12px;
          }
          .app .nav-grid {
            gap:10px;
            grid-template-columns:repeat(auto-fit, minmax(210px, 1fr));
            margin-bottom:10px;
          }
          .app .nav-card {
            border-radius:10px;
            padding:12px;
            box-shadow:0 8px 18px rgba(0,0,0,0.12);
          }
          .app .nav-icon {
            width:34px;
            height:34px;
            border-radius:8px;
            font-size:10px;
          }
          .app .nav-title { font-size:15px; }
          .app .nav-kicker { font-size:10.5px; }
          .table-wrap {
            width:100%;
            overflow-x:auto;
            border-radius:8px;
            border:1px solid var(--line);
            background:rgba(11,19,29,0.82);
            scrollbar-gutter:stable both-edges;
          }
          .table-wrap::-webkit-scrollbar { height:10px; width:10px; }
          .table-wrap::-webkit-scrollbar-track { background:#0f1823; border-radius:999px; }
          .table-wrap::-webkit-scrollbar-thumb { background:#30445a; border-radius:999px; border:2px solid #0f1823; }
          table { width:100%; border-collapse:collapse; font-size:12.5px; }
          th, td { text-align:left; padding:8px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
          th {
            color:var(--muted);
            font-weight:800;
            font-size:11px;
            letter-spacing:0.03em;
            text-transform:uppercase;
            background:rgba(15,24,35,0.72);
            position:sticky;
            top:0;
            z-index:1;
          }
          tr:hover td { background:rgba(61,217,182,0.035); }
          @media (max-width: 1120px) {
            .app { grid-template-columns:1fr; }
            .sidebar { position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }
            .side-nav { grid-template-columns:repeat(auto-fit, minmax(148px, 1fr)); }
            .nav-section-label, .nav-tools { grid-column:1 / -1; }
            .nav-flow { display:contents; }
            .nav-tools-list { grid-template-columns:repeat(auto-fit, minmax(148px, 1fr)); }
          }
          @media (max-width: 720px) {
            .main, .content { padding:12px 8px 20px; }
            .card { border-radius:9px; padding:11px; }
            .brand p, .side-meta { display:none; }
            .side-nav { grid-template-columns:repeat(2, minmax(0, 1fr)); }
            .side-link { padding:9px 9px 9px 11px; }
            .hero, .grid { grid-template-columns:1fr !important; }
            .form-grid { grid-template-columns:1fr !important; }
          }
"""


def render_workspace_nav_html(*, lang: str, active_key: str | None = None, lookback_runs: int = 20) -> str:
    core_items = [
        ("home", f"/dashboard?lang={lang}&lookback_runs={lookback_runs}", "今日决策" if lang == "zh" else "Today", "从市场判断开始" if lang == "zh" else "Start with market posture"),
        ("market", f"/dashboard/market?lang={lang}&lookback_runs={lookback_runs}", "市场判断" if lang == "zh" else "Market", "A 股 / 美股环境" if lang == "zh" else "A-share / US context"),
        ("screeners", f"/screeners?lang={lang}", "发现候选" if lang == "zh" else "Find Candidates", "模型候选与交易条件" if lang == "zh" else "Candidates and trade gates"),
        ("watchlist", f"/watchlist?lang={lang}", "自选与执行" if lang == "zh" else "Watch & Execute", "观察、触发与风险" if lang == "zh" else "Watch, triggers, risk"),
        ("portfolio", f"/portfolio?lang={lang}", "持仓与复盘" if lang == "zh" else "Holdings & Review", "仓位动作与复盘" if lang == "zh" else "Actions and review"),
    ]
    tool_items = [
        ("monitor", f"/dashboard/realtime-monitor?lang={lang}", "盘中重点监控" if lang == "zh" else "Live Monitor", "买点与风险触发" if lang == "zh" else "Buy-zone and risk triggers"),
        ("daily_report", f"/dashboard/ai-daily-report?lang={lang}", "AI 日报" if lang == "zh" else "AI Daily Report", "复盘输出与历史" if lang == "zh" else "Review output and history"),
        ("model_eval", f"/dashboard/model-performance?lang={lang}", "模型评测" if lang == "zh" else "Model Evaluation", "OOS 与模型赛马" if lang == "zh" else "OOS and model race"),
        ("social", f"/social-signals?lang={lang}", "社交信号" if lang == "zh" else "Social", "外部观点验证" if lang == "zh" else "Validate external ideas"),
        ("ai_chat", f"/ai-chat?lang={lang}", "AI 问答" if lang == "zh" else "AI Q&A", "股票研究助手" if lang == "zh" else "Research assistant"),
        ("journal", f"/review-journal?lang={lang}", "复盘心得" if lang == "zh" else "Review Journal", "计划、得失与纪律" if lang == "zh" else "Plans and lessons"),
        ("ops", f"/dashboard/ops?lang={lang}&lookback_runs={lookback_runs}", "任务中心" if lang == "zh" else "Jobs", "运行、维护与历史" if lang == "zh" else "Runs, maintenance, history"),
        ("data", f"/dashboard/data-sources?lang={lang}", "数据健康" if lang == "zh" else "Data Health", "新鲜度与数据源" if lang == "zh" else "Freshness and providers"),
        ("settings", f"/settings?lang={lang}", "设置" if lang == "zh" else "Settings", "通知与系统入口" if lang == "zh" else "Notifications and system"),
    ]

    def render_link(item: tuple[str, str, str, str], *, workflow: bool = False) -> str:
        key, href, label, meta = item
        classes = "side-link"
        if workflow:
            classes += " workflow-link"
        if key == active_key:
            classes += " active"
        return f"<a class='{classes}' href='{href}'><span class='side-label'>{label}</span><span class='side-meta'>{meta}</span></a>"

    tools_open = active_key in {item[0] for item in tool_items}
    primary_label = "每日选股流程" if lang == "zh" else "Daily Selection Flow"
    tools_label = "研究与系统工具" if lang == "zh" else "Research & System Tools"
    return (
        f"<div class='nav-section-label'>{primary_label}</div>"
        f"<div class='nav-flow'>{''.join(render_link(item, workflow=True) for item in core_items)}</div>"
        f"<details class='nav-tools'{' open' if tools_open else ''}>"
        f"<summary>{tools_label}</summary>"
        f"<div class='nav-tools-list'>{''.join(render_link(item) for item in tool_items)}</div>"
        "</details>"
    )
