"""Screening model arena: walk-forward horse race on executable CN labels.

Review-driven experiment (2026-09-30). Motivated by months of low realized hit
rate from the production screening model. Reproduces the legacy composite
target exactly as `trainer.py::_build_short_horizon_target_profile`, then
races it against executable-horizon challengers under one identical,
honest trading convention:

  signal at D close -> entry at D+1 open (limit-up open = unbuyable, slot
  becomes cash) -> exit at D+5 close, flat round-trip cost, frozen Top-N
  membership (no backfilling rejects).

All models share the same point-in-time features (computable at D close),
the same universe rules (mirroring `stock_selection/universe.py` defaults),
the same label maturity queue (label usable only after its exit close is
strictly before the prediction date) and the same evaluation.

Stage 2 (same-day review, 2026-09-30): family-replacement challengers.
The GBDT variants trailed in the first honest window while the linear
profit_logit was the only positive head, so the arena now also races
regularized linear models (ridge / elastic-net), a bagged tree ensemble
(extra-trees), a GBDT-family control (CatBoost ordered boosting) and a
linear+tree rank blend. Same features, labels, universe and execution
convention; the walk-forward decides, not taste.

This is a research script. It writes a JSON artifact under
`data/artifacts/screening_model_arena/` and prints a summary. It never
touches production tables.
"""

from __future__ import annotations

import argparse
import json
import math
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import polars as pl

ROOT_DIR = Path(__file__).resolve().parents[1]

LAKE_GLOB = str(ROOT_DIR / "data" / "lake" / "cn_daily" / "date=*" / "part.parquet")

# --- Conventions (aligned with repo review docs / universe defaults) ---------
ROUND_TRIP_COST_BPS = 40.0
COST = ROUND_TRIP_COST_BPS / 10_000.0
MIN_PRICE = 1.0
MIN_ADV20 = 50_000_000.0
MIN_HISTORY_SESSIONS = 120
CORP_ACTION_JUMP = 0.80          # |close-to-close adjusted| within holding
LIMIT_OPEN_BUFFER = 0.98         # open at/above band*0.98 is treated as unbuyable
LEGACY_COMPOSITE_VERSION = "short_horizon_composite_v1"
EXEC_LABEL_VERSION = "confirmed_next_open_fixed_exit_fill_cost_v2"
ARENA_SCHEMA_VERSION = "screening_model_arena_v1"


def limit_band_pct(symbol: str) -> float:
    """CN price-limit band by board (approximation from symbol prefix)."""
    code = symbol.split(".")[0]
    if code.startswith(("300", "301", "688", "689")):
        return 20.0
    if code.startswith(("83", "87", "88", "43", "92", "82")):
        return 30.0
    return 10.0


def legacy_composite_target(
    *,
    anchor_close: float,
    next_open: float | None,
    next_high: float | None,
    next_low: float | None,
    next_close: float | None,
    max_3d_high: float | None,
    min_3d_low: float | None,
    max_5d_high: float | None,
    min_5d_low: float | None,
    close_5d: float | None,
    limit_band_pct: float | None,
) -> float | None:
    """Faithful re-implementation of trainer._build_short_horizon_target_profile."""
    if anchor_close <= 0:
        return None

    def f(value: float | None, default: float = 0.0) -> float:
        try:
            value = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default
        return value if math.isfinite(value) else default

    def ret(future: float | None, anchor: float) -> float | None:
        if future is None or not math.isfinite(future) or anchor <= 0:
            return None
        return future / anchor - 1.0

    next_1d_close_return = ret(next_close, anchor_close)
    next_1d_open_gap = ret(next_open, anchor_close)
    next_1d_open_to_high = ret(next_high, next_open) if (next_open or 0) > 0 else None
    next_1d_open_to_close = ret(next_close, next_open) if (next_open or 0) > 0 else None
    next_1d_low_drawdown = ret(next_low, anchor_close)
    next_3d_max_return = ret(max_3d_high, anchor_close)
    next_3d_max_drawdown = ret(min_3d_low, anchor_close)
    next_5d_max_return = ret(max_5d_high, anchor_close)
    next_5d_max_drawdown = ret(min_5d_low, anchor_close)

    failed_after_gap_up = 0.0
    if (
        next_1d_open_gap is not None
        and next_1d_open_gap >= 0.025
        and next_1d_open_to_close is not None
        and next_1d_open_to_close <= -0.02
    ):
        failed_after_gap_up = 1.0

    tradable_next_day = 1.0
    cn_limit_threshold = ((limit_band_pct - 0.2) / 100.0) if limit_band_pct and limit_band_pct > 0 else None
    if next_1d_open_gap is not None and cn_limit_threshold is not None and next_1d_open_gap >= cn_limit_threshold:
        tradable_next_day = 0.0
    elif next_1d_open_gap is not None and next_1d_open_gap >= 0.095:
        tradable_next_day = 0.0

    upside = (
        max(next_1d_close_return or 0.0, -0.12) * 0.20
        + max(next_1d_open_to_high or 0.0, 0.0) * 0.30
        + max(next_3d_max_return or 0.0, 0.0) * 0.35
        + max(next_5d_max_return or 0.0, 0.0) * 0.15
    )
    downside = (
        abs(min(next_1d_low_drawdown or 0.0, 0.0)) * 0.18
        + abs(min(next_3d_max_drawdown or 0.0, 0.0)) * 0.32
        + abs(min(next_5d_max_drawdown or 0.0, 0.0)) * 0.18
    )
    penalty = failed_after_gap_up * 0.05 + (0.04 if tradable_next_day < 0.5 else 0.0)
    composite = upside - downside - penalty
    return max(-0.35, min(0.45, composite))


def load_lake() -> pl.DataFrame:
    # Read per-file: a few partitions store `date` as Date while the rest use
    # String, and polars refuses to unify that mismatch across a glob.
    keep = ["date", "symbol", "open", "high", "low", "close", "volume", "adj_close"]
    frames = []
    for path in sorted(ROOT_DIR.glob("data/lake/cn_daily/date=*/part.parquet")):
        part = pl.read_parquet(path).select(keep).with_columns(
            pl.col("date").cast(pl.String).str.slice(0, 10).alias("date"),
            pl.col("symbol").str.strip_chars(),
        )
        frames.append(part)
    frame = pl.concat(frames, how="vertical")
    frame = frame.filter(
        pl.col("close").is_not_null() & (pl.col("close") > 0)
        & pl.col("open").is_not_null() & (pl.col("open") > 0)
        & pl.col("volume").is_not_null()
    ).sort(["symbol", "date"])
    return frame


FEATURE_NAMES = [
    "ret_1d", "ret_3d", "ret_5d", "ret_10d", "ret_20d", "ret_60d",
    "exc_ret_5d", "exc_ret_20d",
    "gap_1d", "inret_1d",
    "sma_ratio_5", "sma_ratio_10", "sma_ratio_20", "sma_ratio_60",
    "dist_20d_high", "dist_60d_high", "dist_20d_low",
    "range_pos_20d", "vol_5d", "vol_20d", "atr_ratio_14",
    "vol_ratio_5_20", "log_dollar_vol_20", "rsi_14",
]


def build_panel_step1(frame: pl.DataFrame) -> pl.DataFrame:
    """Per-symbol shifts, rolling extremes and history counts (sorted frame)."""
    s = pl.col("symbol")

    def over(expr: pl.Expr) -> pl.Expr:
        return expr.over(s)

    step1 = frame.with_columns(
        (pl.col("adj_close") / pl.col("close")).alias("factor"),
    ).with_columns(
        (pl.col("open") * pl.col("factor")).alias("adj_open"),
        (pl.col("high") * pl.col("factor")).alias("adj_high"),
        (pl.col("low") * pl.col("factor")).alias("adj_low"),
    ).with_columns(
        pl.int_range(pl.len()).over(s).add(1).alias("n_hist"),
    ).with_columns(
        over(pl.col("adj_close").shift(1)).alias("prev_adj_close"),
        over(pl.col("close").shift(1)).alias("prev_close"),
        over(pl.col("adj_close").shift(-1)).alias("next_adj_close"),
        over(pl.col("adj_open").shift(-1)).alias("next_adj_open"),
        over(pl.col("open").shift(-1)).alias("next_open"),
        over(pl.col("high").shift(-1)).alias("next_high"),
        over(pl.col("low").shift(-1)).alias("next_low"),
        over(pl.col("close").shift(-1)).alias("next_close"),
        over(pl.col("adj_close").shift(-5)).alias("exit_adj_close_5d"),
        over(pl.col("date").shift(-5)).alias("exit_date_5d"),
        over(pl.col("date").shift(-1)).alias("entry_date"),
        over(pl.col("adj_close").rolling_max(3).shift(-3)).alias("max_3d_high"),
        over(pl.col("low").rolling_min(3).shift(-3)).alias("min_3d_low"),
        over(pl.col("high").rolling_max(5).shift(-5)).alias("max_5d_high"),
        over(pl.col("low").rolling_min(5).shift(-5)).alias("min_5d_low"),
        over(pl.col("close").shift(-5)).alias("close_5d"),
        over(pl.col("adj_low").rolling_min(5).shift(-5)).alias("path_min_adj_low_5d"),
        over(pl.col("adj_close").pct_change().abs().rolling_max(5).shift(-5)).alias("path_max_jump_5d"),
        over(pl.col("close").rolling_mean(20)).alias("sma20_for_range"),
        over(pl.col("adj_high").rolling_max(20)).alias("hi20_for_range"),
        over(pl.col("adj_low").rolling_min(20)).alias("lo20_for_range"),
        over(pl.col("adj_high").rolling_max(60)).alias("hi60"),
    )
    return step1


def build_panel_step2(step1: pl.DataFrame) -> pl.DataFrame:
    """Momentum / volatility / liquidity / context features at signal close."""
    s = pl.col("symbol")

    def over(expr: pl.Expr) -> pl.Expr:
        return expr.over(s)

    ret_1d = pl.col("close") / pl.col("prev_close") - 1.0
    step2 = step1.with_columns(
        ret_1d.alias("ret_1d"),
        (pl.col("close") / over(pl.col("close").shift(3)) - 1.0).alias("ret_3d"),
        (pl.col("close") / over(pl.col("close").shift(5)) - 1.0).alias("ret_5d"),
        (pl.col("close") / over(pl.col("close").shift(10)) - 1.0).alias("ret_10d"),
        (pl.col("close") / over(pl.col("close").shift(20)) - 1.0).alias("ret_20d"),
        (pl.col("close") / over(pl.col("close").shift(60)) - 1.0).alias("ret_60d"),
        (pl.col("open") / pl.col("prev_close") - 1.0).alias("gap_1d"),
        (pl.col("close") / pl.col("open") - 1.0).alias("inret_1d"),
        (pl.col("close") / over(pl.col("close").rolling_mean(5).shift(1)) - 1.0).alias("sma_ratio_5"),
        (pl.col("close") / over(pl.col("close").rolling_mean(10).shift(1)) - 1.0).alias("sma_ratio_10"),
        (pl.col("close") / over(pl.col("close").rolling_mean(20).shift(1)) - 1.0).alias("sma_ratio_20"),
        (pl.col("close") / over(pl.col("close").rolling_mean(60).shift(1)) - 1.0).alias("sma_ratio_60"),
        (pl.col("close") / pl.col("hi20_for_range") - 1.0).alias("dist_20d_high"),
        (pl.col("close") / pl.col("hi60") - 1.0).alias("dist_60d_high"),
        (pl.col("close") / pl.col("lo20_for_range") - 1.0).alias("dist_20d_low"),
    )
    step2 = step2.with_columns(
        (pl.col("ret_1d").rolling_std(5).over(s)).alias("vol_5d"),
        (pl.col("ret_1d").rolling_std(20).over(s)).alias("vol_20d"),
        ((pl.col("close") - pl.col("lo20_for_range"))
         / (pl.col("hi20_for_range") - pl.col("lo20_for_range"))).alias("range_pos_20d"),
    )
    tr = pl.max_horizontal(
        pl.col("adj_high") - pl.col("adj_low"),
        (pl.col("adj_high") - pl.col("prev_adj_close")).abs(),
        (pl.col("adj_low") - pl.col("prev_adj_close")).abs(),
    )
    gain = pl.col("ret_1d").clip(0.0, None).rolling_mean(14).over(s)
    loss = (-pl.col("ret_1d")).clip(0.0, None).rolling_mean(14).over(s)
    step2 = step2.with_columns(
        (tr.rolling_mean(14).over(s) / pl.col("close")).alias("atr_ratio_14"),
        over((pl.col("close") * pl.col("volume")).rolling_mean(20).log1p()).alias("log_dollar_vol_20"),
        (over(pl.col("volume").rolling_mean(5)) / over(pl.col("volume").rolling_mean(20))).alias("vol_ratio_5_20"),
        (gain / (gain + loss + 1e-12) * 100.0).alias("rsi_14"),
        pl.col("symbol").map_elements(limit_band_pct, return_dtype=pl.Float64).alias("band_pct"),
    )
    step2 = step2.with_columns(
        pl.col("ret_5d").mean().over(pl.col("date")).alias("mkt_ret_5d"),
        pl.col("ret_20d").mean().over(pl.col("date")).alias("mkt_ret_20d"),
    ).with_columns(
        (pl.col("ret_5d") - pl.col("mkt_ret_5d")).alias("exc_ret_5d"),
        (pl.col("ret_20d") - pl.col("mkt_ret_20d")).alias("exc_ret_20d"),
    )
    return step2
def build_panel_step3(step2: pl.DataFrame) -> pl.DataFrame:
    """Executable labels, legacy composite target and universe eligibility."""
    # --- Executable labels: D+1 open entry, D+5 close exit, flat cost --------
    entry_gap = pl.col("next_adj_open") / pl.col("close") - 1.0
    limit_threshold = pl.col("band_pct") * LIMIT_OPEN_BUFFER / 100.0
    s = pl.col("symbol")

    def over(expr: pl.Expr) -> pl.Expr:
        return expr.over(s)

    step3 = step2.with_columns(
        entry_gap.alias("entry_gap"),
        (entry_gap >= limit_threshold).alias("entry_unbuyable"),
        (pl.col("next_open").is_null() | pl.col("next_adj_open").is_null()).alias("entry_missing"),
        (pl.col("ret_1d") >= limit_threshold).alias("signal_day_limit_up_close"),
        ((pl.col("next_adj_close") / pl.col("next_adj_open") - 1.0) - COST).alias("net_ret_1d"),
        ((pl.col("exit_adj_close_5d") / pl.col("next_adj_open") - 1.0) - COST).alias("net_ret_5d"),
        ((pl.col("exit_adj_close_5d") / pl.col("close") - 1.0) - COST).alias("net_ret_5d_signal_close"),
        (pl.col("path_min_adj_low_5d") / pl.col("next_adj_open") - 1.0).alias("path_dd_5d"),
        pl.col("exit_adj_close_5d").is_not_null().alias("label_5d_mature"),
    ).with_columns(
        pl.when(pl.col("path_max_jump_5d") >= CORP_ACTION_JUMP)
        .then(True)
        .otherwise(False)
        .alias("corp_action_suspect"),
    )
    # `net_ret_5d` keeps the raw priced outcome (null only when prices are
    # missing); frozen-slot evaluation treats unbuyable/corp-action slots as
    # cash, matching the event-driven engine's reject-then-hold-cash rule.

    # --- Legacy composite target: faithful to trainer.py ---------------------
    def lr(new: str, future: str, anchor: str) -> pl.Expr:
        return (pl.col(future) / pl.col(anchor) - 1.0).alias(new)

    step3 = step3.with_columns(
        lr("l_next_1d_close", "next_close", "close"),
        lr("l_gap", "next_open", "close"),
        lr("l_open_to_high", "next_high", "next_open"),
        lr("l_open_to_close", "next_close", "next_open"),
        lr("l_low_dd", "next_low", "close"),
        lr("l_3d_max", "max_3d_high", "close"),
        lr("l_3d_min", "min_3d_low", "close"),
        lr("l_5d_max", "max_5d_high", "close"),
        lr("l_5d_min", "min_5d_low", "close"),
        lr("l_5d_close", "close_5d", "close"),
    )
    upside = (
        pl.max_horizontal(pl.col("l_next_1d_close").fill_null(0.0), pl.lit(-0.12)) * 0.20
        + pl.max_horizontal(pl.col("l_open_to_high").fill_null(0.0), pl.lit(0.0)) * 0.30
        + pl.max_horizontal(pl.col("l_3d_max").fill_null(0.0), pl.lit(0.0)) * 0.35
        + pl.max_horizontal(pl.col("l_5d_max").fill_null(0.0), pl.lit(0.0)) * 0.15
    )
    downside = (
        (-pl.min_horizontal(pl.col("l_low_dd").fill_null(0.0), pl.lit(0.0))) * 0.18
        + (-pl.min_horizontal(pl.col("l_3d_min").fill_null(0.0), pl.lit(0.0))) * 0.32
        + (-pl.min_horizontal(pl.col("l_5d_min").fill_null(0.0), pl.lit(0.0))) * 0.18
    )
    failed_gap = ((pl.col("l_gap") >= 0.025) & (pl.col("l_open_to_close") <= -0.02)).cast(pl.Float64)
    tradable_next_day = ~(
        (pl.col("l_gap") >= pl.col("band_pct") * LIMIT_OPEN_BUFFER / 100.0)
        | (pl.col("l_gap") >= 0.095)
    )
    step3 = step3.with_columns(
        (upside - downside - failed_gap * 0.05 - (~tradable_next_day).cast(pl.Float64) * 0.04)
        .clip(-0.35, 0.45)
        .alias("legacy_composite"),
    ).with_columns(
        # trainer requires len(future_rows) >= 3, otherwise the target is None.
        pl.when(pl.col("max_3d_high").is_null()).then(None).otherwise(pl.col("legacy_composite")).alias("legacy_composite"),
    )

    # --- Universe eligibility at signal time (no lookahead) ------------------
    step3 = step3.with_columns(
        over((pl.col("close") * pl.col("volume")).rolling_mean(20)).alias("adv20"),
    ).with_columns(
        (
            (pl.col("n_hist") >= MIN_HISTORY_SESSIONS)
            & (pl.col("close") >= MIN_PRICE)
            & (pl.col("volume") > 0)
            & (pl.col("adv20") >= MIN_ADV20)
            & pl.col("ret_20d").is_not_null()
            & pl.col("vol_20d").is_not_null()
            & pl.col("sma_ratio_60").is_not_null()
            & pl.col("rsi_14").is_not_null()
            & ~pl.col("signal_day_limit_up_close").fill_null(True)
        ).alias("eligible"),
    )
    panel = step3.select(
        ["date", "symbol", "n_hist", "eligible", "entry_date", "exit_date_5d",
         "entry_gap", "entry_unbuyable", "entry_missing", "corp_action_suspect",
         "net_ret_1d", "net_ret_5d", "net_ret_5d_signal_close",
         "path_dd_5d", "label_5d_mature", "legacy_composite", "band_pct"]
        + FEATURE_NAMES
    ).with_columns(
        [pl.col(c).cast(pl.Float32) for c in FEATURE_NAMES]
    )
    return panel


def build_panel() -> pl.DataFrame:
    frame = load_lake()
    return build_panel_step3(build_panel_step2(build_panel_step1(frame)))






@dataclass
class ArenaConfig:
    horizon: int = 5
    top_ns: tuple[int, ...] = (5, 10, 20)
    test_start_idx: int = 210
    retrain_every: int = 5
    train_window: int = 240
    max_train_rows_per_date: int = 1200
    purge_sessions: int = 2          # extra sessions beyond label maturity
    min_group_for_ranker: int = 15
    seed: int = 42


MODEL_KEYS = (
    "legacy_composite_lgbm",
    "exec_net_lgbm",
    "profit_logit",
    "rank_lambdarank",
    "exec_ridge",
    "exec_enet",
    "exec_extratrees",
    "exec_catboost",
    "blend_linear_tree",
    "baseline_lowvol",
    "baseline_reversal_5d",
    "baseline_momentum_20d",
)

LEARNABLE_MODEL_KEYS = (
    "legacy_composite_lgbm",
    "exec_net_lgbm",
    "profit_logit",
    "rank_lambdarank",
    "exec_ridge",
    "exec_enet",
    "exec_extratrees",
    "exec_catboost",
    "blend_linear_tree",
)

MODEL_LABELS = {
    "legacy_composite_lgbm": "Legacy composite LGBM (legacy label)",
    "exec_net_lgbm": "Executable net-return LGBM (new default)",
    "profit_logit": "Net-profit logit (challenger)",
    "rank_lambdarank": "LambdaRank cross-section",
    "exec_ridge": "Ridge on executable net-return (linear family)",
    "exec_enet": "ElasticNet on executable net-return (sparse linear)",
    "exec_extratrees": "ExtraTrees on executable net-return (bagged)",
    "exec_catboost": "CatBoost on executable net-return (GBDT control)",
    "blend_linear_tree": "Rank blend: ridge + extra-trees",
    "baseline_lowvol": "Baseline: low 20d volatility",
    "baseline_reversal_5d": "Baseline: 5d reversal",
    "baseline_momentum_20d": "Baseline: 20d momentum",
}


def lgbm_regressor():
    from lightgbm import LGBMRegressor
    return LGBMRegressor(
        objective="regression", n_estimators=260, learning_rate=0.05,
        num_leaves=63, min_child_samples=40, subsample=0.8,
        colsample_bytree=0.8, reg_alpha=0.05, reg_lambda=0.1,
        random_state=42, n_jobs=-1, verbosity=-1,
    )


def lgbm_ranker():
    from lightgbm import LGBMRanker
    return LGBMRanker(
        objective="lambdarank", n_estimators=260, learning_rate=0.05,
        num_leaves=63, min_child_samples=40, subsample=0.8,
        colsample_bytree=0.8, reg_alpha=0.05, reg_lambda=0.1,
        label_gain=[0, 1, 3],
        random_state=42, n_jobs=-1, verbosity=-1,
    )


def profit_logit_model():
    from sklearn.linear_model import SGDClassifier
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import make_pipeline
    return make_pipeline(
        StandardScaler(),
        SGDClassifier(
            loss="log_loss", penalty="elasticnet", alpha=1e-4, l1_ratio=0.3,
            max_iter=30, tol=1e-4, random_state=42, average=True,
        ),
    )


def ridge_model():
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    return make_pipeline(StandardScaler(), Ridge(alpha=20.0))


def enet_model():
    from sklearn.linear_model import ElasticNet
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    return make_pipeline(
        StandardScaler(),
        ElasticNet(alpha=0.005, l1_ratio=0.2, max_iter=300, tol=1e-3,
                   random_state=42),
    )


def extratrees_model():
    from sklearn.ensemble import ExtraTreesRegressor
    return ExtraTreesRegressor(
        n_estimators=200, min_samples_leaf=25, max_features=0.5,
        n_jobs=-1, random_state=42,
    )


def catboost_model():
    from catboost import CatBoostRegressor
    return CatBoostRegressor(
        iterations=250, depth=4, learning_rate=0.05, l2_leaf_reg=10.0,
        loss_function="RMSE", bootstrap_type="Bernoulli", subsample=0.8,
        random_seed=42, verbose=0, allow_writing_files=False, thread_count=-1,
    )


class RankBlend:
    """Cross-sectional rank average of fitted scorers.

    `TrainedModels.scores()` is called once per test date, so a plain rank
    average computed inside predict() is exactly the per-day cross-sectional
    blend used for Top-N ranking. No training state of its own.
    """

    def __init__(self, models: list):
        self.models = models

    def predict(self, X: np.ndarray) -> np.ndarray:
        parts = []
        for model in self.models:
            raw = np.asarray(model.predict(X), dtype=np.float64)
            ranks = raw.argsort().argsort().astype(np.float64)
            parts.append(ranks / max(len(ranks) - 1, 1))
        return np.mean(parts, axis=0)


def static_scores(model_key: str, frame: pl.DataFrame) -> np.ndarray:
    if model_key == "baseline_lowvol":
        col = "vol_20d"
    elif model_key == "baseline_reversal_5d":
        col = "ret_5d"
    elif model_key == "baseline_momentum_20d":
        col = "ret_20d"
    else:
        raise ValueError(model_key)
    values = frame[col].cast(pl.Float64).fill_null(0.0).to_numpy()
    return -values  # ascending metric; higher score ranks first


class TrainedModels:
    """Models fitted at one refresh point; produces scores for a test date."""

    def __init__(self, trained: dict, feature_names: list[str]):
        self.trained = trained
        self.feature_names = feature_names

    def scores(self, model_key: str, frame: pl.DataFrame) -> np.ndarray:
        if model_key in {"baseline_lowvol", "baseline_reversal_5d", "baseline_momentum_20d"}:
            return static_scores(model_key, frame)
        model = self.trained.get(model_key)
        if model is None:
            return np.full(frame.height, np.nan)
        X = frame.select(self.feature_names).to_numpy().astype(np.float64)
        X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            if model_key == "profit_logit":
                return model.predict_proba(X)[:, 1]
            return model.predict(X)


def fit_models(
    train_frame: pl.DataFrame,
    cfg: ArenaConfig,
    feature_names: list[str],
    rng: np.random.Generator,
    log_prefix: str,
) -> TrainedModels:
    """Fit every learnable model on the same matured, eligible sample set."""
    X_frame = train_frame.select(feature_names)
    X = X_frame.to_numpy().astype(np.float64)
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    trained: dict = {}

    legacy_y = train_frame["legacy_composite"].cast(pl.Float64).to_numpy()
    mask = np.isfinite(legacy_y)
    if mask.sum() >= 2000:
        model = lgbm_regressor()
        model.fit(X[mask], legacy_y[mask])
        trained["legacy_composite_lgbm"] = model

    exec_y = train_frame["net_ret_5d"].cast(pl.Float64).to_numpy()
    mask = np.isfinite(exec_y)
    if mask.sum() >= 2000:
        model = lgbm_regressor()
        model.fit(X[mask], exec_y[mask])
        trained["exec_net_lgbm"] = model

    profit_y = (exec_y > 0).astype(int)
    mask = np.isfinite(exec_y)
    if mask.sum() >= 2000 and 0 < profit_y[mask].mean() < 1:
        model = profit_logit_model()
        model.fit(X[mask], profit_y[mask])
        trained["profit_logit"] = model

    # LambdaRank: per-date relevance terciles on the executable net return.
    ranked = train_frame.with_columns(
        pl.col("net_ret_5d").cast(pl.Float64).alias("y5"),
    ).filter(pl.col("y5").is_finite())
    group_sizes = ranked.group_by("date").len().sort("date")
    big = group_sizes.filter(pl.col("len") >= cfg.min_group_for_ranker)
    if big.height >= 30:
        keep_dates = set(big["date"].to_list())
        ranked = ranked.filter(pl.col("date").is_in(list(keep_dates))).sort("date")
        relevance = (
            ranked.with_columns(
                pl.col("y5").rank("ordinal").over("date").alias("r"),
                pl.len().over("date").alias("n"),
            ).with_columns(
                pl.when(pl.col("r") <= (pl.col("n") // 3)).then(0)
                .when(pl.col("r") <= (2 * pl.col("n") // 3)).then(1)
                .otherwise(2).alias("rel")
            )
        )
        Xr = relevance.select(feature_names).to_numpy().astype(np.float64)
        Xr = np.nan_to_num(Xr, nan=0.0, posinf=0.0, neginf=0.0)
        rel = relevance["rel"].to_numpy().astype(np.int32)
        sizes = relevance.group_by("date", maintain_order=True).len()["len"].to_numpy()
        model = lgbm_ranker()
        model.fit(Xr, rel, group=sizes)
        trained["rank_lambdarank"] = model

    # --- Stage-2 family-replacement challengers (2026-09-30 review) --------
    # Same matured sample, same executable label, same X preprocessing.
    # Ridge/ElasticNet test the linear family with a proper solver (the
    # profit_logit SGD is a noisy proxy); ExtraTrees tests bagging vs
    # boosting; CatBoost controls for the GBDT family itself (ordered
    # boosting, shallower trees, stronger L2 on small panels).
    mask = np.isfinite(exec_y)
    if mask.sum() >= 2000:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            trained["exec_ridge"] = ridge_model().fit(X[mask], exec_y[mask])
            trained["exec_enet"] = enet_model().fit(X[mask], exec_y[mask])
            trained["exec_extratrees"] = extratrees_model().fit(
                X[mask], exec_y[mask]
            )
            trained["exec_catboost"] = catboost_model().fit(
                X[mask], exec_y[mask]
            )
    if "exec_ridge" in trained and "exec_extratrees" in trained:
        trained["blend_linear_tree"] = RankBlend(
            [trained["exec_ridge"], trained["exec_extratrees"]]
        )

    return TrainedModels(trained, feature_names)


def frozen_top_n_outcomes(
    scored: pl.DataFrame,
    top_n: int,
    score_col: str,
) -> dict:
    """Freeze Top-N by score, then price the slots with execution reality.

    Unbuyable / corp-action-suspect slots become cash (0.0). Immature labels
    are counted but excluded from realized stats.
    """
    ranked = scored.sort([score_col, "symbol"], descending=[True, False], nulls_last=True).head(top_n)
    rows = ranked.to_dicts()
    slot_returns: list[float] = []
    tradable_returns: list[float] = []
    gaps: list[float] = []
    cc_returns: list[float] = []
    unbuyable = 0
    pending = 0
    for row in rows:
        if row["entry_unbuyable"] or row["entry_missing"] or row["corp_action_suspect"]:
            unbuyable += 1
            slot_returns.append(0.0)
            continue
        if not row["label_5d_mature"] or row["net_ret_5d"] is None:
            pending += 1
            continue
        net = float(row["net_ret_5d"])
        slot_returns.append(net)
        tradable_returns.append(net)
        gaps.append(float(row["entry_gap"]))
        if row["net_ret_5d_signal_close"] is not None:
            cc_returns.append(float(row["net_ret_5d_signal_close"]))
    realized = [v for v in slot_returns if v is not None]
    return {
        "slot_returns": realized,
        "tradable_returns": tradable_returns,
        "gaps": gaps,
        "cc_returns": cc_returns,
        "unbuyable": unbuyable,
        "pending": pending,
        "n_slots": len(rows),
    }


def nav_stats(block_returns: list[float]) -> dict:
    nav, peak, max_dd = 1.0, 1.0, 0.0
    for ret in block_returns:
        nav *= (1.0 + ret)
        peak = max(peak, nav)
        max_dd = min(max_dd, nav / peak - 1.0)
    return {"total_return": nav - 1.0, "max_drawdown": max_dd, "blocks": len(block_returns)}


def quintile_of_rank(rank_one_based: int, n: int) -> int:
    if n <= 0:
        return 0
    q = int(math.ceil(rank_one_based / n * 5))
    return min(5, max(1, q))


def run_arena(cfg: ArenaConfig, verbose: bool = True) -> dict:
    started = time.time()
    if verbose:
        print("Loading CN lake and building panel ...")
    panel = build_panel()
    dates = panel["date"].unique().sort().to_list()
    date_to_idx = {d: i for i, d in enumerate(dates)}
    panel = panel.with_columns(
        pl.col("date").replace_strict(date_to_idx, return_dtype=pl.Int64).alias("date_idx"),
    )
    rng = np.random.default_rng(cfg.seed)

    learnable = LEARNABLE_MODEL_KEYS
    daily_rows: list[dict] = []
    reliability: dict[str, list[tuple[int, float]]] = {k: [] for k in MODEL_KEYS}
    universe_daily: list[dict] = []
    models: TrainedModels | None = None
    retrain_log: list[dict] = []

    test_indices = list(range(cfg.test_start_idx, len(dates)))
    if verbose:
        print(f"Test window: {dates[cfg.test_start_idx]} .. {dates[-1]} ({len(test_indices)} sessions)")

    for t in test_indices:
        need_refresh = models is None or (t - cfg.test_start_idx) % cfg.retrain_every == 0
        if need_refresh:
            train_hi = t - (cfg.horizon + cfg.purge_sessions)
            train_lo = max(0, t - cfg.train_window)
            train_frame = panel.filter(
                (pl.col("date_idx") >= train_lo)
                & (pl.col("date_idx") <= train_hi)
                & pl.col("eligible")
                & pl.col("label_5d_mature")
                & pl.col("net_ret_5d").is_not_null()
            )
            if cfg.max_train_rows_per_date and train_frame.height:
                cap = cfg.max_train_rows_per_date
                train_frame = train_frame.with_columns(
                    pl.Series("u", rng.random(train_frame.height)),
                ).with_columns(
                    pl.col("u").rank("ordinal").over("date").alias("rk"),
                ).filter(pl.col("rk") <= cap).drop("u", "rk")
            models = fit_models(train_frame, cfg, FEATURE_NAMES, rng, log_prefix=dates[t])
            retrain_log.append({
                "as_of": dates[t], "train_lo": dates[train_lo],
                "train_hi": dates[train_hi], "rows": train_frame.height,
                "fitted": sorted(models.trained.keys()),
            })
            if verbose:
                print(f"  [retrain @{dates[t]}] rows={train_frame.height} "
                      f"fitted={sorted(models.trained.keys())}")

        test_frame = panel.filter((pl.col("date_idx") == t) & pl.col("eligible"))
        universe_mean_val = (
            test_frame["net_ret_5d"].mean() if test_frame.height else None
        )
        universe_daily.append({
            "date": dates[t],
            "eligible": test_frame.height,
            "universe_mean_net_5d": (
                float(universe_mean_val) if universe_mean_val is not None else None
            ),
        })
        if test_frame.height < 30:
            continue

        rel_base = test_frame.filter(
            pl.col("label_5d_mature") & pl.col("net_ret_5d").is_not_null()
        )
        rel_n = rel_base.height
        rel_y = rel_base["net_ret_5d"].cast(pl.Float64).to_numpy() if rel_n else np.array([])

        for model_key in MODEL_KEYS:
            if model_key in learnable:
                scores = models.scores(model_key, test_frame)
            else:
                scores = static_scores(model_key, test_frame)
            if not np.isfinite(scores).any():
                continue
            scored = test_frame.with_columns(pl.Series("score", scores))
            if rel_n:
                rel_scores = np.asarray(
                    scored.filter(
                        pl.col("label_5d_mature") & pl.col("net_ret_5d").is_not_null()
                    )["score"].to_numpy()
                )
                order = np.argsort(np.argsort(-rel_scores, kind="stable"), kind="stable") + 1
                for yi in range(rel_n):
                    reliability[model_key].append(
                        (quintile_of_rank(int(order[yi]), rel_n), float(rel_y[yi]))
                    )
            daily_rows = _evaluate_model_date(
                scored, model_key, dates[t], cfg, daily_rows
            )

    return {
        "started_at": started,
        "dates": dates,
        "date_to_idx": date_to_idx,
        "cfg": cfg,
        "retrain_log": retrain_log,
        "daily_rows": daily_rows,
        "universe_daily": universe_daily,
        "reliability": reliability,
    }


def _evaluate_model_date(
    scored: pl.DataFrame,
    model_key: str,
    date: str,
    cfg: ArenaConfig,
    daily_rows: list[dict],
) -> list[dict]:
    for top_n in cfg.top_ns:
        outcomes = frozen_top_n_outcomes(scored, top_n, "score")
        batch = float(np.mean(outcomes["slot_returns"])) if outcomes["slot_returns"] else 0.0
        tradable = outcomes["tradable_returns"]
        daily_rows.append({
            "date": date, "model": model_key, "top_n": top_n,
            "batch_net": batch,
            "batch_profit": batch > 0,
            "unbuyable": outcomes["unbuyable"], "pending": outcomes["pending"],
            "n_slots": outcomes["n_slots"],
            "tradable_mean": float(np.mean(tradable)) if tradable else None,
            "tradable_win": (
                float(np.mean([1.0 if r > 0 else 0.0 for r in tradable])) if tradable else None
            ),
            "mean_gap": float(np.mean(outcomes["gaps"])) if outcomes["gaps"] else None,
            "cc_mean": float(np.mean(outcomes["cc_returns"])) if outcomes["cc_returns"] else None,
        })
    return daily_rows


def aggregate(result: dict) -> dict:
    cfg: ArenaConfig = result["cfg"]
    dates: list[str] = result["dates"]
    date_to_idx = result["date_to_idx"]
    rows_pl = pl.DataFrame(result["daily_rows"])
    summary: dict[str, dict] = {}

    for model_key in MODEL_KEYS:
        per_model = rows_pl.filter(pl.col("model") == model_key) if rows_pl.height else None
        model_summary: dict[str, dict] = {}
        for top_n in cfg.top_ns:
            sub = per_model.filter(pl.col("top_n") == top_n) if per_model is not None else None
            if sub is None or sub.height == 0:
                continue
            block_mask = [
                (date_to_idx[d] - cfg.test_start_idx) % cfg.horizon == 0
                for d in sub["date"].to_list()
            ]
            sub = sub.with_columns(pl.Series("_block", block_mask))
            block_rows = sub.filter(pl.col("_block"))
            nav = nav_stats(block_rows["batch_net"].to_list())
            unbuyable_slots = int(sub["unbuyable"].sum())
            total_slots = int(sub["n_slots"].sum())
            model_summary[str(top_n)] = {
                "n_dates": sub.height,
                "mean_daily_batch_net_bps": round(float(sub["batch_net"].mean()) * 1e4, 2),
                "batch_profit_day_ratio": round(float(sub["batch_profit"].cast(pl.Float64).mean()), 4),
                "single_signal_win_rate": round(
                    float(sub.filter(pl.col("tradable_win").is_not_null())["tradable_win"].mean()), 4
                ),
                "mean_tradable_net_bps": round(
                    float(sub.filter(pl.col("tradable_mean").is_not_null())["tradable_mean"].mean()) * 1e4, 2
                ),
                "unbuyable_slot_ratio": round(unbuyable_slots / max(total_slots, 1), 4),
                "mean_entry_gap_bps": round(
                    float(sub.filter(pl.col("mean_gap").is_not_null())["mean_gap"].mean()) * 1e4, 2
                ),
                "signal_close_net_bps": round(
                    float(sub.filter(pl.col("cc_mean").is_not_null())["cc_mean"].mean()) * 1e4, 2
                ),
                "block_total_return_pct": round(nav["total_return"] * 100.0, 2),
                "block_max_drawdown_pct": round(nav["max_drawdown"] * 100.0, 2),
                "n_blocks": nav["blocks"],
            }
        reliability_summary: dict[str, dict] = {}
        for q in range(1, 6):
            samples = [y for (b, y) in result["reliability"].get(model_key, []) if b == q]
            if samples:
                arr = np.array(samples)
                reliability_summary[f"q{q}"] = {
                    "n": int(arr.size),
                    "mean_net_5d_bps": round(float(arr.mean()) * 1e4, 2),
                    "win_rate": round(float((arr > 0).mean()), 4),
                }
        summary[model_key] = {"top_n": model_summary, "score_reliability": reliability_summary}

    universe_vals = [
        r["universe_mean_net_5d"] for r in result["universe_daily"]
        if r["universe_mean_net_5d"] is not None
    ]
    universe_mean = float(np.mean(universe_vals)) if universe_vals else None

    artifact = {
        "schema_version": ARENA_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config": {
            "horizon": cfg.horizon,
            "top_ns": list(cfg.top_ns),
            "test_start_idx": cfg.test_start_idx,
            "test_start_date": dates[cfg.test_start_idx],
            "test_end_date": dates[-1],
            "retrain_every": cfg.retrain_every,
            "train_window": cfg.train_window,
            "max_train_rows_per_date": cfg.max_train_rows_per_date,
            "purge_sessions": cfg.purge_sessions,
            "round_trip_cost_bps": ROUND_TRIP_COST_BPS,
            "min_price": MIN_PRICE,
            "min_adv20": MIN_ADV20,
            "min_history_sessions": MIN_HISTORY_SESSIONS,
            "seed": cfg.seed,
            "trading_dates_total": len(dates),
            "feature_names": list(FEATURE_NAMES),
        },
        "label_versions": {"legacy": LEGACY_COMPOSITE_VERSION, "executable": EXEC_LABEL_VERSION},
        "universe_mean_net_5d_bps": round(universe_mean * 1e4, 2) if universe_mean is not None else None,
        "retrain_log": result["retrain_log"],
        "summary": summary,
        "daily_rows": result["daily_rows"],
        "universe_daily": result["universe_daily"],
        "elapsed_seconds": round(time.time() - result["started_at"], 1),
    }
    return artifact


def cfg_horizon(artifact: dict) -> int:
    return int(artifact.get("config", {}).get("horizon", 5))


def print_summary(artifact: dict, top_n: int = 5) -> None:
    print(
        f"\n==== Arena frozen Top-{top_n} "
        f"(D+1 open entry, D+{cfg_horizon(artifact)} close exit, {ROUND_TRIP_COST_BPS}bps RT) ===="
    )
    cols = f"{'model':26s} {'win%':>6s} {'batchbp/d':>10s} {'profitDay%':>10s} {'gapbp':>7s} {'ccbp':>7s} {'NAV%':>8s} {'maxDD%':>7s} {'unbuy%':>7s}"
    print(cols)
    for model_key in MODEL_KEYS:
        entry = artifact["summary"].get(model_key, {}).get("top_n", {}).get(str(top_n))
        if not entry:
            print(f"{model_key:26s} (no data)")
            continue
        print(
            f"{model_key:26s} "
            f"{entry['single_signal_win_rate'] * 100:6.2f} "
            f"{entry['mean_daily_batch_net_bps']:10.2f} "
            f"{entry['batch_profit_day_ratio'] * 100:10.2f} "
            f"{entry['mean_entry_gap_bps']:7.1f} "
            f"{entry['signal_close_net_bps']:7.1f} "
            f"{entry['block_total_return_pct']:8.2f} "
            f"{entry['block_max_drawdown_pct']:7.2f} "
            f"{entry['unbuyable_slot_ratio'] * 100:7.2f}"
        )
    print(
        f"{'universe_equal_weight':26s} "
        f"{'-':>6s} {artifact['universe_mean_net_5d_bps']:10.2f}   (universe mean net 5d per slot)"
    )

    print("\nScore reliability (quintiles by score, mean net 5d bps / win rate):")
    for model_key in LEARNABLE_MODEL_KEYS:
        rel = artifact["summary"].get(model_key, {}).get("score_reliability", {})
        if not rel:
            continue
        cells = " ".join(
            f"q{q}:{rel[f'q{q}']['mean_net_5d_bps']:7.1f}/{rel[f'q{q}']['win_rate'] * 100:5.1f}%"
            for q in range(1, 6) if f"q{q}" in rel
        )
        print(f"  {model_key:24s} {cells}")


def write_artifact(artifact: dict) -> Path:
    out_dir = ROOT_DIR / "data" / "artifacts" / "screening_model_arena"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"arena_{stamp}.json"
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    latest = out_dir / "latest.json"
    latest.write_text(json.dumps({"latest": path.name}, ensure_ascii=False), encoding="utf-8")
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-start-idx", type=int, default=None,
                        help="Trading-session index where out-of-sample testing starts.")
    parser.add_argument("--retrain-every", type=int, default=None)
    parser.add_argument("--train-window", type=int, default=None)
    parser.add_argument("--max-train-rows-per-date", type=int, default=None)
    parser.add_argument("--top-n", type=int, default=None,
                        help="Restrict summary printing to one Top-N (default 5).")
    parser.add_argument("--smoke", action="store_true",
                        help="Short smoke run: last ~40 sessions, lighter training caps.")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    cfg = ArenaConfig()
    if args.test_start_idx is not None:
        cfg.test_start_idx = args.test_start_idx
    if args.retrain_every is not None:
        cfg.retrain_every = args.retrain_every
    if args.train_window is not None:
        cfg.train_window = args.train_window
    if args.max_train_rows_per_date is not None:
        cfg.max_train_rows_per_date = args.max_train_rows_per_date
    if args.smoke:
        n_dates = len_dates_hint()
        cfg.test_start_idx = max(cfg.test_start_idx, n_dates - 40)
        cfg.max_train_rows_per_date = 600
    result = run_arena(cfg, verbose=not args.quiet)
    artifact = aggregate(result)
    print_summary(artifact, top_n=args.top_n or cfg.top_ns[0])
    path = write_artifact(artifact)
    print(f"\nArtifact written: {path}")
    print(f"Elapsed: {artifact['elapsed_seconds']}s")


def len_dates_hint() -> int:
    return len(list(ROOT_DIR.glob("data/lake/cn_daily/date=*/part.parquet")))


if __name__ == "__main__":
    main()