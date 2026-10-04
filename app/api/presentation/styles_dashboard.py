"""Page-level CSS constants extracted from app/api/routes/dashboard.py.

Pure static presentation data (no financial decisions). Shared workspace
chrome styles live in app/services/workspace_nav.py and are composed here.
"""

# pylint: disable=line-too-long  # CSS kept byte-identical to the original inline blocks

from app.services.workspace_nav import WORKSPACE_COMPACT_STYLE, WORKSPACE_SIDEBAR_STYLE
from app.api.presentation.tokens import (
    ROOT_TOKENS_BASE,
    ROOT_TOKENS_BASE_PANEL2,
    ROOT_TOKENS_BASE_SIGNALS,
    ROOT_TOKENS_BASE_SOFT,
    ROOT_TOKENS_SURFACE,
)


DASHBOARD_WORKSPACE_STYLE = (
    """
          :root {
            --bg:#071018;
            --bg-soft:#0d1722;
            --panel:#111c28;
            --panel-2:#152231;
            --panel-3:#1a2a3c;
            --ink:#e6edf3;
            --muted:#90a3b8;
            --line:#223246;
            --accent:#3dd9b6;
            --accent-2:#52a8ff;
            --danger:#ff6b81;
            --warn:#f6c85f;
            --good:#4ade80;
          }
          * { box-sizing:border-box; }
          body {
            margin:0;
            font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            color:var(--ink);
            background:
              radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),
              radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),
              linear-gradient(180deg, #08111a 0%, #071018 100%);
          }
          a { color:inherit; text-decoration:none; }
          """
    + WORKSPACE_COMPACT_STYLE
    + """
          """
    + WORKSPACE_SIDEBAR_STYLE
    + """
          .brand { margin-bottom:28px; }
          .content { padding:20px 18px 28px; }
          .topbar { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; flex-wrap:wrap; margin-bottom:14px; }
          .hero h2 { margin:0 0 8px; font-size:32px; line-height:1.04; max-width:760px; }
          .hero p { margin:0; color:var(--muted); font-size:14px; max-width:720px; }
          .top-actions { display:flex; gap:10px; flex-wrap:wrap; }
          .top-pill {
            display:inline-flex; align-items:center; justify-content:center;
            min-height:38px; padding:0 14px; border-radius:999px; border:1px solid var(--line);
            background:rgba(17,28,40,0.72); color:var(--muted); font-weight:700; font-size:13px;
          }
          .top-pill.active { color:var(--ink); border-color:rgba(82,168,255,0.35); background:rgba(82,168,255,0.16); }
          .banner { margin-bottom:12px; padding:12px 14px; border-radius:14px; background:#172534; border:1px solid var(--line); }
          .decision-card {
            display:grid;
            grid-template-columns:minmax(0, 1fr) auto;
            gap:18px;
            align-items:center;
            margin-bottom:12px;
            padding:18px;
            border:1px solid rgba(61,217,182,0.28);
            border-radius:16px;
            background:linear-gradient(115deg, rgba(61,217,182,0.15), rgba(82,168,255,0.08) 52%, rgba(17,28,40,0.92));
          }
          .decision-card h3 { margin:0; font-size:23px; line-height:1.15; }
          .decision-card p { max-width:700px; margin:7px 0 0; color:var(--muted); font-size:13px; line-height:1.5; }
          .decision-kicker { color:var(--accent); font-size:11px; font-weight:900; letter-spacing:0.08em; text-transform:uppercase; }
          .workflow-steps { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:9px; margin-bottom:12px; }
          .workflow-step {
            display:flex;
            gap:10px;
            min-width:0;
            padding:12px;
            border:1px solid var(--line);
            border-radius:12px;
            background:rgba(13,23,34,0.72);
            transition:background 140ms ease, border-color 140ms ease, transform 140ms ease;
          }
          .workflow-step:hover { border-color:rgba(61,217,182,0.34); background:rgba(21,34,49,0.88); transform:translateY(-1px); }
          .workflow-number { color:var(--accent); font-size:11px; font-weight:900; letter-spacing:0.06em; }
          .workflow-copy { display:grid; gap:3px; min-width:0; }
          .workflow-copy b { font-size:12px; }
          .workflow-copy strong { color:var(--ink); font-size:15px; line-height:1.2; }
          .workflow-copy small { color:var(--muted); font-size:11px; line-height:1.35; }
          .system-status { margin:0 0 12px; }
          .system-status summary { padding:11px 13px; }
          .system-status .readiness-grid { padding:0 12px 12px; margin:0; }
          .readiness-grid { display:grid; gap:12px; grid-template-columns:repeat(4, minmax(0, 1fr)); margin-bottom:12px; }
          .readiness-card {
            padding:14px 15px;
            border-radius:18px;
            border:1px solid rgba(61,217,182,0.16);
            background:
              linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94));
            box-shadow:0 12px 28px rgba(15,23,42,0.12);
            min-width:0;
          }
          .readiness-top { display:flex; align-items:center; justify-content:space-between; gap:8px; color:var(--muted); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; }
          .readiness-value { margin-top:10px; color:var(--ink); font-size:18px; font-weight:900; line-height:1.25; word-break:break-word; overflow-wrap:anywhere; }
          .summary-grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); margin-bottom:12px; }
          .metric { font-size:26px; font-weight:800; line-height:1; margin:0 0 6px; }
          .metric.metric-compact { font-size:18px; line-height:1.25; word-break:break-word; overflow-wrap:anywhere; }
          .muted { color:var(--muted); font-size:13px; line-height:1.5; }
          .workspace { display:grid; gap:12px; grid-template-columns:minmax(0, 1.35fr) minmax(320px, 0.82fr); align-items:start; }
          .workspace > .stack { align-self:start; align-content:start; }
          .stack { display:grid; gap:12px; align-content:start; }
          .panel-head { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:10px; }
          .compact-card { padding:16px; }
          .compact-head { margin-bottom:6px; align-items:center; }
          .home-list-card { padding:12px; }
          .home-list-card { align-self:start; }
          .home-list-card .panel-head { align-items:center; margin-bottom:6px; }
          .home-list-card .eyebrow { margin-bottom:4px; }
          .home-list-card h3 { margin:0; font-size:17px; line-height:1.15; }
          .home-list-card .panel-head p { display:none; }
          .home-list-card .cta { min-height:32px; padding:0 10px; border-radius:10px; font-size:12px; }
          .home-list-card .list-stack { gap:6px; }
          .home-list-card .list-row { padding:7px 9px; border-radius:10px; }
          .home-list-card .ticker { font-size:13px; }
          .home-list-card .subtle { font-size:11px; margin-top:2px; }
          .home-list-card .signal { padding:4px 8px; font-size:11px; }
          .home-list-card .mini-metric { font-size:12px; }
          .home-list-meta { display:flex; flex-wrap:wrap; gap:6px; margin:0 0 7px; }
          .home-list-meta span { display:inline-flex; align-items:center; padding:4px 7px; border-radius:999px; background:rgba(82,168,255,0.10); border:1px solid rgba(82,168,255,0.16); color:#9acbff; font-size:11px; font-weight:800; }
          .home-side-card { padding:12px; }
          .home-side-card .panel-head { margin-bottom:7px; align-items:center; }
          .home-side-card .panel-head p { display:none; }
          .home-side-card h3 { margin:0; font-size:17px; line-height:1.15; }
          .home-side-card .list-stack { gap:6px; }
          .home-side-card .signal-row { padding:8px 9px; border-radius:10px; }
          .home-side-card .cta-row { margin-top:10px; gap:6px; }
          .home-side-card .cta { min-height:32px; padding:0 10px; border-radius:10px; font-size:12px; }
          .home-digest {
            padding:0;
            overflow:hidden;
          }
          .home-digest summary {
            display:flex;
            align-items:center;
            justify-content:space-between;
            gap:10px;
            cursor:pointer;
            padding:12px;
            list-style:none;
          }
          .home-digest summary::-webkit-details-marker { display:none; }
          .home-digest summary::after {
            content:"+";
            display:inline-flex;
            align-items:center;
            justify-content:center;
            width:22px;
            height:22px;
            border-radius:999px;
            background:rgba(82,168,255,0.10);
            color:#9acbff;
            font-weight:900;
          }
          .home-digest[open] summary::after { content:"−"; }
          .home-digest-title { display:grid; gap:3px; }
          .home-digest-title b { font-size:14px; }
          .home-digest-title span { color:var(--muted); font-size:11px; line-height:1.35; }
          .home-digest-body { padding:0 12px 12px; display:grid; gap:8px; }
          .home-digest-body .card { box-shadow:none; margin-bottom:0; }
          .home-digest-body .panel-head p { display:none; }
          .panel-head h3 { margin:0; font-size:20px; }
          .panel-head p { margin:6px 0 0; color:var(--muted); font-size:13px; }
          .list-stack { display:grid; gap:10px; }
          .list-row, .signal-row, .job-row {
            display:flex; justify-content:space-between; gap:12px; align-items:center;
            padding:11px; border-radius:14px; background:rgba(11,19,29,0.82); border:1px solid rgba(34,50,70,0.92);
          }
          .row-right { display:flex; align-items:center; gap:10px; flex-wrap:wrap; justify-content:flex-end; }
          .ticker { font-weight:800; font-size:15px; }
          .subtle { color:var(--muted); font-size:12px; margin-top:4px; }
          .signal { display:inline-flex; align-items:center; padding:6px 10px; border-radius:999px; font-size:12px; font-weight:800; }
          .sig-buy { background:rgba(74,222,128,0.14); color:#8af0a6; }
          .sig-sell { background:rgba(255,107,129,0.14); color:#ff93a4; }
          .sig-watch { background:rgba(82,168,255,0.14); color:#89c2ff; }
          .sig-hold { background:rgba(246,200,95,0.14); color:#ffd982; }
          .mini-metric { font-weight:800; font-size:13px; color:var(--ink); }
          .mini-metric.pos { color:#8af0a6; }
          .mini-metric.neg { color:#ff93a4; }
          .dashboard-portfolio-actual { display:none; }
          [data-dashboard-portfolio-privacy].portfolio-values-visible .dashboard-portfolio-mask { display:none; }
          [data-dashboard-portfolio-privacy].portfolio-values-visible .dashboard-portfolio-actual { display:inline; }
          .dashboard-privacy-toggle {
            display:inline-flex; align-items:center; justify-content:center; width:34px; height:34px; padding:0;
            border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.8); color:var(--muted); cursor:pointer;
          }
          .dashboard-privacy-toggle:hover { color:var(--accent); border-color:rgba(61,217,182,0.45); }
          .dashboard-privacy-toggle:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
          .dashboard-privacy-toggle svg { width:17px; height:17px; }
          .dashboard-privacy-toggle .privacy-eye-open { display:none; }
          .dashboard-privacy-toggle[aria-pressed="true"] .privacy-eye-open { display:block; }
          .dashboard-privacy-toggle[aria-pressed="true"] .privacy-eye-closed { display:none; }
          .chip-row { display:flex; gap:8px; flex-wrap:wrap; margin-top:12px; }
          .chip { display:inline-flex; align-items:center; padding:7px 10px; border-radius:999px; background:rgba(82,168,255,0.10); border:1px solid rgba(82,168,255,0.18); color:#9acbff; font-size:12px; font-weight:700; }
          .news-market-block { display:grid; gap:10px; margin-bottom:12px; }
          .news-market-block:last-child { margin-bottom:0; }
          .news-market-title { font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; color:var(--muted); }
          .cta-row { display:flex; gap:10px; flex-wrap:wrap; margin-top:14px; }
          .cta {
            display:inline-flex; align-items:center; justify-content:center;
            min-height:40px; padding:0 14px; border-radius:14px; font-weight:800; font-size:13px;
            border:1px solid var(--line); background:rgba(17,28,40,0.8);
          }
          .cta.primary { background:linear-gradient(180deg, rgba(61,217,182,0.26), rgba(61,217,182,0.14)); border-color:rgba(61,217,182,0.28); }
          .job-status { padding:6px 10px; border-radius:999px; font-size:12px; font-weight:800; text-transform:uppercase; }
          .job-status.success { background:rgba(74,222,128,0.14); color:#8af0a6; }
          .job-status.failed { background:rgba(255,107,129,0.14); color:#ff93a4; }
          .job-status.partial { background:rgba(246,200,95,0.14); color:#ffd982; }
          .job-status.running { background:rgba(82,168,255,0.14); color:#89c2ff; }
          .job-status.idle { background:rgba(144,163,184,0.14); color:#c0cfde; }
          .job-status.unknown { background:rgba(144,163,184,0.14); color:#c0cfde; }
          .job-type { font-weight:700; font-size:13px; }
          .empty { padding:18px; border-radius:16px; background:rgba(11,19,29,0.65); border:1px dashed var(--line); color:var(--muted); font-size:13px; }
          @media (max-width: 1120px) {
            .app { grid-template-columns:1fr; }
            .sidebar { position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }
            .workspace, .summary-grid, .readiness-grid, .workflow-steps { grid-template-columns:1fr; }
            .decision-card { grid-template-columns:1fr; }
          }
        """
)


DASHBOARD_DATA_SOURCES_STYLE = (
    ("""
          """ + (ROOT_TOKENS_BASE) + """
          * { box-sizing:border-box; }
          body { margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }
          a { color:inherit; text-decoration:none; }
          .app { display:grid; grid-template-columns:248px minmax(0,1fr); min-height:100vh; }
          """)
    + WORKSPACE_SIDEBAR_STYLE
    + """
          .main { padding:20px 18px 28px; }
          .topbar,.chip-row,.action-row,.row-right { display:flex; flex-wrap:wrap; gap:10px; }
          .topbar { justify-content:space-between; align-items:center; margin-bottom:24px; }
          .top-pill,.cta,.mini-metric,.status-pill { display:inline-flex; align-items:center; justify-content:center; }
          .top-pill,.cta { padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.7); color:var(--muted); font-size:13px; font-weight:700; }
          .cta.primary { background:linear-gradient(135deg, rgba(61,217,182,0.28), rgba(82,168,255,0.24)); color:var(--ink); }
          .hero { display:grid; grid-template-columns:minmax(0,1.4fr) minmax(280px,0.9fr); gap:16px; margin-bottom:16px; }
          .card { background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); }
          .eyebrow { display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.06em; text-transform:uppercase; }
          h1 { margin:14px 0 10px; font-size:40px; line-height:1.02; letter-spacing:-0.03em; }
          .section-title { margin:0 0 6px; font-size:22px; }
          .lead,.section-copy,.subtle,.metric-meta,li,.empty { color:var(--muted); }
          .lead,.section-copy { font-size:15px; line-height:1.6; }
          .metrics-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:16px; margin:16px 0; }
          .metric-card { padding:18px; border-radius:20px; background:rgba(21,34,49,0.82); border:1px solid var(--line); }
          .metric-label { color:var(--muted); font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:0.05em; }
          .metric-value { margin-top:12px; font-size:26px; font-weight:800; letter-spacing:-0.03em; word-break:break-word; }
          .workspace-grid { display:grid; grid-template-columns:minmax(0,1.05fr) minmax(320px,0.95fr); gap:16px; align-items:start; }
          .stack,.list-stack { display:grid; gap:16px; }
          .list-row,.sync-row { display:flex; justify-content:space-between; align-items:flex-start; gap:14px; padding:14px 0; border-top:1px solid rgba(144,163,184,0.12); }
          .list-row:first-child,.sync-row:first-child { border-top:none; padding-top:0; }
          .ticker { font-weight:800; font-size:15px; color:var(--ink); }
          .subtle { margin-top:4px; font-size:12px; line-height:1.45; }
          .mini-metric { padding:7px 10px; border-radius:999px; background:rgba(82,168,255,0.12); color:#b9dcff; font-size:12px; font-weight:700; }
          .status-pill { padding:7px 10px; border-radius:999px; font-size:12px; font-weight:800; text-transform:uppercase; letter-spacing:0.04em; background:rgba(144,163,184,0.14); color:#b4c5d8; }
          .status-pill.success { background:rgba(74,222,128,0.14); color:#8df0aa; } .status-pill.failed { background:rgba(255,107,129,0.16); color:#ff9aaa; } .status-pill.partial { background:rgba(246,200,95,0.16); color:#ffd98a; } .status-pill.running { background:rgba(82,168,255,0.16); color:#9bd0ff; }
          ul { margin:10px 0 0 18px; padding:0; } li { margin:8px 0; line-height:1.55; }
          @media (max-width:1120px) { .metrics-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } .workspace-grid, .hero { grid-template-columns:1fr; } }
          @media (max-width:900px) { .app { grid-template-columns:1fr; } .sidebar { position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); } }
          @media (max-width:640px) { .main { padding:20px 16px 36px; } h1 { font-size:30px; } .metrics-grid { grid-template-columns:1fr; } }
        """
)


SIGNAL_KEY_STYLE = (
    ("""
          """ + (ROOT_TOKENS_BASE_SOFT) + """
          * { box-sizing: border-box; }
          body { margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }
          a { color:inherit; text-decoration:none; }
          .app { display:grid; grid-template-columns:248px minmax(0,1fr); min-height:100vh; }
          """)
    + WORKSPACE_SIDEBAR_STYLE
    + """
          .main { padding:20px 18px 28px; min-width:0; }
          .wrap { max-width:none; margin:0; }
          .card { background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }
          .eyebrow { display:inline-flex; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }
          .metric { font-size: 30px; font-weight: 800; margin: 8px 0; }
          .muted { color: var(--muted); font-size: 14px; }
          .grid { display:grid; gap:16px; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); margin-bottom:16px; }
          .action-grid { display:grid; gap:16px; grid-template-columns: minmax(260px, 1fr) minmax(280px, 1.2fr); margin-bottom:16px; }
          .mini-grid { display:grid; gap:16px; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); }
          .mini-card { background:rgba(21,34,49,0.82); border:1px solid var(--line); border-radius:18px; padding:14px; }
          .mini-top { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:6px; }
          .mini-score { font-size:12px; font-weight:800; color:var(--accent); background:var(--accent-soft); padding:4px 8px; border-radius:999px; }
          .mini-name { color:var(--muted); font-size:13px; margin-bottom:10px; min-height:34px; }
          .mini-metrics { display:flex; justify-content:space-between; gap:10px; color:var(--ink); font-size:12px; font-weight:700; margin-top:8px; }
          .compare-row { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; }
          .compare-pill { display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; background:rgba(17,28,40,0.75); border:1px solid var(--line); color:var(--muted); text-decoration:none; font-size:12px; font-weight:800; }
          .compare-pill.active { background:rgba(61,217,182,0.16); border-color:rgba(61,217,182,0.24); color:var(--ink); }
          .table-wrap { width:100%; overflow-x:auto; border-radius:16px; border:1px solid var(--line); background:rgba(11,19,29,0.82); }
          table { width:100%; min-width:980px; border-collapse:collapse; }
          th, td { text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
          th { color: var(--muted); font-weight: 600; }
          .banner { margin-bottom:16px; padding:14px 16px; border-radius:16px; background:rgba(61,217,182,0.12); color:var(--accent); font-weight:700; border:1px solid rgba(61,217,182,0.24); }
          .stack { display:grid; gap:12px; }
          input, button { border-radius:12px; border:1px solid var(--line); padding:10px 12px; font:inherit; background:#0f1823; color:var(--ink); }
          button { background:linear-gradient(135deg, rgba(61,217,182,0.88), rgba(82,168,255,0.82)); color:#03131f; border-color:transparent; font-weight:800; cursor:pointer; }
          .checkline { display:inline-flex; align-items:center; gap:8px; color:var(--muted); font-size:14px; }
          .action-link { display:inline-flex; align-items:center; padding:10px 12px; border-radius:12px; background:rgba(61,217,182,0.12); color:var(--accent); border:1px solid rgba(61,217,182,0.2); text-decoration:none; font-weight:800; }
          .sidebar-foot { margin-top:24px; padding:16px; border:1px solid var(--line); border-radius:18px; background:rgba(17,28,40,0.68); color:var(--muted); font-size:13px; line-height:1.55; }
          @media (max-width:1100px) { .app { grid-template-columns:1fr; } .sidebar { position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); } .main { padding:20px 16px 36px; } .action-grid { grid-template-columns:1fr; } }
        """
)


SORT_LINK_STYLE = (
    ("""
          """ + (ROOT_TOKENS_BASE_SOFT) + """
          * { box-sizing: border-box; }
          body { margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at top right, rgba(61,217,182,0.12), transparent 24%),linear-gradient(180deg,#08111a 0%,#071018 100%); }
          a { color:inherit; text-decoration:none; }
          .app { display:grid; grid-template-columns:248px minmax(0,1fr); min-height:100vh; }
          """)
    + WORKSPACE_SIDEBAR_STYLE
    + """
          .content { padding:20px 18px 28px; }
          .wrap { max-width:none; margin:0; }
          .card { background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }
          .eyebrow { display:inline-block; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }
          .metric { font-size: 30px; font-weight: 800; margin: 8px 0; color:var(--ink); }
          .muted { color: var(--muted); font-size: 14px; }
          .grid { display:grid; gap:16px; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); margin-bottom:16px; }
          .compare-row,.toolbar { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; align-items:center; }
          .compare-pill,.pill { display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; background:rgba(17,28,40,0.75); border:1px solid var(--line); color:var(--muted); text-decoration:none; font-size:12px; font-weight:800; }
          .compare-pill.active,.pill.active { background:rgba(61,217,182,0.14); color:var(--ink); border-color:rgba(61,217,182,0.28); }
          .table-wrap { width:100%; overflow-x:auto; border-radius:14px; border:1px solid var(--line); background:rgba(11,19,29,0.82); }
          table { width:100%; min-width:980px; border-collapse:collapse; }
          th, td { text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); white-space:nowrap; vertical-align:top; }
          th { color: var(--muted); font-weight: 600; }
          .stack { display:grid; gap:12px; }
          input, button, select { border-radius:12px; border:1px solid var(--line); padding:10px 12px; font:inherit; background:#0f1823; color:var(--ink); }
          button { background:var(--accent); color:#041119; border-color:var(--accent); font-weight:800; }
          .checkbox-row { display:inline-flex; align-items:center; gap:8px; color:var(--muted); font-size:14px; }
          .action-link { display:inline-flex; align-items:center; padding:10px 12px; border-radius:12px; background:rgba(61,217,182,0.10); color:var(--accent); font-weight:700; }
          h1 { margin:0 0 8px; font-size:38px; line-height:1.05; letter-spacing:-0.03em; }
          .sidebar-foot { margin-top:24px; padding:16px; border:1px solid var(--line); border-radius:18px; background:rgba(17,28,40,0.68); color:var(--muted); font-size:13px; line-height:1.55; }
          @media (max-width: 1120px) { .app { grid-template-columns:1fr; } .sidebar { position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); } .content { padding:20px 16px 36px; } }
        """
)


MARKET_LABEL_STYLE = (
    ("""
          """ + (ROOT_TOKENS_BASE) + """
          * { box-sizing:border-box; }
          body { margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }
          a { color:inherit; text-decoration:none; }
          .app { display:grid; grid-template-columns:248px minmax(0,1fr); min-height:100vh; }
          """)
    + WORKSPACE_SIDEBAR_STYLE
    + """
          .main { padding:20px 18px 28px; }
          .wrap { max-width:none; margin:0; }
          .toolbar { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }
          .pill,button { display:inline-flex; align-items:center; justify-content:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.7); color:var(--ink); font-size:13px; font-weight:800; }
          .card { background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }
          .eyebrow { display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }
          h2 { margin:0 0 8px; font-size:28px; }
          .muted { color:var(--muted); font-size:14px; line-height:1.55; }
          .filters { display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); margin-top:14px; }
          label { display:block; color:var(--muted); font-size:12px; margin-bottom:6px; }
          select,input { width:100%; padding:10px 12px; border-radius:12px; border:1px solid var(--line); background:#0d1721; color:var(--ink); }
          .metric-grid { display:grid; gap:14px; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); margin-top:14px; }
          .metric-card { border:1px solid var(--line); border-radius:18px; padding:16px; background:rgba(11,19,29,0.78); }
          .metric { font-size:30px; font-weight:800; margin:6px 0; }
          .decision-grid { display:grid; gap:14px; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); margin-top:14px; }
          .decision-card { border:1px solid rgba(82,168,255,0.18); border-radius:20px; padding:18px; background:rgba(11,19,29,0.82); }
          .decision-card.primary { border-color:rgba(61,217,182,0.28); background:linear-gradient(180deg, rgba(61,217,182,0.13), rgba(11,19,29,0.84)); }
          .decision-card.caution { border-color:rgba(246,200,95,0.24); background:linear-gradient(180deg, rgba(246,200,95,0.10), rgba(11,19,29,0.84)); }
          .decision-card h3 { margin:0 0 8px; font-size:21px; line-height:1.25; }
          .decision-card p { margin:0 0 12px; color:var(--muted); font-size:14px; line-height:1.55; }
          .table-wrap { width:100%; overflow-x:auto; border-radius:16px; border:1px solid var(--line); background:rgba(11,19,29,0.82); margin-top:14px; }
          table { width:100%; min-width:880px; border-collapse:collapse; font-size:14px; }
          th, td { text-align:left; padding:12px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
          th { color:var(--muted); font-weight:700; }
          @media (max-width: 960px) { .app { grid-template-columns:1fr; } .sidebar { position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); } .main { padding:20px 16px 36px; } }
        """
)


TEMPLATE_HREF_STYLE = (
    ("""
      """ + (ROOT_TOKENS_BASE) + """
      * { box-sizing:border-box; } body { margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); } a { color:inherit; text-decoration:none; }
      .app { display:grid; grid-template-columns:248px minmax(0,1fr); min-height:100vh; } """)
    + WORKSPACE_SIDEBAR_STYLE
    + """
      .main { padding:20px 18px 28px; } .wrap { max-width:none; margin:0; } .toolbar { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }
      .pill { display:inline-flex; align-items:center; justify-content:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.7); color:var(--ink); font-size:13px; font-weight:800; } .pill.active { border-color:rgba(61,217,182,0.32); background:rgba(61,217,182,0.14); color:var(--accent); }
      .card { background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }
      .eyebrow { display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; } h1,h2 { margin:0 0 8px; } .muted { color:var(--muted); font-size:14px; line-height:1.55; }
      .metric-grid { display:grid; gap:14px; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); margin-top:14px; } .metric-card { border:1px solid var(--line); border-radius:18px; padding:16px; background:rgba(11,19,29,0.78); } .metric { font-size:30px; font-weight:900; margin:6px 0; } .empty { padding:18px; border-radius:16px; background:rgba(11,19,29,0.65); border:1px dashed var(--line); color:var(--muted); }
      .table-wrap { width:100%; overflow-x:auto; border-radius:16px; border:1px solid var(--line); background:rgba(11,19,29,0.82); margin-top:14px; } table { width:100%; min-width:980px; border-collapse:collapse; font-size:14px; } th,td { text-align:left; padding:12px 10px; border-bottom:1px solid var(--line); vertical-align:top; } th { color:var(--muted); font-weight:700; } td a { color:var(--accent); font-weight:800; }
      @media (max-width: 960px) { .app { grid-template-columns:1fr; } .sidebar { position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); } .main { padding:20px 16px 36px; } }
    """
)


BOARD_DETAIL_ROWS_STYLE = (
    ("""
          """ + (ROOT_TOKENS_BASE_SOFT) + """
          * { box-sizing:border-box; }
          body { margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }
          a { color:inherit; text-decoration:none; }
          .app { display:grid; grid-template-columns:248px minmax(0,1fr); min-height:100vh; }
          """)
    + WORKSPACE_SIDEBAR_STYLE
    + """
          """
    + WORKSPACE_COMPACT_STYLE
    + """
          .main { padding:18px 14px 28px; }
          .wrap { max-width:none; margin:0; }
          .toolbar,.compare-row { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }
          .card { background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }
          .eyebrow { display:inline-flex; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.05em; text-transform:uppercase; margin-bottom:12px; }
          .muted { color:var(--muted); font-size:14px; }
          .pill,.compare-pill { display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; background:rgba(17,28,40,0.75); border:1px solid var(--line); color:var(--muted); font-size:13px; font-weight:700; text-decoration:none; }
          .compare-pill.active, .pill.active { background:rgba(61,217,182,0.16); border-color:rgba(61,217,182,0.24); color:var(--ink); }
          .mode-switch { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; margin-bottom:16px; }
          .mode-pill { display:block; padding:13px 14px; border-radius:18px; background:rgba(11,19,29,0.72); border:1px solid rgba(34,50,70,0.92); transition:transform .16s ease,border-color .16s ease,background .16s ease; }
          .mode-pill:hover { transform:translateY(-1px); border-color:rgba(61,217,182,0.34); background:rgba(15,25,37,0.92); }
          .mode-pill.active { border-color:rgba(61,217,182,0.52); background:linear-gradient(180deg, rgba(61,217,182,0.14), rgba(11,19,29,0.78)); box-shadow:0 12px 28px rgba(0,0,0,0.16); }
          .mode-pill b { display:block; font-size:14px; margin-bottom:5px; }
          .mode-pill span { display:block; color:var(--muted); font-size:11px; line-height:1.35; }
	          .grid { display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); margin-bottom:16px; }
	          .market-hero { display:grid; grid-template-columns:minmax(0,1.3fr) minmax(300px,0.7fr); gap:18px; align-items:stretch; }
	          .market-hero h1 { font-size:42px; }
	          .market-kpis { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin-top:18px; }
	          .market-kpi { padding:14px; border-radius:18px; background:rgba(11,19,29,0.7); border:1px solid rgba(61,217,182,0.11); }
	          .market-kpi-link { display:block; transition:transform .16s ease, border-color .16s ease, background .16s ease; }
	          .market-kpi-link:hover { transform:translateY(-1px); border-color:rgba(61,217,182,0.34); background:rgba(14,24,36,0.88); }
	          .market-kpi-link.active { border-color:rgba(61,217,182,0.5); background:rgba(20,36,51,0.92); box-shadow:0 12px 28px rgba(0,0,0,0.18); }
	          .market-kpi b { display:block; font-size:23px; line-height:1; margin-bottom:6px; }
	          .mode-steps { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; margin-top:16px; }
	          .mode-step { padding:12px; border-radius:16px; background:rgba(11,19,29,0.62); border:1px solid rgba(61,217,182,0.11); }
	          .mode-step b { display:block; font-size:13px; margin-bottom:5px; }
	          .mode-step span { display:block; color:var(--muted); font-size:12px; line-height:1.45; }
	          .market-actions { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; margin-bottom:16px; }
	          .market-action { position:relative; overflow:hidden; display:flex; min-height:220px; flex-direction:column; justify-content:space-between; padding:20px; border-radius:24px; background:linear-gradient(180deg, rgba(17,28,40,0.98), rgba(8,16,25,0.94)); border:1px solid var(--line); box-shadow:0 18px 40px rgba(0,0,0,0.2); transition:transform .16s ease, border-color .16s ease, background .16s ease; }
	          .market-action:hover { transform:translateY(-2px); border-color:rgba(61,217,182,0.36); background:linear-gradient(180deg, rgba(20,36,51,0.98), rgba(8,16,25,0.94)); }
	          .market-action h2 { margin:0 0 8px; font-size:23px; letter-spacing:-0.02em; }
	          .market-action p { margin:0; color:var(--muted); font-size:13px; line-height:1.55; }
	          .market-action .metric { font-size:34px; font-weight:900; letter-spacing:-0.03em; }
	          .market-action .go { display:inline-flex; align-items:center; align-self:flex-start; gap:6px; margin-top:14px; color:var(--accent); font-weight:900; font-size:13px; }
	          .market-mini-list { display:grid; gap:8px; margin-top:12px; }
	          .market-mini-row { display:flex; align-items:center; justify-content:space-between; gap:10px; padding:10px 12px; border-radius:14px; background:rgba(11,19,29,0.72); border:1px solid rgba(34,50,70,0.82); }
          .market-mini-row strong { display:block; font-size:13px; }
          .market-mini-row span { display:block; margin-top:3px; color:var(--muted); font-size:11px; }
          .market-mini-row b { color:var(--accent); font-size:16px; }
          .market-scope-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; }
          .market-scope-card { display:block; min-height:188px; padding:16px; border-radius:18px; background:rgba(11,19,29,0.74); border:1px solid rgba(34,50,70,0.92); transition:transform .16s ease, border-color .16s ease, background .16s ease; }
          .market-scope-card:hover { transform:translateY(-1px); border-color:rgba(61,217,182,0.36); background:rgba(15,25,37,0.92); }
          .market-scope-card.active { border-color:rgba(61,217,182,0.5); box-shadow:0 12px 28px rgba(0,0,0,0.16); }
          .market-scope-head { display:flex; justify-content:space-between; gap:12px; align-items:baseline; }
          .market-scope-head strong { font-size:16px; }
          .market-scope-head span { color:var(--muted); font-size:11px; }
          .market-scope-headline { display:flex; justify-content:flex-start; margin-top:10px; }
          .market-scope-chip { display:inline-flex; align-items:center; padding:5px 9px; border-radius:999px; font-size:11px; font-weight:800; letter-spacing:0.02em; }
          .market-scope-chip.up { background:rgba(74,222,128,0.14); color:#8af0a6; }
          .market-scope-chip.down { background:rgba(255,107,129,0.14); color:#ff93a4; }
          .market-scope-chip.flat { background:rgba(82,168,255,0.14); color:#89c2ff; }
          .market-scope-stats { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; margin-top:14px; }
          .market-scope-stats b { display:block; font-size:22px; line-height:1; }
          .market-scope-stats span { display:block; margin-top:6px; color:var(--muted); font-size:11px; }
          .market-scope-meta { margin-top:10px; color:var(--muted); font-size:12px; }
          .market-scope-help { margin-top:12px; padding:11px 12px; border-radius:14px; border:1px dashed rgba(61,217,182,0.24); background:rgba(61,217,182,0.06); color:var(--muted); font-size:12px; line-height:1.55; }
          .market-scope-trend-wrap { margin-top:12px; }
          .market-scope-trend-wrap span { display:block; color:var(--muted); font-size:11px; margin-bottom:8px; }
          .mini-trend { height:34px; display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:5px; align-items:end; }
          .mini-trend span { display:block; border-radius:999px 999px 3px 3px; background:linear-gradient(180deg, rgba(61,217,182,0.96), rgba(61,217,182,0.28)); box-shadow:0 6px 12px rgba(0,0,0,0.14); }
          .mini-trend.empty { grid-template-columns:1fr; align-items:center; }
          .mini-trend.empty span { height:auto; border-radius:0; background:none; box-shadow:none; color:var(--muted); font-size:11px; }
          .advanced-panel { background:rgba(17,28,40,0.7); border:1px solid rgba(34,50,70,0.74); border-radius:20px; padding:14px; }
	          .advanced-panel summary { cursor:pointer; color:var(--ink); font-weight:900; }
	          .signal-row {
	            display:flex; justify-content:space-between; gap:12px; align-items:center;
	            padding:14px; border-radius:16px; background:rgba(11,19,29,0.82); border:1px solid rgba(34,50,70,0.92);
          }
          .row-right { display:flex; align-items:center; gap:10px; flex-wrap:wrap; justify-content:flex-end; }
          .ticker { font-weight:800; font-size:15px; }
          .subtle { color:var(--muted); font-size:12px; margin-top:4px; }
          .signal { display:inline-flex; align-items:center; padding:6px 10px; border-radius:999px; font-size:12px; font-weight:800; }
          .sig-buy { background:rgba(74,222,128,0.14); color:#8af0a6; }
          .sig-sell { background:rgba(255,107,129,0.14); color:#ff93a4; }
          .sig-watch { background:rgba(82,168,255,0.14); color:#89c2ff; }
          .sig-hold { background:rgba(246,200,95,0.14); color:#ffd982; }
          .mini-metric { font-weight:800; font-size:13px; color:var(--ink); }
          .empty { padding:18px; border-radius:16px; background:rgba(11,19,29,0.65); border:1px dashed var(--line); color:var(--muted); font-size:13px; }
          .table-wrap { width:100%; overflow-x:auto; border-radius:14px; border:1px solid var(--line); background:rgba(11,19,29,0.82); }
          table { width:100%; min-width:640px; border-collapse:collapse; font-size:14px; }
          th, td { text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; white-space:nowrap; }
          th { color:var(--muted); font-weight:600; }
          h1 { margin:0 0 8px; font-size:38px; line-height:1.04; letter-spacing:-0.03em; }
          input, button { width:100%; padding:10px 12px; border-radius:12px; border:1px solid var(--line); background:#0f1823; color:var(--ink); font:inherit; }
          button { width:auto; background:var(--accent); color:#041119; font-weight:800; cursor:pointer; }
          .sidebar-foot { margin-top:24px; padding:16px; border:1px solid var(--line); border-radius:18px; background:rgba(17,28,40,0.68); color:var(--muted); font-size:13px; line-height:1.55; }
	          @media (max-width: 1100px) { .app { grid-template-columns:1fr; } .sidebar { position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); } .main { padding:20px 10px 36px; } .market-hero,.market-actions,.mode-switch,.mode-steps { grid-template-columns:1fr; } .market-kpis { grid-template-columns:repeat(2,minmax(0,1fr)); } }
        """
)


HEATMAP_METRIC_FOR_MARKET_LINK_STYLE = (
    ("""
          """ + (ROOT_TOKENS_BASE_SOFT) + """
          * { box-sizing:border-box; }
          body { margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }
          a { color:inherit; text-decoration:none; }
          .app { display:grid; grid-template-columns:248px minmax(0,1fr); min-height:100vh; }
          """)
    + WORKSPACE_SIDEBAR_STYLE
    + """
          .main { padding:20px 18px 28px; }
          .wrap { max-width:none; margin:0; }
          .card { background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }
          .eyebrow { display:inline-flex; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }
          .toolbar,.compare-row { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }
          .muted { color:var(--muted); font-size:14px; }
          .pill,.compare-pill { display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; background:rgba(17,28,40,0.75); border:1px solid var(--line); color:var(--muted); font-size:13px; font-weight:700; text-decoration:none; }
          .compare-pill.active { background:rgba(61,217,182,0.16); border-color:rgba(61,217,182,0.24); color:var(--ink); }
		          .grid { display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); margin-bottom:16px; }
		          .heat-stage { border-radius:26px; padding:14px; background:radial-gradient(circle at top left, rgba(61,217,182,0.12), transparent 32%), rgba(5,11,18,0.64); border:1px solid rgba(34,50,70,0.72); }
		          .heat-grid { position:relative; height:min(680px, calc(100vh - 330px)); min-height:520px; margin-top:12px; border-radius:20px; overflow:hidden; background:#050b12; box-shadow:inset 0 0 0 1px rgba(255,255,255,0.04); }
		          .heat-tile { position:absolute; color:#fff; border:3px solid #050b12; border-radius:11px; padding:13px; display:flex; flex-direction:column; justify-content:space-between; text-decoration:none; box-shadow:inset 0 1px 0 rgba(255,255,255,0.16),0 12px 28px rgba(0,0,0,0.28); transition:transform .16s ease, filter .16s ease, box-shadow .16s ease, border-color .16s ease; overflow:hidden; }
		          .heat-tile:hover { z-index:5; transform:translateY(-2px) scale(1.012); filter:saturate(1.14) brightness(1.05); border-color:rgba(226,232,240,0.32); box-shadow:inset 0 1px 0 rgba(255,255,255,0.22),0 22px 42px rgba(0,0,0,0.38); }
	          .heat-label { font-weight:900; line-height:1.25; font-size:15px; text-shadow:0 1px 1px rgba(0,0,0,0.22); }
	          .heat-metric { font-size:24px; font-weight:950; letter-spacing:-0.04em; }
	          .heat-meta { font-size:11px; opacity:0.9; line-height:1.35; }
	          .heat-tags { display:flex; gap:5px; flex-wrap:wrap; margin-top:4px; }
	          .heat-tags span { display:inline-flex; padding:3px 7px; border-radius:999px; background:rgba(255,255,255,0.12); font-size:10px; font-weight:800; }
	          .heat-legend { display:flex; justify-content:space-between; gap:12px; flex-wrap:wrap; margin-top:12px; color:var(--muted); font-size:12px; }
	          .legend-scale { display:inline-grid; grid-template-columns:repeat(5,32px); gap:3px; align-items:center; }
	          .legend-scale span { height:8px; border-radius:999px; }
          .market-scope-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; }
          .market-scope-card { display:block; min-height:188px; padding:16px; border-radius:18px; background:rgba(11,19,29,0.74); border:1px solid rgba(34,50,70,0.92); transition:transform .16s ease, border-color .16s ease, background .16s ease; }
          .market-scope-card:hover { transform:translateY(-1px); border-color:rgba(61,217,182,0.36); background:rgba(15,25,37,0.92); }
          .market-scope-card.active { border-color:rgba(61,217,182,0.5); box-shadow:0 12px 28px rgba(0,0,0,0.16); }
          .market-scope-head { display:flex; justify-content:space-between; gap:12px; align-items:baseline; }
          .market-scope-head strong { font-size:16px; }
          .market-scope-head span { color:var(--muted); font-size:11px; }
          .market-scope-headline { display:flex; justify-content:flex-start; margin-top:10px; }
          .market-scope-chip { display:inline-flex; align-items:center; padding:5px 9px; border-radius:999px; font-size:11px; font-weight:800; letter-spacing:0.02em; }
          .market-scope-chip.up { background:rgba(74,222,128,0.14); color:#8af0a6; }
          .market-scope-chip.down { background:rgba(255,107,129,0.14); color:#ff93a4; }
          .market-scope-chip.flat { background:rgba(82,168,255,0.14); color:#89c2ff; }
          .market-scope-stats { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:10px; margin-top:14px; }
          .market-scope-stats b { display:block; font-size:22px; line-height:1; }
          .market-scope-stats span { display:block; margin-top:6px; color:var(--muted); font-size:11px; }
          .market-scope-meta { margin-top:10px; color:var(--muted); font-size:12px; }
          .market-scope-help { margin-top:12px; padding:11px 12px; border-radius:14px; border:1px dashed rgba(61,217,182,0.24); background:rgba(61,217,182,0.06); color:var(--muted); font-size:12px; line-height:1.55; }
          .method-note { margin-top:14px; padding:12px 14px; border-radius:16px; border:1px solid rgba(82,168,255,0.22); background:rgba(82,168,255,0.08); color:var(--muted); font-size:13px; line-height:1.6; }
          .method-note strong { color:var(--ink); }
          .market-scope-trend-wrap { margin-top:12px; }
          .market-scope-trend-wrap span { display:block; color:var(--muted); font-size:11px; margin-bottom:8px; }
          .mini-trend { height:34px; display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:5px; align-items:end; }
          .mini-trend span { display:block; border-radius:999px 999px 3px 3px; background:linear-gradient(180deg, rgba(61,217,182,0.96), rgba(61,217,182,0.28)); box-shadow:0 6px 12px rgba(0,0,0,0.14); }
          .mini-trend.empty { grid-template-columns:1fr; align-items:center; }
          .mini-trend.empty span { height:auto; border-radius:0; background:none; box-shadow:none; color:var(--muted); font-size:11px; }
          table { width:100%; border-collapse:collapse; font-size:14px; }
          th, td { text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
          th { color:var(--muted); font-weight:600; }
          .ticker-links { max-width:280px; line-height:1.8; }
          .ticker-links a { display:inline-flex; padding:2px 7px; margin:1px 2px 1px 0; border:1px solid rgba(61,217,182,0.22); border-radius:999px; background:rgba(61,217,182,0.08); color:#bff7eb; font-size:12px; font-weight:800; }
          a.ticker-links { display:inline-flex; padding:4px 8px; border:1px solid rgba(61,217,182,0.22); border-radius:999px; background:rgba(61,217,182,0.08); color:#bff7eb; font-size:12px; font-weight:900; }
          .action-link { display:inline-flex; align-items:center; padding:7px 10px; border-radius:11px; background:rgba(61,217,182,0.10); color:var(--accent); font-size:12px; font-weight:900; }
          .heat-detail-card { border-color:rgba(82,168,255,0.24); }
          .heat-detail-table table { min-width:820px; }
          input, button { width:100%; padding:10px 12px; border-radius:12px; border:1px solid var(--line); background:#0f1823; color:var(--ink); font:inherit; }
          button { width:auto; background:var(--accent); color:#041119; font-weight:800; cursor:pointer; }
          .sidebar-foot { margin-top:24px; padding:16px; border:1px solid var(--line); border-radius:18px; background:rgba(17,28,40,0.68); color:var(--muted); font-size:13px; line-height:1.55; }
          h1 { margin:0 0 8px; font-size:38px; line-height:1.04; letter-spacing:-0.03em; }
	          @media (max-width:1100px) { .app { grid-template-columns:1fr; } .sidebar { position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); } .main { padding:20px 16px 36px; } .heat-grid { height:620px; min-height:620px; } .heat-label { font-size:13px; } .heat-metric { font-size:20px; } .heat-extra,.heat-tags { display:none; } }
        """
)


CONCEPT_TRACKER_SORT_LINK_STYLE = (
    ("""
          """ + (ROOT_TOKENS_BASE_SOFT) + """
          * { box-sizing:border-box; }
          body { margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }
          a { color:inherit; text-decoration:none; }
          .app { display:grid; grid-template-columns:248px minmax(0,1fr); min-height:100vh; }
          """)
    + WORKSPACE_SIDEBAR_STYLE
    + """
          .main { padding:20px 18px 28px; }
          .wrap { max-width:none; margin:0; }
          .card { background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }
          .eyebrow { display:inline-flex; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }
          .toolbar,.compare-row { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }
          .muted { color:var(--muted); font-size:14px; }
          .pill,.compare-pill { display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; background:rgba(17,28,40,0.75); border:1px solid var(--line); color:var(--muted); font-size:13px; font-weight:700; text-decoration:none; }
          .compare-pill.active { background:rgba(61,217,182,0.16); border-color:rgba(61,217,182,0.24); color:var(--ink); }
          table { width:100%; border-collapse:collapse; font-size:14px; }
          th, td { text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
          th { color:var(--muted); font-weight:600; }
          .ticker-links { max-width:280px; line-height:1.8; }
          .ticker-links a { display:inline-flex; padding:2px 7px; margin:1px 2px 1px 0; border:1px solid rgba(61,217,182,0.22); border-radius:999px; background:rgba(61,217,182,0.08); color:#bff7eb; font-size:12px; font-weight:800; }
          input, button { width:100%; padding:10px 12px; border-radius:12px; border:1px solid var(--line); background:#0f1823; color:var(--ink); font:inherit; }
          button { width:auto; background:var(--accent); color:#041119; font-weight:800; cursor:pointer; }
          .sidebar-foot { margin-top:24px; padding:16px; border:1px solid var(--line); border-radius:18px; background:rgba(17,28,40,0.68); color:var(--muted); font-size:13px; line-height:1.55; }
          h1 { margin:0 0 8px; font-size:38px; line-height:1.04; letter-spacing:-0.03em; }
          @media (max-width:1100px) { .app { grid-template-columns:1fr; } .sidebar { position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); } .main { padding:20px 16px 36px; } }
        """
)


RISK_CARD_STYLE = (
    """
    :root{--bg:#071018;--surface:#0e1926;--surface-2:#111f2e;--ink:#e8eef6;--muted:#91a4b8;--line:#23374a;--accent:#43d7b8;--blue:#6daeff;--warn:#f7ca65;--bad:#ff8397;}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}a{color:inherit;text-decoration:none}.app{display:grid;grid-template-columns:248px minmax(0,1fr);min-height:100vh}"""
    + WORKSPACE_SIDEBAR_STYLE
    + """.main{padding:34px;max-width:1500px;width:100%}.top{display:flex;justify-content:space-between;align-items:end;gap:20px;margin-bottom:28px}h1{margin:8px 0 0;font-size:34px;letter-spacing:-.04em}.eyebrow{color:var(--accent);font-size:12px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}.subtle,.muted{color:var(--muted);font-size:13px;line-height:1.5}.top-actions,.actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.outline,.detail-link{border:1px solid var(--line);padding:9px 12px;border-radius:10px;font-size:13px;font-weight:750}.notice{padding:12px 14px;border-left:3px solid var(--accent);background:rgba(67,215,184,.09);color:#c9f7ed;margin-bottom:20px}.today-job-module{display:flex;justify-content:space-between;align-items:center;gap:20px;margin:0 0 24px;padding:18px 20px;border:1px solid rgba(67,215,184,.38);border-radius:16px;background:linear-gradient(105deg,rgba(67,215,184,.13),rgba(109,174,255,.08));box-shadow:0 16px 36px rgba(0,0,0,.14)}.today-job-module h2{margin:4px 0}.today-job-module p{margin:6px 0 0;max-width:620px}.today-job-actions{display:flex;align-items:flex-end;gap:12px;flex-direction:column;white-space:nowrap}.today-job-stats{display:flex;gap:7px;flex-wrap:wrap;justify-content:flex-end}.today-job-stats span{padding:6px 9px;border:1px solid rgba(145,164,184,.26);border-radius:999px;color:var(--muted);font-size:12px}.today-job-stats strong{margin-left:4px;color:var(--ink)}.today-job-stats .good strong{color:#99f0b1}.today-job-stats .warn strong{color:#ffe09a}.today-job-link{border-color:rgba(67,215,184,.55);background:rgba(7,16,24,.34)}.section,.risk-overview{border-top:1px solid var(--line);padding-top:22px;margin-top:28px}.section-head{display:flex;align-items:end;justify-content:space-between;gap:16px;margin-bottom:12px}.section-head.compact{align-items:center}h2{margin:0;font-size:19px;letter-spacing:-.02em}.risk-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.risk-card{background:linear-gradient(180deg,rgba(17,31,46,.98),rgba(12,23,35,.98));border:1px solid var(--line);border-radius:18px;padding:15px;box-shadow:0 18px 50px rgba(0,0,0,.18)}.risk-card.bad{border-color:rgba(255,131,151,.42);background:linear-gradient(180deg,rgba(67,23,35,.72),rgba(22,22,32,.98))}.risk-card.warn{border-color:rgba(247,202,101,.38);background:linear-gradient(180deg,rgba(63,47,20,.6),rgba(22,24,32,.98))}.risk-card.good{border-color:rgba(67,215,184,.34)}.risk-top{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:10px;color:var(--muted);font-size:12px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}.risk-top strong{font-size:16px;color:var(--ink);letter-spacing:0;text-transform:none}.risk-headline{font-size:14px;font-weight:800;line-height:1.45;min-height:40px}.risk-metrics,.risk-flags{display:flex;gap:7px;flex-wrap:wrap;margin-top:12px}.risk-metrics span,.mini-flag{display:inline-flex;border:1px solid rgba(145,164,184,.18);background:rgba(145,164,184,.09);border-radius:999px;padding:5px 8px;color:#c7d5e3;font-size:11px}table{width:100%;border-collapse:collapse;background:var(--surface)}th{text-align:left;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em;font-weight:700;padding:11px 12px;border-bottom:1px solid var(--line)}td{padding:15px 12px;border-bottom:1px solid rgba(35,55,74,.72);font-size:13px;vertical-align:middle}tbody tr:hover{background:rgba(109,174,255,.045)}strong{font-size:14px}.job-note{display:block;color:var(--muted);font-size:12px;line-height:1.45;margin-top:4px;max-width:410px}button{border:0;border-radius:9px;padding:8px 11px;background:var(--accent);color:#05131a;font:700 12px ui-sans-serif,-apple-system,sans-serif;cursor:pointer}button:hover{filter:brightness(1.08)}.job-status{display:inline-flex;padding:5px 8px;border-radius:999px;font-size:11px;font-weight:800;white-space:nowrap;background:rgba(145,164,184,.13);color:#c1d0df}.job-status.success{background:rgba(74,222,128,.13);color:#99f0b1}.job-status.partial,.job-status.not_configured{background:rgba(247,202,101,.14);color:#ffe09a}.job-status.failed{background:rgba(255,131,151,.14);color:#ffafba}.job-status.running{background:rgba(109,174,255,.14);color:#a9d1ff}.actions{justify-content:flex-end}.detail-link{padding:7px 9px;color:var(--muted)}@media(max-width:1040px){.app{grid-template-columns:1fr}.sidebar{position:relative;height:auto;border-right:none;border-bottom:1px solid var(--line)}.main{padding:24px 16px}.top{align-items:start;flex-direction:column}.today-job-module{align-items:flex-start;flex-direction:column}.today-job-actions{align-items:flex-start}.today-job-stats{justify-content:flex-start}.risk-grid{grid-template-columns:1fr}table{min-width:940px}.table-scroll{overflow:auto}} """
)


LOAD_CN_SYNC_STATS_STYLE = (
    """
          :root {
            --bg:#071018;
            --bg-soft:#0d1722;
            --panel:#111c28;
            --panel-2:#152231;
            --ink:#e6edf3;
            --muted:#90a3b8;
            --line:#223246;
            --accent:#3dd9b6;
            --accent-2:#52a8ff;
            --warn:#f6c85f;
          }
          * { box-sizing:border-box; }
          body { margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }
          .app { display:grid; grid-template-columns:248px minmax(0,1fr); min-height:100vh; }
          """
    + WORKSPACE_SIDEBAR_STYLE
    + """
          .main { padding:20px 18px 28px; }
          .wrap { max-width:1108px; margin:0 auto; }
          .card { background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }
          .toolbar,.grid { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:16px; align-items:center; }
          .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:16px; }
          .eyebrow { display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }
          .pill, .action-link { display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; background:rgba(17,28,40,0.7); border:1px solid var(--line); color:var(--ink); text-decoration:none; font-size:13px; font-weight:700; }
          .muted { color:var(--muted); font-size:14px; }
          table { width:100%; border-collapse:collapse; font-size:14px; }
          th, td { text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
          th { color:var(--muted); font-weight:600; }
          form { display:grid; gap:10px; }
          input, select, button { border-radius:14px; border:1px solid var(--line); padding:10px 12px; font:inherit; background:rgba(21,34,49,0.82); color:var(--ink); }
          button { background:linear-gradient(135deg, rgba(61,217,182,0.88), rgba(82,168,255,0.82)); color:#03131f; border-color:transparent; font-weight:800; }
          h1 { margin:0 0 8px; font-size:36px; line-height:1.05; letter-spacing:-0.03em; }
        """
)


DASHBOARD_OPS_MODELS_PAGE_STYLE = (
    """
        :root { --bg:#071018; --panel:#111c28; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; --accent-2:#52a8ff; }
        * { box-sizing:border-box; } body { margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }
        .app { display:grid; grid-template-columns:248px minmax(0,1fr); min-height:100vh; } """
    + WORKSPACE_SIDEBAR_STYLE
    + """
        .main { padding:20px 18px 28px; } .wrap { max-width:1108px; margin:0 auto; } .card { background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }
        .toolbar { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; } .pill { display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.7); color:var(--ink); text-decoration:none; font-size:13px; font-weight:700; }
        .eyebrow { display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }
        .muted { color:var(--muted); font-size:14px; } .grid { display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); margin-bottom:16px; } .mini-row { display:flex; justify-content:space-between; gap:12px; padding:10px 0; border-top:1px solid var(--line); font-size:14px; } .tag-row { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; } .tag { display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; } .table-wrap { width:100%; overflow-x:auto; border-radius:14px; border:1px solid var(--line); background:rgba(11,19,29,0.82); } table { width:100%; min-width:820px; border-collapse:collapse; font-size:14px; } th, td { text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; white-space:nowrap; } th { color:var(--muted); font-weight:600; }
        input, select, button { border-radius:14px; border:1px solid var(--line); padding:10px 12px; font:inherit; background:rgba(21,34,49,0.82); color:var(--ink); } button { background:linear-gradient(135deg, rgba(61,217,182,0.88), rgba(82,168,255,0.82)); color:#03131f; border-color:transparent; font-weight:800; } pre, code { white-space:pre-wrap; word-break:break-word; }
        h1 { margin:0 0 8px; font-size:36px; line-height:1.05; letter-spacing:-0.03em; }
      """
)


JOB_DETAIL_LINK_STYLE = (
    """
    :root{--bg:#071018;--surface:#0e1926;--surface-2:#111f2e;--ink:#e8eef6;--muted:#91a4b8;--line:#23374a;--accent:#43d7b8;--warn:#f7ca65;--bad:#ff8397}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}a{color:inherit;text-decoration:none}.app{display:grid;grid-template-columns:248px minmax(0,1fr);min-height:100vh}"""
    + WORKSPACE_SIDEBAR_STYLE
    + """.main{padding:34px;max-width:1500px;width:100%}.bar{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap;margin-bottom:22px}h1{margin:6px 0;font-size:32px;letter-spacing:-.04em}h2{margin:0;font-size:18px}.eyebrow{color:var(--accent);font-size:12px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}.muted{color:var(--muted);font-size:13px;line-height:1.5}.actions{display:flex;gap:8px;flex-wrap:wrap}.outline,.detail-link{display:inline-flex;border:1px solid var(--line);padding:9px 11px;border-radius:10px;font-size:13px;font-weight:750}.summary{border:1px solid var(--line);border-radius:16px;padding:18px;background:var(--surface-2)}.summary.good{border-color:rgba(67,215,184,.42)}.summary.warn{border-color:rgba(247,202,101,.46)}.summary.bad{border-color:rgba(255,131,151,.5)}.summary p{margin:7px 0 0;font-weight:700;line-height:1.55}.metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-top:14px}.metric{padding:12px;border:1px solid var(--line);border-radius:12px;background:rgba(7,16,24,.4)}.metric span{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}.metric strong{display:block;margin-top:5px;font-size:22px}.section{border-top:1px solid var(--line);padding-top:22px;margin-top:28px}.section-head{display:flex;justify-content:space-between;gap:16px;align-items:end;margin-bottom:12px}.attention{margin:0;padding:0;list-style:none;border:1px solid var(--line);border-radius:12px;background:var(--surface)}.attention li{padding:13px 14px;border-bottom:1px solid var(--line);display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap;font-size:13px}.attention li:last-child{border-bottom:0}.attention li span:last-child{color:var(--muted);line-height:1.45;flex:1 1 380px}.scroll{overflow:auto;border:1px solid var(--line);border-radius:12px}table{width:100%;min-width:880px;border-collapse:collapse;background:var(--surface)}th,td{text-align:left;padding:13px 12px;border-bottom:1px solid var(--line);font-size:13px;vertical-align:top}th{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}tbody tr:hover{background:rgba(109,174,255,.045)}.job-type{display:block;color:var(--muted);font-size:11px;margin-top:4px}.message{color:var(--muted);max-width:520px;line-height:1.5}.job-status{display:inline-flex;padding:5px 8px;border-radius:999px;font-size:11px;font-weight:800;white-space:nowrap;background:rgba(145,164,184,.13);color:#c1d0df}.job-status.success{background:rgba(74,222,128,.13);color:#99f0b1}.job-status.partial,.job-status.not_configured,.job-status.empty{background:rgba(247,202,101,.14);color:#ffe09a}.job-status.failed,.job-status.failed_timeout{background:rgba(255,131,151,.14);color:#ffafba}.job-status.running{background:rgba(109,174,255,.14);color:#a9d1ff}@media(max-width:1040px){.app{grid-template-columns:1fr}.sidebar{position:relative;height:auto;border-right:none;border-bottom:1px solid var(--line)}.main{padding:24px 16px}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}}"""
)


DASHBOARD_OPS_HISTORY_PAGE_STYLE = (
    ("""
    """ + (ROOT_TOKENS_SURFACE) + """*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}a{color:inherit;text-decoration:none}.app{display:grid;grid-template-columns:248px minmax(0,1fr);min-height:100vh}""")
    + WORKSPACE_SIDEBAR_STYLE
    + """.main{padding:34px;max-width:1500px}.bar{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:22px;flex-wrap:wrap}h1{margin:0;font-size:28px;letter-spacing:-.03em}.muted{color:var(--muted);font-size:13px}form{display:flex;gap:8px;align-items:center}input,button{border-radius:9px;padding:9px 10px;border:1px solid var(--line);font:inherit;background:var(--surface);color:var(--ink)}button{background:var(--accent);border:0;color:#04161b;font-weight:800;cursor:pointer}.back{border:1px solid var(--line);border-radius:9px;padding:9px 11px;font-size:13px}.scroll{overflow:auto}table{width:100%;min-width:900px;border-collapse:collapse;background:var(--surface)}th,td{text-align:left;padding:13px 12px;border-bottom:1px solid var(--line);font-size:13px;vertical-align:top}th{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}tbody tr:hover{background:rgba(67,215,184,.04)}.message{color:var(--muted);max-width:480px;line-height:1.5}.job-status{display:inline-flex;padding:5px 8px;border-radius:999px;font-size:11px;font-weight:800;background:rgba(145,164,184,.13)}.job-status.success{color:#9cf0b4;background:rgba(74,222,128,.13)}.job-status.partial,.job-status.not_configured{color:#ffe09a;background:rgba(247,202,101,.14)}.job-status.failed{color:#ffafba;background:rgba(255,131,151,.14)}.job-status.running{color:#a9d1ff;background:rgba(109,174,255,.14)}@media(max-width:1040px){.app{grid-template-columns:1fr}.sidebar{position:relative;height:auto;border-right:none;border-bottom:1px solid var(--line)}.main{padding:24px 16px}}"""
)


DASHBOARD_OPS_JOB_DETAIL_STYLE = (
    ("""
    """ + (ROOT_TOKENS_SURFACE) + """*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}a{color:inherit;text-decoration:none}.app{display:grid;grid-template-columns:248px minmax(0,1fr);min-height:100vh}""")
    + WORKSPACE_SIDEBAR_STYLE
    + """.main{padding:34px;max-width:1200px}.back,.cta{display:inline-flex;border:1px solid var(--line);border-radius:9px;padding:9px 11px;font-size:13px;font-weight:750;margin-right:8px}.cta{background:var(--accent);border:0;color:#04161b}h1{margin:18px 0 6px;font-size:30px;letter-spacing:-.03em}.muted{color:var(--muted);font-size:13px;line-height:1.55}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:1px;background:var(--line);border:1px solid var(--line);margin:24px 0}.cell{padding:14px;background:var(--surface)}.label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}pre{white-space:pre-wrap;word-break:break-word;margin:0;padding:16px;background:var(--surface);border:1px solid var(--line);font-size:12px;line-height:1.55}h2{font-size:17px;margin:28px 0 10px}.job-status{display:inline-flex;padding:5px 8px;border-radius:999px;font-size:11px;font-weight:800;background:rgba(145,164,184,.13)}.job-status.success{color:#9cf0b4;background:rgba(74,222,128,.13)}.job-status.partial,.job-status.not_configured{color:#ffe09a;background:rgba(247,202,101,.14)}.job-status.failed{color:#ffafba;background:rgba(255,131,151,.14)}.job-status.running{color:#a9d1ff;background:rgba(109,174,255,.14)}@media(max-width:900px){.app{grid-template-columns:1fr}.sidebar{position:relative;height:auto;border-right:none;border-bottom:1px solid var(--line)}.main{padding:24px 16px}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}}"""
)


RESULT_SUMMARY_STYLE = (
    ("""
        """ + (ROOT_TOKENS_BASE) + """
        * { box-sizing:border-box; } body { margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }
        .app { display:grid; grid-template-columns:248px minmax(0,1fr); min-height:100vh; } """)
    + WORKSPACE_SIDEBAR_STYLE
    + """
        .main { padding:20px 18px 28px; } .wrap { max-width:1108px; margin:0 auto; } .card { background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }
        .toolbar { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; } .pill { display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.7); color:var(--ink); text-decoration:none; font-size:13px; font-weight:700; }
        .eyebrow { display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }
        .muted { color:var(--muted); font-size:14px; } .job-subtle { margin-top:6px; color:var(--muted); font-size:12px; line-height:1.4; white-space:normal; } .table-wrap { width:100%; overflow-x:auto; border-radius:14px; border:1px solid var(--line); background:rgba(11,19,29,0.82); } table { width:100%; min-width:960px; border-collapse:collapse; font-size:14px; } th, td { text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; white-space:nowrap; } th { color:var(--muted); font-weight:600; } code { white-space:pre-wrap; word-break:break-word; }
        .inline-actions { display:flex; flex-wrap:wrap; gap:10px; margin-top:16px; } .action-with-note { display:grid; gap:6px; align-content:start; } .inline-form { display:inline-flex; margin:0; } .cta { display:inline-flex; align-items:center; justify-content:center; padding:10px 14px; border-radius:999px; border:1px solid var(--line); background:rgba(21,34,49,0.92); color:var(--ink); font-size:13px; font-weight:800; } .cta.primary { background:linear-gradient(135deg, rgba(61,217,182,0.28), rgba(82,168,255,0.24)); border-color:rgba(61,217,182,0.3); } .cta.compact { padding:8px 12px; font-size:12px; } .precompute-note { margin-top:10px; } .action-receipt { max-width:260px; }
        h1 { margin:0 0 8px; font-size:36px; line-height:1.05; letter-spacing:-0.03em; }
        @media (max-width:960px) { .app { grid-template-columns:1fr; } .sidebar { position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); } .main { padding:20px 16px 36px; min-width:0; } .wrap { max-width:100%; } h1 { font-size:30px; } .inline-actions { align-items:stretch; } .action-with-note { flex:1 1 220px; } }
      """
)


DASHBOARD_WEEKLY_REVIEW_STYLE = (
    ("""
          """ + (ROOT_TOKENS_BASE) + """
          * { box-sizing:border-box; }
          body { margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }
          a { color:inherit; text-decoration:none; }
          .app { display:grid; grid-template-columns:248px minmax(0,1fr); min-height:100vh; }
          """)
    + WORKSPACE_SIDEBAR_STYLE
    + """
          .main { padding:20px 18px 28px; }
          .wrap { max-width:1108px; margin:0 auto; }
          .toolbar,.stackline { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }
          .pill { display:inline-flex; align-items:center; justify-content:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.7); color:var(--ink); font-size:13px; font-weight:800; }
          .card { background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }
          .eyebrow { display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }
          .muted { color:var(--muted); font-size:14px; line-height:1.55; }
          h2 { margin:0 0 8px; font-size:30px; }
          .metric-grid { display:grid; gap:14px; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); }
          .metric-card { border:1px solid var(--line); border-radius:18px; padding:16px; background:rgba(11,19,29,0.78); }
          .metric { font-size:30px; font-weight:800; margin:6px 0; }
          .table-wrap { width:100%; overflow-x:auto; border-radius:16px; border:1px solid var(--line); background:rgba(11,19,29,0.82); margin-top:14px; }
          table { width:100%; min-width:860px; border-collapse:collapse; font-size:14px; }
          th, td { text-align:left; padding:12px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
          th { color:var(--muted); font-weight:700; }
          @media (max-width: 960px) { .app { grid-template-columns:1fr; } .sidebar { position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); } .main { padding:20px 16px 36px; } }
        """
)


PRICE_SOURCE_TEXT_STYLE = (
    ("""
          """ + (ROOT_TOKENS_BASE_SIGNALS) + """
          * { box-sizing:border-box; }
          body { margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(96,165,250,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.14), transparent 26%),linear-gradient(180deg,#08111a 0%,#071018 100%); }
          a { color:inherit; text-decoration:none; }
          .app { display:grid; grid-template-columns:248px minmax(0,1fr); min-height:100vh; }
          """)
    + WORKSPACE_SIDEBAR_STYLE
    + """
          .main { padding:18px; }
          .wrap { max-width:1180px; margin:0 auto; }
          .toolbar { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:14px; }
          .pill { display:inline-flex; align-items:center; justify-content:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.72); color:var(--ink); font-size:13px; font-weight:850; }
          .pill.active { border-color:rgba(61,217,182,0.42); background:rgba(61,217,182,0.16); color:var(--ink); }
          .hero,.monitor-card,.metric { background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; box-shadow:0 18px 40px rgba(0,0,0,0.22); }
          .hero { padding:20px; margin-bottom:14px; }
          .eyebrow { display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:950; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:10px; }
          h1 { margin:0 0 8px; font-size:32px; line-height:1.08; letter-spacing:-0.04em; }
          .muted,.name,.monitor-detail { color:var(--muted); font-size:14px; line-height:1.55; }
          .metrics { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin:14px 0; }
          .metric { padding:14px; }
          .metric span { color:var(--muted); font-size:12px; font-weight:850; }
          .metric strong { display:block; margin-top:6px; font-size:24px; letter-spacing:-0.04em; }
          .filter-metric { appearance:none; width:100%; text-align:left; color:var(--ink); font:inherit; cursor:pointer; transition:transform .16s ease,border-color .16s ease,background .16s ease; }
          .filter-metric:hover,.filter-metric.active { transform:translateY(-1px); border-color:rgba(61,217,182,.56); background:linear-gradient(180deg,rgba(20,47,55,.98),rgba(12,29,38,.96)); }
          .filter-metric:focus-visible { outline:3px solid rgba(61,217,182,.72); outline-offset:3px; }
          .monitor-filter-state { margin:-2px 0 10px; color:var(--accent); font-size:13px; font-weight:850; }
          .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:12px; }
          .monitor-card { padding:16px; position:relative; overflow:hidden; }
          .monitor-card::before { content:""; position:absolute; left:0; top:0; bottom:0; width:4px; background:var(--line); }
          .monitor-card.risk::before { background:var(--danger); }
          .monitor-card.in_zone::before,.monitor-card.ready::before { background:var(--accent); }
          .monitor-card.extended::before { background:var(--warn); }
          .monitor-card.watch::before { background:var(--blue); }
          .monitor-top { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }
          .ticker { font-size:24px; font-weight:950; letter-spacing:-0.03em; }
          .status-chip { flex:0 0 auto; padding:6px 10px; border-radius:999px; font-size:12px; font-weight:950; border:1px solid rgba(255,255,255,0.08); }
          .status-chip.risk { color:#fecdd3; background:rgba(251,113,133,0.16); }
          .status-chip.in_zone,.status-chip.ready { color:#bbf7d0; background:rgba(61,217,182,0.14); }
          .status-chip.extended { color:#fde68a; background:rgba(251,191,36,0.14); }
          .status-chip.watch { color:#bfdbfe; background:rgba(96,165,250,0.14); }
          .status-chip.track,.status-chip.no_price { color:#cbd5e1; background:rgba(148,163,184,0.14); }
          .price-row { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-top:12px; }
          .price-row strong { font-size:22px; }
          .price-row span { color:var(--muted); font-size:12px; font-weight:850; padding:5px 8px; border-radius:999px; background:rgba(255,255,255,0.05); }
          .monitor-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; margin-top:14px; }
          .monitor-grid div { min-width:0; padding:10px; border-radius:16px; background:rgba(21,34,49,0.72); border:1px solid rgba(255,255,255,0.06); }
          .monitor-grid span { display:block; color:var(--muted); font-size:12px; font-weight:850; margin-bottom:5px; }
          .monitor-grid strong { display:block; font-size:13px; line-height:1.35; overflow-wrap:anywhere; }
          .monitor-detail { margin-top:12px; }
          .risk-row { display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }
          .risk-chip { display:inline-flex; padding:5px 8px; border-radius:999px; background:rgba(251,191,36,0.10); color:#fde68a; border:1px solid rgba(251,191,36,0.22); font-size:12px; font-weight:850; }
          .card-actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; }
          .track-button,.track-link { appearance:none; border:1px solid rgba(61,217,182,0.28); border-radius:999px; padding:8px 12px; background:rgba(61,217,182,0.12); color:var(--ink); font-size:12px; font-weight:950; cursor:pointer; }
          .track-link { border-color:rgba(148,163,184,0.20); background:rgba(148,163,184,0.10); }
          .track-button:hover,.track-link:hover { transform:translateY(-1px); border-color:rgba(61,217,182,0.55); }
          body.modal-open { overflow:hidden; }
          .kline-modal[hidden] { display:none; }
          .kline-modal { position:fixed; inset:0; z-index:50; display:grid; place-items:center; padding:18px; background:rgba(2,6,12,0.72); backdrop-filter:blur(10px); }
          .kline-dialog { width:min(980px, 96vw); max-height:92vh; overflow:auto; border:1px solid rgba(148,163,184,0.22); border-radius:26px; background:linear-gradient(180deg, rgba(13,23,35,0.98), rgba(8,16,25,0.98)); box-shadow:0 30px 90px rgba(0,0,0,0.46); padding:16px; }
          .kline-head { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:10px; }
          .kline-title { font-size:20px; font-weight:950; letter-spacing:-0.03em; }
          .kline-meta,.kline-message { color:var(--muted); font-size:13px; line-height:1.55; }
          .kline-close { width:34px; height:34px; border-radius:999px; border:1px solid rgba(148,163,184,0.22); background:rgba(255,255,255,0.06); color:var(--ink); font-size:20px; cursor:pointer; }
          .kline-chart-wrap { border:1px solid rgba(148,163,184,0.16); border-radius:18px; background:rgba(3,8,14,0.48); padding:10px; }
          #kline-canvas { display:block; width:100%; height:420px; }
          @media (max-width:960px) { .app { grid-template-columns:1fr; } .sidebar { position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); } .main { padding:14px; } .metrics { grid-template-columns:repeat(2,minmax(0,1fr)); } .wrap { max-width:100%; } }
          @media (max-width:560px) { h1 { font-size:27px; } .cards,.monitor-grid,.metrics { grid-template-columns:1fr; } .ticker { font-size:22px; } #kline-canvas { height:320px; } }
        """
)


BUY_ZONE_TEXT_STYLE = (
    """
          :root { --bg:#071018; --panel:#111c28; --ink:#e6edf3; --muted:#90a3b8; --line:#223246; --accent:#3dd9b6; --warn:#fbbf24; }
          * { box-sizing:border-box; }
          body { margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }
          a { color:inherit; text-decoration:none; }
          .app { display:grid; grid-template-columns:248px minmax(0,1fr); min-height:100vh; }
          """
    + WORKSPACE_SIDEBAR_STYLE
    + """
          .main { padding:18px; }
          .wrap { max-width:920px; margin:0 auto; }
          .toolbar { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:14px; }
          .pill { display:inline-flex; align-items:center; justify-content:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.7); color:var(--ink); font-size:13px; font-weight:800; }
          .hero,.plan-card { background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:18px; box-shadow:0 18px 40px rgba(0,0,0,0.22); }
          .hero { margin-bottom:14px; }
          .eyebrow { display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:900; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:10px; }
          h1 { margin:0 0 8px; font-size:30px; line-height:1.1; }
          .muted,.name,.note { color:var(--muted); font-size:14px; line-height:1.55; }
          .cards { display:grid; gap:12px; }
          .plan-top { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }
          .ticker { font-size:24px; font-weight:950; letter-spacing:-0.03em; }
          .badge { flex:0 0 auto; padding:6px 10px; border-radius:999px; font-size:12px; font-weight:900; border:1px solid transparent; }
          .badge.actionable { color:#022c22; background:linear-gradient(135deg,#6ee7b7,#3dd9b6); }
          .badge.watch { color:#2f1b00; background:linear-gradient(135deg,#fde68a,#fbbf24); }
          .headline { margin-top:12px; font-size:16px; font-weight:850; line-height:1.45; }
          .plan-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; margin-top:14px; }
          .plan-grid div { min-width:0; padding:10px; border-radius:16px; background:rgba(21,34,49,0.72); border:1px solid rgba(255,255,255,0.06); }
          .plan-grid span { display:block; color:var(--muted); font-size:12px; font-weight:800; margin-bottom:5px; }
          .plan-grid strong { display:block; font-size:14px; line-height:1.35; overflow-wrap:anywhere; }
          .risk { margin-top:12px; padding:10px 12px; border-radius:16px; background:rgba(251,191,36,0.10); border:1px solid rgba(251,191,36,0.22); color:#fde68a; font-size:13px; font-weight:800; }
          .note { margin-top:8px; }
          .empty { border-style:dashed; }
          @media (max-width: 960px) { .app { grid-template-columns:1fr; } .sidebar { position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); } .main { padding:14px; } .wrap { max-width:100%; } }
          @media (max-width: 520px) { .plan-grid { grid-template-columns:1fr; } h1 { font-size:26px; } .ticker { font-size:22px; } }
        """
)


AI_REPORT_NAME_CELL_STYLE = (
    ("""
          """ + (ROOT_TOKENS_BASE_PANEL2) + """
          * { box-sizing:border-box; }
          body { margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }
          a { color:inherit; text-decoration:none; }
          .app { display:grid; grid-template-columns:248px minmax(0,1fr); min-height:100vh; }
          """)
    + WORKSPACE_SIDEBAR_STYLE
    + """
          .main { padding:20px 18px 28px; }
          .wrap { max-width:1108px; margin:0 auto; }
          .toolbar { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }
          .pill { display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.7); color:var(--ink); font-size:13px; font-weight:700; }
          .card { background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }
          .eyebrow { display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }
          .metric { font-size:32px; font-weight:800; margin:4px 0 8px; }
          .muted { color:var(--muted); font-size:14px; line-height:1.55; }
          .hero-grid { display:grid; grid-template-columns:minmax(0,1.15fr) minmax(320px,0.85fr); gap:16px; margin-bottom:16px; }
          .playbook { margin-top:12px; padding:14px; border-radius:18px; background:rgba(21,34,49,0.82); border:1px solid var(--line); }
          .action-row { display:flex; gap:10px; flex-wrap:wrap; margin-top:14px; }
          .cta { display:inline-flex; align-items:center; justify-content:center; padding:10px 14px; border-radius:999px; border:1px solid var(--line); background:rgba(21,34,49,0.92); color:var(--ink); font-size:13px; font-weight:800; }
          .table-wrap { width:100%; overflow-x:auto; border-radius:16px; border:1px solid var(--line); background:rgba(11,19,29,0.82); margin-top:14px; }
          table { width:100%; min-width:1120px; border-collapse:collapse; font-size:14px; }
          th, td { text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
          th { color:var(--muted); font-weight:600; }
          button { border-radius:999px; border:1px solid transparent; padding:10px 14px; font:inherit; font-weight:800; background:linear-gradient(135deg, rgba(61,217,182,0.88), rgba(82,168,255,0.82)); color:#03131f; cursor:pointer; }
          @media (max-width: 960px) { .app { grid-template-columns:1fr; } .sidebar { position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); } .main { padding:20px 16px 36px; } .hero-grid { grid-template-columns:1fr; } }
        """
)


WINDOW_PILL_STYLE = (
    ("""
          """ + (ROOT_TOKENS_BASE) + """
          * { box-sizing:border-box; }
          body { margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }
          a { color:inherit; text-decoration:none; }
          .app { display:grid; grid-template-columns:248px minmax(0,1fr); min-height:100vh; }
          """)
    + WORKSPACE_SIDEBAR_STYLE
    + """
          .main { padding:20px 18px 28px; }
          .wrap { max-width:1108px; margin:0 auto; }
          .toolbar { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }
          .pill,.cta { display:inline-flex; align-items:center; justify-content:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.7); color:var(--ink); font-size:13px; font-weight:800; }
          .card { background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); }
          .eyebrow { display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }
          h2 { margin:0 0 8px; font-size:28px; }
          .muted { color:var(--muted); font-size:14px; line-height:1.55; }
          .table-wrap { width:100%; overflow-x:auto; border-radius:16px; border:1px solid var(--line); background:rgba(11,19,29,0.82); margin-top:14px; }
          table { width:100%; min-width:880px; border-collapse:collapse; font-size:14px; }
          th, td { text-align:left; padding:12px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
          th { color:var(--muted); font-weight:700; }
          tbody tr:hover { background:rgba(61,217,182,0.05); }
          @media (max-width: 960px) { .app { grid-template-columns:1fr; } .sidebar { position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); } .main { padding:20px 16px 36px; } }
        """
)


REPORT_POOL_TABLE_ROWS_STYLE = (
    ("""
          """ + (ROOT_TOKENS_BASE) + """
          * { box-sizing:border-box; }
          body { margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }
          a { color:inherit; text-decoration:none; }
          .app { display:grid; grid-template-columns:248px minmax(0,1fr); min-height:100vh; }
          """)
    + WORKSPACE_SIDEBAR_STYLE
    + """
          .main { padding:20px 18px 28px; }
          .wrap { max-width:1108px; margin:0 auto; }
          .toolbar { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }
          .pill { display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.7); color:var(--ink); font-size:13px; font-weight:800; }
          .card { background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); margin-bottom:16px; }
          .eyebrow { display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }
          h2 { margin:0 0 8px; font-size:28px; }
          .muted { color:var(--muted); font-size:14px; line-height:1.55; }
          .table-wrap { width:100%; overflow-x:auto; border-radius:16px; border:1px solid var(--line); background:rgba(11,19,29,0.82); margin-top:14px; }
          table { width:100%; min-width:980px; border-collapse:collapse; font-size:14px; }
          th, td { text-align:left; padding:12px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
          th { color:var(--muted); font-weight:700; }
          textarea { width:100%; min-height:360px; border:1px solid var(--line); border-radius:16px; padding:14px; font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace; background:rgba(21,34,49,0.72); color:var(--ink); }
          @media (max-width: 960px) { .app { grid-template-columns:1fr; } .sidebar { position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); } .main { padding:20px 16px 36px; } }
        """
)


DASHBOARD_AI_DAILY_REPORT_MESSAGE_STYLE = (
    ("""
          """ + (ROOT_TOKENS_BASE) + """
          * { box-sizing:border-box; }
          body { margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.16), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.12), transparent 26%),linear-gradient(180deg, #08111a 0%, #071018 100%); }
          a { color:inherit; text-decoration:none; }
          .app { display:grid; grid-template-columns:248px minmax(0,1fr); min-height:100vh; }
          """)
    + WORKSPACE_SIDEBAR_STYLE
    + """
          .main { padding:20px 18px 28px; }
          .wrap { max-width:980px; margin:0 auto; }
          .toolbar { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-bottom:16px; }
          .pill { display:inline-flex; align-items:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.7); color:var(--ink); font-size:13px; font-weight:700; }
          .card { background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); border:1px solid var(--line); border-radius:24px; padding:22px; box-shadow:0 18px 40px rgba(0,0,0,0.22); }
          .eyebrow { display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:800; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:12px; }
          .muted { color:var(--muted); font-size:14px; line-height:1.55; }
          textarea { width:100%; min-height:420px; border:1px solid var(--line); border-radius:16px; padding:14px; font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace; background:rgba(21,34,49,0.72); color:var(--ink); }
          @media (max-width: 960px) { .app { grid-template-columns:1fr; } .sidebar { position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); } .main { padding:20px 16px 36px; } }
        """
)
