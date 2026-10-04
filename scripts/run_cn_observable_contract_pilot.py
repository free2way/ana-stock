"""Run an isolated CN Ridge pilot on HiThink raw prices under evidence policy v2.

The pilot creates no database model run and cannot replace the production
champion.  Every generated candidate keeps its replay status/reason.  Training
and holdout labels both come from the same execution reconciliation function.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.execution_reconciliation import VERSION, execution_contract, replay_candidate
from app.services.execution_costs import FillCostModel
from app.services.stock_selection.frozen_panel_comparison import compare_frozen_panel_models
from app.services.trainer import SignalTrainer

SHANGHAI = ZoneInfo("Asia/Shanghai")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def internal_ticker(value: str) -> str:
    return value[:-3] + ".SS" if value.endswith(".SH") else value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy-receipt", type=Path, required=True)
    parser.add_argument("--price-dump", type=Path, required=True)
    parser.add_argument("--action-dump", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ticker-count", type=int, default=30)
    parser.add_argument("--holdout-dates", type=int, default=10)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("immutable pilot receipt already exists")
    policy = json.loads(args.policy_receipt.read_text(encoding="utf-8"))
    if policy.get("small_scope_status") != "READY" or not policy.get("training_authorized_by_policy_receipt"):
        raise RuntimeError("policy receipt does not authorize the bounded pilot")
    if (policy.get("policy") or {}).get("version") != "raw_price_volume_company_action_v2":
        raise RuntimeError("unexpected evidence policy")
    if not 20 <= args.ticker_count <= 100 or not 5 <= args.holdout_dates <= 30:
        raise ValueError("pilot bounds are 20..100 tickers and 5..30 holdout dates")

    raw = pl.scan_parquet(args.price_dump).filter(
        (pl.col("date_ms") >= 1767225600000) &
        (pl.col("thscode").str.ends_with(".SZ") | pl.col("thscode").str.ends_with(".SH"))
    ).collect()
    recent_dates = sorted(raw.get_column("date_ms").unique().to_list())[-60:]
    liquid = (raw.filter(pl.col("date_ms").is_in(recent_dates))
              .group_by("thscode").agg(pl.col("turnover").mean().alias("avg_turnover"))
              .sort(["avg_turnover", "thscode"], descending=[True, False])
              .head(args.ticker_count).get_column("thscode").to_list())
    frame = raw.filter(pl.col("thscode").is_in(liquid)).sort(["thscode", "date_ms"])
    price_sha = file_sha256(args.price_dump)
    action_sha = file_sha256(args.action_dump)
    action_frame = pl.scan_parquet(args.action_dump).filter(pl.col("thscode").is_in(liquid)).collect()
    action_keys = {(internal_ticker(row["thscode"]), datetime.fromtimestamp(row["ex_date_ms"] / 1000,
                   tz=SHANGHAI).date().isoformat()) for row in action_frame.iter_rows(named=True)}
    rows = []
    for item in frame.iter_rows(named=True):
        day = datetime.fromtimestamp(item["date_ms"] / 1000, tz=SHANGHAI).date().isoformat()
        ticker = internal_ticker(item["thscode"])
        rows.append({
            "date": day, "symbol": ticker, "open": item["open_price"],
            "high": item["high_price"], "low": item["low_price"],
            "close": item["close_price"], "volume": item["volume"],
            "price_basis": "raw",
            "execution_source_reference": f"hithink:daily-k:sha256:{price_sha}",
            "corporate_action_status": "action" if (ticker, day) in action_keys else "none",
        })

    trainer = SignalTrainer.__new__(SignalTrainer)
    trainer.settings = SimpleNamespace(
        trainer_cn_execution_commission_bps=8,
        trainer_cn_execution_slippage_bps=12,
        trainer_cn_label_profile=VERSION,
    )
    samples = trainer._build_lightgbm_samples(
        rows=rows, lookback_days=3, horizon_days=5, symbol_feature_context={}, market="CN")
    dated = sorted({sample["trade_date"] for sample in samples if sample["target"] is not None})
    holdout = set(dated[-args.holdout_dates:])
    first_holdout = min(holdout)
    train = [sample for sample in samples if sample["target"] is not None
             and sample["trade_date"] < first_holdout
             and str(sample.get("label_available_date") or "") < first_holdout]
    test = [sample for sample in samples if sample["trade_date"] in holdout]
    feature_names = trainer._feature_names(lookback_days=3)
    if len(train) < 1000 or len(test) < args.ticker_count * 3:
        raise RuntimeError("pilot lacks enough leakage-safe train/holdout samples")
    scaler = StandardScaler()
    x_train = scaler.fit_transform([[sample["features"][name] for name in feature_names] for sample in train])
    model = Ridge(alpha=10.0).fit(x_train, [sample["target"] for sample in train])
    x_test = scaler.transform([[sample["features"][name] for name in feature_names] for sample in test])
    scores = model.predict(x_test)

    by_symbol: dict[str, list[dict]] = {}
    for row in rows:
        by_symbol.setdefault(row["symbol"], []).append(row)
    candidates = []
    mismatch_count = 0
    for sample, score in zip(test, scores, strict=True):
        replay = replay_candidate(
            ticker=sample["symbol"], market="CN", signal_date=sample["trade_date"],
            horizon_days=5, rows=by_symbol[sample["symbol"]],
            contract=execution_contract("CN", FillCostModel(8, 12)))
        target = sample.get("target")
        if target is not None and replay.get("net_return") is not None and abs(target - replay["net_return"]) > 1e-12:
            mismatch_count += 1
        candidates.append({
            "ticker": sample["symbol"], "signal_date": sample["trade_date"],
            "score": float(score), "training_target": target,
            "replay_status": replay["status"], "replay_reason": replay["reason"],
            "replay_net_return": replay["net_return"],
            "optional_evidence_gaps": replay["optional_evidence_gaps"],
        })
    selected = []
    for day in sorted(holdout):
        selected.extend(sorted((row for row in candidates if row["signal_date"] == day),
                               key=lambda row: (-row["score"], row["ticker"]))[:5])
    closed = [row for row in selected if row["replay_status"] == "CLOSED"]
    returns = [row["replay_net_return"] for row in closed]
    status_counts = dict(Counter(row["replay_status"] for row in candidates))
    chain_pass = mismatch_count == 0 and bool(closed)
    comparison = compare_frozen_panel_models(
        train=train, test=test, feature_names=feature_names,
        lower_better={name for name in feature_names if trainer._feature_direction(name) < 0}, top_n=5)
    model_gate_pass = chain_pass and bool(comparison["passing_models"])
    payload = {
        "schema": "cn_observable_contract_pilot_v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "CHAIN_PASS_CHALLENGER_FOUND" if model_gate_pass else
                  "CHAIN_PASS_MODEL_REJECTED" if chain_pass else "BLOCKED",
        "chain_status": "PASS" if chain_pass else "BLOCKED",
        "model_gate_status": "RESEARCH_PASS" if model_gate_pass else "REJECTED",
        "promotion_authorized": False,
        "scope": "isolated_research_only_not_production_not_model_promotion",
        "market": "CN", "contract": execution_contract("CN", FillCostModel(8, 12)),
        "lineage": {
            "policy_receipt": str(args.policy_receipt),
            "policy_receipt_sha256": file_sha256(args.policy_receipt),
            "price_dump_sha256": price_sha, "action_dump_sha256": action_sha,
        },
        "ticker_count": len(liquid), "train_sample_count": len(train),
        "holdout_candidate_count": len(test), "holdout_dates": sorted(holdout),
        "candidate_status_counts": status_counts, "per_trade_mismatch_count": mismatch_count,
        "top5": {
            "count": len(selected), "closed_count": len(closed),
            "hit_rate": (sum(value > 0 for value in returns) / len(returns)) if returns else None,
            "mean_net_return": float(np.mean(returns)) if returns else None,
            "median_net_return": float(np.median(returns)) if returns else None,
        },
        "model": {"family": "ridge", "alpha": 10.0, "feature_names": feature_names,
                  "coefficient_sha256": hashlib.sha256(np.asarray(model.coef_).tobytes()).hexdigest()},
        "frozen_panel_comparison": comparison,
        "candidates": candidates,
        "old_results_rewritten": False, "database_model_run_created": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print(json.dumps({key: payload[key] for key in ("status", "ticker_count", "train_sample_count",
          "holdout_candidate_count", "candidate_status_counts", "per_trade_mismatch_count", "top5")},
          ensure_ascii=False))


if __name__ == "__main__":
    main()
