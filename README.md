# Personal Quant Workbench

A local-first personal finance analysis tool that combines:

- OpenBB for market data ingestion
- Qlib for research, model training, and backtesting
- SQLite for application state and analysis results
- FastAPI for local APIs

## Project Layout

```text
app/
  api/
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
```

## Quick Start

1. Create a virtual environment.
2. Install core dependencies with `.venv/bin/pip install -r requirements.txt`.
3. If you want live OpenBB ingestion, install the optional OpenBB layer:

```bash
.venv/bin/pip install -r requirements-openbb.txt
```

4. If you want Qlib dataset generation and later model training, install the optional Qlib layer:

```bash
.venv/bin/pip install -r requirements-qlib.txt
```

5. Initialize the database with `.venv/bin/python scripts/init_db.py`.
6. Start the API with `.venv/bin/uvicorn app.api.main:app --reload`.

## First Workflow

1. Add one or more symbols through `POST /symbols`.
2. Sync raw market data with:

```bash
.venv/bin/python scripts/sync_market_data.py --tickers AAPL MSFT --provider yfinance
```

If OpenBB is not installed yet, the sync script will fail with a clear message and record the failed sync state.

3. Normalize the raw CSV files:

```bash
.venv/bin/python scripts/build_dataset.py
```

If you only want normalized CSV output for now:

```bash
.venv/bin/python scripts/build_dataset.py --normalize-only
```

If Qlib is installed, the same command will also build `data/qlib/`.
If Qlib is not installed, the script will keep the normalized files and print the exact install command.

## Current Status

This is an MVP scaffold. The database, API shape, local storage layout, raw-data sync path,
normalized-to-Qlib build entrypoint, and a local baseline training/backtest flow are in place.
Qlib-native training is the next major implementation step once the environment supports it.
