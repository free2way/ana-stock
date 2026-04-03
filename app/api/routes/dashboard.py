import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.core.db import get_db_session
from app.services.repository import (
    BacktestRepository,
    DataJobRepository,
    ModelRunRepository,
    PredictionRepository,
    PriceSyncStateRepository,
)


router = APIRouter(prefix="/dashboard", tags=["dashboard"])


def _load_summary(db: Session) -> dict:
    model_repo = ModelRunRepository(db)
    signal_repo = PredictionRepository(db)
    backtest_repo = BacktestRepository(db)
    sync_repo = PriceSyncStateRepository(db)
    job_repo = DataJobRepository(db)

    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "latest_model": model_repo.get_latest_run_summary(),
        "recent_model_runs": model_repo.list_recent_runs(limit=8),
        "latest_signals": signal_repo.list_latest_predictions(limit=10),
        "latest_backtest": backtest_repo.get_latest_backtest_summary(),
        "latest_backtest_curve": backtest_repo.get_latest_backtest_curve(),
        "sync_states": sync_repo.list_states_with_symbols(),
        "recent_jobs": job_repo.list_recent_jobs(limit=8),
    }


@router.get("/summary")
def dashboard_summary(db: Session = Depends(get_db_session)) -> dict:
    return _load_summary(db)


@router.get("", response_class=HTMLResponse)
def dashboard_page(request: Request, db: Session = Depends(get_db_session)) -> str:
    summary = _load_summary(db)
    generated_at = summary["generated_at"]
    latest_model = summary["latest_model"]
    recent_model_runs = summary["recent_model_runs"]
    latest_backtest = summary["latest_backtest"]
    latest_backtest_curve = summary["latest_backtest_curve"]
    latest_signals = summary["latest_signals"]
    sync_states = summary["sync_states"]
    recent_jobs = summary["recent_jobs"]
    job_status = request.query_params.get("job_status")
    job_id = request.query_params.get("job_id")
    job_message = request.query_params.get("job_message")

    signal_items = "".join(
        f"<tr><td><a href='/symbols/{item['ticker']}'>{item['ticker']}</a></td><td>{item['trade_date']}</td><td>{item['score']:.6f}</td><td>{int(item['rank_value'])}</td></tr>"
        for item in latest_signals
    ) or "<tr><td colspan='4'>No signals yet</td></tr>"

    sync_items = "".join(
        f"<tr><td><a href='/symbols/{item['ticker']}'>{item['ticker']}</a></td><td>{item['provider']}</td><td>{item['last_synced_date'] or '-'}</td><td>{item['status'] or '-'}</td></tr>"
        for item in sync_states
    ) or "<tr><td colspan='4'>No sync history yet</td></tr>"

    backtest_pre = json.dumps(latest_backtest, indent=2) if latest_backtest else "No backtest yet"
    recent_model_rows = "".join(
        "<tr>"
        f"<td>{item['id']}</td>"
        f"<td>{item['name']}</td>"
        f"<td>{item['status']}</td>"
        f"<td><code>{item['config_json'] or '-'}</code></td>"
        f"<td>{item['created_at']}</td>"
        "<td>"
        f"<form action='/jobs/backtest' method='post' style='margin:0;'>"
        f"<input type='hidden' name='redirect_to' value='/dashboard' />"
        f"<input type='hidden' name='top_n' value='1' />"
        f"<input type='hidden' name='model_run_id' value='{item['id']}' />"
        f"<button type='submit' style='padding:8px 10px;font-size:12px;'>Backtest This Run</button>"
        "</form>"
        "</td>"
        "</tr>"
        for item in recent_model_runs
    ) or "<tr><td colspan='6'>No model runs yet</td></tr>"
    def status_badge(status: str) -> str:
        tone = {
            "success": ("#dcfce7", "#166534"),
            "failed": ("#fee2e2", "#991b1b"),
            "partial": ("#fef3c7", "#92400e"),
            "running": ("#dbeafe", "#1d4ed8"),
        }.get(status, ("#e5e7eb", "#374151"))
        return (
            f"<span style=\"display:inline-block;padding:4px 8px;border-radius:999px;"
            f"background:{tone[0]};color:{tone[1]};font-size:12px;font-weight:700;\">{status}</span>"
        )

    recent_job_rows = "".join(
        "<tr>"
        f"<td>{item['id']}</td>"
        f"<td>{item['job_type']}</td>"
        f"<td>{status_badge(item['status'])}</td>"
        f"<td>{item['started_at']}</td>"
        f"<td>{item['finished_at'] or '-'}</td>"
        f"<td><code>{json.dumps(item['params']) if item['params'] else '-'}</code></td>"
        f"<td>{item['message'] or '-'}</td>"
        "</tr>"
        for item in recent_jobs
    ) or "<tr><td colspan='7'>No jobs yet</td></tr>"

    curve_svg = "<div class='muted'>No backtest curve yet</div>"
    if latest_backtest_curve:
        width = 520
        height = 220
        left_pad = 18
        bottom_pad = 18
        top_pad = 12
        nav_values = [float(item["nav"]) for item in latest_backtest_curve if item["nav"] is not None]
        min_nav = min(nav_values)
        max_nav = max(nav_values)
        nav_span = max(max_nav - min_nav, 0.000001)
        step_x = (width - left_pad * 2) / max(len(latest_backtest_curve) - 1, 1)

        points = []
        labels = []
        for index, item in enumerate(latest_backtest_curve):
            nav = float(item["nav"])
            x = left_pad + index * step_x
            y = top_pad + (height - top_pad - bottom_pad) * (1 - ((nav - min_nav) / nav_span))
            points.append(f"{x:.2f},{y:.2f}")
            labels.append(
                f"<text x='{x:.2f}' y='{height - 2}' font-size='10' fill='#6b7280' text-anchor='middle'>{item['trade_date'][5:]}</text>"
            )

        curve_svg = f"""
        <svg viewBox="0 0 {width} {height}" width="100%" height="220" role="img" aria-label="Backtest NAV curve">
          <rect x="0" y="0" width="{width}" height="{height}" rx="14" fill="#f8faf7"></rect>
          <line x1="{left_pad}" y1="{height-bottom_pad}" x2="{width-left_pad}" y2="{height-bottom_pad}" stroke="#d6cfc2" />
          <polyline fill="none" stroke="#0f766e" stroke-width="3" points="{' '.join(points)}"></polyline>
          {''.join(labels)}
        </svg>
        """

    banner_html = ""
    if job_status or job_message:
        tone = {
            "success": ("#dcfce7", "#166534"),
            "failed": ("#fee2e2", "#991b1b"),
            "partial": ("#fef3c7", "#92400e"),
        }.get(job_status or "", ("#e5e7eb", "#374151"))
        banner_html = (
            f"<div style='margin-bottom:18px;padding:14px 16px;border-radius:16px;"
            f"background:{tone[0]};color:{tone[1]};font-weight:600;'>"
            f"Job {job_id or '-'} · {job_status or 'done'} · {job_message or 'Completed'}"
            f"</div>"
        )

    return f"""
    <!DOCTYPE html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>Personal Quant Workbench</title>
        <style>
          :root {{
            --bg: #f5efe2;
            --panel: #fffdf7;
            --ink: #1f2937;
            --muted: #6b7280;
            --line: #d6cfc2;
            --accent: #0f766e;
            --accent-soft: #dff5ef;
          }}
          * {{ box-sizing: border-box; }}
          body {{
            margin: 0;
            font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            color: var(--ink);
            background:
              radial-gradient(circle at top left, #fff6d8 0, transparent 30%),
              radial-gradient(circle at top right, #d9f3ee 0, transparent 35%),
              var(--bg);
          }}
          .wrap {{
            max-width: 1080px;
            margin: 0 auto;
            padding: 32px 20px 56px;
          }}
          h1 {{
            margin: 0 0 8px;
            font-size: 38px;
            line-height: 1.05;
          }}
          p.lead {{
            margin: 0 0 24px;
            color: var(--muted);
            max-width: 720px;
          }}
          .grid {{
            display: grid;
            gap: 16px;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            margin-bottom: 16px;
          }}
          .card {{
            background: var(--panel);
            border: 1px solid var(--line);
            border-radius: 18px;
            padding: 18px;
            box-shadow: 0 8px 24px rgba(31, 41, 55, 0.05);
          }}
          .eyebrow {{
            display: inline-block;
            padding: 6px 10px;
            border-radius: 999px;
            background: var(--accent-soft);
            color: var(--accent);
            font-size: 12px;
            font-weight: 700;
            letter-spacing: 0.04em;
            text-transform: uppercase;
            margin-bottom: 12px;
          }}
          .metric {{
            font-size: 28px;
            font-weight: 700;
            margin: 6px 0;
          }}
          .toolbar {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            align-items: center;
            margin-bottom: 18px;
          }}
          .muted {{
            color: var(--muted);
            font-size: 14px;
          }}
          .pill {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 8px 12px;
            border-radius: 999px;
            background: #eef8f5;
            color: #0f766e;
            font-size: 13px;
            font-weight: 700;
          }}
          button {{
            border: 1px solid #0f766e;
            background: #0f766e;
            color: #fff;
            border-radius: 12px;
            padding: 10px 12px;
            font-size: 14px;
            font-weight: 600;
            cursor: pointer;
          }}
          button:hover {{
            background: #0c625c;
          }}
          .action-form {{
            display: grid;
            gap: 10px;
            margin-bottom: 12px;
          }}
          .action-row {{
            display: flex;
            gap: 10px;
            align-items: center;
            flex-wrap: wrap;
          }}
          input[type="number"] {{
            border: 1px solid var(--line);
            border-radius: 10px;
            padding: 8px 10px;
            font-size: 14px;
            width: 96px;
            background: #fff;
            color: var(--ink);
          }}
          input[type="text"] {{
            border: 1px solid var(--line);
            border-radius: 10px;
            padding: 8px 10px;
            font-size: 14px;
            width: 100%;
            background: #fff;
            color: var(--ink);
          }}
          select {{
            border: 1px solid var(--line);
            border-radius: 10px;
            padding: 8px 10px;
            font-size: 14px;
            background: #fff;
            color: var(--ink);
          }}
          .checkbox-row {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            color: var(--muted);
            font-size: 14px;
          }}
          table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
          }}
          th, td {{
            text-align: left;
            padding: 10px 8px;
            border-bottom: 1px solid var(--line);
          }}
          th {{
            color: var(--muted);
            font-weight: 600;
          }}
          pre {{
            margin: 0;
            white-space: pre-wrap;
            word-break: break-word;
            font-size: 13px;
            color: #0b3b36;
          }}
          code {{
            font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
            font-size: 12px;
            background: #f3f4f6;
            padding: 2px 6px;
            border-radius: 8px;
          }}
        </style>
        <script>
          const AUTO_REFRESH_MS = 10000;
          let refreshTimer = null;

          function scheduleRefresh() {{
            if (refreshTimer) {{
              clearTimeout(refreshTimer);
            }}
            refreshTimer = setTimeout(() => window.location.reload(), AUTO_REFRESH_MS);
          }}

          window.addEventListener("DOMContentLoaded", () => {{
            const checkbox = document.getElementById("auto-refresh");
            const label = document.getElementById("refresh-label");
            const button = document.getElementById("refresh-now");

            const saved = localStorage.getItem("dashboard_auto_refresh");
            const enabled = saved === null ? true : saved === "true";
            checkbox.checked = enabled;

            const updateLabel = () => {{
              label.textContent = checkbox.checked ? "Auto-refresh every 10s" : "Auto-refresh paused";
            }};

            updateLabel();

            if (checkbox.checked) {{
              scheduleRefresh();
            }}

            checkbox.addEventListener("change", () => {{
              localStorage.setItem("dashboard_auto_refresh", String(checkbox.checked));
              updateLabel();
              if (checkbox.checked) {{
                scheduleRefresh();
              }} else if (refreshTimer) {{
                clearTimeout(refreshTimer);
              }}
            }});

            button.addEventListener("click", () => window.location.reload());
          }});
        </script>
      </head>
      <body>
        <main class="wrap">
          <div class="eyebrow">Local Dashboard</div>
          <h1>Personal Quant Workbench</h1>
          <p class="lead">
            A single-page snapshot of the local pipeline: market syncs, latest signals, and the most recent backtest.
          </p>
          {banner_html}
          <div class="toolbar">
            <span class="pill" id="refresh-label">Auto-refresh every 10s</span>
            <label class="muted" style="display:inline-flex;align-items:center;gap:8px;">
              <input type="checkbox" id="auto-refresh" checked />
              Auto refresh
            </label>
            <button id="refresh-now" type="button">Refresh Now</button>
            <span class="muted">Last updated: {generated_at}</span>
          </div>

          <section class="grid">
            <article class="card">
              <div class="eyebrow">Latest Model</div>
              <div class="metric">{latest_model['name'] if latest_model else 'None'}</div>
              <div class="muted">Status: {latest_model['status'] if latest_model else '-'}</div>
              <div class="muted">Type: {latest_model['model_type'] if latest_model else '-'}</div>
            </article>
            <article class="card">
              <div class="eyebrow">Signals</div>
              <div class="metric">{len(latest_signals)}</div>
              <div class="muted">Latest date: {latest_signals[0]['trade_date'] if latest_signals else '-'}</div>
              <div class="muted">Top ticker: {latest_signals[0]['ticker'] if latest_signals else '-'}</div>
            </article>
            <article class="card">
              <div class="eyebrow">Backtest</div>
              <div class="metric">{latest_backtest['status'] if latest_backtest else 'None'}</div>
              <div class="muted">Run: {latest_backtest['name'] if latest_backtest else '-'}</div>
              <div class="muted">Period: {latest_backtest['start_date'] if latest_backtest else '-'} to {latest_backtest['end_date'] if latest_backtest else '-'}</div>
            </article>
          </section>

          <section class="grid">
            <article class="card">
              <div class="eyebrow">Quick Actions</div>
              <form class="action-form" action="/jobs/seed-sample-data?redirect_to=/dashboard" method="post">
                <button type="submit">Seed Sample Data</button>
              </form>
              <form class="action-form" action="/jobs/sync-market-data" method="post">
                <input type="hidden" name="redirect_to" value="/dashboard" />
                <div class="action-row" style="display:block;">
                  <label for="sync-tickers" class="muted" style="display:block;margin-bottom:6px;">Tickers</label>
                  <input id="sync-tickers" type="text" name="tickers" placeholder="AAPL,MSFT" />
                </div>
                <div class="action-row">
                  <label for="sync-provider" class="muted">Provider</label>
                  <select id="sync-provider" name="provider">
                    <option value="yfinance" selected>yfinance</option>
                  </select>
                </div>
                <div class="action-row">
                  <label for="start-date" class="muted">Start</label>
                  <input id="start-date" type="text" name="start_date" placeholder="YYYY-MM-DD" />
                </div>
                <div class="action-row">
                  <label for="end-date" class="muted">End</label>
                  <input id="end-date" type="text" name="end_date" placeholder="YYYY-MM-DD" />
                </div>
                <button type="submit">Sync Market Data</button>
              </form>
              <form class="action-form" action="/jobs/run-pipeline" method="post">
                <input type="hidden" name="redirect_to" value="/dashboard" />
                <div class="action-row" style="display:block;">
                  <label for="pipeline-tickers" class="muted" style="display:block;margin-bottom:6px;">Pipeline Tickers</label>
                  <input id="pipeline-tickers" type="text" name="tickers" placeholder="AAPL,MSFT" />
                </div>
                <div class="action-row" style="display:block;">
                  <label for="pipeline-run-name" class="muted" style="display:block;margin-bottom:6px;">Pipeline Run Name</label>
                  <input id="pipeline-run-name" type="text" name="run_name" value="pipeline_run" />
                </div>
                <div class="action-row">
                  <label for="pipeline-signal-type" class="muted">Signal</label>
                  <select id="pipeline-signal-type" name="signal_type">
                    <option value="momentum" selected>Momentum</option>
                    <option value="reversal">Reversal</option>
                  </select>
                </div>
                <div class="action-row">
                  <label for="pipeline-lookback" class="muted">Lookback</label>
                  <input id="pipeline-lookback" type="number" name="lookback_days" min="1" step="1" value="3" />
                </div>
                <div class="action-row">
                  <label for="pipeline-top-n" class="muted">Top N</label>
                  <input id="pipeline-top-n" type="number" name="top_n" min="1" step="1" value="1" />
                </div>
                <div class="action-row">
                  <label for="pipeline-provider" class="muted">Provider</label>
                  <select id="pipeline-provider" name="provider">
                    <option value="yfinance" selected>yfinance</option>
                  </select>
                </div>
                <button type="submit">Run Full Pipeline</button>
              </form>
              <form class="action-form" action="/jobs/build-dataset" method="post">
                <input type="hidden" name="redirect_to" value="/dashboard" />
                <label class="checkbox-row">
                  <input type="checkbox" name="normalize_only" value="true" checked />
                  Normalize only
                </label>
                <button type="submit">Build Dataset</button>
              </form>
              <form class="action-form" action="/jobs/train" method="post">
                <input type="hidden" name="redirect_to" value="/dashboard" />
                <div class="action-row" style="display:block;">
                  <label for="run-name" class="muted" style="display:block;margin-bottom:6px;">Run Name</label>
                  <input id="run-name" type="text" name="run_name" value="baseline_momentum" />
                </div>
                <div class="action-row">
                  <label for="signal-type" class="muted">Signal</label>
                  <select id="signal-type" name="signal_type">
                    <option value="momentum" selected>Momentum</option>
                    <option value="reversal">Reversal</option>
                  </select>
                </div>
                <div class="action-row">
                  <label for="lookback-days" class="muted">Lookback</label>
                  <input id="lookback-days" type="number" name="lookback_days" min="1" step="1" value="3" />
                </div>
                <button type="submit">Run Training</button>
              </form>
              <form class="action-form" action="/jobs/backtest" method="post">
                <input type="hidden" name="redirect_to" value="/dashboard" />
                <div class="action-row">
                  <label for="top-n" class="muted">Top N</label>
                  <input id="top-n" type="number" name="top_n" min="1" step="1" value="1" />
                </div>
                <div class="action-row" style="display:block;">
                  <label for="model-run-id" class="muted" style="display:block;margin-bottom:6px;">Model Run ID</label>
                  <input id="model-run-id" type="number" name="model_run_id" min="1" step="1" placeholder="Leave blank for latest" />
                </div>
                <button type="submit">Run Backtest</button>
              </form>
            </article>
            <article class="card">
              <div class="eyebrow">JSON Shortcuts</div>
              <div><a href="/dashboard/summary">Dashboard Summary JSON</a></div>
              <div><a href="/signals/latest">Latest Signals JSON</a></div>
              <div><a href="/backtests/latest/curve">Latest Backtest Curve JSON</a></div>
              <div><a href="/jobs/sync-states">Sync States JSON</a></div>
            </article>
          </section>

          <section class="card" style="margin-bottom:16px;">
            <div class="eyebrow">Latest Signals</div>
            <table>
              <thead>
                <tr><th>Ticker</th><th>Date</th><th>Score</th><th>Rank</th></tr>
              </thead>
              <tbody>{signal_items}</tbody>
            </table>
          </section>

          <section class="grid">
            <article class="card">
              <div class="eyebrow">Sync States</div>
              <table>
                <thead>
                  <tr><th>Ticker</th><th>Provider</th><th>Last Sync</th><th>Status</th></tr>
                </thead>
                <tbody>{sync_items}</tbody>
              </table>
            </article>
            <article class="card">
              <div class="eyebrow">Backtest Summary</div>
              <pre>{backtest_pre}</pre>
            </article>
          </section>

          <section class="card" style="margin-top:16px;">
            <div class="eyebrow">Recent Model Runs</div>
            <table>
              <thead>
                <tr><th>ID</th><th>Name</th><th>Status</th><th>Config</th><th>Created</th><th>Action</th></tr>
              </thead>
              <tbody>{recent_model_rows}</tbody>
            </table>
          </section>

          <section class="card" style="margin-top:16px;">
            <div class="eyebrow">Equity Curve</div>
            {curve_svg}
          </section>

          <section class="card" style="margin-top:16px;">
            <div class="eyebrow">Recent Jobs</div>
            <table>
              <thead>
                <tr><th>ID</th><th>Type</th><th>Status</th><th>Started</th><th>Finished</th><th>Params</th><th>Message</th></tr>
              </thead>
              <tbody>{recent_job_rows}</tbody>
            </table>
          </section>
        </main>
      </body>
    </html>
    """
