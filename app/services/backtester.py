import csv
from collections import defaultdict

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.services.repository import (
    ModelRunRepository,
    PredictionWriteRepository,
    StrategyRunRepository,
    SymbolRepository,
)


class BacktestRunner:
    """Run a lightweight top-N backtest from stored predictions."""

    def __init__(self) -> None:
        self.settings = get_settings()

    def _load_next_day_returns(self) -> dict[tuple[str, str], float]:
        closes_by_symbol: dict[str, list[tuple[str, float]]] = defaultdict(list)
        returns: dict[tuple[str, str], float] = {}

        for csv_path in sorted(self.settings.normalized_data_dir.glob("*.csv")):
            with csv_path.open("r", newline="", encoding="utf-8") as input_file:
                reader = csv.DictReader(input_file)
                for row in reader:
                    symbol = row.get("symbol")
                    date = row.get("date")
                    close = row.get("close")
                    if symbol and date and close:
                        closes_by_symbol[symbol].append((date, float(close)))

        for symbol, items in closes_by_symbol.items():
            items.sort(key=lambda item: item[0])
            for index in range(len(items) - 1):
                date, close = items[index]
                next_date, next_close = items[index + 1]
                if close:
                    returns[(symbol, date)] = (next_close / close) - 1.0

        return returns

    def run(self, top_n: int = 1, model_run_id: int | None = None) -> int:
        next_day_returns = self._load_next_day_returns()
        if not next_day_returns:
            raise RuntimeError("No normalized market data available for backtest. Build normalized CSVs first.")

        with SessionLocal() as db:
            model_repo = ModelRunRepository(db)
            symbol_repo = SymbolRepository(db)
            prediction_repo = PredictionWriteRepository(db)
            strategy_repo = StrategyRunRepository(db)

            model_run = model_repo.get_run_by_id(model_run_id) if model_run_id is not None else model_repo.get_latest_run()
            if model_run is None:
                raise RuntimeError("No model run found for the requested id. Train a model first.")

            predictions = prediction_repo.list_for_model_run(model_run.id)
            if not predictions:
                raise RuntimeError("No predictions found for the selected model run.")

            ticker_by_id = {symbol.id: symbol.ticker for symbol in symbol_repo.list_symbols()}
            grouped: dict[str, list] = defaultdict(list)
            for prediction in predictions:
                grouped[prediction.trade_date].append(prediction)

            dates = sorted(grouped.keys())
            strategy_run = strategy_repo.create_run(
                model_run_id=model_run.id,
                name=f"top_n_{model_run.name}",
                strategy_type="top_n_equal_weight",
                start_date=dates[0] if dates else None,
                end_date=dates[-1] if dates else None,
                config={"top_n": top_n, "model_run_id": model_run.id, "model_run_name": model_run.name},
                status="running",
            )

            nav = 1.0
            peak = 1.0
            metrics: list[dict] = []

            for date in dates:
                ranked = sorted(grouped[date], key=lambda item: item.rank_value or 999999.0)
                selected = ranked[:top_n]
                returns = []
                for prediction in selected:
                    ticker = ticker_by_id.get(prediction.symbol_id)
                    if ticker is None:
                        continue
                    next_return = next_day_returns.get((ticker, date))
                    if next_return is not None:
                        returns.append(next_return)

                if not returns:
                    continue

                daily_return = sum(returns) / len(returns)
                nav *= 1.0 + daily_return
                peak = max(peak, nav)
                drawdown = (nav / peak) - 1.0
                metrics.append(
                    {
                        "trade_date": date,
                        "nav": nav,
                        "daily_return": daily_return,
                        "benchmark_return": None,
                        "drawdown": drawdown,
                        "turnover": None,
                    }
                )

            if not metrics:
                raise RuntimeError("Backtest produced no daily metrics. The dataset may be too short.")

            strategy_repo.replace_daily_metrics(strategy_run.id, metrics)
            summary = {
                "model_run_id": model_run.id,
                "model_run_name": model_run.name,
                "start_nav": 1.0,
                "end_nav": metrics[-1]["nav"],
                "total_return": metrics[-1]["nav"] - 1.0,
                "max_drawdown": min(metric["drawdown"] for metric in metrics),
                "days": len(metrics),
                "top_n": top_n,
            }
            strategy_repo.complete_run(strategy_run.id, status="success", summary=summary)
            return len(metrics)
