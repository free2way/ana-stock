"""Run the frozen ``sentiment_v1`` forward-validation batch.

Three stages, all driven by the frozen spec written by
``scripts/init_sentiment_forward_batch.py``:

* ``collect``  — fetch the day's HiThink features (reusing
  :mod:`scripts.sync_hithink_sentiment_features`), rank the frozen universe under
  the batch cutoff, and persist one immutable per-effective-date snapshot.
* ``status``   — report matured / pending / blocked decision dates.
* ``evaluate`` — run the shared forward-shadow maturity gate and, once the batch
  is mature, the experiment framework.  An immature batch is explicitly blocked
  and emits **no** conclusion.

Example (weekly cron after the CN close)::

    python scripts/run_sentiment_forward_batch.py --stage collect
    python scripts/run_sentiment_forward_batch.py --stage status
    python scripts/run_sentiment_forward_batch.py --stage evaluate --execute
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.market_calendar import next_market_open_date  # noqa: E402
from app.services.market_freshness import latest_completed_market_date  # noqa: E402
from app.services.market_lake import get_latest_lake_trade_date, load_lake_rows  # noqa: E402
from app.services.stock_selection.experiment_registry import attempt_stats  # noqa: E402
from app.services.stock_selection.forward_shadow_evaluation import (  # noqa: E402
    CNForwardShadowEvaluationConfig,
    build_cn_forward_shadow_evaluation,
)
from app.services.stock_selection.sentiment_features import (  # noqa: E402
    build_sentiment_feature_matrix,
    load_sentiment_observations,
)
from app.services.stock_selection.sentiment_forward_batch import (  # noqa: E402
    DEFAULT_BATCH_PATH,
    assess_sentiment_forward_maturity,
    build_sentiment_forward_panel_from_scores,
    build_sentiment_forward_scores,
    load_sentiment_forward_batch,
    run_sentiment_forward_experiment,
)
from app.services.time_utils import app_now_iso  # noqa: E402

DEFAULT_SNAPSHOT_ROOT = ROOT_DIR / "data" / "artifacts" / "stock_selection_research" / "sentiment_forward"
DEFAULT_REPORT_ROOT = ROOT_DIR / "data" / "experiments" / "sentiment_v1_forward_reports"
SYNC_SCRIPT = ROOT_DIR / "scripts" / "sync_hithink_sentiment_features.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=("collect", "status", "evaluate"))
    parser.add_argument("--batch-path", type=Path, default=DEFAULT_BATCH_PATH)
    parser.add_argument("--snapshot-root", type=Path, default=DEFAULT_SNAPSHOT_ROOT)
    parser.add_argument("--report-root", type=Path, default=DEFAULT_REPORT_ROOT)
    parser.add_argument("--trade-date", default=None, help="collect: defaults to latest completed CN session.")
    parser.add_argument("--as-of-date", default=None, help="status/evaluate: maturity as-of date.")
    parser.add_argument("--universe-parquet", type=Path, default=None, help="Frozen PIT universe.parquet for the collect universe.")
    parser.add_argument("--skip-fetch", action="store_true", help="collect: reuse the existing HiThink store.")
    parser.add_argument("--registry-path", type=Path, default=None)
    parser.add_argument("--attempts", type=int, default=None, help="Overrides the experiment-registry attempts count.")
    parser.add_argument("--execute", action="store_true", help="evaluate: persist the report JSON.")
    return parser.parse_args()


def _snapshot_dir(root: Path, batch) -> Path:
    return root / batch.batch_id / "snapshots"


def _effective_trade_date(feature_date: date) -> str:
    return next_market_open_date("CN", feature_date, include_self=False)


def _load_snapshots(snapshot_dir: Path) -> list[dict]:
    snapshots: list[dict] = []
    for path in sorted(snapshot_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            snapshots.append(payload)
    return snapshots


def _collect_universe(args: argparse.Namespace, trade_date: str) -> tuple[list[str], str]:
    if args.universe_parquet is not None:
        import polars as pl

        frame = (
            pl.scan_parquet(args.universe_parquet)
            .filter((pl.col("trade_date") == trade_date) & pl.col("included"))
            .select("ticker")
            .collect()
        )
        tickers = sorted({str(value).strip().upper() for value in frame["ticker"].to_list()})
        if tickers:
            return tickers, f"pit_universe_artifact:{args.universe_parquet}"
        raise SystemExit(f"no included tickers for {trade_date} in {args.universe_parquet}")
    rows = load_lake_rows(markets=["CN"], start_date=trade_date, end_date=trade_date)
    tickers = sorted(
        {
            str(row.get("symbol") or "").strip().upper()
            for row in rows
            if str(row.get("date") or "")[:10] == trade_date and str(row.get("symbol") or "").strip()
        }
    )
    return tickers, "lake_symbols_for_date"


def _collect(args: argparse.Namespace, batch) -> dict:
    trade_date = args.trade_date or latest_completed_market_date("CN")
    feature_date = date.fromisoformat(trade_date[:10])
    if not batch.covers(feature_date):
        return {
            "status": "skipped_out_of_coverage",
            "feature_date": trade_date,
            "message": "feature date is outside the frozen coverage window",
        }
    if not args.skip_fetch:
        completed = subprocess.run(
            [sys.executable, str(SYNC_SCRIPT), "--trade-date", trade_date],
            cwd=ROOT_DIR,
            check=False,
        )
        if completed.returncode != 0:
            raise SystemExit(f"hithink feature sync failed with code {completed.returncode}")
    tickers, universe_source = _collect_universe(args, trade_date)
    if not tickers:
        raise SystemExit(f"no CN universe tickers resolved for {trade_date}")
    matrix = build_sentiment_feature_matrix(
        observations=load_sentiment_observations(),
        universe_by_date={feature_date: tickers},
        cutoff_by_date=batch.cutoff_by_date([feature_date]),
    )
    scored = build_sentiment_forward_scores(
        batch=batch,
        features_by_key=matrix.features_by_key,
        universe_by_date={feature_date: tickers},
    )
    score_rows = scored["score_rows"]
    top_rows = sorted(score_rows, key=lambda row: (-row["cross_sectional_rank"], row["ticker"]))[
        : batch.top_n
    ]
    effective_date = _effective_trade_date(feature_date)
    payload = {
        "schema_version": "sentiment_v1_forward_snapshot_v1",
        "batch_id": batch.batch_id,
        "dataset_hash": batch.dataset_hash,
        "factor_set": batch.factor_set,
        "feature_date": trade_date,
        "effective_trade_date": effective_date,
        "horizon_days": batch.horizon_days,
        "cutoff_semantics": batch.cutoff.semantics,
        "coverage_window": batch.coverage.window,
        "source_version": matrix.source_version,
        "universe_source": universe_source,
        "created_at": app_now_iso(),
        "universe_symbol_count": len(tickers),
        "scored_symbol_count": len(score_rows),
        "coverage_excluded_dates": scored["coverage_excluded_dates"],
        "top_observations": [
            {"ticker": row["ticker"], "cross_sectional_rank": row["cross_sectional_rank"]}
            for row in top_rows
        ],
        "score_rows": score_rows,
    }
    snapshot_dir = _snapshot_dir(args.snapshot_root, batch)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    target = snapshot_dir / f"{effective_date}.json"
    if target.exists():
        existing = json.loads(target.read_text(encoding="utf-8"))
        if existing.get("dataset_hash") != batch.dataset_hash:
            raise SystemExit(
                f"snapshot {target} belongs to a different dataset_hash; refusing to overwrite"
            )
        return {
            "status": "reused_existing",
            "feature_date": existing.get("feature_date"),
            "effective_trade_date": existing.get("effective_trade_date"),
            "snapshot_path": str(target),
            "top_count": len(existing.get("top_observations") or []),
            "universe_source": existing.get("universe_source"),
        }
    temporary = target.with_name(f".{target.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "status": "created",
        "feature_date": trade_date,
        "effective_trade_date": effective_date,
        "snapshot_path": str(target),
        "top_count": len(top_rows),
        "universe_source": universe_source,
    }


def _resolve_as_of(args: argparse.Namespace) -> str:
    resolved = str(
        args.as_of_date or get_latest_lake_trade_date(market="CN") or latest_completed_market_date("CN")
    )[:10]
    if not resolved:
        raise SystemExit("no CN lake date is available")
    return resolved


def _evaluate_core(args: argparse.Namespace, batch, *, as_of_date: str) -> dict:
    snapshots = _load_snapshots(_snapshot_dir(args.snapshot_root, batch))
    if not snapshots:
        raise SystemExit("no frozen sentiment snapshots found; run --stage collect first")
    feature_dates = sorted(str(item.get("feature_date") or "")[:10] for item in snapshots)
    tickers = {
        str(obs.get("ticker") or "").strip().upper()
        for item in snapshots
        for obs in (item.get("top_observations") or [])
        if str(obs.get("ticker") or "").strip()
    }
    price_rows = load_lake_rows(
        markets=["CN"],
        tickers=tickers,
        start_date=feature_dates[0],
        end_date=as_of_date,
    )
    evaluation = build_cn_forward_shadow_evaluation(
        snapshots,
        price_rows,
        as_of_date=as_of_date,
        config=CNForwardShadowEvaluationConfig(
            top_ns=(batch.top_n,),
            round_trip_cost_bps=batch.cost_bps,
            minimum_confirmation_dates=batch.acceptance.min_matured_dates,
        ),
    )
    aggregate = (evaluation.get("aggregate_top_n") or {}).get(str(batch.top_n), {})
    maturity = assess_sentiment_forward_maturity(
        matured_date_count=int(evaluation.get("evaluated_date_count") or 0),
        min_matured_dates=batch.acceptance.min_matured_dates,
        pending_date_count=int(evaluation.get("pending_date_count") or 0),
        incomplete_date_count=int(evaluation.get("incomplete_date_count") or 0),
    )
    core = {
        "batch_id": batch.batch_id,
        "dataset_hash": batch.dataset_hash,
        "as_of_date": as_of_date,
        "maturity": maturity,
        "primary_metric": batch.acceptance.primary_metric,
        "primary_metric_value": aggregate.get("stock_signal_win_rate"),
        "aggregate_top_n": aggregate,
        "forward_shadow_evaluation": {
            "evaluated_date_count": evaluation.get("evaluated_date_count"),
            "pending_date_count": evaluation.get("pending_date_count"),
            "excluded_date_count": evaluation.get("excluded_date_count"),
            "incomplete_date_count": evaluation.get("incomplete_date_count"),
            "remaining_confirmation_dates": evaluation.get("remaining_confirmation_dates"),
            "promotion_status": evaluation.get("promotion_status"),
            "promotion_blockers": evaluation.get("promotion_blockers"),
        },
    }
    if not maturity["conclusion_allowed"]:
        return {
            "status": "blocked",
            "conclusion": None,
            "conclusion_allowed": False,
            "message": "batch is not mature; no forward conclusion is produced",
            **core,
        }
    score_rows = [row for item in snapshots for row in (item.get("score_rows") or [])]
    panel = build_sentiment_forward_panel_from_scores(
        batch=batch,
        score_rows=score_rows,
        price_rows=price_rows,
        as_of_date=as_of_date,
    )
    matured = panel["matured_dates"]
    attempts = args.attempts
    if attempts is None:
        stats = attempt_stats(model_key=batch.batch_id, path=args.registry_path)
        attempts = int(stats["attempts"])
    report = run_sentiment_forward_experiment(
        batch=batch,
        treated_rows=panel["treated_rows"],
        control_rows=panel["control_rows"],
        oos_end=matured[-1] if matured else batch.start_date,
        attempts=attempts,
    )
    hit_rate = core["primary_metric_value"]
    acceptance_passed = bool(
        hit_rate is not None
        and float(hit_rate) >= batch.acceptance.min_net_hit_rate
        and report["overall"]["ci95"][0] > 0
        and report["overall"]["q_value"] <= batch.acceptance.fdr
        and report["overall"]["independent_date_count"] >= batch.acceptance.min_independent_dates
    )
    return {
        "status": "evaluated",
        "conclusion_allowed": True,
        "conclusion": report["decision"],
        "classification": report["classification"],
        "acceptance_passed": acceptance_passed,
        "experiment": report,
        **core,
    }


def main() -> int:
    args = parse_args()
    batch = load_sentiment_forward_batch(args.batch_path)

    if args.stage == "collect":
        result = {"stage": "collect", "dataset_hash": batch.dataset_hash, **_collect(args, batch)}
    elif args.stage == "status":
        as_of = _resolve_as_of(args)
        result = {"stage": "status", **_evaluate_core(args, batch, as_of_date=as_of)}
    else:
        as_of = _resolve_as_of(args)
        result = _evaluate_core(args, batch, as_of_date=as_of)
        result = {"stage": "evaluate", **result}
        if args.execute and result["status"] == "evaluated":
            args.report_root.mkdir(parents=True, exist_ok=True)
            report_path = args.report_root / f"{batch.batch_id}-forward-report.json"
            temporary = report_path.with_name(f".{report_path.name}.tmp")
            temporary.write_text(
                json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, report_path)
            result["report_path"] = str(report_path)
        elif args.execute:
            result["report_path"] = None

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
