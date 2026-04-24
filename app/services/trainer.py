from __future__ import annotations

import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.services.market_lake import load_lake_rows
from app.services.model_signal_summary import enrich_model_output, summarize_model_output
from app.services.repository import (
    ModelRunRepository,
    PredictionDetailRepository,
    PredictionExplanationRepository,
    PredictionWriteRepository,
    SymbolRepository,
)

try:
    import lightgbm as lgb  # type: ignore
except ImportError:  # pragma: no cover - handled at runtime
    lgb = None


class SignalTrainer:
    """Train the production LightGBM multifactor signal model over the local market lake."""

    def __init__(self) -> None:
        self.settings = get_settings()

    def _load_rows(self, *, tickers: set[str] | None = None) -> list[dict]:
        rows: list[dict] = []
        csv_paths = sorted(self.settings.normalized_data_dir.glob("*.csv"))
        selected_paths = csv_paths
        if tickers:
            matched_paths = [csv_path for csv_path in csv_paths if csv_path.stem.upper() in tickers]
            if matched_paths:
                selected_paths = matched_paths
        for csv_path in selected_paths:
            with csv_path.open("r", newline="", encoding="utf-8") as input_file:
                reader = csv.DictReader(input_file)
                if tickers:
                    rows.extend(
                        row for row in reader if str(row.get("symbol") or "").strip().upper() in tickers
                    )
                else:
                    rows.extend(reader)
        if not rows:
            rows = load_lake_rows(tickers=tickers)
        rows.sort(key=lambda row: (row.get("symbol") or "", row.get("date") or ""))
        return rows

    def _moving_average(self, values: list[float], window: int) -> float | None:
        if not values:
            return None
        sample = values[-window:] if len(values) >= window else values
        return sum(sample) / len(sample)

    def _clamp(self, value: float, lower: float, upper: float) -> float:
        return max(lower, min(upper, value))

    def _safe_float(self, value: object, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _stddev(self, values: list[float]) -> float:
        if len(values) < 2:
            return 0.0
        try:
            return float(statistics.pstdev(values))
        except statistics.StatisticsError:
            return 0.0

    def _baseline_explanations(
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

    def _feature_names(self, *, lookback_days: int) -> list[str]:
        return [
            "recent_daily_return",
            f"lookback_momentum_{lookback_days}d",
            "price_vs_ma20",
            "ma_alignment",
            "ma_stack",
            "volume_ratio_20d",
            "volatility_10d",
            "breakout_gap_20d",
            "drawdown_from_20d_high",
        ]

    def _feature_direction(self, feature_name: str) -> float:
        if feature_name == "volatility_10d":
            return -1.0
        return 1.0

    def _build_lightgbm_samples(
        self,
        *,
        rows: list[dict],
        lookback_days: int,
        horizon_days: int,
    ) -> list[dict]:
        grouped: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            trade_date = str(row.get("date") or "").strip()
            close = row.get("close")
            if not symbol or not trade_date or close in {None, ""}:
                continue
            grouped[symbol].append(row)

        samples: list[dict] = []
        for symbol, symbol_rows in grouped.items():
            symbol_rows.sort(key=lambda row: str(row.get("date") or ""))
            closes = [self._safe_float(row.get("close")) for row in symbol_rows]
            volumes = [self._safe_float(row.get("volume")) for row in symbol_rows]
            for index, row in enumerate(symbol_rows):
                if index < 1:
                    continue
                trade_date = str(row.get("date") or "").strip()
                close = closes[index]
                previous_close = closes[index - 1]
                if close <= 0 or previous_close <= 0:
                    continue
                history_closes = closes[: index + 1]
                history_volumes = volumes[: index + 1]
                ma5 = self._moving_average(history_closes, 5)
                ma20 = self._moving_average(history_closes, 20)
                ma60 = self._moving_average(history_closes, 60)
                avg_volume_20 = self._moving_average(history_volumes, 20)
                recent_returns = [
                    (history_closes[pos] / history_closes[pos - 1]) - 1.0
                    for pos in range(max(1, index - 9), index + 1)
                    if history_closes[pos - 1] > 0
                ]
                prior_window = history_closes[max(0, index - 20) : index]
                prior_high_20 = max(prior_window) if prior_window else previous_close
                lookback_anchor = history_closes[max(0, index - lookback_days)]
                lookback_momentum = ((close / lookback_anchor) - 1.0) if lookback_anchor > 0 else 0.0
                breakout_gap_20d = ((close / prior_high_20) - 1.0) if prior_high_20 > 0 else 0.0
                drawdown_from_20d_high = breakout_gap_20d
                volume_ratio_20d = (history_volumes[-1] / avg_volume_20) if avg_volume_20 and history_volumes[-1] > 0 else 1.0
                sample = {
                    "symbol": symbol,
                    "trade_date": trade_date,
                    "features": {
                        "recent_daily_return": (close / previous_close) - 1.0,
                        f"lookback_momentum_{lookback_days}d": lookback_momentum,
                        "price_vs_ma20": ((close / ma20) - 1.0) if ma20 else 0.0,
                        "ma_alignment": ((ma5 / ma20) - 1.0) if ma5 and ma20 else 0.0,
                        "ma_stack": ((ma20 / ma60) - 1.0) if ma20 and ma60 else 0.0,
                        "volume_ratio_20d": volume_ratio_20d - 1.0,
                        "volatility_10d": self._stddev(recent_returns),
                        "breakout_gap_20d": breakout_gap_20d,
                        "drawdown_from_20d_high": drawdown_from_20d_high,
                    },
                    "target": None,
                }
                if index + horizon_days < len(closes) and close > 0:
                    forward_close = closes[index + horizon_days]
                    if forward_close > 0:
                        sample["target"] = (forward_close / close) - 1.0
                samples.append(sample)
        samples.sort(key=lambda item: (item["trade_date"], item["symbol"]))
        return samples

    def _training_stats(self, samples: list[dict], feature_names: list[str]) -> dict[str, tuple[float, float]]:
        stats: dict[str, tuple[float, float]] = {}
        for feature_name in feature_names:
            values = [self._safe_float(sample["features"].get(feature_name)) for sample in samples]
            if not values:
                stats[feature_name] = (0.0, 1.0)
                continue
            mean = sum(values) / len(values)
            std = self._stddev(values) or 1.0
            stats[feature_name] = (mean, std)
        return stats

    def _build_lightgbm_explanations(
        self,
        *,
        symbol_id: int,
        trade_date: str,
        feature_values: dict[str, float],
        feature_names: list[str],
        feature_importance: dict[str, float],
        feature_stats: dict[str, tuple[float, float]],
    ) -> list[dict]:
        rows: list[dict] = []
        for feature_name in feature_names:
            value = self._safe_float(feature_values.get(feature_name))
            mean, std = feature_stats.get(feature_name, (0.0, 1.0))
            z_score = ((value - mean) / std) if std else 0.0
            contribution = z_score * feature_importance.get(feature_name, 0.0) * self._feature_direction(feature_name)
            rows.append(
                {
                    "symbol_id": symbol_id,
                    "trade_date": trade_date,
                    "feature_name": feature_name,
                    "feature_value": round(value * 100, 4),
                    "contribution": round(contribution * 100, 4),
                    "direction": "positive" if contribution >= 0 else "negative",
                    "display_order": 0,
                }
            )
        ranked = sorted(rows, key=lambda item: abs(float(item.get("contribution") or 0.0)), reverse=True)[:5]
        for index, row in enumerate(ranked, start=1):
            row["display_order"] = index
        return ranked

    def _build_detail_row(
        self,
        *,
        symbol_id: int,
        trade_date: str,
        score: float,
        rank_value: float,
        universe_size: int,
        horizon_days: int,
        run_name: str,
    ) -> dict:
        enriched = enrich_model_output(
            {
                "score": score,
                "rank_value": rank_value,
                "universe_size": universe_size,
                "percentile": round(
                    max(0.0, min(100.0, (1 - ((float(rank_value) - 1) / max(universe_size, 1))) * 100.0)),
                    1,
                ),
                "target_horizon_days": max(5, min(20, horizon_days)),
                "model_run": {"name": run_name},
            },
            lang="en",
        ) or {}
        return {
            "symbol_id": symbol_id,
            "trade_date": trade_date,
            "confidence": enriched.get("confidence"),
            "bullish_prob": enriched.get("bullish_prob"),
            "bearish_prob": enriched.get("bearish_prob"),
            "expected_return_5d": enriched.get("expected_return_5d"),
            "expected_return_20d": enriched.get("expected_return_20d"),
            "expected_drawdown_20d": enriched.get("expected_drawdown_20d"),
            "model_reward_risk_ratio": enriched.get("model_reward_risk_ratio"),
            "risk_score": enriched.get("risk_score"),
            "target_horizon_days": enriched.get("target_horizon_days"),
            "universe_size": enriched.get("universe_size"),
            "percentile": enriched.get("percentile"),
            "regime_label": enriched.get("regime_label"),
            "conviction_bucket": enriched.get("conviction_bucket"),
            "position_size_hint": enriched.get("position_size_hint"),
            "entry_style": enriched.get("entry_style"),
            "signal_label": enriched.get("signal_label"),
            "signal_strength": enriched.get("signal_strength"),
            "summary_text": enriched.get("summary_text") or summarize_model_output(enriched, lang="en"),
        }

    def _train_baseline(
        self,
        *,
        run_name: str,
        signal_type: str,
        lookback_days: int,
        normalized_tickers: set[str] | None,
        market: str | None,
        universe: str | None,
        rows: list[dict],
    ) -> int:
        signal_rows: list[dict] = []
        detail_rows: list[dict] = []
        explanation_rows: list[dict] = []
        by_date: dict[str, list[dict]] = defaultdict(list)
        close_history_by_symbol: dict[str, list[float]] = defaultdict(list)
        volume_history_by_symbol: dict[str, list[float]] = defaultdict(list)
        explanation_row_limit = 2000
        if str(market or "").upper() == "US" and len(normalized_tickers or []) >= 5000:
            explanation_row_limit = 0

        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            model_repo = ModelRunRepository(db)
            prediction_repo = PredictionWriteRepository(db)
            detail_repo = PredictionDetailRepository(db)
            explanation_repo = PredictionExplanationRepository(db)
            model_repo.complete_stale_running_runs(
                stale_after_hours=6,
                message_prefix="Trainer cleanup closed a stale running model run.",
            )

            dates = sorted({row["date"] for row in rows if row.get("date")})
            latest_prediction_date = dates[-1] if dates else None
            run = model_repo.create_run(
                name=run_name,
                model_type="local_baseline",
                market=market or "US",
                universe=universe or ("local_watchlist" if normalized_tickers else "full_dataset"),
                train_start=dates[0] if dates else None,
                train_end=dates[-1] if dates else None,
                test_start=dates[0] if dates else None,
                test_end=dates[-1] if dates else None,
                config={
                    "model_type": "baseline",
                    "signal_type": signal_type,
                    "lookback_days": lookback_days,
                    "ticker_count": len(normalized_tickers or []),
                    "tickers": sorted(normalized_tickers) if normalized_tickers else None,
                },
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
                    "_explanations": self._baseline_explanations(
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
                    if latest_prediction_date and record["trade_date"] == latest_prediction_date:
                        detail_rows.append(
                            self._build_detail_row(
                                symbol_id=record["symbol_id"],
                                trade_date=record["trade_date"],
                                score=float(record["score"]),
                                rank_value=float(record["rank_value"]),
                                universe_size=len(ranked),
                                horizon_days=lookback_days * 5,
                                run_name=run_name,
                            )
                        )
                    if (
                        explanation_row_limit != 0
                        and latest_prediction_date
                        and record["trade_date"] == latest_prediction_date
                        and idx <= explanation_row_limit
                    ):
                        explanation_rows.extend(record.pop("_explanations", []))
                    else:
                        record.pop("_explanations", None)
                    signal_rows.append(record)

            if not signal_rows:
                model_repo.complete_run(run.id, status="failed", artifact_path=None)
                raise RuntimeError(
                    "The baseline trainer produced no predictions. You likely need at least 2-3 trading days per symbol."
                )

            count = prediction_repo.replace_for_model_run(run.id, signal_rows)
            detail_repo.replace_for_model_run(run.id, detail_rows)
            explanation_repo.replace_for_model_run(run.id, explanation_rows)
            artifact_path = str((self.settings.artifacts_dir / f"model_run_{run.id}.json").resolve())
            Path(artifact_path).write_text(
                json.dumps(
                    {
                        "model": run_name,
                        "model_type": "baseline",
                        "signal_type": signal_type,
                        "lookback_days": lookback_days,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            model_repo.complete_run(run.id, status="success", artifact_path=artifact_path)
            return count

    def _train_lightgbm(
        self,
        *,
        run_name: str,
        signal_type: str,
        lookback_days: int,
        normalized_tickers: set[str] | None,
        market: str | None,
        universe: str | None,
        rows: list[dict],
    ) -> int:
        if lgb is None:
            raise RuntimeError("LightGBM is not installed. Run `.venv/bin/pip install -r requirements.txt` first.")
        if signal_type != "momentum":
            raise RuntimeError("The LightGBM trainer currently supports `momentum` signal_type only.")

        horizon_days = max(5, min(10, lookback_days * 2))
        feature_names = self._feature_names(lookback_days=lookback_days)
        samples = self._build_lightgbm_samples(rows=rows, lookback_days=lookback_days, horizon_days=horizon_days)
        if not samples:
            raise RuntimeError("LightGBM trainer found no usable feature rows. The market lake may still be too short.")

        samples_by_date: dict[str, list[dict]] = defaultdict(list)
        labeled_by_date: dict[str, list[dict]] = defaultdict(list)
        all_dates: list[str] = []
        seen_dates: set[str] = set()
        for sample in samples:
            trade_date = sample["trade_date"]
            samples_by_date[trade_date].append(sample)
            if sample.get("target") is not None:
                labeled_by_date[trade_date].append(sample)
            if trade_date not in seen_dates:
                seen_dates.add(trade_date)
                all_dates.append(trade_date)
        all_dates.sort()
        warmup_dates = max(20, lookback_days * 8)
        if len(all_dates) <= warmup_dates + 5:
            raise RuntimeError("LightGBM trainer needs a longer price history before it can score recent trade dates.")
        prediction_start_index = max(warmup_dates, len(all_dates) - 60)
        prediction_dates = all_dates[prediction_start_index:]
        if not prediction_dates:
            raise RuntimeError("LightGBM trainer found no prediction dates.")

        first_prediction_date = prediction_dates[0]
        train_pool = [
            sample
            for sample in samples
            if sample.get("target") is not None and sample["trade_date"] < first_prediction_date
        ]
        if len(train_pool) < 1000:
            raise RuntimeError("LightGBM trainer needs more labeled history before the first prediction date.")

        with SessionLocal() as db:
            symbol_repo = SymbolRepository(db)
            model_repo = ModelRunRepository(db)
            prediction_repo = PredictionWriteRepository(db)
            detail_repo = PredictionDetailRepository(db)
            explanation_repo = PredictionExplanationRepository(db)
            model_repo.complete_stale_running_runs(
                stale_after_hours=6,
                message_prefix="Trainer cleanup closed a stale running model run.",
            )
            symbol_map = {symbol.ticker.upper(): symbol.id for symbol in symbol_repo.list_symbols()}
            run = model_repo.create_run(
                name=run_name,
                model_type="lightgbm_multifactor",
                market=market or "US",
                universe=universe or ("local_watchlist" if normalized_tickers else "full_dataset"),
                train_start=all_dates[0] if all_dates else None,
                train_end=prediction_dates[-1] if prediction_dates else None,
                test_start=prediction_dates[0] if prediction_dates else None,
                test_end=prediction_dates[-1] if prediction_dates else None,
                config={
                    "model_type": "lightgbm",
                    "signal_type": signal_type,
                    "lookback_days": lookback_days,
                    "prediction_horizon_days": horizon_days,
                    "ticker_count": len(normalized_tickers or []),
                    "prediction_dates": len(prediction_dates),
                },
                artifact_path=None,
                status="running",
            )

            signal_rows: list[dict] = []
            detail_rows: list[dict] = []
            explanation_rows: list[dict] = []
            retrain_interval = 5
            max_training_rows = 120000 if str(market or "").upper() == "US" else 80000
            normalized_market = str(market or "").upper()
            full_market_run = (
                len(normalized_tickers or []) >= 5000
                or str(universe or "").lower() in {"full_market_cn_lake", "full_market_us_lake", "full_dataset"}
            )
            explanation_rank_limit = 2000
            if full_market_run and normalized_market in {"CN", "US"}:
                explanation_rank_limit = 0
            model = None
            feature_importance: dict[str, float] = {}
            feature_stats: dict[str, tuple[float, float]] = {}
            latest_prediction_date = prediction_dates[-1]

            for index, trade_date in enumerate(prediction_dates):
                if not train_pool:
                    train_pool.extend(labeled_by_date.get(trade_date, []))
                    continue
                if model is None or index % retrain_interval == 0:
                    train_window = train_pool[-max_training_rows:]
                    x_train = [
                        [self._safe_float(sample["features"].get(feature_name)) for feature_name in feature_names]
                        for sample in train_window
                    ]
                    y_train = [self._safe_float(sample.get("target")) for sample in train_window]
                    sample_weights = [
                        0.65 + (position / max(len(train_window) - 1, 1)) * 0.7
                        for position in range(len(train_window))
                    ]
                    model = lgb.LGBMRegressor(
                        objective="regression",
                        n_estimators=260,
                        learning_rate=0.05,
                        num_leaves=63,
                        min_child_samples=40,
                        subsample=0.8,
                        colsample_bytree=0.8,
                        reg_alpha=0.05,
                        reg_lambda=0.1,
                        random_state=42,
                        n_jobs=-1,
                    )
                    model.fit(x_train, y_train, sample_weight=sample_weights)
                    raw_importance_obj = getattr(model, "feature_importances_", None)
                    if raw_importance_obj is None:
                        raw_importance = [0.0] * len(feature_names)
                    else:
                        raw_importance = [float(value) for value in list(raw_importance_obj)]
                    if len(raw_importance) < len(feature_names):
                        raw_importance.extend([0.0] * (len(feature_names) - len(raw_importance)))
                    raw_importance = raw_importance[: len(feature_names)]
                    importance_total = sum(raw_importance) or 1.0
                    feature_importance = {
                        feature_name: raw_importance[pos] / importance_total
                        for pos, feature_name in enumerate(feature_names)
                    }
                    feature_stats = self._training_stats(train_window, feature_names)

                date_samples = samples_by_date.get(trade_date) or []
                if not date_samples:
                    train_pool.extend(labeled_by_date.get(trade_date, []))
                    continue
                x_date = [
                    [self._safe_float(sample["features"].get(feature_name)) for feature_name in feature_names]
                    for sample in date_samples
                ]
                predicted_scores = list(model.predict(x_date)) if model is not None else []
                ranked_pairs = sorted(
                    zip(date_samples, predicted_scores, strict=False),
                    key=lambda pair: float(pair[1]),
                    reverse=True,
                )
                for rank_index, (sample, raw_score) in enumerate(ranked_pairs, start=1):
                    symbol = sample["symbol"]
                    symbol_id = symbol_map.get(symbol)
                    if symbol_id is None:
                        continue
                    score = self._clamp(float(raw_score), -0.35, 0.35)
                    signal_rows.append(
                        {
                            "symbol_id": symbol_id,
                            "trade_date": trade_date,
                            "score": score,
                            "rank_value": float(rank_index),
                        }
                    )
                    if trade_date == latest_prediction_date:
                        detail_rows.append(
                            self._build_detail_row(
                                symbol_id=symbol_id,
                                trade_date=trade_date,
                                score=score,
                                rank_value=float(rank_index),
                                universe_size=len(ranked_pairs),
                                horizon_days=horizon_days,
                                run_name=run_name,
                            )
                        )
                        if explanation_rank_limit and rank_index <= explanation_rank_limit:
                            explanation_rows.extend(
                                self._build_lightgbm_explanations(
                                    symbol_id=symbol_id,
                                    trade_date=trade_date,
                                    feature_values=sample["features"],
                                    feature_names=feature_names,
                                    feature_importance=feature_importance,
                                    feature_stats=feature_stats,
                                )
                            )
                train_pool.extend(labeled_by_date.get(trade_date, []))

            if not signal_rows:
                model_repo.complete_run(run.id, status="failed", artifact_path=None)
                raise RuntimeError("LightGBM trainer produced no predictions.")

            count = prediction_repo.replace_for_model_run(run.id, signal_rows)
            detail_repo.replace_for_model_run(run.id, detail_rows)
            explanation_repo.replace_for_model_run(run.id, explanation_rows)
            artifact_path = str((self.settings.artifacts_dir / f"model_run_{run.id}.json").resolve())
            Path(artifact_path).write_text(
                json.dumps(
                    {
                        "model": run_name,
                        "model_type": "lightgbm",
                        "signal_type": signal_type,
                        "lookback_days": lookback_days,
                        "prediction_horizon_days": horizon_days,
                        "feature_names": feature_names,
                        "prediction_dates": prediction_dates,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            model_repo.complete_run(run.id, status="success", artifact_path=artifact_path)
            return count

    def train(
        self,
        run_name: str = "lightgbm_momentum",
        signal_type: str = "momentum",
        lookback_days: int = 3,
        tickers: list[str] | None = None,
        market: str | None = None,
        universe: str | None = None,
        model_type: str = "lightgbm",
    ) -> int:
        normalized_tickers = {
            str(ticker).strip().upper() for ticker in (tickers or []) if str(ticker).strip()
        } or None
        rows = self._load_rows(tickers=normalized_tickers)
        if not rows:
            raise RuntimeError("No local market data found. Refresh the Parquet market lake or rebuild normalized CSVs first.")
        if lookback_days < 1:
            raise RuntimeError("lookback_days must be at least 1.")
        normalized_model_type = str(model_type or "lightgbm").strip().lower()
        if normalized_model_type in {"baseline", "local_baseline"}:
            raise RuntimeError(
                "The legacy baseline trainer has been retired. Use model_type=`lightgbm` for all new signal runs."
            )
        if normalized_model_type in {"lightgbm", "lightgbm_multifactor", "lgbm"}:
            return self._train_lightgbm(
                run_name=run_name,
                signal_type=signal_type,
                lookback_days=lookback_days,
                normalized_tickers=normalized_tickers,
                market=market,
                universe=universe,
                rows=rows,
            )
        raise RuntimeError(f"Unsupported model_type `{model_type}`.")
