import csv
from collections import defaultdict
from pathlib import Path

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.services.repository import (
    ModelRunRepository,
    PredictionExplanationRepository,
    PredictionWriteRepository,
    SymbolRepository,
)


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

    def _build_explanations(
        self,
        *,
        symbol_id: int,
        trade_date: str,
        components: list[dict],
    ) -> list[dict]:
        rows: list[dict] = []
        for index, component in enumerate(components, start=1):
            contribution = component.get("contribution") or 0.0
            rows.append(
                {
                    "symbol_id": symbol_id,
                    "trade_date": trade_date,
                    "feature_name": component["feature_name"],
                    "feature_value": round((component.get("feature_value") or 0.0) * 100, 4),
                    "contribution": round(contribution * 100, 4),
                    "direction": "positive" if contribution >= 0 else "negative",
                    "display_order": index,
                }
            )
        return rows

    def _moving_average(self, values: list[float], window: int) -> float | None:
        if not values:
            return None
        sample = values[-window:] if len(values) >= window else values
        return sum(sample) / len(sample)

    def _clamp(self, value: float, lower: float, upper: float) -> float:
        return max(lower, min(upper, value))

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
        explanation_rows: list[dict] = []
        by_date: dict[str, list[dict]] = defaultdict(list)
        close_history_by_symbol: dict[str, list[float]] = defaultdict(list)
        volume_history_by_symbol: dict[str, list[float]] = defaultdict(list)

        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            model_repo = ModelRunRepository(db)
            prediction_repo = PredictionWriteRepository(db)
            explanation_repo = PredictionExplanationRepository(db)

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

                closes = close_history_by_symbol[symbol]
                volumes = volume_history_by_symbol[symbol]
                close = float(close_value)
                volume = float(row.get("volume") or 0.0)
                score = None
                components: list[dict] = []
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
                        momentum_component = sum(trailing_returns) / len(trailing_returns)

                        projected_closes = closes + [close]
                        ma5 = self._moving_average(projected_closes, 5)
                        ma20 = self._moving_average(projected_closes, 20)
                        ma60 = self._moving_average(projected_closes, 60)
                        price_vs_ma20 = ((close / ma20) - 1.0) if ma20 else 0.0
                        ma_alignment = ((ma5 / ma20) - 1.0) if ma5 and ma20 else 0.0
                        ma_stack = ((ma20 / ma60) - 1.0) if ma20 and ma60 else 0.0
                        projected_volumes = volumes + ([volume] if volume else [])
                        avg_volume_20 = self._moving_average(projected_volumes, 20)
                        volume_ratio = (volume / avg_volume_20) if avg_volume_20 and volume else 1.0

                        structure_component = self._clamp(price_vs_ma20, -0.08, 0.08) * 0.35
                        alignment_component = self._clamp(ma_alignment + ma_stack, -0.08, 0.08) * 0.25
                        volume_component = self._clamp(volume_ratio - 1.0, -0.75, 1.25) * 0.03

                        score = momentum_component + structure_component + alignment_component + volume_component
                        if signal_type == "reversal":
                            score = -score

                        polarity = -1.0 if signal_type == "reversal" else 1.0
                        components = [
                            {
                                "feature_name": "recent_daily_return",
                                "feature_value": daily_return,
                                "contribution": (daily_return / len(trailing_returns)) * polarity,
                            },
                            {
                                "feature_name": f"lookback_momentum_{lookback_days}d",
                                "feature_value": momentum_component,
                                "contribution": momentum_component * polarity,
                            },
                            {
                                "feature_name": "price_vs_ma20",
                                "feature_value": price_vs_ma20,
                                "contribution": structure_component * polarity,
                            },
                            {
                                "feature_name": "ma_alignment",
                                "feature_value": ma_alignment + ma_stack,
                                "contribution": alignment_component * polarity,
                            },
                            {
                                "feature_name": "volume_ratio_20d",
                                "feature_value": volume_ratio - 1.0,
                                "contribution": volume_component * polarity,
                            },
                        ]
                        historical_returns = trailing_returns[:-1]
                        latest_first = list(reversed(historical_returns))
                        for index, value in enumerate(latest_first, start=1):
                            components.append(
                                {
                                    "feature_name": f"lag_return_{index}d",
                                    "feature_value": value,
                                    "contribution": (value / len(trailing_returns)) * polarity,
                                }
                            )

                closes.append(close)
                if volume:
                    volumes.append(volume)

                if score is None:
                    continue

                record = {
                    "symbol_id": symbol_record.id,
                    "trade_date": date,
                    "score": score,
                    "rank_value": None,
                    "_explanations": self._build_explanations(
                        symbol_id=symbol_record.id,
                        trade_date=date,
                        components=components,
                    ),
                }
                by_date[date].append(record)

            for date, date_rows in by_date.items():
                ranked = sorted(date_rows, key=lambda item: item["score"], reverse=True)
                for idx, record in enumerate(ranked, start=1):
                    record["rank_value"] = float(idx)
                    explanation_rows.extend(record.pop("_explanations", []))
                    signal_rows.append(record)

            if not signal_rows:
                model_repo.complete_run(run.id, status="failed", artifact_path=None)
                raise RuntimeError(
                    "The baseline trainer produced no predictions. You likely need at least 2-3 trading days per symbol."
                )

            count = prediction_repo.replace_for_model_run(run.id, signal_rows)
            explanation_repo.replace_for_model_run(run.id, explanation_rows)
            artifact_path = str((self.settings.artifacts_dir / f"model_run_{run.id}.json").resolve())
            Path(artifact_path).write_text(
                f'{{"model":"{run_name}","signal_type":"{signal_type}","lookback_days":{lookback_days}}}',
                encoding="utf-8",
            )
            model_repo.complete_run(run.id, status="success", artifact_path=artifact_path)
            return count
