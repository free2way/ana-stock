"""Same-panel model comparison for bounded execution-contract pilots."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
import math
import statistics

import lightgbm as lgb

from app.services.stock_selection.factor_baseline import equal_weight_predictions, fit_ridge_factor_baseline
from app.services.stock_selection.factor_pipeline import (
    CrossSectionalFactorPipeline, FactorDirection, FactorObservation, FactorSpec,
)
from app.services.stock_selection.ranker import RankerConfig, fit_cross_sectional_ranker


def _observations(samples: list[dict], feature_names: list[str]) -> tuple[FactorObservation, ...]:
    return tuple(FactorObservation(
        observation_id=f"{row['symbol']}:{row['trade_date']}:5",
        ticker=row["symbol"], feature_date=date.fromisoformat(row["trade_date"]), horizon_days=5,
        features={name: float(row["features"].get(name) or 0.0) for name in feature_names},
        label_available_date=(date.fromisoformat(row["label_available_date"])
                              if row.get("label_available_date") and row.get("target") is not None else None),
        label_value=(float(row["target"]) if row.get("target") is not None else None),
        eligible=True,
    ) for row in samples)


def _metrics(predictions: dict[str, float], test_by_id: dict[str, FactorObservation], *, top_n: int) -> dict:
    grouped: dict[date, list[tuple[str, float]]] = defaultdict(list)
    for sample_id, score in predictions.items():
        grouped[test_by_id[sample_id].feature_date].append((sample_id, float(score)))
    selected, daily_returns = [], []
    for day in sorted(grouped):
        top = sorted(grouped[day], key=lambda item: (-item[1], item[0]))[:top_n]
        closed = [float(test_by_id[item[0]].label_value) for item in top
                  if test_by_id[item[0]].label_value is not None]
        selected.extend(top)
        if closed:
            daily_returns.append(statistics.fmean(closed))
    returns = [float(test_by_id[item[0]].label_value) for item in selected
               if test_by_id[item[0]].label_value is not None]
    equity = peak = 1.0
    max_drawdown = 0.0
    for value in daily_returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1.0)
    gains = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    return {
        "selected_count": len(selected), "closed_count": len(returns),
        "unverified_count": len(selected) - len(returns),
        "hit_rate": sum(value > 0 for value in returns) / len(returns) if returns else None,
        "mean_net_return": statistics.fmean(returns) if returns else None,
        "median_net_return": statistics.median(returns) if returns else None,
        "profit_loss_ratio": ((statistics.fmean(gains) / abs(statistics.fmean(losses)))
                              if gains and losses else None),
        "cohort_path_max_drawdown": max_drawdown if daily_returns else None,
        "gate_pass": bool(returns and statistics.fmean(returns) > 0 and statistics.median(returns) > 0),
    }


def compare_frozen_panel_models(*, train: list[dict], test: list[dict], feature_names: list[str],
                                lower_better: set[str], top_n: int = 5) -> dict:
    if len(train) < 100 or not test or not feature_names:
        raise ValueError("frozen panel comparison requires populated train/test/features")
    first_test = min(date.fromisoformat(row["trade_date"]) for row in test)
    train_obs = _observations(train, feature_names)
    test_obs = _observations(test, feature_names)
    if any(item.label_available_date is None or item.label_available_date >= first_test for item in train_obs):
        raise ValueError("training labels are not frozen before holdout")
    specs = tuple(FactorSpec(name, FactorDirection.LOWER_BETTER if name in lower_better
                             else FactorDirection.HIGHER_BETTER) for name in feature_names)
    pipeline = CrossSectionalFactorPipeline(specs)
    train_scores, test_scores = pipeline.transform(train_obs), pipeline.transform(test_obs)
    train_by_id = {item.sample_id: item for item in train_scores}
    test_by_id = {item.observation_id: item for item in test_obs}
    if len(test_by_id) != len(test):
        raise ValueError("duplicate frozen holdout candidate")

    outputs: dict[str, dict[str, float]] = {}
    outputs["equal_weight"] = {item.sample_id: item.raw_score
                               for item in equal_weight_predictions(test_scores)}
    ridge = fit_ridge_factor_baseline(train_scores, factor_names=tuple(feature_names),
        horizon_days=5, prediction_date=first_test, alpha=10.0)
    outputs["ridge"] = {item.sample_id: item.raw_score for item in ridge.predict(test_scores)}
    ranker = fit_cross_sectional_ranker(train_scores, config=RankerConfig(
        feature_names=tuple(feature_names), horizon_days=5, n_estimators=120),
        prediction_date=first_test)
    outputs["lambdarank"] = {item.sample_id: item.raw_score for item in ranker.predict(test_scores)}

    x_train = [[float(train_by_id[f"{row['symbol']}:{row['trade_date']}:5"].factor_values[name])
                for name in feature_names] for row in train]
    y_train = [float(row["target"]) for row in train]
    estimator = lgb.LGBMRegressor(objective="regression", n_estimators=260, learning_rate=0.05,
        num_leaves=63, min_child_samples=40, subsample=0.8, colsample_bytree=0.8,
        reg_alpha=0.05, reg_lambda=0.1, random_state=42, n_jobs=1, verbosity=-1,
        deterministic=True, force_col_wise=True)
    estimator.fit(x_train, y_train)
    ordered_test = list(test_scores)
    outputs["lightgbm"] = {item.sample_id: float(score) for item, score in zip(
        ordered_test, estimator.predict([[float(item.factor_values[name]) for name in feature_names]
                                         for item in ordered_test]), strict=True)}

    if any(set(values) != set(test_by_id) for values in outputs.values()):
        raise ValueError("models did not score the identical frozen panel")
    reports = {model: _metrics(values, test_by_id, top_n=top_n) for model, values in outputs.items()}
    return {"panel_candidate_count": len(test_by_id), "first_holdout_date": first_test.isoformat(),
            "top_n": top_n, "models": reports,
            "passing_models": sorted(model for model, report in reports.items() if report["gate_pass"])}
