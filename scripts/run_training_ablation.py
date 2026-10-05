#!/usr/bin/env python
"""Training-side ablation harness for the batch-2 robustness knobs.

The four knobs under test (all resolved by ``SignalTrainer`` from ``Settings``):

1. label winsorization + robust (Huber) objective
   (``trainer_label_winsorize_enabled`` / ``trainer_objective``),
2. drawdown penalty ``trainer_drawdown_penalty`` -- applied either only to the
   OOS metric (default) or, via ``trainer_fit_on_risk_adjusted``, to the fit
   target itself,
3. horizon-sized purge embargo ``trainer_embargo_sessions``,
4. per-trade-date cross-sectional feature transform
   (``trainer_feature_transform_enabled``).

The harness reuses the production training path end to end
(:meth:`app.services.trainer.SignalTrainer.train`, which runs the purged
walk-forward, persists a ``model_runs`` row and writes the
``walk_forward_oos_evaluation_v1`` evidence), so every variant is scored by the
same real OOS evaluation code.

Panel / protocol / cost are frozen identically across variants:

* one pilot panel (same tickers, same lake partitions) is loaded once and
  injected into every variant through ``app.services.trainer.load_lake_rows``;
* the OOS window is the trainer's fixed trailing 60-session prediction slice;
* per-fill cost is pinned so the trainer's nominal round-trip equals the
  repository canonical cost (``app.services.cost_basis``, default 50 bps).

This is an engineering ablation scope, **not** a model-selection protocol: the
pilot universe is a latest-liquidity subset (survivor/look-ahead biased), the
training window is shortened to keep the run cheap, and only a handful of
variants are evaluated. The report records all of that explicitly.

Nothing in ``app/`` is modified; the harness only patches two symbols at
runtime (the lake reader, to pin the panel, and the OOS metric accessor, to
capture the matured top-N samples behind the summary).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT_DIR = Path(__file__).resolve().parents[1]

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.services.cost_basis import canonical_round_trip_cost_bps  # noqa: E402
from app.services.market_lake import (  # noqa: E402
    load_lake_latest_metrics,
    load_lake_rows,
)
from app.services.stock_selection.experiment_framework import (  # noqa: E402
    freeze_dataset_hash,
)
from app.services.stock_selection.production_research import (  # noqa: E402
    market_lake_source_version,
)
from app.services.trainer import SignalTrainer  # noqa: E402
import app.services.trainer as trainer_module  # noqa: E402


SCHEMA_VERSION = "training_ablation_v1"
DEFAULT_LABEL_VERSION = "executable_net_return_v1"
DEFAULT_FACTOR_SET_KEY = "trainer_lightgbm_momentum_v1"
OOS_TOP_N = 5  # mirrors trainer.OOS_EVALUATION_TOP_N (fixed by the trainer)


@dataclass(frozen=True, slots=True)
class Variant:
    key: str
    label: str
    overrides: dict[str, Any] = field(default_factory=dict)


# Named switch combinations. "canonical" keeps all four changes on (the
# shipped defaults), and each explicit "*_off" flips exactly one knob.
VARIANTS: dict[str, Variant] = {
    "canonical": Variant(
        key="canonical",
        label="all four changes on (shipped defaults)",
        overrides={},
    ),
    "winsor_off": Variant(
        key="winsor_off",
        label="label winsorization off (Huber kept)",
        overrides={"trainer_label_winsorize_enabled": False},
    ),
    "objective_l2": Variant(
        key="objective_l2",
        label="L2/least-squares objective (winsor kept)",
        overrides={"trainer_objective": "l2"},
    ),
    "label_robust_off": Variant(
        key="label_robust_off",
        label="change 1 off: winsor off + L2 objective",
        overrides={
            "trainer_label_winsorize_enabled": False,
            "trainer_objective": "l2",
        },
    ),
    "drawdown_off": Variant(
        key="drawdown_off",
        label="change 2 off: drawdown penalty 0.0",
        overrides={"trainer_drawdown_penalty": 0.0},
    ),
    "fit_risk_adjusted": Variant(
        key="fit_risk_adjusted",
        label="penalty on the fit target: fit on risk_adjusted_return",
        overrides={"trainer_fit_on_risk_adjusted": True},
    ),
    "embargo_off": Variant(
        key="embargo_off",
        label="change 3 off: embargo 0 sessions",
        overrides={"trainer_embargo_sessions": 0},
    ),
    "cross_section_off": Variant(
        key="cross_section_off",
        label="change 4 off: cross-sectional transform disabled",
        overrides={"trainer_feature_transform_enabled": False},
    ),
    "all_off": Variant(
        key="all_off",
        label="all four changes off",
        overrides={
            "trainer_label_winsorize_enabled": False,
            "trainer_objective": "l2",
            "trainer_drawdown_penalty": 0.0,
            "trainer_embargo_sessions": 0,
            "trainer_feature_transform_enabled": False,
            "trainer_fit_on_risk_adjusted": False,
        },
    ),
}

DEFAULT_VARIANTS = (
    "canonical",
    "label_robust_off",
    "drawdown_off",
    "fit_risk_adjusted",
    "embargo_off",
    "cross_section_off",
)


@dataclass
class _Capture:
    """Runtime capture of the matured top-N samples scored by the trainer."""

    rows: list[dict[str, Any]] = field(default_factory=list)

    def reset(self) -> None:
        self.rows = []


_CAPTURE = _Capture()
_ORIGINAL_OOS_METRIC = SignalTrainer._oos_metric_value


def _capturing_oos_metric(sample: dict[str, Any]) -> float | None:
    value = _ORIGINAL_OOS_METRIC(sample)
    profile = sample.get("target_profile") or {}
    _CAPTURE.rows.append(
        {
            "trade_date": str(sample.get("trade_date") or ""),
            "net_return": sample.get("target"),
            "risk_adjusted_return": profile.get("risk_adjusted_return"),
            "metric_value": value,
            "commission_bps_one_way": profile.get("commission_bps_one_way"),
            "slippage_bps_one_way": profile.get("slippage_bps_one_way"),
            "cost_model_hash": profile.get("cost_model_hash"),
        }
    )
    return value


def _install_oos_capture() -> None:
    trainer_module.SignalTrainer._oos_metric_value = staticmethod(_capturing_oos_metric)


def _parse_scalar(raw: str) -> Any:
    text_value = raw.strip()
    lowered = text_value.lower()
    if lowered in {"none", "null"}:
        return None
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    try:
        return int(text_value)
    except ValueError:
        pass
    try:
        return float(text_value)
    except ValueError:
        return text_value


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--market", choices=("CN", "US"), default="CN")
    parser.add_argument(
        "--pilot-tickers",
        type=int,
        default=24,
        help="Number of latest-liquidity pilot tickers (ignored when --tickers is set).",
    )
    parser.add_argument(
        "--tickers",
        default=None,
        help="Explicit comma-separated ticker list (overrides --pilot-tickers).",
    )
    parser.add_argument(
        "--history-sessions",
        type=int,
        default=400,
        help="Cap on the number of most recent lake session partitions to load.",
    )
    parser.add_argument(
        "--window-dates",
        type=int,
        default=180,
        help="Trainer training-window date budget for every variant.",
    )
    parser.add_argument("--lookback-days", type=int, default=3)
    parser.add_argument(
        "--seed",
        action="append",
        default=None,
        metavar="SEED",
        help=(
            "Estimator seed for the GBDT (repeatable, or comma-separated). "
            "Each seed reruns every variant with trainer_random_seed=SEED. "
            "Defaults to a single seed 42 (historical behaviour)."
        ),
    )
    parser.add_argument(
        "--variants",
        default=",".join(DEFAULT_VARIANTS),
        help="Comma-separated variant keys (see --list-variants).",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Extra Setting override applied to every variant (repeatable).",
    )
    parser.add_argument("--artifact-dir", type=Path, default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve the panel and variants without training.",
    )
    parser.add_argument(
        "--list-variants",
        action="store_true",
        help="Print the variant registry and exit.",
    )
    return parser.parse_args(argv)


def _require_market(market: str) -> str:
    code = str(market or "").strip().upper()
    if code not in {"CN", "US"}:
        raise ValueError("market must be CN or US")
    return code


def _cn_equity(ticker: str) -> bool:
    code = ticker.split(".", 1)[0]
    return code.startswith(("0", "3", "4", "6", "8"))


def _db_symbols(market: str) -> set[str]:
    with SessionLocal() as db:
        rows = db.execute(
            text("select ticker from symbols where market = :market"),
            {"market": market},
        ).fetchall()
    return {str(row[0]).strip().upper() for row in rows if str(row[0]).strip()}


def select_pilot_tickers(market: str, limit: int) -> list[str]:
    """Latest-liquidity pilot universe (same heuristic as production research)."""

    if limit < 2:
        raise ValueError("pilot tickers must be at least 2")
    metrics = load_lake_latest_metrics(market=market, lookback_days=180)
    eligible = [
        (ticker, values)
        for ticker, values in metrics.items()
        if int(values.get("history_days") or 0) >= 120
        and int(values.get("duplicate_conflict_days") or 0) == 0
        and (market != "CN" or _cn_equity(ticker))
    ]
    eligible.sort(key=lambda item: (-float(item[1].get("avg_dollar_volume") or 0.0), item[0]))
    listed = _db_symbols(market)
    selected = [ticker for ticker, _ in eligible if ticker in listed]
    return selected[:limit]


def resolve_tickers(args: argparse.Namespace, market: str) -> tuple[list[str], int]:
    if args.tickers:
        requested = sorted(
            {str(item).strip().upper() for item in args.tickers.split(",") if str(item).strip()}
        )
        listed = _db_symbols(market)
        dropped = [ticker for ticker in requested if ticker not in listed]
        return [ticker for ticker in requested if ticker in listed], len(dropped)
    return select_pilot_tickers(market, args.pilot_tickers), 0


def _panel_hash(rows: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    ordered = sorted(
        rows,
        key=lambda row: (str(row.get("symbol") or ""), str(row.get("date") or "")),
    )
    for row in ordered:
        digest.update(
            "|".join(
                [
                    str(row.get("symbol") or ""),
                    str(row.get("date") or "")[:10],
                    repr(row.get("open")),
                    repr(row.get("high")),
                    repr(row.get("low")),
                    repr(row.get("close")),
                    repr(row.get("volume")),
                    repr(row.get("adj_close")),
                ]
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _cost_overrides(market: str, settings: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pin per-fill cost so the trainer's nominal round-trip == canonical bps."""

    canonical = float(canonical_round_trip_cost_bps(market))
    commission = float(getattr(settings, "trainer_cn_execution_commission_bps", 2.5) or 0.0)
    slippage = canonical / 2.0 - commission
    if slippage < 0:
        raise RuntimeError(
            f"canonical cost {canonical}bps is below twice the one-way commission {commission}bps"
        )
    overrides = {
        "trainer_cn_execution_commission_bps": commission,
        "trainer_cn_execution_slippage_bps": slippage,
    }
    nominal = 2.0 * (commission + slippage)
    audit = {
        "canonical_round_trip_cost_bps": canonical,
        "commission_bps_one_way": commission,
        "slippage_bps_one_way": slippage,
        "nominal_round_trip_bps": nominal,
        "derivation": (
            "slippage_bps_one_way = canonical_round_trip_bps / 2 - commission_bps_one_way; "
            "the trainer's executable label builds FillCostModel(commission, slippage)"
        ),
        "note": (
            "differs from the shipped per-fill default (2.5/15 -> 35bps nominal); "
            "the override aligns every variant to the repository canonical cost"
        ),
    }
    return overrides, audit


def _window_overrides(market: str, window_dates: int) -> dict[str, Any]:
    prefix = "trainer_cn_window" if market == "CN" else "trainer_us_window"
    return {
        f"{prefix}_mode": "complete_dates_v1",
        f"{prefix}_dates": int(window_dates),
    }


def _parse_set_overrides(pairs: list[str], settings: Any) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    valid = set(type(settings).model_fields)
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--set expects KEY=VALUE, got {pair!r}")
        key, raw = pair.split("=", 1)
        key = key.strip()
        if key not in valid:
            raise SystemExit(f"--set key {key!r} is not a Settings field")
        overrides[key] = _parse_scalar(raw)
    return overrides


def _fetch_run_config(run_name: str) -> dict[str, Any] | None:
    with SessionLocal() as db:
        row = db.execute(
            text(
                "select id, status, config_json from model_runs "
                "where name = :name order by id desc limit 1"
            ),
            {"name": run_name},
        ).fetchone()
    if row is None:
        return None
    return {
        "run_id": int(row[0]),
        "status": str(row[1] or ""),
        "config": json.loads(row[2] or "{}"),
    }


def _knob_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "label_winsorize": config.get("label_winsorize"),
        "objective": config.get("objective"),
        "drawdown_penalty": config.get("drawdown_penalty"),
        "fit_target": config.get("fit_target"),
        "random_seed": config.get("random_seed"),
        "embargo_sessions": config.get("embargo_sessions"),
        "purge_gap_days": config.get("purge_gap_days"),
        "feature_transform": config.get("feature_transform"),
    }


def _capture_metrics(captured: list[dict[str, Any]]) -> dict[str, Any]:
    net_values = [
        float(row["net_return"])
        for row in captured
        if row.get("net_return") is not None
    ]
    metric_values = [
        float(row["metric_value"])
        for row in captured
        if row.get("metric_value") is not None
    ]
    by_date: dict[str, list[float]] = {}
    for row in captured:
        if row.get("net_return") is None:
            continue
        by_date.setdefault(str(row.get("trade_date") or ""), []).append(float(row["net_return"]))
    daily_positive = [sum(values) / len(values) > 0 for values in by_date.values()]
    label_cost = None
    for row in captured:
        if row.get("commission_bps_one_way") is not None:
            label_cost = {
                "commission_bps_one_way": row["commission_bps_one_way"],
                "slippage_bps_one_way": row["slippage_bps_one_way"],
                "nominal_round_trip_bps": 2
                * (float(row["commission_bps_one_way"]) + float(row["slippage_bps_one_way"])),
                "cost_model_hash": row.get("cost_model_hash"),
            }
            break
    return {
        "captured_sample_count": len(captured),
        "net_sample_count": len(net_values),
        "net_hit_rate": (sum(1 for value in net_values if value > 0) / len(net_values))
        if net_values
        else None,
        "risk_adjusted_sample_hit_rate": (
            sum(1 for value in metric_values if value > 0) / len(metric_values)
        )
        if metric_values
        else None,
        "net_positive_date_rate": (
            sum(1 for value in daily_positive if value) / len(daily_positive)
        )
        if daily_positive
        else None,
        "evaluated_date_count_captured": len(by_date),
        "label_cost": label_cost,
    }


def _run_variant(
    *,
    variant: Variant,
    market: str,
    tickers: list[str],
    panel_rows: list[dict[str, Any]],
    base_overrides: dict[str, Any],
    lookback_days: int,
    run_name: str,
    settings: Any,
    seed: int | None = None,
) -> dict[str, Any]:
    overrides = {**base_overrides, **variant.overrides}
    if seed is not None:
        overrides["trainer_random_seed"] = int(seed)
    trainer = SignalTrainer()
    trainer.settings = settings.model_copy(update=overrides)

    _CAPTURE.reset()

    def _pinned_load_lake_rows(**_kwargs: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in panel_rows]

    original_loader = trainer_module.load_lake_rows
    trainer_module.load_lake_rows = _pinned_load_lake_rows
    started = time.perf_counter()
    error: str | None = None
    prediction_row_count: int | None = None
    try:
        prediction_row_count = trainer.train(
            run_name=run_name,
            lookback_days=lookback_days,
            tickers=tickers,
            market=market,
            universe=run_name,
            model_type="lightgbm",
        )
    except Exception as exc:  # noqa: BLE001 - a blocked variant must not kill the sweep
        error = f"{type(exc).__name__}: {exc}"
    finally:
        trainer_module.load_lake_rows = original_loader
    elapsed = time.perf_counter() - started

    record: dict[str, Any] = {
        "key": variant.key,
        "label": variant.label,
        "seed": seed,
        "run_name": run_name,
        "settings_overrides": overrides,
        "elapsed_seconds": round(elapsed, 3),
        "prediction_row_count": prediction_row_count,
        "error": error,
        "captured": _capture_metrics(_CAPTURE.rows),
    }
    run = _fetch_run_config(run_name)
    if run is not None:
        config = run["config"]
        oos = config.get("oos_evaluation") or {}
        record["run"] = {
            "run_id": run["run_id"],
            "status": run["status"],
            "training_sample_count": config.get("training_sample_count"),
            "knobs": _knob_snapshot(config),
            "execution_cost": config.get("execution_cost_bps"),
            "metrics": {
                "mean_risk_adjusted_return": oos.get("mean_risk_adjusted_return"),
                "mean_net_return": oos.get("mean_net_return"),
                "positive_date_rate": oos.get("positive_date_rate"),
                "evaluated_sample_count": oos.get("evaluated_sample_count"),
                "evaluated_date_count": oos.get("evaluated_date_count"),
                "date_min": oos.get("date_min"),
                "date_max": oos.get("date_max"),
            },
        }
    return record


def _resolve_seeds(raw: list[str] | None) -> list[int]:
    """Parse ``--seed`` (repeatable and/or comma-separated) into unique seeds."""

    if not raw:
        return [42]
    seeds: list[int] = []
    for item in raw:
        for piece in str(item).split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                seed = int(piece)
            except ValueError as exc:
                raise SystemExit(f"--seed expects integers, got {piece!r}") from exc
            if seed < 0:
                raise SystemExit(f"--seed must be non-negative, got {seed}")
            if seed not in seeds:
                seeds.append(seed)
    if not seeds:
        raise SystemExit("--seed resolved no seeds")
    return seeds


def _stat(values: list[Any]) -> dict[str, Any] | None:
    """Cross-seed mean / dispersion for one metric."""

    clean = [
        float(value)
        for value in values
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    if not clean:
        return None
    return {
        "n": len(clean),
        "mean": round(sum(clean) / len(clean), 8),
        "std": round(statistics.stdev(clean), 8) if len(clean) >= 2 else 0.0,
        "min": round(min(clean), 8),
        "max": round(max(clean), 8),
    }


def _aggregate_variants(
    records: list[dict[str, Any]], metric_keys: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Group per-run records by variant and report cross-seed mean/std."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for record in records:
        key = str(record.get("key") or "")
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(record)
    summaries: list[dict[str, Any]] = []
    for key in order:
        recs = grouped[key]
        metrics = {
            metric: _stat(
                [
                    (rec.get("run") or {}).get("metrics", {}).get(metric)
                    for rec in recs
                ]
            )
            for metric in metric_keys
        }
        summaries.append(
            {
                "key": key,
                "label": recs[0].get("label"),
                "seed_count": len(recs),
                "seeds": [rec.get("seed") for rec in recs],
                "metrics": metrics,
            }
        )
    return summaries


def _paired_seed_metrics(
    records: list[dict[str, Any]], metric: str
) -> dict[str, dict[Any, float]]:
    """Map ``variant_key -> {seed: metric}`` for paired per-seed comparisons."""

    by_key: dict[str, dict[Any, float]] = {}
    for record in records:
        value = (record.get("run") or {}).get("metrics", {}).get(metric)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            by_key.setdefault(str(record.get("key") or ""), {})[record.get("seed")] = float(
                value
            )
    return by_key


def _direction_support(
    by_key: dict[str, dict[Any, float]],
    *,
    baseline_key: str,
    key: str,
    metric: str,
) -> dict[str, Any]:
    """Per-seed paired direction of ``key`` vs ``baseline_key`` on one metric.

    A direction counts as supported only when it holds on at least
    ``ceil(2/3 * shared_seeds)`` seeds -- never from a single seed.
    """

    base = by_key.get(baseline_key) or {}
    other = by_key.get(key) or {}
    shared = sorted(set(other) & set(base), key=lambda item: str(item))
    wins = sum(1 for seed in shared if other[seed] > base[seed])
    losses = sum(1 for seed in shared if other[seed] < base[seed])
    ties = len(shared) - wins - losses
    threshold = math.ceil(len(shared) * 2 / 3) if shared else None
    if not shared:
        direction = "undetermined"
    elif wins > losses:
        direction = "higher"
    elif losses > wins:
        direction = "lower"
    else:
        direction = "mixed"
    return {
        "metric": metric,
        "baseline_key": baseline_key,
        "variant_key": key,
        "shared_seed_count": len(shared),
        "variant_gt_baseline": wins,
        "variant_lt_baseline": losses,
        "ties": ties,
        "direction": direction,
        "support_threshold": threshold,
        "supported": (
            None if threshold is None else (wins if direction == "higher" else losses) >= threshold
        ),
        "per_seed_delta": {
            str(seed): round(other[seed] - base[seed], 8) for seed in shared
        },
    }


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}"
    return str(value)


def _markdown_report(report: dict[str, Any]) -> str:
    scope = report["scope"]
    cost = report["cost"]
    panel = report["panel"]
    protocol = report["protocol"]
    lines: list[str] = []
    lines.append(f"# 训练消融报告（{report['market']}，{report['generated_at']}）")
    lines.append("")
    lines.append(f"- schema：`{report['schema_version']}`")
    lines.append(f"- scope：`{scope['mode']}`（**{scope['disclaimer']}**）")
    lines.append(
        f"- 面板：{panel['ticker_count']} 标的 / {panel['row_count']} 行 / "
        f"{panel['date_min']}–{panel['date_max']}；dataset_hash=`{panel['dataset_hash']}`"
    )
    lines.append(
        f"- 成本：canonical {cost['canonical_round_trip_cost_bps']:.0f}bps "
        f"(commission {cost['commission_bps_one_way']} + slippage "
        f"{cost['slippage_bps_one_way']} 单边，名义往返 {cost['nominal_round_trip_bps']:.0f}bps)"
    )
    lines.append(
        f"- 协议：lookback={protocol['lookback_days']}，horizon={protocol['horizon_days']}，"
        f"训练窗口={protocol['window_dates']} 日，OOS=最后 {protocol['prediction_dates']} 个会话，"
        f"top-N={protocol['top_n']}，模型={protocol['model_family']}"
    )
    seeds = protocol.get("seeds") or [42]
    lines.append(
        f"- 随机种子：{seeds}（每变体逐 seed 重跑，跨 seed 报 mean/std；判定门槛=同一方向在 ≥⌈2/3×seed⌉ 上成立）"
    )
    lines.append("")
    lines.append("## 逐 run 明细（含 seed 维度）")
    lines.append("")
    lines.append(
        "| 变体 | seed | 说明 | mean_risk_adjusted | mean_net | 净命中率 | positive_date_rate | "
        "OOS 样本 | 训练样本 | 耗时(s) | status |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for variant in report["variants"]:
        run = variant.get("run") or {}
        metrics = run.get("metrics") or {}
        captured = variant.get("captured") or {}
        status = run.get("status", "error")
        if variant.get("error"):
            status = f"error: {variant['error'][:60]}"
        lines.append(
            "| {key} | {seed} | {label} | {mar} | {mnet} | {hit} | {pdr} | {samples} | {train} | "
            "{elapsed} | {status} |".format(
                key=variant["key"],
                seed=_fmt(variant.get("seed"), 0) if variant.get("seed") is not None else "-",
                label=variant["label"],
                mar=_fmt(metrics.get("mean_risk_adjusted_return"), 6),
                mnet=_fmt(metrics.get("mean_net_return"), 6),
                hit=_fmt(captured.get("net_hit_rate"), 4),
                pdr=_fmt(metrics.get("positive_date_rate"), 4),
                samples=metrics.get("evaluated_sample_count"),
                train=run.get("training_sample_count"),
                elapsed=_fmt(variant.get("elapsed_seconds"), 1),
                status=status,
            )
        )
    lines.append("")
    lines.append("## 跨 seed 汇总（mean ± std，n=seed 数）")
    lines.append("")
    lines.append(
        "| 变体 | seed 数 | mean_net (mean±std) | mean_risk_adjusted (mean±std) | "
        "positive_date_rate (mean±std) |"
    )
    lines.append("|---|---|---|---|---|")
    for summary in report.get("variant_summary", []):
        metrics = summary.get("metrics") or {}

        def _ms(metric_key: str) -> str:
            stat = metrics.get(metric_key)
            if not stat:
                return "-"
            return f"{_fmt(stat['mean'], 6)} ± {_fmt(stat['std'], 6)}"

        lines.append(
            "| {key} | {n} | {mnet} | {mar} | {pdr} |".format(
                key=summary["key"],
                n=summary.get("seed_count"),
                mnet=_ms("mean_net_return"),
                mar=_ms("mean_risk_adjusted_return"),
                pdr=_ms("positive_date_rate"),
            )
        )
    lines.append("")
    comparisons = report.get("comparisons") or []
    if comparisons:
        lines.append("## 方向一致性判定（变体 vs canonical，逐 seed 配对）")
        lines.append("")
        lines.append(
            "| 指标 | 对比 | 共享 seed | 变体更高/更低/持平 | 方向 | 门槛 | 初步支持 |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        for item in comparisons:
            lines.append(
                "| {metric} | {key} vs {base} | {shared} | {wins}/{losses}/{ties} | {direction} | "
                "≥{threshold} | {supported} |".format(
                    metric=item["metric"],
                    key=item["variant_key"],
                    base=item["baseline_key"],
                    shared=item["shared_seed_count"],
                    wins=item["variant_gt_baseline"],
                    losses=item["variant_lt_baseline"],
                    ties=item["ties"],
                    direction=item["direction"],
                    threshold=item.get("support_threshold"),
                    supported=item.get("supported"),
                )
            )
        lines.append("")
        lines.append("> 判定是逐 seed 配对方向，不是单 seed 结论；`初步支持` 仅在方向达标时成立。")
        lines.append("")
    lines.append("## 变体开关快照")
    lines.append("")
    for variant in report["variants"]:
        run = variant.get("run") or {}
        knobs = run.get("knobs") or variant.get("settings_overrides")
        label_cost = (variant.get("captured") or {}).get("label_cost")
        lines.append(
            f"- **{variant['key']}**（seed={variant.get('seed')}，{variant['label']}）："
            f"`{json.dumps(knobs, ensure_ascii=False, default=str)}`"
        )
        if label_cost:
            lines.append(
                f"  - 标签成本核验：{json.dumps(label_cost, ensure_ascii=False, default=str)}"
            )
    lines.append("")
    lines.append("## 限制与未跑项")
    lines.append("")
    for note in report["limitations"]:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def _report_paths(artifact_dir: Path, timestamp: str) -> tuple[Path, Path]:
    base = artifact_dir / f"training-ablation-{timestamp}"
    return base.with_suffix(".json"), base.with_suffix(".md")


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list_variants:
        for key, variant in VARIANTS.items():
            print(f"{key}\t{variant.label}\t{json.dumps(variant.overrides, default=str)}")
        return 0

    market = _require_market(args.market)
    variant_keys = [key.strip() for key in args.variants.split(",") if key.strip()]
    unknown = [key for key in variant_keys if key not in VARIANTS]
    if unknown:
        raise SystemExit(f"unknown variant(s): {', '.join(unknown)}; see --list-variants")
    seeds = _resolve_seeds(args.seed)

    settings = get_settings()
    extra_overrides = _parse_set_overrides(args.set, settings)
    cost_overrides, cost_audit = _cost_overrides(market, settings)
    window_overrides = _window_overrides(market, args.window_dates)
    base_overrides = {**cost_overrides, **window_overrides, **extra_overrides}

    tickers, dropped = resolve_tickers(args, market)
    if not tickers:
        raise SystemExit("pilot selection resolved no tickers present in the symbols table")
    panel_rows = load_lake_rows(
        markets=[market],
        tickers=set(tickers),
        limit_per_symbol=max(1, int(args.history_sessions)),
    )
    if not panel_rows:
        raise SystemExit("no lake rows loaded for the pilot panel")
    dates = sorted({str(row.get("date") or "")[:10] for row in panel_rows if row.get("date")})
    dataset = freeze_dataset_hash(
        market=market,
        factor_set_key=DEFAULT_FACTOR_SET_KEY,
        label_version=DEFAULT_LABEL_VERSION,
        universe_version=f"training_ablation_{market.lower()}_pilot:{len(tickers)}",
        source_version=market_lake_source_version(market),
    )
    horizon_days = max(5, min(10, int(args.lookback_days) * 2))
    generated_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "market": market,
        "scope": {
            "mode": "engineering_pilot_not_for_model_selection",
            "disclaimer": (
                "latest-liquidity pilot universe (survivor/look-ahead biased) and a "
                "shortened training window; results are directional ablation evidence, "
                "not a model-selection decision"
            ),
            "dropped_tickers_not_in_symbols": dropped,
        },
        "cost": cost_audit,
        "panel": {
            "dataset_hash": dataset.dataset_hash,
            "source_version": dataset.source_version,
            "components": dict(dataset.components),
            "panel_hash": _panel_hash(panel_rows),
            "tickers": tickers,
            "ticker_count": len(tickers),
            "row_count": len(panel_rows),
            "session_count": len(dates),
            "date_min": dates[0] if dates else None,
            "date_max": dates[-1] if dates else None,
        },
        "protocol": {
            "model_family": "lightgbm",
            "lookback_days": int(args.lookback_days),
            "horizon_days": horizon_days,
            "window_dates": int(args.window_dates),
            "history_sessions": int(args.history_sessions),
            "prediction_dates": 60,
            "top_n": OOS_TOP_N,
            "label_version": DEFAULT_LABEL_VERSION,
            "seeds": seeds,
            "seed_count": len(seeds),
            "variants": variant_keys,
        },
        "variants": [],
        "variant_summary": [],
        "comparisons": [],
        "limitations": [
            "pilot universe is a latest-liquidity subset, not a point-in-time tradable universe",
            "training window shortened via trainer_*_window_dates for cost; not the 252-session production window",
            "drawdown_off keeps the default fit target (net_return): it changes only mean_risk_adjusted_return mechanically and leaves mean_net_return untouched by construction; use fit_risk_adjusted to test the penalty as a fit target",
            "net hit rate is computed from the matured top-N OOS samples captured around the trainer's OOS metric accessor",
            "multi-seed dispersion makes estimator variance visible, but a single pilot panel is reused across seeds, so panel-level (universe/session) uncertainty is not estimated; the 2/3-seed direction threshold is a weak screen, not a significance test",
        ],
    }

    if args.dry_run:
        artifact_dir = args.artifact_dir or (settings.data_dir / "experiments")
        json_path, md_path = _report_paths(Path(artifact_dir), generated_at)
        report["limitations"].append("dry-run: no variant was trained")
        _write_report(report, json_path, md_path)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        print(f"wrote {json_path} and {md_path}")
        return 0

    _install_oos_capture()
    timestamp = generated_at
    for seed in seeds:
        for key in variant_keys:
            variant = VARIANTS[key]
            run_name = f"training_ablation_{timestamp}_{key}_s{seed}"
            print(f"[{key}][seed={seed}] training {len(tickers)} tickers ...", flush=True)
            record = _run_variant(
                variant=variant,
                market=market,
                tickers=tickers,
                panel_rows=panel_rows,
                base_overrides=base_overrides,
                lookback_days=int(args.lookback_days),
                run_name=run_name,
                settings=settings,
                seed=seed,
            )
            report["variants"].append(record)
            status = record.get("error") or (record.get("run") or {}).get("status")
            print(
                f"[{key}][seed={seed}] done in {record['elapsed_seconds']}s status={status}",
                flush=True,
            )

    metric_keys = (
        "mean_net_return",
        "mean_risk_adjusted_return",
        "positive_date_rate",
        "evaluated_sample_count",
        "evaluated_date_count",
    )
    report["variant_summary"] = _aggregate_variants(report["variants"], metric_keys)
    comparisons: list[dict[str, Any]] = []
    if "canonical" in variant_keys:
        for metric in ("mean_risk_adjusted_return", "mean_net_return"):
            by_key = _paired_seed_metrics(report["variants"], metric)
            for key in variant_keys:
                if key == "canonical":
                    continue
                comparisons.append(
                    _direction_support(
                        by_key, baseline_key="canonical", key=key, metric=metric
                    )
                )
    report["comparisons"] = comparisons

    artifact_dir = Path(args.artifact_dir) if args.artifact_dir else (settings.data_dir / "experiments")
    json_path, md_path = _report_paths(artifact_dir, timestamp)
    _write_report(report, json_path, md_path)
    print(f"wrote {json_path} and {md_path}")
    return 0


def _write_report(report: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(_markdown_report(report), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
