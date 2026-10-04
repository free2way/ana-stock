"""Page-level CSS constants extracted from app/api/routes/screener.py.

Pure static presentation data (no financial decisions). Shared workspace
chrome styles live in app/services/workspace_nav.py and are composed here.
"""

# pylint: disable=line-too-long  # CSS kept byte-identical to the original inline blocks

from app.services.workspace_nav import WORKSPACE_COMPACT_STYLE, WORKSPACE_SIDEBAR_STYLE
from app.api.presentation.tokens import (
    ROOT_TOKENS_BASE_PANEL2,
    ROOT_TOKENS_BASE_SOFT,
    ROOT_TOKENS_LAB,
    ROOT_TOKENS_SOFT_MULTILINE,
)


FACTOR_LAB_PAGE_STYLE = (
    """
        """
    + WORKSPACE_SIDEBAR_STYLE
    + """
        """
    + WORKSPACE_COMPACT_STYLE
    + ("""
        """ + (ROOT_TOKENS_LAB) + """
        body { margin:0; background:radial-gradient(circle at 20% 0%, rgba(53,224,180,.14), transparent 28%), linear-gradient(135deg,#07111b,#0a1724 58%,#07111b); color:var(--ink); font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
        .workspace-shell { gap:12px; }
        .workspace-main { max-width:1480px; }
        .hero { display:grid; gap:16px; grid-template-columns:minmax(0,1.5fr) minmax(320px,.8fr); align-items:stretch; margin-bottom:14px; }
        .card { background:linear-gradient(180deg, rgba(17,31,45,.96), rgba(10,20,31,.96)); border:1px solid var(--line); border-radius:22px; padding:18px; box-shadow:0 18px 40px rgba(0,0,0,.22); }
        .section-head { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:12px; }
        .eyebrow { color:var(--accent); font-size:12px; font-weight:900; letter-spacing:.12em; text-transform:uppercase; }
        h1,h2,h3 { margin:0; }
        h1 { font-size:34px; letter-spacing:-.04em; }
        h2 { font-size:20px; letter-spacing:-.02em; }
        p { color:var(--muted); line-height:1.6; margin:8px 0 0; }
        .grid { display:grid; gap:14px; grid-template-columns:repeat(2, minmax(0, 1fr)); }
        .muted { color:var(--muted); }
        .mini { font-size:12px; margin-top:4px; }
        .mini-button { padding:6px 9px; font-size:12px; white-space:nowrap; }
        .pill,.chip { display:inline-flex; align-items:center; gap:6px; border-radius:999px; padding:6px 10px; background:rgba(53,224,180,.10); color:var(--accent); font-weight:800; font-size:12px; border:1px solid rgba(53,224,180,.20); }
        .chip { margin:4px 6px 4px 0; color:#dffdf5; background:rgba(255,255,255,.06); border-color:rgba(255,255,255,.09); }
        .toolbar { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-top:14px; }
        a.button, button { border:0; border-radius:14px; padding:11px 14px; background:var(--accent); color:#04231b; font-weight:900; text-decoration:none; cursor:pointer; }
        a.secondary, button.secondary { background:#18293a; color:var(--ink); border:1px solid var(--line); }
        select,input { width:100%; box-sizing:border-box; border:1px solid var(--line); background:#081522; color:var(--ink); border-radius:12px; padding:10px 12px; }
        label { display:block; color:var(--muted); font-size:12px; font-weight:800; margin-bottom:6px; }
        .form-grid { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:12px; }
        table { width:100%; border-collapse:collapse; min-width:920px; }
        th,td { text-align:left; padding:10px 9px; border-bottom:1px solid var(--line); vertical-align:top; }
        th { color:var(--muted); font-size:12px; white-space:nowrap; }
        td { font-size:13px; }
        .table-wrap { overflow:auto; border:1px solid var(--line); border-radius:16px; background:rgba(8,18,29,.62); }
        .compact-table table { min-width:1050px; }
        .notice { border:1px solid rgba(53,224,180,.25); background:rgba(53,224,180,.08); color:#c9fff2; border-radius:16px; padding:12px 14px; margin-bottom:14px; }
        .status-ready { color:var(--accent); font-weight:900; }
        .status-wait { color:var(--warn); font-weight:900; }
        @media (max-width: 980px) { .hero,.grid,.form-grid { grid-template-columns:1fr; } .workspace-shell { display:block; } }
      """)
)


SELECTION_QUALITY_PAGE_STYLE = (
    """
        """
    + WORKSPACE_SIDEBAR_STYLE
    + """
        """
    + WORKSPACE_COMPACT_STYLE
    + ("""
        """ + (ROOT_TOKENS_LAB) + """
        body { margin:0; background:radial-gradient(circle at 18% 0%, rgba(53,224,180,.13), transparent 28%), linear-gradient(135deg,#07111b,#0a1724 58%,#07111b); color:var(--ink); font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
        .workspace-shell { gap:12px; }
        .workspace-main { max-width:1480px; }
        .hero { display:grid; gap:14px; grid-template-columns:minmax(0,1.35fr) minmax(320px,.75fr); margin-bottom:14px; }
        .card { background:linear-gradient(180deg, rgba(17,31,45,.96), rgba(10,20,31,.96)); border:1px solid var(--line); border-radius:22px; padding:18px; box-shadow:0 18px 40px rgba(0,0,0,.22); }
        .section-head { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:12px; }
        .eyebrow { color:var(--accent); font-size:12px; font-weight:900; letter-spacing:.12em; text-transform:uppercase; }
        h1,h2 { margin:0; letter-spacing:-.03em; }
        h1 { font-size:34px; }
        h2 { font-size:20px; }
        p,li { color:var(--muted); line-height:1.58; }
        .metric-grid { display:grid; gap:10px; grid-template-columns:repeat(4,minmax(0,1fr)); margin-top:14px; }
        .metric { border:1px solid var(--line); border-radius:16px; padding:12px; background:rgba(255,255,255,.04); }
        .metric b { display:block; font-size:22px; color:var(--ink); }
        .metric span,.muted { color:var(--muted); }
        .mini { font-size:12px; margin-top:4px; }
        .toolbar { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-top:14px; }
        a.button, button { border:0; border-radius:14px; padding:11px 14px; background:var(--accent); color:#04231b; font-weight:900; text-decoration:none; cursor:pointer; }
        a.secondary, button.secondary { background:#18293a; color:var(--ink); border:1px solid var(--line); }
        table { width:100%; border-collapse:collapse; min-width:980px; }
        th,td { text-align:left; padding:10px 9px; border-bottom:1px solid var(--line); vertical-align:top; font-size:13px; }
        th { color:var(--muted); font-size:12px; white-space:nowrap; }
        .table-wrap { overflow:auto; border:1px solid var(--line); border-radius:16px; background:rgba(8,18,29,.62); }
        .risk-chip { display:inline-flex; margin:2px 4px 2px 0; border-radius:999px; padding:4px 8px; background:rgba(248,201,93,.12); color:#ffe4a3; border:1px solid rgba(248,201,93,.24); font-size:12px; font-weight:800; }
        a { color:#a7f3df; text-decoration:none; }
        @media (max-width: 980px) { .hero,.metric-grid { grid-template-columns:1fr; } .workspace-shell { display:block; } }
      """)
)


OUTCOME_CELL_STYLE = (
    """
        """
    + WORKSPACE_SIDEBAR_STYLE
    + """
        """
    + WORKSPACE_COMPACT_STYLE
    + """
        :root { --bg:#07111b; --panel:#0d1824; --line:#213447; --ink:#edf5f2; --muted:#8da1ad; --accent:#35e0b4; --warn:#f8c95d; --bad:#fb7185; --good:#6ee7b7; }
        body { margin:0; color:var(--ink); background:radial-gradient(circle at 16% 0%, rgba(53,224,180,.13), transparent 30%), #07111b; font-family:ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
        .workspace-shell { gap:12px; }
        .workspace-main { max-width:1500px; }
        .card { background:linear-gradient(180deg, rgba(17,31,45,.96), rgba(10,20,31,.96)); border:1px solid var(--line); border-radius:22px; padding:18px; box-shadow:0 18px 40px rgba(0,0,0,.22); margin-bottom:14px; }
        .section-head { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:12px; }
        .eyebrow { color:var(--accent); font-size:12px; font-weight:900; letter-spacing:.12em; text-transform:uppercase; }
        h1,h2 { margin:0; }
        h1 { font-size:32px; letter-spacing:-.04em; }
        p { color:var(--muted); line-height:1.6; margin:8px 0 0; }
        .toolbar { display:flex; gap:10px; flex-wrap:wrap; align-items:center; margin-top:14px; }
        a.button, button { border:0; border-radius:14px; padding:10px 13px; background:var(--accent); color:#04231b; font-weight:900; text-decoration:none; cursor:pointer; }
        a.secondary, button.secondary { background:#18293a; color:var(--ink); border:1px solid var(--line); }
        .metric-grid { display:grid; gap:10px; grid-template-columns:repeat(auto-fit, minmax(160px, 1fr)); margin-top:14px; }
        .metric { border:1px solid rgba(255,255,255,.07); background:rgba(8,18,29,.62); border-radius:16px; padding:12px; }
        .metric span { color:var(--muted); font-size:12px; }
        .metric strong { display:block; font-size:22px; margin-top:5px; }
        .muted { color:var(--muted); }
        .mini { font-size:12px; margin-top:4px; }
        .pill,.chip { display:inline-flex; align-items:center; gap:6px; border-radius:999px; padding:6px 10px; background:rgba(53,224,180,.10); color:var(--accent); font-weight:800; font-size:12px; border:1px solid rgba(53,224,180,.20); margin:3px 4px 3px 0; }
        .chip { color:#dffdf5; background:rgba(255,255,255,.06); border-color:rgba(255,255,255,.09); }
        .table-wrap { overflow:auto; border:1px solid var(--line); border-radius:16px; background:rgba(8,18,29,.62); }
        table { width:100%; border-collapse:collapse; min-width:1260px; }
        th,td { text-align:left; padding:10px 9px; border-bottom:1px solid var(--line); vertical-align:top; font-size:13px; }
        th { color:var(--muted); font-size:12px; white-space:nowrap; }
        .sticky-col { position:sticky; left:0; z-index:2; background:#0d1824; min-width:150px; box-shadow:10px 0 16px rgba(0,0,0,.12); }
        th.sticky-col { z-index:4; }
        .ticker-link { color:var(--accent); font-weight:900; text-decoration:none; }
        .pos { color:var(--good); font-weight:900; }
        .neg { color:var(--bad); font-weight:900; }
        .flat { color:var(--muted); font-weight:900; }
      """
)


TEMPLATE_CARD_HREF_STYLE = (
    ("""
          """ + (ROOT_TOKENS_SOFT_MULTILINE) + """
          * { box-sizing: border-box; }
          body {
            margin: 0;
            font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            color: var(--ink);
            background:
              radial-gradient(circle at top left, rgba(82,168,255,0.14) 0, transparent 28%),
              radial-gradient(circle at top right, rgba(61,217,182,0.10) 0, transparent 26%),
              var(--bg);
          }
          """)
    + WORKSPACE_COMPACT_STYLE
    + """
          """
    + WORKSPACE_SIDEBAR_STYLE
    + """
          .content { padding:16px 14px 24px; }
          .wrap { max-width:none; margin:0; padding: 0 0 36px; }
          .toolbar { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin-bottom:12px; }
          .toolbar a { color: var(--accent); text-decoration:none; font-weight:700; }
          .nav-grid { display:grid; gap:8px; grid-template-columns:repeat(auto-fit, minmax(210px, 1fr)); margin-bottom:10px; }
          .nav-card {
            display:block;
            text-decoration:none;
            color:inherit;
            background:linear-gradient(180deg, rgba(17,28,40,0.98) 0%, rgba(21,34,49,0.98) 100%);
            border:1px solid var(--line);
            border-radius:10px;
            padding:10px;
            box-shadow:0 8px 18px rgba(0,0,0,0.10);
          }
          .nav-card:hover { border-color:var(--accent); box-shadow:0 12px 28px rgba(61,217,182,0.08); }
          .nav-head { display:flex; align-items:center; gap:8px; margin-bottom:5px; }
          .nav-icon {
            width:30px; height:30px; border-radius:9px; display:inline-flex; align-items:center; justify-content:center;
            background:rgba(61,217,182,0.10); color:var(--accent); font-size:11px; font-weight:900; letter-spacing:0.04em; border:1px solid rgba(61,217,182,0.18); flex:0 0 auto;
          }
          .nav-title { font-size:14px; font-weight:800; color:var(--ink); }
          .nav-kicker { color:var(--muted); font-size:10px; font-weight:700; letter-spacing:0.04em; text-transform:uppercase; }
          h1 { margin:0 0 6px; font-size:32px; }
          .lead { margin:0; color:var(--muted); max-width:760px; }
          .section-stack { display:grid; gap:12px; }
          .workbench-grid {
            display:grid;
            gap:12px;
            grid-template-columns:repeat(auto-fit, minmax(220px, 1fr));
            margin:12px 0;
          }
          .workbench-card {
            display:grid;
            gap:10px;
            min-height:112px;
            padding:12px;
            border-radius:12px;
            border:1px solid rgba(61,217,182,0.18);
            background:
              linear-gradient(135deg, rgba(61,217,182,0.12), rgba(82,168,255,0.07)),
              rgba(11,19,29,0.88);
            color:inherit;
            text-decoration:none;
            box-shadow:0 14px 34px rgba(0,0,0,0.18);
          }
          .workbench-card:hover { border-color:var(--accent); transform:translateY(-1px); }
          .workbench-code {
            width:max-content;
            display:inline-flex;
            align-items:center;
            padding:6px 10px;
            border-radius:999px;
            background:rgba(61,217,182,0.12);
            color:var(--accent);
            font-size:11px;
            font-weight:900;
            letter-spacing:0.06em;
          }
          .workbench-card strong { font-size:15px; color:var(--ink); }
          .workbench-card span:last-child { color:var(--muted); font-size:12px; line-height:1.38; }
          .strategy-template-section { display:grid; gap:12px; margin-bottom:12px; }
          .strategy-template-grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fit, minmax(260px, 1fr)); }
          .strategy-template-card {
            display:grid;
            gap:9px;
            align-content:start;
            min-height:170px;
            padding:12px;
            border-radius:12px;
            border:1px solid var(--line);
            background:linear-gradient(180deg, rgba(17,28,40,0.98), rgba(12,21,32,0.96));
            box-shadow:0 12px 28px rgba(0,0,0,0.14);
          }
          .strategy-template-title { font-size:15px; font-weight:900; color:var(--ink); }
          .strategy-meta-row { display:flex; flex-wrap:wrap; gap:8px; }
          .strategy-template-actions { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-top:auto; }
          .run-receipt-card { background:linear-gradient(180deg, rgba(17,28,40,0.98), rgba(9,17,27,0.96)); border-color:rgba(82,168,255,0.20); }
          .receipt-grid { display:grid; gap:10px; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); margin-top:12px; }
          .receipt-row {
            display:grid;
            gap:6px;
            min-width:0;
            padding:12px 13px;
            border-radius:14px;
            border:1px solid rgba(255,255,255,0.05);
            background:rgba(11,19,29,0.82);
          }
          .receipt-row span { color:var(--muted); font-size:11px; font-weight:900; letter-spacing:0.04em; text-transform:uppercase; }
          .receipt-row strong { color:var(--ink); font-size:13px; line-height:1.45; word-break:break-word; overflow-wrap:anywhere; }
          .template-group-stack { display:grid; gap:18px; margin-top:12px; }
          .template-grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); margin-top:12px; }
          .multi-template-stack { display:grid; gap:14px; margin-top:12px; }
          .multi-template-grid { display:grid; gap:10px; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); margin-top:12px; }
          .multi-template-chip {
            display:flex;
            align-items:center;
            gap:10px;
            padding:12px 14px;
            border-radius:14px;
            border:1px solid var(--line);
            background:rgba(11,19,29,0.82);
            color:var(--ink);
          }
          .multi-template-chip input {
            width:18px;
            min-width:18px;
            height:18px;
            margin:0;
            padding:0;
          }
          .multi-template-chip span {
            font-size:13px;
            font-weight:700;
            line-height:1.35;
          }
          .template-card {
            display:grid;
            gap:10px;
            padding:14px;
            border-radius:15px;
            border:1px solid var(--line);
            background:rgba(11,19,29,0.82);
            color:inherit;
            text-decoration:none;
          }
          .template-card.active { border-color:rgba(61,217,182,0.34); background:linear-gradient(180deg, rgba(61,217,182,0.14), rgba(82,168,255,0.08)); }
          .template-top { display:flex; align-items:center; justify-content:space-between; gap:8px; }
          .template-mode, .template-market, .default-chip {
            display:inline-flex; align-items:center; padding:6px 10px; border-radius:999px; font-size:11px; font-weight:800;
          }
          .template-mode { background:rgba(61,217,182,0.10); color:var(--accent); text-transform:uppercase; }
          .template-market { background:rgba(82,168,255,0.10); color:#9acbff; }
          .template-title { font-size:16px; font-weight:800; color:var(--ink); }
          .template-desc { color:var(--muted); font-size:13px; line-height:1.5; }
          .default-chip { background:rgba(246,200,95,0.10); color:#ffd982; }
          details.advanced-panel { border-top:1px solid var(--line); padding-top:12px; margin-top:12px; }
          details.advanced-panel > summary { cursor:pointer; list-style:none; font-weight:800; color:var(--ink); }
          details.advanced-panel > summary::-webkit-details-marker { display:none; }
          details.research-fold { margin:0 0 12px; border:1px solid var(--line); border-radius:10px; background:rgba(11,19,29,0.52); }
          details.research-fold > summary { display:flex; justify-content:space-between; gap:12px; align-items:center; padding:11px 12px; cursor:pointer; list-style:none; font-weight:850; }
          details.research-fold > summary::-webkit-details-marker { display:none; }
          details.research-fold > summary::after { content:"+"; color:var(--accent); font-size:18px; }
          details.research-fold[open] > summary::after { content:"−"; }
          .research-fold-body { display:grid; gap:10px; padding:0 10px 10px; }
          .market-scope-switch { display:flex; gap:8px; flex-wrap:wrap; margin:0 0 12px; }
          .market-scope-option { display:inline-flex; align-items:center; min-height:34px; padding:6px 11px; border:1px solid var(--line); border-radius:999px; color:var(--muted); text-decoration:none; font-size:12px; font-weight:850; background:rgba(11,19,29,0.54); }
          .market-scope-option.active { color:var(--accent); border-color:rgba(61,217,182,0.38); background:rgba(61,217,182,0.12); }
          .screen-flow { display:grid; gap:10px; grid-template-columns:repeat(3, minmax(0, 1fr)); margin:14px 0 8px; }
          .flow-step { display:grid; gap:7px; min-width:0; padding:12px; border:1px solid var(--line); border-radius:12px; background:rgba(11,19,29,0.56); }
          .flow-step-num { color:var(--accent); font-size:10px; font-weight:900; letter-spacing:0.08em; }
          .flow-step b { font-size:13px; }
          .flow-step small { color:var(--muted); font-size:11px; line-height:1.35; }
          .flow-step .run-screen { align-self:end; min-height:38px; background:linear-gradient(180deg, rgba(61,217,182,0.28), rgba(61,217,182,0.14)); border-color:rgba(61,217,182,0.34); color:var(--ink); font-weight:900; }
          details.selection-advanced { margin-top:12px; border:1px solid var(--line); border-radius:12px; background:rgba(11,19,29,0.42); }
          details.selection-advanced > summary { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:11px 12px; cursor:pointer; list-style:none; font-weight:850; }
          details.selection-advanced > summary::-webkit-details-marker { display:none; }
          details.selection-advanced > summary::after { content:"+"; color:var(--accent); font-size:18px; }
          details.selection-advanced[open] > summary::after { content:"−"; }
          .selection-advanced-body { padding:0 12px 12px; }
          .summary-note { color:var(--muted); font-size:13px; margin-top:8px; }
          .rules-grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); align-items:end; }
          .action-grid { display:grid; gap:12px; grid-template-columns:repeat(2, minmax(0, 1fr)); }
          .action-form {
            display:grid;
            gap:10px;
            align-content:start;
            height:100%;
            padding:14px;
            border:1px solid var(--line);
            border-radius:16px;
            background:rgba(11,19,29,0.82);
          }
          .action-head {
            display:flex;
            align-items:flex-start;
            justify-content:space-between;
            gap:12px;
          }
          .action-kicker {
            color:var(--muted);
            font-size:11px;
            font-weight:800;
            letter-spacing:0.06em;
            text-transform:uppercase;
            margin-bottom:4px;
          }
          .action-title {
            color:var(--ink);
            font-size:16px;
            font-weight:800;
            line-height:1.2;
          }
          .action-icon {
            width:36px;
            height:36px;
            border-radius:12px;
            background:#eef8f5;
            border:1px solid #cde9e4;
            color:#0f766e;
            display:inline-flex;
            align-items:center;
            justify-content:center;
            font-size:11px;
            font-weight:900;
            letter-spacing:0.05em;
            flex:0 0 auto;
          }
          .action-label {
            color:var(--muted);
            font-size:12px;
            font-weight:800;
            letter-spacing:0.04em;
            text-transform:uppercase;
            min-height:28px;
            display:flex;
            align-items:flex-end;
          }
          .action-row {
            display:grid;
            grid-template-columns:minmax(0, 1fr);
            gap:10px;
          }
          .action-input-wrap {
            min-height:42px;
            display:flex;
            align-items:center;
          }
          .action-checkbox {
            display:flex;
            align-items:center;
            gap:8px;
            min-height:42px;
            color:var(--muted);
            font-size:14px;
            font-weight:600;
          }
          .action-checkbox input {
            width:18px;
            min-width:18px;
            height:18px;
            margin:0;
            padding:0;
            border-radius:6px;
          }
          .action-submit {
            min-height:42px;
            display:flex;
            align-items:flex-end;
          }
          .action-submit button {
            width:100%;
            min-width:0;
          }
          .results-toolbar {
            display:flex;
            align-items:center;
            justify-content:space-between;
            gap:12px;
            flex-wrap:wrap;
            margin-bottom:12px;
          }
          .results-toolbar form {
            margin:0;
          }
          .results-toolbar button {
            width:auto;
            min-width:140px;
          }
          input, select, button {
            border-radius:12px;
            border:1px solid var(--line);
            padding:10px 12px;
            font:inherit;
            background:rgba(11,19,29,0.82);
            color:var(--ink);
            width:100%;
            max-width:100%;
          }
          button { background:var(--accent); color:#fff; border-color:var(--accent); font-weight:700; }
          .muted { color:var(--muted); font-size:14px; }
          .table-wrap { width:100%; max-width:100%; overflow-x:auto; overflow-y:hidden; border-radius:14px; border:1px solid var(--line); background:rgba(11,19,29,0.82); padding-bottom:8px; scrollbar-gutter:stable both-edges; }
          .table-wrap::-webkit-scrollbar { height:12px; }
          .table-wrap::-webkit-scrollbar-track { background:#0f1823; border-radius:999px; }
          .table-wrap::-webkit-scrollbar-thumb { background:#32465d; border-radius:999px; border:2px solid #0f1823; }
          .table-wrap::-webkit-scrollbar-thumb:hover { background:#47627f; }
          table { width:100%; border-collapse:collapse; min-width:1660px; font-size:14px; table-layout:auto; }
          th, td { text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; white-space:nowrap; }
          th { color:var(--muted); font-weight:600; white-space:nowrap; }
          td { white-space:nowrap; }
          .table-wrap th:nth-child(7), .table-wrap td:nth-child(7) { min-width:220px; width:220px; }
          .table-wrap th:nth-child(8), .table-wrap td:nth-child(8) { min-width:120px; width:120px; }
          .table-wrap th:nth-child(9), .table-wrap td:nth-child(9) { min-width:100px; width:100px; }
          .table-wrap th:nth-child(18), .table-wrap td:nth-child(18) { min-width:110px; width:110px; }
          .sticky-col { position:sticky; background:var(--panel); z-index:2; }
          th.sticky-col { z-index:4; }
          .sticky-col-1 { left:0; min-width:124px; box-shadow: 10px 0 14px rgba(31,41,55,0.05); }
          .sticky-col-2 { left:124px; min-width:180px; box-shadow: 10px 0 14px rgba(31,41,55,0.05); }
          .sticky-col-3 { left:304px; min-width:120px; box-shadow: 10px 0 14px rgba(31,41,55,0.04); }
          .market-section-row td { background:#132031; color:var(--accent); font-weight:800; letter-spacing:0.03em; border-top:1px solid var(--line); }
          .detail-row td { white-space:normal; background:#0d1722; padding:12px 10px 14px; }
          .row-detail-toggle > summary {
            cursor:pointer;
            color:var(--accent);
            font-weight:800;
            list-style:none;
            margin-bottom:10px;
            display:flex;
            align-items:center;
            justify-content:space-between;
            gap:12px;
            padding:10px 12px;
            border:1px dashed #294256;
            border-radius:14px;
            background:#0f1823;
          }
          .row-detail-toggle > summary::-webkit-details-marker { display:none; }
          .detail-summary-meta { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-left:auto; }
          .detail-summary-price {
            display:inline-flex;
            align-items:center;
            padding:5px 10px;
            border-radius:999px;
            background:#f3f4f6;
            color:#374151;
            font-weight:800;
            font-size:12px;
          }
          .detail-grid { display:grid; gap:10px; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); }
          .detail-card {
            border:1px solid var(--line);
            border-radius:14px;
            background:#111c28;
            padding:12px;
            min-width:0;
            overflow:hidden;
          }
          .detail-card-wide { grid-column:span 2; }
          .detail-card-action { display:flex; flex-direction:column; justify-content:space-between; }
          .detail-action-stack { display:grid; gap:10px; align-items:start; }
          .detail-chip-row { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }
          .detail-label {
            color:var(--muted);
            font-size:12px;
            font-weight:800;
            letter-spacing:0.04em;
            text-transform:uppercase;
            margin-bottom:8px;
          }
          .detail-value { color:var(--ink); line-height:1.5; white-space:normal; }
          .detail-link {
            display:inline-flex;
            align-items:center;
            justify-content:center;
            padding:10px 14px;
            border-radius:12px;
            background:rgba(61,217,182,0.10);
            color:var(--accent);
            font-weight:800;
            text-decoration:none;
          }
          .main-open-link {
            display:inline-flex;
            align-items:center;
            justify-content:center;
            padding:6px 9px;
            border-radius:999px;
            background:rgba(61,217,182,0.10);
            color:var(--accent);
            text-decoration:none;
            font-weight:800;
            font-size:12px;
            white-space:nowrap;
          }
          .row-action-stack {
            display:flex;
            align-items:center;
            gap:8px;
            flex-wrap:wrap;
            min-width:220px;
          }
          .row-action-stack form { margin:0; }
          .row-action-stack button {
            width:auto;
            min-width:0;
            padding:6px 9px;
            border-radius:999px;
            font-size:12px;
            white-space:nowrap;
          }
          .mobile-result-list, .mobile-preset-list { display:none; gap:10px; }
          .mobile-result-card, .mobile-preset-card {
            border:1px solid var(--line);
            border-radius:14px;
            background:rgba(11,19,29,0.82);
            padding:12px;
          }
          .mobile-result-head {
            display:flex;
            justify-content:space-between;
            gap:10px;
            align-items:flex-start;
          }
          .mobile-result-ticker a { color:var(--accent); font-size:15px; font-weight:800; text-decoration:none; }
          .mobile-result-price { display:flex; align-items:flex-start; justify-content:flex-end; }
          .mobile-result-chip-row { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }
          .mobile-result-grid {
            display:grid;
            gap:8px;
            grid-template-columns:repeat(2, minmax(0, 1fr));
            margin-top:10px;
          }
          .mobile-result-grid > div {
            border:1px solid rgba(255,255,255,0.04);
            border-radius:12px;
            background:rgba(21,34,49,0.9);
            padding:10px 12px;
            min-width:0;
          }
          .mobile-result-summary {
            margin-top:10px;
            color:var(--muted);
            font-size:13px;
            line-height:1.5;
            white-space:normal;
          }
          .mobile-result-meta {
            margin-top:8px;
            color:var(--muted);
            font-size:12px;
            line-height:1.5;
            white-space:normal;
          }
          .mobile-result-actions {
            display:grid;
            gap:8px;
            margin-top:10px;
          }
          .detail-collapse-note {
            margin-top:10px;
            color:var(--muted);
            font-size:12px;
            font-weight:700;
          }
          .scroll-hint { margin-top:10px; font-size:12px; color:var(--muted); }
          .lang-switch { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-left:auto; }
          @media (max-width: 920px) {
            .rules-grid { grid-template-columns:1fr; }
            .action-grid { grid-template-columns:1fr; }
            .screen-flow { grid-template-columns:repeat(2, minmax(0, 1fr)); }
            h1 { font-size:30px; }
            .sticky-col, .sticky-col-1, .sticky-col-2 { position:static; box-shadow:none; min-width:auto; }
            .table-wrap th:nth-child(7), .table-wrap td:nth-child(7),
            .table-wrap th:nth-child(8), .table-wrap td:nth-child(8),
            .table-wrap th:nth-child(9), .table-wrap td:nth-child(9),
            .table-wrap th:nth-child(18), .table-wrap td:nth-child(18) { width:auto; min-width:unset; }
            .detail-card-wide { grid-column:span 1; }
            .row-detail-toggle > summary { align-items:flex-start; flex-direction:column; }
            .detail-summary-meta { margin-left:0; }
          }
          @media (max-width: 720px) {
            .results-table-wrap, .saved-strategies-wrap { display:none; }
            .mobile-result-list, .mobile-preset-list { display:grid; }
            .results-toolbar { align-items:flex-start; }
            .screen-flow { grid-template-columns:1fr; }
          }
          @media (max-width: 1120px) {
            .app { grid-template-columns:1fr; }
            .sidebar { position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }
            .content { padding:20px 10px 40px; }
          }
        """
)


FILTER_HREF_STYLE = (
    ("""
          """ + (ROOT_TOKENS_BASE_PANEL2) + """
          * { box-sizing:border-box; }
          body { margin:0; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.15), transparent 28%),radial-gradient(circle at bottom right, rgba(61,217,182,0.10), transparent 26%),var(--bg); }
          a { color:inherit; text-decoration:none; }
          """)
    + WORKSPACE_COMPACT_STYLE
    + """
          """
    + WORKSPACE_SIDEBAR_STYLE
    + """
          .content { padding:16px 14px 28px; }
          .wrap { max-width:none; margin:0; }
          .toolbar,.chip-row { display:flex; align-items:center; flex-wrap:wrap; gap:10px; margin-bottom:12px; }
          .pill,.filter-chip { display:inline-flex; align-items:center; justify-content:center; padding:8px 12px; border-radius:999px; border:1px solid var(--line); background:rgba(17,28,40,0.72); color:var(--muted); font-size:13px; font-weight:800; }
          .filter-chip.active,.pill.primary { color:#06131b; border-color:var(--accent); background:var(--accent); }
          .card { border:1px solid var(--line); border-radius:18px; background:linear-gradient(180deg, rgba(17,28,40,0.96), rgba(12,21,31,0.94)); padding:16px; margin-bottom:12px; box-shadow:0 14px 34px rgba(0,0,0,0.18); }
          .hero { display:grid; grid-template-columns:minmax(0,1.2fr) minmax(280px,0.8fr); gap:12px; }
          .eyebrow { display:inline-flex; padding:6px 10px; border-radius:999px; background:rgba(61,217,182,0.12); color:var(--accent); font-size:12px; font-weight:900; letter-spacing:0.04em; text-transform:uppercase; margin-bottom:10px; }
          h1 { margin:0 0 8px; font-size:32px; letter-spacing:-0.03em; }
          .lead,.muted { color:var(--muted); line-height:1.5; }
          .metric-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }
          .metric { border:1px solid rgba(144,163,184,0.12); border-radius:14px; background:rgba(11,19,29,0.72); padding:12px; }
          .metric strong { display:block; font-size:24px; margin-top:4px; }
          .table-wrap { width:100%; overflow:auto; border-radius:16px; border:1px solid var(--line); background:rgba(11,19,29,0.8); }
          table { width:100%; min-width:1280px; border-collapse:collapse; font-size:13px; }
          th,td { padding:10px 9px; text-align:left; vertical-align:top; border-bottom:1px solid var(--line); white-space:nowrap; }
          th { color:var(--muted); font-weight:800; }
          td .muted { font-size:12px; white-space:normal; max-width:360px; }
          .sticky-col { position:sticky; left:0; z-index:2; background:var(--panel); box-shadow:10px 0 16px rgba(0,0,0,0.14); }
          th.sticky-col { z-index:4; }
          .sticky-col-1 { min-width:170px; }
          .ticker-link { color:var(--accent); font-weight:900; }
          .status-badge,.decision-chip { display:inline-flex; align-items:center; padding:5px 9px; border-radius:999px; font-size:12px; font-weight:900; border:1px solid rgba(144,163,184,0.2); }
          .status-badge.ready,.decision-chip.support { color:#9ff3d5; border-color:rgba(61,217,182,0.32); background:rgba(61,217,182,0.08); }
          .status-badge.pending,.decision-chip.neutral { color:#ffd08a; border-color:rgba(255,190,92,0.32); background:rgba(255,190,92,0.08); }
          .status-badge.skipped { color:#b9c7d7; background:rgba(144,163,184,0.08); }
          .status-badge.failed,.decision-chip.avoid { color:#ffaaa5; border-color:rgba(239,68,68,0.32); background:rgba(239,68,68,0.08); }
          form.inline { display:inline-flex; align-items:center; gap:8px; margin:0; }
          input,select,button { border-radius:999px; border:1px solid var(--line); padding:8px 12px; background:rgba(11,19,29,0.82); color:var(--ink); font:inherit; font-weight:800; }
          button { background:var(--accent); color:#06131b; border-color:var(--accent); cursor:pointer; }
          @media (max-width:1120px) { .app { grid-template-columns:1fr; } .sidebar { position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); } .hero { grid-template-columns:1fr; } }
        """
)


TODAY_FOCUS_POOL_PAGE_STYLE = (
    ("""
          """ + (ROOT_TOKENS_BASE_SOFT) + """
          body { margin:0; font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.14) 0, transparent 28%),radial-gradient(circle at top right, rgba(61,217,182,0.10) 0, transparent 26%),var(--bg); }
          .app { display:grid; grid-template-columns:260px minmax(0, 1fr); min-height:100vh; }
          """)
    + WORKSPACE_SIDEBAR_STYLE
    + """
          .content { padding:28px; }
          .wrap { max-width:1108px; margin:0 auto; padding:0 0 40px; }
          .toolbar { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:16px; }
          .toolbar a { color:var(--accent); text-decoration:none; font-weight:700; }
          .card { background:linear-gradient(180deg, rgba(21,34,49,0.98), rgba(17,28,40,0.98)); border:1px solid var(--line); border-radius:22px; padding:18px; box-shadow:0 24px 48px rgba(0,0,0,0.18); margin-bottom:16px; }
          .eyebrow { display:inline-block; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:700; text-transform:uppercase; margin-bottom:12px; }
          .market-scope-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; margin-bottom:16px; }
          .market-scope-card { display:block; min-height:188px; padding:16px; border-radius:18px; background:rgba(11,19,29,0.74); border:1px solid rgba(34,50,70,0.92); transition:transform .16s ease, border-color .16s ease, background .16s ease; color:var(--ink); text-decoration:none; }
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
          .market-scope-trend-wrap { margin-top:12px; }
          .market-scope-trend-wrap span { display:block; color:var(--muted); font-size:11px; margin-bottom:8px; }
          .mini-trend { height:34px; display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:5px; align-items:end; }
          .mini-trend span { display:block; border-radius:999px 999px 3px 3px; background:linear-gradient(180deg, rgba(61,217,182,0.96), rgba(61,217,182,0.28)); box-shadow:0 6px 12px rgba(0,0,0,0.14); }
          .mini-trend.empty { grid-template-columns:1fr; align-items:center; }
          .mini-trend.empty span { height:auto; border-radius:0; background:none; box-shadow:none; color:var(--muted); font-size:11px; }
          .table-wrap { overflow-x:auto; border-radius:14px; border:1px solid var(--line); background:rgba(11,19,29,0.82); }
          table { width:100%; border-collapse:collapse; min-width:980px; }
          th, td { text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
          th { color:var(--muted); font-weight:700; }
          .main-open-link { display:inline-flex; align-items:center; justify-content:center; padding:6px 9px; border-radius:999px; background:rgba(61,217,182,0.10); color:var(--accent); text-decoration:none; font-weight:800; font-size:12px; }
          h1 { margin:0 0 8px; font-size:36px; }
          p { color:var(--muted); }
          @media (max-width: 1120px) {
            .app { grid-template-columns:1fr; }
            .sidebar { position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }
            .content { padding:20px 10px 40px; }
          }
        """
)


MARKET_SNAPSHOT_PAGE_STYLE = (
    ("""
          """ + (ROOT_TOKENS_BASE_SOFT) + """
          body { margin:0; font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color:var(--ink); background:radial-gradient(circle at top left, rgba(82,168,255,0.14) 0, transparent 28%),radial-gradient(circle at top right, rgba(61,217,182,0.10) 0, transparent 26%),var(--bg); }
          .app { display:grid; grid-template-columns:260px minmax(0, 1fr); min-height:100vh; }
          """)
    + WORKSPACE_SIDEBAR_STYLE
    + """
          .content { padding:28px; }
          .wrap { max-width:1200px; margin:0 auto; padding:0 0 56px; }
          .toolbar { display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:16px; }
          .toolbar a { color:var(--accent); text-decoration:none; font-weight:700; }
          .card { background:linear-gradient(180deg, rgba(21,34,49,0.98), rgba(17,28,40,0.98)); border:1px solid var(--line); border-radius:22px; padding:18px; box-shadow:0 24px 48px rgba(0,0,0,0.18); margin-bottom:16px; }
          .eyebrow { display:inline-block; padding:6px 10px; border-radius:999px; background:var(--accent-soft); color:var(--accent); font-size:12px; font-weight:700; text-transform:uppercase; margin-bottom:12px; }
          .market-scope-help { margin:12px 0 16px; padding:11px 12px; border-radius:14px; border:1px dashed rgba(61,217,182,0.24); background:rgba(61,217,182,0.06); color:var(--muted); font-size:12px; line-height:1.55; }
          .table-wrap { overflow-x:auto; border-radius:14px; border:1px solid var(--line); background:rgba(11,19,29,0.82); }
          table { width:100%; border-collapse:collapse; min-width:1080px; }
          th, td { text-align:left; padding:10px 8px; border-bottom:1px solid var(--line); vertical-align:top; }
          th { color:var(--muted); font-weight:700; white-space:nowrap; }
          .main-open-link { display:inline-flex; align-items:center; justify-content:center; padding:6px 9px; border-radius:999px; background:rgba(61,217,182,0.10); color:var(--accent); text-decoration:none; font-weight:800; font-size:12px; white-space:nowrap; }
          .muted { color:var(--muted); }
          h1 { margin:0 0 8px; font-size:36px; }
          p { color:var(--muted); }
          @media (max-width: 1120px) {
            .app { grid-template-columns:1fr; }
            .sidebar { position:relative; height:auto; border-right:none; border-bottom:1px solid var(--line); }
            .content { padding:20px 10px 40px; }
          }
        """
)
