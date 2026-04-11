# Personal Quant Workbench

A local-first stock analysis app built for personal research.

It combines:

- OpenBB for market data ingestion
- Qlib as an optional research dataset layer
- SQLite for app state, model runs, jobs, and backtest summaries
- FastAPI for local APIs and a lightweight HTML dashboard
- Local baseline training and backtesting so the app remains usable even without Qlib

## What It Does

Current MVP capabilities:

- Manage symbols in SQLite
- Sync market data into `data/raw/`
- Normalize CSV datasets into `data/normalized/`
- Optionally build a Qlib dataset in `data/qlib/`
- Train simple local baseline signals
- Run top-N backtests
- Track jobs, model runs, predictions, and strategy runs
- Browse a local dashboard and symbol detail pages

## Project Layout

```text
app/
  api/
    routes/
  core/
  models/
  services/
data/
  raw/
  normalized/
  qlib/
  artifacts/
scripts/
storage/
  app.db
```

## Quick Start

1. Create a virtual environment and install the core app dependencies.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

2. Initialize the SQLite database.

```bash
.venv/bin/python scripts/init_db.py
```

3. Seed local sample data if you want to try the app without OpenBB.

```bash
.venv/bin/python scripts/seed_sample_data.py
```

4. Start the API server.

```bash
.venv/bin/uvicorn app.api.main:app --reload
```

5. Open the local dashboard.

```text
http://127.0.0.1:8000/dashboard
```

## PostgreSQL Migration Prep

The app still defaults to SQLite, but it now also supports a PostgreSQL-style `PQW_DATABASE_URL`.

1. Install PostgreSQL and create a database/user.

```bash
brew install postgresql@17
brew services start postgresql@17
createuser ana_user --pwprompt
createdb ana_prod -O ana_user
```

2. Configure the app to use PostgreSQL.

```env
PQW_DATABASE_URL=postgresql+psycopg://ana_user:your_password@127.0.0.1:5432/ana_prod
```

3. Copy the existing SQLite data into PostgreSQL.

```bash
.venv/bin/python scripts/migrate_database.py \
  --source-url sqlite:////absolute/path/to/app.db \
  --target-url postgresql+psycopg://ana_user:your_password@127.0.0.1:5432/ana_prod
```

Use `--truncate-target` if you want to overwrite an existing PostgreSQL copy.

4. Validate the copy by comparing row counts table-by-table.

```bash
.venv/bin/python scripts/migrate_database.py \
  --mode validate \
  --source-url sqlite:////absolute/path/to/app.db \
  --target-url postgresql+psycopg://ana_user:your_password@127.0.0.1:5432/ana_prod
```

5. Restart the app and smoke test:

- `/dashboard`
- `/watchlist`
- `/dashboard/ai-daily-report`
- `/dashboard/ops/jobs`

For a production-style home-lab deployment on an internal Mac mini with external access through Cloudflare Tunnel, see:

- `docs/macmini-cloudflare-tunnel.md`

## Run With Docker

Start the app with Docker Compose:

```bash
docker compose up --build
```

Then open:

```text
http://127.0.0.1:8000/dashboard
```

Notes:

- SQLite data is persisted in your local [storage](/Users/vincent/LAB/claudecode/ana/storage) directory.
- Raw and normalized market data stay in your local [data](/Users/vincent/LAB/claudecode/ana/data) directory.
- The container currently installs only the core dependencies from `requirements.txt`.
- If you want OpenBB or Qlib inside Docker later, we can extend the image with optional dependency targets.

## Optional Dependencies

Install OpenBB support if you want live market sync:

```bash
.venv/bin/pip install -r requirements-openbb.txt
```

Install Qlib support if you want to build Qlib datasets:

```bash
.venv/bin/pip install -r requirements-qlib.txt
```

If you already have Qlib-style prediction outputs and want to feed them into the app without changing the UI layer, you can import them directly:

```bash
.venv/bin/python scripts/import_qlib_predictions.py --csv path/to/predictions.csv --run-name qlib_demo
```

There is also a thin Qlib predictor bridge that validates the local Qlib dataset and can import a Qlib-style predictions CSV into the app:

```bash
.venv/bin/python scripts/run_qlib_predictor.py --run-name qlib_demo --predictions-csv path/to/predictions.csv
```

If your trained artifact directory already contains a native-style predictions export named `predictions.csv`, `predictions.json`, or `predictions.jsonl`, you can point the predictor at the artifact directory directly:

```bash
.venv/bin/python scripts/run_qlib_predictor.py --run-name qlib_demo --artifact-path path/to/qlib_artifact_dir
```

The artifact directory may also include an optional `manifest.json` with metadata such as:

```json
{
  "run_name": "qlib_demo",
  "market": "US",
  "universe": "sp500",
  "model_type": "qlib_native",
  "target_horizon_days": 10
}
```

If present, the manifest metadata will be merged into the imported model run context.

If you prefer a structured payload instead of CSV, `predictions.json` is also accepted. It should contain a list of prediction objects, for example:

```json
[
  {
    "ticker": "AAPL",
    "trade_date": "2026-04-03",
    "score": 0.29,
    "signal_label": "Buy",
    "signal_strength": 72,
    "summary_text": "Imported from structured Qlib JSON."
  }
]
```

Line-delimited JSON is also accepted as `predictions.jsonl`, with one prediction object per line.

For a more native artifact contract, you can also provide a single `native_result.json` file that bundles predictions and companion outputs together:

```json
{
  "manifest": {
    "run_name": "qlib_native_bundle",
    "market": "US",
    "universe": "sp500"
  },
  "predictions": [
    {
      "ticker": "AAPL",
      "trade_date": "2026-04-03",
      "score": 0.29,
      "signal_label": "Buy",
      "signal_strength": 72
    }
  ],
  "explanations": [
    {
      "ticker": "AAPL",
      "trade_date": "2026-04-03",
      "feature_name": "momentum_20",
      "contribution": 0.18
    }
  ],
  "chart_signals": [
    {
      "ticker": "AAPL",
      "trade_date": "2026-04-02",
      "signal_label": "Pullback",
      "signal_strength": 68
    }
  ],
  "trade_plan": [
    {
      "ticker": "AAPL",
      "trade_date": "2026-04-03",
      "entry_low": 188.0,
      "entry_high": 190.0
    }
  ]
}
```

You can also include an optional `explanations.csv` or `features.csv` in the same artifact directory to import feature contributions into the app's `prediction_explanations` layer. The expected columns are:

```text
ticker,trade_date,feature_name,feature_value,contribution,direction,display_order
```

You can also include an optional `chart_signals.csv` to control the historical buy/watch/sell markers drawn on the interactive insight chart. The expected columns are:

```text
ticker,trade_date,score,rank_value,signal_label,signal_strength,note
```

You can also include an optional `trade_plan.json` to override the insight page execution levels with model-provided values. The expected fields are:

```json
[
  {
    "ticker": "AAPL",
    "trade_date": "2026-04-03",
    "entry_low": 180.0,
    "entry_high": 183.0,
    "breakout_level": 188.0,
    "take_profit_low": 194.0,
    "take_profit_high": 198.0,
    "risk_level": 176.5,
    "support_level": 178.0,
    "resistance_level": 188.0,
    "stop_type": "trailing",
    "trailing_stop_pct": 6.5,
    "invalidation_reason": "Break below the post-breakout low.",
    "execution_tags": ["gap-risk", "earnings-soon"],
    "note": "Model-generated execution plan."
  }
]
```

To inspect the expected native Qlib output schema before wiring a real predictor, run:

```bash
.venv/bin/python scripts/run_qlib_predictor.py --run-name qlib_demo --print-schema
```

Install TuShare Pro support if you want A-share fundamentals for multi-model screeners:

```bash
.venv/bin/pip install -r requirements-tushare.txt
```

Notes:

- OpenBB is optional. Without it, sample data and local workflows still work.
- Qlib is optional. The app can still train and backtest via the built-in baseline flow.
- TuShare Pro is optional. It is used for A-share fundamental templates such as `高成长低估值`.
- The screener now supports multiple templates, including `高成长低估值` and `高ROE稳增长`.
- On this machine, `pyqlib` was not available for Python 3.14, so the baseline path is the default working mode.

To enable CN fundamentals, set:

```bash
export PQW_TUSHARE_TOKEN=your_token_here
```

## Main Workflows

### Workflow A: Try It Immediately With Sample Data

```bash
.venv/bin/python scripts/init_db.py
.venv/bin/python scripts/seed_sample_data.py
.venv/bin/python scripts/build_dataset.py --normalize-only
.venv/bin/python scripts/train_signal_model.py
.venv/bin/python scripts/run_backtest.py --top-n 1
.venv/bin/uvicorn app.api.main:app --reload
```

Then open:

- `http://127.0.0.1:8000/dashboard`
- `http://127.0.0.1:8000/symbols/AAPL`

### Workflow B: Use Live Market Data

After installing OpenBB support:

```bash
.venv/bin/python scripts/sync_market_data.py --tickers AAPL MSFT NVDA --provider yfinance
.venv/bin/python scripts/build_dataset.py --normalize-only
.venv/bin/python scripts/train_signal_model.py
.venv/bin/python scripts/run_backtest.py --top-n 2
```

## Dashboard Features

The dashboard at `/dashboard` currently supports:

- Seed sample data
- Sync market data
- Run a full pipeline
- Build normalized datasets
- Train a baseline signal with configurable run name, signal type, and lookback
- Run a backtest with configurable `top_n` and optional `model_run_id`
- Review recent jobs, recent model runs, latest signals, and the latest equity curve

There is also a one-click pipeline action that runs:

```text
Sync -> Build Dataset -> Train -> Backtest
```

## Run Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

GitHub Actions also runs this regression suite automatically on `push` to `main` and on pull requests.

## Key Routes

HTML pages:

- `GET /dashboard`
- `GET /symbols/{ticker}`

JSON APIs:

- `GET /health`
- `GET /dashboard/summary`
- `GET /symbols`
- `GET /symbols/{ticker}/overview`
- `GET /symbols/{ticker}/history`
- `GET /symbols/{ticker}/signals`
- `GET /signals/latest`
- `GET /backtests/latest/curve`
- `GET /jobs/recent`
- `GET /jobs/sync-states`

Job triggers:

- `POST /jobs/seed-sample-data`
- `POST /jobs/sync-market-data`
- `POST /jobs/build-dataset`
- `POST /jobs/sync-cn-fundamentals`
- `POST /jobs/train`
- `POST /jobs/import-model-output`
- `POST /jobs/backtest`
- `POST /jobs/run-pipeline`

## Storage Strategy

This app intentionally uses a split storage model:

- `SQLite` stores symbols, jobs, model runs, predictions, backtest summaries, and app state
- `data/raw/` stores raw market CSV files
- `data/normalized/` stores normalized CSV files
- `data/artifacts/` stores model artifacts
- `data/qlib/` is reserved for optional Qlib datasets

This keeps SQLite light while still preserving a local-first workflow.

## Current Status

This repository is already usable as a personal quant workbench MVP.

Implemented:

- Local dashboard
- Symbol detail pages
- Job tracking
- Baseline training
- Baseline backtesting
- Real or mocked market sync
- One-click local pipeline

Next likely upgrades:

- Qlib-native training when the Python environment supports it
- Richer factor engineering
- Better experiment comparison views
- Scheduled daily refresh jobs
