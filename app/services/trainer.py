import csv
from collections import defaultdict
from pathlib import Path

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.services.repository import ModelRunRepository, PredictionWriteRepository, SymbolRepository


class SignalTrainer:
    """Train a lightweight baseline model over normalized CSVs."""

    def __init__(self) -> None:
        self.settings = get_settings()

    def _load_rows(self) -> list[dict]:
        rows: list[dict] = []
        for csv_path in sorted(self.settings.normalized_data_dir.glob("*.csv")):
            with csv_path.open("r", newline="", encoding="utf-8") as input_file:
                reader = csv.DictReader(input_file)
                rows.extend(reader)
        rows.sort(key=lambda row: (row.get("symbol") or "", row.get("date") or ""))
        return rows

    def train(
        self,
        run_name: str = "baseline_momentum",
        signal_type: str = "momentum",
        lookback_days: int = 3,
    ) -> int:
        rows = self._load_rows()
        if not rows:
            raise RuntimeError("No normalized CSV files found. Run `scripts/build_dataset.py --normalize-only` first.")
        if lookback_days < 1:
            raise RuntimeError("lookback_days must be at least 1.")
        if signal_type not in {"momentum", "reversal"}:
            raise RuntimeError("signal_type must be either 'momentum' or 'reversal'.")

        signal_rows: list[dict] = []
        by_date: dict[str, list[dict]] = defaultdict(list)
        history_by_symbol: dict[str, list[float]] = defaultdict(list)

        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            model_repo = ModelRunRepository(db)
            prediction_repo = PredictionWriteRepository(db)

            dates = sorted({row["date"] for row in rows if row.get("date")})
            run = model_repo.create_run(
                name=run_name,
                model_type="local_baseline",
                market="US",
                universe="local_watchlist",
                train_start=dates[0] if dates else None,
                train_end=dates[-1] if dates else None,
                test_start=dates[0] if dates else None,
                test_end=dates[-1] if dates else None,
                config={"signal_type": signal_type, "lookback_days": lookback_days},
                artifact_path=None,
                status="running",
            )

            for row in rows:
                symbol = row.get("symbol")
                date = row.get("date")
                close_value = row.get("close")
                if not symbol or not date or not close_value:
                    continue

                symbol_record = symbol_repo.get_by_ticker(symbol)
                if symbol_record is None:
                    continue

                closes = history_by_symbol[symbol]
                close = float(close_value)
                score = None
                if closes:
                    daily_return = (close / closes[-1]) - 1.0
                    trailing = closes[-lookback_days:]
                    if trailing:
                        trailing_returns = []
                        previous = None
                        for trailing_close in trailing:
                            if previous is not None:
                                trailing_returns.append((trailing_close / previous) - 1.0)
                            previous = trailing_close
                        trailing_returns.append(daily_return)
                        score = sum(trailing_returns) / len(trailing_returns)
                        if signal_type == "reversal":
                            score = -score
                closes.append(close)

                if score is None:
                    continue

                record = {
                    "symbol_id": symbol_record.id,
                    "trade_date": date,
                    "score": score,
                    "rank_value": None,
                }
                by_date[date].append(record)

            for date, date_rows in by_date.items():
                ranked = sorted(date_rows, key=lambda item: item["score"], reverse=True)
                for idx, record in enumerate(ranked, start=1):
                    record["rank_value"] = float(idx)
                    signal_rows.append(record)

            if not signal_rows:
                model_repo.complete_run(run.id, status="failed", artifact_path=None)
                raise RuntimeError(
                    "The baseline trainer produced no predictions. You likely need at least 2-3 trading days per symbol."
                )

            count = prediction_repo.replace_for_model_run(run.id, signal_rows)
            artifact_path = str((self.settings.artifacts_dir / f"model_run_{run.id}.json").resolve())
            Path(artifact_path).write_text(
                f'{{"model":"{run_name}","signal_type":"{signal_type}","lookback_days":{lookback_days}}}',
                encoding="utf-8",
            )
            model_repo.complete_run(run.id, status="success", artifact_path=artifact_path)
            return count
