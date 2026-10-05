from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.stock_selection.experiment_framework import (  # noqa: E402
    PerTicketTradabilityConfig,
    ExperimentRow,
    SelectionExperimentSpec,
    apply_per_ticket_gate,
    build_in_sample_rows,
    evaluate_experiment,
    freeze_dataset_hash,
    load_experiment_spec,
    per_ticket_gate_summary,
    persist_experiment_report,
)
from app.services.stock_selection.experiment_registry import (  # noqa: E402
    attempt_stats,
    record_attempt,
)
from app.services.stock_selection.sentiment_features import (  # noqa: E402
    SENTIMENT_FACTOR_SET_KEY,
)

DEFAULT_OUTPUT_DIR = Path("data") / "artifacts" / "acceptance-20261002"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="运行一次选股胜率提升实验（同面板 treated/control + 显著性 + 随机对照）。"
    )
    parser.add_argument("--spec", type=Path, required=True, help="假设规格 YAML/JSON。")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--panel-json", type=Path, help="离线面板 JSON（treated/control 收益向量）。")
    source.add_argument("--treated-evidence-dir", type=Path, help="新方案 evidence 目录（含 predictions.parquet）。")
    source.add_argument(
        "--lake-label-dataset-dir",
        type=Path,
        help=(
            "生产标签数据集目录（含 samples.parquet）。启用后直接用 market_lake 动量源"
            "重建逐票可成交面板：treated=逐票门槛子集，control=全池。"
        ),
    )
    parser.add_argument("--control-evidence-dir", type=Path)
    parser.add_argument("--treated-dataset-dir", type=Path)
    parser.add_argument("--control-dataset-dir", type=Path)
    parser.add_argument("--treated-model-key", default="equal_weight")
    parser.add_argument("--control-model-key", default="equal_weight")
    parser.add_argument("--strata-json", type=Path, help="行业/市值/流动性桶映射（可选）。")
    parser.add_argument("--market-key", default=None, help="registry 分组键；默认用 experiment_id。")
    parser.add_argument("--factor-set-key", default="original_v1")
    parser.add_argument(
        "--sentiment-metadata-json",
        type=Path,
        default=None,
        help=(
            "sentiment_v1 覆盖/cutoff 元数据 JSON（sentiment_factor_set_key/source_version/"
            "coverage_start/coverage_end/missing_policy/decision_cutoff/cutoff_time_local/"
            "auction_path_enabled）。启用 --factor-set-key sentiment_v1 时必填，否则 fail-closed。"
        ),
    )
    parser.add_argument("--source-version", default=None, help="market_lake 来源版本覆盖（离线复算用）。")
    parser.add_argument("--registry-path", type=Path, default=None)
    parser.add_argument("--attempts", type=int, default=None, help="覆盖 registry 中的 attempts 计数。")
    parser.add_argument("--record-attempt", action="store_true", help="评估后把本次结果追加进哈希链 registry。")
    # 逐票可成交门槛（lake 模式）；默认 --no-limit-up-gate 关闭。
    parser.add_argument("--min-liquidity-percentile", type=float, default=20.0, help="逐票成交额分位门槛（0-100）。")
    parser.add_argument("--no-limit-up-gate", action="store_true", help="不剔除信号日涨停封板。")
    parser.add_argument("--no-zero-volume-gate", action="store_true", help="不剔除 volume<=0（停牌代理）。")
    parser.add_argument("--lake-min-dollar-volume", type=float, default=0.0, help="lake 全池最小成交额（默认 0=最宽）。")
    parser.add_argument("--lake-limit", type=int, default=10000, help="lake 每日取数上限。")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--data-source", default="panel_json")
    return parser.parse_args()



def _load_panel_json(path: Path, spec: SelectionExperimentSpec) -> tuple[list[ExperimentRow], list[ExperimentRow], list[ExperimentRow]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("panel JSON must be an object with treated/control arrays")
    treated = _rows_from_payload(raw.get("treated"), label="treated")
    control = _rows_from_payload(raw.get("control"), label="control")
    in_sample = build_in_sample_rows(
        treated_rows=treated, control_rows=control, oos_start=spec.panel.oos_start
    )
    return treated, control, in_sample


def _rows_from_payload(payload: Any, *, label: str) -> list[ExperimentRow]:
    if payload is None:
        raise ValueError(f"panel JSON is missing the {label} array")
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"panel JSON {label} must be a non-empty array")
    rows: list[ExperimentRow] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"{label}[{index}] must be an object")
        rows.append(_row_from_mapping(item, label=label, index=index))
    return rows


def _coerce_float_map(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    output: dict[str, float] = {}
    for key, value in raw.items():
        if value is None:
            continue
        try:
            output[str(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return output


RANKING_CONVENIENCE_FIELDS = ("score", "trend_score", "momentum_5", "momentum_20", "volume_ratio", "model_score")
TRADABILITY_CONVENIENCE_FIELDS = ("limit_up_today", "volume", "dollar_volume")


def _row_from_mapping(item: dict[str, Any], *, label: str, index: int) -> ExperimentRow:
    try:
        feature_date = date.fromisoformat(str(item["date"])[:10])
        ticker = str(item["ticker"])
        raw_score = float(item["score"])
        net_return = float(item["net_return"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{label}[{index}] must provide date/ticker/score/net_return") from exc
    strata_raw = item.get("strata") or {}
    strata = {str(key): str(value) for key, value in dict(strata_raw).items()} if strata_raw else {}
    label_value = item.get("risk_adjusted_return", item.get("label_value"))
    gross = item.get("gross_return")
    market_excess = item.get("market_excess_return")
    ranking_values = _coerce_float_map(item.get("ranking_values"))
    for name in RANKING_CONVENIENCE_FIELDS:
        if item.get(name) is not None:
            ranking_values.setdefault(name, float(item[name]))
    ranking_values.setdefault("raw_score", raw_score)
    tradability_values = _coerce_float_map(item.get("tradability_values"))
    for name in TRADABILITY_CONVENIENCE_FIELDS:
        if item.get(name) is not None:
            tradability_values.setdefault(name, float(item[name]))
    return ExperimentRow(
        feature_date=feature_date,
        ticker=ticker,
        raw_score=raw_score,
        net_return=net_return,
        gross_return=None if gross is None else float(gross),
        market_excess_return=None if market_excess is None else float(market_excess),
        label_value=None if label_value is None else float(label_value),
        strata=strata,
        ranking_values=ranking_values,
        tradability_values=tradability_values,
    )


def _load_evidence_rows(
    *,
    spec: SelectionExperimentSpec,
    treated_evidence_dir: Path,
    control_evidence_dir: Path,
    treated_dataset_dir: Path,
    control_dataset_dir: Path,
    treated_model_key: str,
    control_model_key: str,
    strata_path: Path | None,
) -> tuple[list[ExperimentRow], list[ExperimentRow], list[ExperimentRow]]:
    from app.services.stock_selection.robustness import load_robustness_rows

    strata_lookup = _load_strata_lookup(strata_path)
    treated = _robustness_rows(
        load_robustness_rows(
            predictions_path=treated_evidence_dir / "predictions.parquet",
            samples_path=treated_dataset_dir / "samples.parquet",
            model_key=treated_model_key,
        ),
        spec=spec,
        strata_lookup=strata_lookup,
    )
    control = _robustness_rows(
        load_robustness_rows(
            predictions_path=control_evidence_dir / "predictions.parquet",
            samples_path=control_dataset_dir / "samples.parquet",
            model_key=control_model_key,
        ),
        spec=spec,
        strata_lookup=strata_lookup,
    )
    in_sample = build_in_sample_rows(
        treated_rows=treated, control_rows=control, oos_start=spec.panel.oos_start
    )
    return treated, control, in_sample


def _load_strata_lookup(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("strata JSON must be an object")
    lookup: dict[str, dict[str, str]] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            raise ValueError("strata JSON values must be objects of dimension->bucket")
        lookup[str(key)] = {str(dim): str(bucket) for dim, bucket in value.items()}
    return lookup


def _robustness_rows(
    rows: Iterable[Any],
    *,
    spec: SelectionExperimentSpec,
    strata_lookup: dict[str, dict[str, str]],
) -> list[ExperimentRow]:
    materialized = list(rows)
    market_return_by_date: dict[date, list[float]] = {}
    for row in materialized:
        components = dict(row.label_components)
        market_return_by_date.setdefault(row.feature_date, []).append(
            float(components["net_return"]) - float(components["market_excess_return"])
        )
    regime_by_date: dict[date, str] = {}
    for feature_date, values in market_return_by_date.items():
        mean_market = sum(values) / len(values)
        regime_by_date[feature_date] = "market_up" if mean_market >= 0 else "market_down"

    output: list[ExperimentRow] = []
    for row in materialized:
        components = dict(row.label_components)
        strata: dict[str, str] = {}
        keyed = strata_lookup.get(f"{row.feature_date.isoformat()}|{row.ticker}") or strata_lookup.get(row.ticker)
        if keyed:
            strata.update(keyed)
        for dimension in spec.strata:
            if dimension == "regime" and "regime" not in strata:
                strata["regime"] = regime_by_date[row.feature_date]
        output.append(
            ExperimentRow(
                feature_date=row.feature_date,
                ticker=row.ticker,
                raw_score=float(row.raw_score),
                net_return=float(components["net_return"]),
                gross_return=float(components.get("gross_return")) if components.get("gross_return") is not None else None,
                market_excess_return=float(components["market_excess_return"]),
                label_value=float(row.label_value),
                strata=strata,
                ranking_values={str(name): float(value) for name, value in dict(row.features).items()},
                tradability_values=_tradability_from_features(row.features),
            )
        )
    return output


def _tradability_from_features(features: Any) -> dict[str, float]:
    """把生产数据集逐票特征映射到逐票可成交特征（仅单票自身，禁止 regime）。"""

    values: dict[str, float] = {}
    mapped = {str(name): float(value) for name, value in dict(features).items()}
    if "dollar_volume_log" in mapped:
        values["dollar_volume"] = float(10.0 ** mapped["dollar_volume_log"])
    return values


def _lake_label_components(components: Mapping[str, Any], *, cost_bps: float) -> dict[str, float]:
    """把生产标签（其 round-trip 成本可能非 canonical）重算到 ``cost_bps``。

    ``gross_return`` 与成本无关；``net``/``market_excess``/``risk_adjusted`` 由
    gross 与成本无关的市场/行业收益重建，保证 ``gross - net == cost_bps/10000``。
    """

    gross = float(components["gross_return"])
    dataset_net = float(components["net_return"])
    industry_return = dataset_net - float(components["risk_adjusted_return"])
    market_return = dataset_net - float(components["market_excess_return"])
    net = gross - cost_bps / 10_000.0
    return {
        "gross_return": gross,
        "net_return": net,
        "market_excess_return": net - market_return,
        "risk_adjusted_return": net - industry_return,
    }


def _lake_experiment_row(
    lake_row: Mapping[str, Any],
    *,
    feature_date: date,
    components: Mapping[str, Any],
    cost_bps: float,
    limit_up_stratum: str,
) -> ExperimentRow:
    labels = _lake_label_components(components, cost_bps=cost_bps)
    trend_score = float(lake_row.get("trend_score") or 0.0)
    ranking_values = {
        "raw_score": trend_score,
        "trend_score": trend_score,
        "momentum_20": float(lake_row.get("momentum_20") or 0.0),
        "momentum_5": float(lake_row.get("momentum_5") or 0.0),
        "volume_ratio": float(lake_row.get("volume_ratio") or 0.0),
        "dollar_volume": float(lake_row.get("dollar_volume") or 0.0),
    }
    # Continuous ranking basis emitted by the production lake momentum pool
    # (see market_lake.screen_lake_momentum); keeps experiment sorting on the
    # same field the snapshot/query path uses instead of the coarse trend_score.
    if lake_row.get("model_score") is not None:
        ranking_values["model_score"] = float(lake_row["model_score"])
    if lake_row.get("model_percentile") is not None:
        ranking_values["model_percentile"] = float(lake_row["model_percentile"])
    if lake_row.get("rank_percentile") is not None:
        ranking_values["rank_percentile"] = float(lake_row["rank_percentile"])
    tradability_values: dict[str, float] = {}
    if lake_row.get("limit_up_today") is not None:
        tradability_values["limit_up_today"] = 1.0 if lake_row.get("limit_up_today") else 0.0
    if lake_row.get("volume") is not None:
        tradability_values["volume"] = float(lake_row["volume"])
    if lake_row.get("dollar_volume") is not None:
        tradability_values["dollar_volume"] = float(lake_row["dollar_volume"])
    return ExperimentRow(
        feature_date=feature_date,
        ticker=str(lake_row.get("ticker") or lake_row.get("symbol")),
        raw_score=trend_score,
        net_return=labels["net_return"],
        gross_return=labels["gross_return"],
        market_excess_return=labels["market_excess_return"],
        label_value=labels["risk_adjusted_return"],
        strata={"limit_up_today": limit_up_stratum},
        ranking_values=ranking_values,
        tradability_values=tradability_values,
    )


def _attach_regime_and_liquidity_strata(rows: list[ExperimentRow]) -> list[ExperimentRow]:
    """按日截面补 regime / 流动性分层（regime 层与逐票层分开报告）。"""

    market_return_by_date: dict[date, list[float]] = {}
    liquidity_by_date: dict[date, list[float]] = {}
    for row in rows:
        market_return = float(row.label_value or 0.0) - float(row.market_excess_return or 0.0)
        market_return_by_date.setdefault(row.feature_date, []).append(market_return)
        dollar_volume = (row.tradability_values or {}).get("dollar_volume")
        if dollar_volume is not None:
            liquidity_by_date.setdefault(row.feature_date, []).append(float(dollar_volume))
    regime_by_date: dict[date, str] = {}
    for feature_date, values in market_return_by_date.items():
        mean_market = sum(values) / len(values)
        regime_by_date[feature_date] = "market_up" if mean_market >= 0 else "market_down"
    liquidity_thresholds: dict[date, tuple[float | None, float | None]] = {}
    for feature_date, values in liquidity_by_date.items():
        ordered = sorted(values)
        if not ordered:
            liquidity_thresholds[feature_date] = (None, None)
            continue
        low_index = max(0, min(int(len(ordered) * 0.33), len(ordered) - 1))
        high_index = max(0, min(int(len(ordered) * 0.66), len(ordered) - 1))
        liquidity_thresholds[feature_date] = (ordered[low_index], ordered[high_index])
    output: list[ExperimentRow] = []
    for row in rows:
        low, high = liquidity_thresholds.get(row.feature_date, (None, None))
        dollar_volume = (row.tradability_values or {}).get("dollar_volume")
        if dollar_volume is None or low is None or high is None:
            bucket = "unknown"
        elif float(dollar_volume) <= low:
            bucket = "low"
        elif float(dollar_volume) > high:
            bucket = "high"
        else:
            bucket = "mid"
        strata = dict(row.strata)
        strata["regime"] = regime_by_date.get(row.feature_date, "unknown")
        strata["liquidity_bucket"] = bucket
        output.append(
            ExperimentRow(
                feature_date=row.feature_date,
                ticker=row.ticker,
                raw_score=row.raw_score,
                net_return=row.net_return,
                gross_return=row.gross_return,
                market_excess_return=row.market_excess_return,
                label_value=row.label_value,
                strata=strata,
                ranking_values=dict(row.ranking_values),
                tradability_values=dict(row.tradability_values),
            )
        )
    return output


def _load_lake_panel(
    *,
    spec: SelectionExperimentSpec,
    label_dataset_dir: Path,
    gate_config: PerTicketTradabilityConfig,
    min_dollar_volume: float,
    limit: int,
) -> tuple[list[ExperimentRow], list[ExperimentRow], list[ExperimentRow], dict[str, Any]]:
    """用 market_lake 动量源 + 生产标签构建逐票可成交 A/B 面板。

    treated = 通过逐票可成交门槛的子集；control = 同一全池（不按 regime 过滤）。
    """

    import polars as pl

    from app.services.market_lake import screen_lake_momentum

    samples_path = label_dataset_dir / "samples.parquet"
    if not samples_path.exists():
        raise SystemExit(f"label dataset samples.parquet not found: {samples_path}")
    horizon_days = max(spec.panel.horizons)
    frame = pl.scan_parquet(samples_path).filter(pl.col("horizon_days") == horizon_days)
    all_dates = sorted(
        str(item) for item in frame.select("feature_date").unique().collect()["feature_date"].to_list()
    )
    oos_dates = [
        value
        for value in all_dates
        if spec.panel.oos_start.isoformat() <= value <= spec.panel.oos_end.isoformat()
    ]
    if not oos_dates:
        raise SystemExit("label dataset has no mature (date,ticker) rows inside the OOS window")
    labels_frame = (
        frame.filter(pl.col("feature_date").is_in(oos_dates))
        .select(["ticker", "feature_date", "label_components_json"])
        .collect()
    )
    labels = {
        (str(item["feature_date"]), str(item["ticker"])): json.loads(str(item["label_components_json"]))
        for item in labels_frame.iter_rows(named=True)
    }

    rows: list[ExperimentRow] = []
    missing_label = 0
    lake_row_count = 0
    for date_key in oos_dates:
        feature_date = date.fromisoformat(date_key)
        lake_rows = screen_lake_momentum(
            market=spec.market,
            trade_date=date_key,
            limit=limit,
            min_dollar_volume=min_dollar_volume,
        )
        lake_row_count += len(lake_rows)
        for lake_row in lake_rows:
            components = labels.get((date_key, str(lake_row.get("ticker"))))
            if components is None:
                missing_label += 1
                continue
            rows.append(
                _lake_experiment_row(
                    lake_row,
                    feature_date=feature_date,
                    components=components,
                    cost_bps=spec.panel.cost_bps,
                    limit_up_stratum="yes" if lake_row.get("limit_up_today") else "no",
                )
            )
    if not rows:
        raise SystemExit("no (date,ticker) rows survived the lake/label join")
    rows = _attach_regime_and_liquidity_strata(rows)
    treated = apply_per_ticket_gate(rows, gate_config)
    meta = {
        "source": f"{spec.market} market_lake momentum full pool + production labels",
        "label_dataset_dir": str(label_dataset_dir),
        "horizon_days": horizon_days,
        "oos_dates": oos_dates,
        "lake_rows": lake_row_count,
        "joined_rows": len(rows),
        "missing_label_rows": missing_label,
        "treated_rows": len(treated),
        "gate_summary": per_ticket_gate_summary(rows, gate_config),
    }
    return treated, rows, [], meta



def _recompute_command(args: argparse.Namespace, spec: SelectionExperimentSpec) -> str:
    parts = [
        "python scripts/run_selection_experiment.py",
        f"--spec {args.spec}",
    ]
    if args.panel_json is not None:
        parts.append(f"--panel-json {args.panel_json}")
    elif args.lake_label_dataset_dir is not None:
        parts.append(f"--lake-label-dataset-dir {args.lake_label_dataset_dir}")
        parts.append(f"--min-liquidity-percentile {args.min_liquidity_percentile}")
        if args.no_limit_up_gate:
            parts.append("--no-limit-up-gate")
        if args.no_zero_volume_gate:
            parts.append("--no-zero-volume-gate")
        parts.append(f"--lake-min-dollar-volume {args.lake_min_dollar_volume}")
        parts.append(f"--lake-limit {args.lake_limit}")
    else:
        parts.append(f"--treated-evidence-dir {args.treated_evidence_dir}")
        parts.append(f"--control-evidence-dir {args.control_evidence_dir}")
        parts.append(f"--treated-dataset-dir {args.treated_dataset_dir}")
        parts.append(f"--control-dataset-dir {args.control_dataset_dir}")
        parts.append(f"--treated-model-key {args.treated_model_key}")
        parts.append(f"--control-model-key {args.control_model_key}")
    if args.strata_json is not None:
        parts.append(f"--strata-json {args.strata_json}")
    parts.append(f"--factor-set-key {args.factor_set_key}")
    if args.sentiment_metadata_json is not None:
        parts.append(f"--sentiment-metadata-json {args.sentiment_metadata_json}")
    if args.source_version:
        parts.append(f"--source-version {args.source_version}")
    parts.append(f"--output-dir {args.output_dir}")
    parts.append(f"--data-source {args.data_source}")
    return " ".join(parts)


def _load_sentiment_components(args: argparse.Namespace) -> dict[str, Any] | None:
    """Load and cross-check optional sentiment coverage/cutoff metadata.

    ``sentiment_v1`` must declare its coverage window and decision cutoff: if the
    factor set is requested without metadata the run fails closed instead of
    silently reusing a price-only dataset hash.
    """

    metadata_path = args.sentiment_metadata_json
    if metadata_path is None:
        if args.factor_set_key == SENTIMENT_FACTOR_SET_KEY:
            raise SystemExit(
                "--factor-set-key sentiment_v1 requires --sentiment-metadata-json "
                "(coverage window + decision cutoff must be declared)"
            )
        return None
    if args.factor_set_key != SENTIMENT_FACTOR_SET_KEY:
        raise SystemExit(
            "--sentiment-metadata-json requires --factor-set-key sentiment_v1"
        )
    raw = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise SystemExit("sentiment metadata JSON must be an object")
    return dict(raw)


def main() -> None:
    args = parse_args()
    spec = load_experiment_spec(args.spec)
    lake_meta: dict[str, Any] = {}
    sentiment_components = _load_sentiment_components(args)

    if args.panel_json is not None:
        treated, control, in_sample = _load_panel_json(args.panel_json, spec)
    elif args.lake_label_dataset_dir is not None:
        gate_config = PerTicketTradabilityConfig(
            exclude_limit_up_today=not args.no_limit_up_gate,
            exclude_zero_volume=not args.no_zero_volume_gate,
            min_dollar_volume_percentile=(
                None if args.min_liquidity_percentile <= 0 else args.min_liquidity_percentile
            ),
        )
        treated, control, in_sample, lake_meta = _load_lake_panel(
            spec=spec,
            label_dataset_dir=args.lake_label_dataset_dir,
            gate_config=gate_config,
            min_dollar_volume=args.lake_min_dollar_volume,
            limit=args.lake_limit,
        )
    else:
        missing = [
            name
            for name, value in (
                ("--control-evidence-dir", args.control_evidence_dir),
                ("--treated-dataset-dir", args.treated_dataset_dir),
                ("--control-dataset-dir", args.control_dataset_dir),
            )
            if value is None
        ]
        if missing:
            raise SystemExit(f"evidence mode requires: {', '.join(missing)}")
        treated, control, in_sample = _load_evidence_rows(
            spec=spec,
            treated_evidence_dir=args.treated_evidence_dir,
            control_evidence_dir=args.control_evidence_dir,
            treated_dataset_dir=args.treated_dataset_dir,
            control_dataset_dir=args.control_dataset_dir,
            treated_model_key=args.treated_model_key,
            control_model_key=args.control_model_key,
            strata_path=args.strata_json,
        )

    dataset = freeze_dataset_hash(
        market=spec.market,
        factor_set_key=args.factor_set_key,
        label_version=spec.panel.label_version,
        universe_version=spec.panel.universe_version,
        source_version=args.source_version,
        sentiment=sentiment_components,
    )

    market_key = args.market_key or spec.experiment_id
    attempts = args.attempts
    if attempts is None:
        stats = attempt_stats(model_key=market_key, path=args.registry_path)
        attempts = int(stats["attempts"])

    report = evaluate_experiment(
        spec,
        treated_rows=treated,
        control_rows=control,
        dataset_hash=dataset,
        attempts=attempts,
        in_sample_rows=in_sample or None,
        recompute_command=_recompute_command(args, spec),
    )
    files = persist_experiment_report(report, output_dir=args.output_dir, data_source=args.data_source)

    recorded = None
    if args.record_attempt:
        recorded = record_attempt(
            model_key=market_key,
            dataset_hash=report.dataset_hash,
            protocol_id=f"{spec.experiment_id}:{spec.panel.horizons}:{spec.panel.top_n}",
            verdict=report.decision,
            metrics={
                "classification": report.classification,
                "effect_pp": report.overall.effect_pp,
                "q_value": report.overall.q_value,
                "attempts": attempts,
            },
            path=args.registry_path,
        )

    print(
        json.dumps(
            {
                "status": "success",
                "experiment_id": report.experiment_id,
                "decision": report.decision,
                "classification": report.classification,
                "dataset_hash": report.dataset_hash,
                "attempts": report.attempts,
                "tightening_factor": report.tightening_factor,
                "effective_min_effect_pp": report.effective_min_effect_pp,
                "effect_pp": report.overall.effect_pp,
                "ci95": list(report.overall.ci95),
                "q_value": report.overall.q_value,
                "ranking_fields": list(report.ranking_fields),
                "ranked_by": report.ranked_by,
                "ranking_sensitivity": report.ranking_sensitivity,
                "layers": report.layers,
                "report_digest": report.report_digest,
                "json_report": str(files.json_path),
                "markdown_report": str(files.markdown_path),
                "recorded_attempt": recorded["entry_hash"] if recorded else None,
                "lake_panel_meta": lake_meta or None,
                "sentiment_metadata": sentiment_components,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
