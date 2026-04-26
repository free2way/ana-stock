# Personal Quant Workbench

Personal Quant Workbench is a local-first daily review and model screening platform for amateur stock traders.

It focuses on one thing: after the market close, refresh the data, run the models, generate actionable review output, and let the user make better next-day decisions from one place.

## Positioning

- Not a broker
- Not an auto-trading system
- Not an intraday high-frequency platform
- A post-close quantitative review workbench for A-shares and U.S. equities

The product goal is to turn "what is worth researching" into "what should I review, track, trim, or prepare for tomorrow".

## What The App Does

- Refresh A-share and U.S. daily market data
- Store market history in a local Parquet lake
- Use DuckDB and Polars for fast cross-sectional scans
- Train and score LightGBM multifactor signals
- Precompute screener snapshots instead of doing heavy calculations in page requests
- Manage watchlists and holdings separately
- Generate portfolio review suggestions such as `HOLD`, `REVIEW`, `TRIM`, and `EXIT`
- Produce AI daily reports with market summary, portfolio review, full-market top ideas, and social cross-validation
- Track jobs, failures, stale runs, and post-close automation status from the ops center

## Current Stack

- FastAPI for routes and HTML pages
- PostgreSQL for application state and audit records
- Parquet as the market data lake
- DuckDB + Polars for scanning and snapshot computation
- LightGBM for multifactor signal training and scoring
- Background jobs for post-close refresh, training, screening, and report generation

## Data Source Strategy

### China

- Price data: TuShare
- Fundamentals and symbol metadata: TuShare
- Concept data: TuShare concept endpoints when the account has permission

### U.S.

- Price data: Alpaca by default
- Full-market grouped daily expansion: Polygon when configured
- Fallback for limited cases: yfinance

### News And Social

- U.S. market news enrichment: Polygon news
- Social signal tracking: tracked X accounts with local parsing and validation

## Product Modules

- `/dashboard`: action-first home page
- `/watchlist`: watchlist and decision panel
- `/portfolio`: holding review and action table
- `/screeners`: model screening and saved model presets
- `/dashboard/model-performance`: model evaluation overview
- `/dashboard/market`: market overview, sector heatmap, concept tracking
- `/dashboard/ai-daily-report`: current AI daily report
- `/dashboard/ai-daily-report/history`: historical report archive
- `/dashboard/ops`: jobs, pipeline status, sync health, and operational diagnostics
- `/social`: tracked accounts, parsed mentions, and validation results

## Daily Workflow

The intended workflow is:

1. Refresh full-market end-of-day data after the close.
2. Update factors and market snapshots.
3. Train or update LightGBM signal runs.
4. Precompute screener outputs for model templates.
5. Refresh watchlist and portfolio review suggestions.
6. Generate the AI daily report and push-ready text.
7. Let the user review the dashboard, watchlist, holdings, and next-day candidates.

Heavy computations are designed to run in background jobs, while pages should mainly read precomputed snapshots.

## Repository Layout

```text
app/
  api/
    routes/
  core/
  models/
  services/
data/
  lake/
  artifacts/
docs/
scripts/
storage/
tests/
```

## Quick Start

### 1. Create a virtual environment

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Configure environment variables

Create `.env` and set at least:

```env
PQW_DATABASE_URL=postgresql+psycopg://ana_user:your_password@127.0.0.1:5432/ana_prod
PQW_AUTH_PASSWORD=change_me
PQW_TUSHARE_TOKEN=your_tushare_token
```

Optional but recommended:

```env
PQW_ALPACA_API_KEY=your_alpaca_key
PQW_ALPACA_API_SECRET=your_alpaca_secret
PQW_POLYGON_API_KEY=your_polygon_key
PQW_X_BEARER_TOKEN=your_x_bearer_token
PQW_TELEGRAM_BOT_TOKEN=your_telegram_bot_token
PQW_TELEGRAM_CHAT_ID=your_telegram_chat_id
```

### 3. Initialize the database schema

```bash
.venv/bin/python scripts/init_db.py
```

### 4. Start the app

```bash
.venv/bin/uvicorn app.api.main:app --host 127.0.0.1 --port 8000
```

Open:

```text
http://127.0.0.1:8000/dashboard
```

## Notes On Storage

- PostgreSQL is the supported primary app database.
- Parquet is the primary market history storage format.
- DuckDB queries the Parquet lake directly for fast scans.
- Legacy SQLite-related files or migration notes may still exist in the repo for historical compatibility, but the current target architecture is PostgreSQL-first.

## Documentation

- [项目介绍（中文）](docs/project-introduction-zh.md)
- [产品目标方案（中文）](docs/amateur-quant-workbench-roadmap-zh.md)
- [验收清单（中文）](docs/amateur-quant-workbench-acceptance-checklist-zh.md)

## Current Status

The project has reached a stage where it can be used as an amateur trader's daily review workbench:

- the main post-close pipeline is in place
- model screening is largely precomputed
- watchlist and portfolio workflows are connected
- AI daily reports and history are available
- ops pages can be used to inspect failures and stale jobs

The remaining work is mainly around deeper attribution, broader concept coverage, and continued data-quality hardening, not basic workflow completeness.
