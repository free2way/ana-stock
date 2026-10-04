"""Run an isolated US Ridge pilot from immutable Alpaca evidence."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.execution_costs import FillCostModel
from app.services.execution_reconciliation import VERSION, digest, execution_contract, replay_candidate
from app.services.stock_selection.frozen_panel_comparison import compare_frozen_panel_models
from app.services.trainer import SignalTrainer


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--holdout-dates", type=int, default=10)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("immutable pilot receipt already exists")
    envelope = json.loads(args.evidence.read_text(encoding="utf-8"))
    source = envelope.get("payload") or {}
    source_digest = hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if envelope.get("sha256") != source_digest:
        raise ValueError("US evidence hash mismatch")
    if (source.get("schema") != "us_alpaca_observable_evidence_v1"
            or source.get("price_basis") != "raw" or source.get("feed") != "iex"
            or source.get("fallback_used") is not False
            or source.get("policy") != "raw_price_volume_company_action_v2"):
        raise ValueError("unsupported US evidence contract")
    tickers = source["scope"]["tickers"]
    if len(tickers) < 20:
        raise ValueError("US pilot evidence is too narrow")
    action_keys = {(str(item.get("symbol") or "").upper(), str(item.get("ex_date") or "")[:10])
                   for item in source.get("corporate_actions") or [] if item.get("ex_date")}
    reference = f"alpaca:iex:sha256:{envelope['sha256']}"
    rows = [{"date": item["date"], "symbol": item["ticker"], "open": item["open"],
             "high": item["high"], "low": item["low"], "close": item["close"],
             "volume": item["volume"], "price_basis": "raw",
             "execution_source_reference": reference,
             "corporate_action_status": ("action" if (item["ticker"], item["date"]) in action_keys else "none")}
            for item in source["bars"]]
    trainer = SignalTrainer.__new__(SignalTrainer)
    trainer.settings = SimpleNamespace(trainer_cn_execution_commission_bps=8,
        trainer_cn_execution_slippage_bps=12, trainer_cn_label_profile=VERSION)
    samples = trainer._build_lightgbm_samples(
        rows=rows, lookback_days=3, horizon_days=5, symbol_feature_context={}, market="US")
    dated = sorted({sample["trade_date"] for sample in samples if sample["target"] is not None})
    if len(dated) <= args.holdout_dates:
        raise RuntimeError("insufficient matured US dates")
    holdout = set(dated[-args.holdout_dates:])
    first_holdout = min(holdout)
    train = [sample for sample in samples if sample["target"] is not None
             and sample["trade_date"] < first_holdout
             and str(sample.get("label_available_date") or "") < first_holdout]
    test = [sample for sample in samples if sample["trade_date"] in holdout]
    feature_names = trainer._feature_names(lookback_days=3)
    if len(train) < 1000 or len(test) < len(tickers) * 3:
        raise RuntimeError("pilot lacks leakage-safe train/holdout samples")
    scaler = StandardScaler()
    x_train = scaler.fit_transform([[sample["features"][name] for name in feature_names] for sample in train])
    model = Ridge(alpha=10.0).fit(x_train, [sample["target"] for sample in train])
    scores = model.predict(scaler.transform(
        [[sample["features"][name] for name in feature_names] for sample in test]))
    by_symbol = {ticker: sorted((row for row in rows if row["symbol"] == ticker), key=lambda row: row["date"])
                 for ticker in tickers}
    contract = execution_contract("US", FillCostModel(8, 12))
    candidates, mismatches = [], 0
    for sample, score in zip(test, scores, strict=True):
        replay = replay_candidate(ticker=sample["symbol"], market="US", signal_date=sample["trade_date"],
            horizon_days=5, rows=by_symbol[sample["symbol"]], contract=contract)
        target = sample.get("target")
        if target is not None and replay.get("net_return") is not None and abs(target - replay["net_return"]) > 1e-12:
            mismatches += 1
        candidates.append({"ticker": sample["symbol"], "signal_date": sample["trade_date"],
            "score": float(score), "training_target": target, "replay_status": replay["status"],
            "replay_reason": replay["reason"], "replay_net_return": replay["net_return"],
            "optional_evidence_gaps": replay["optional_evidence_gaps"]})
    selected = []
    for day in sorted(holdout):
        selected.extend(sorted((row for row in candidates if row["signal_date"] == day),
                               key=lambda row: (-row["score"], row["ticker"]))[:5])
    closed = [row for row in selected if row["replay_status"] == "CLOSED"]
    returns = [row["replay_net_return"] for row in closed]
    chain_pass = mismatches == 0 and bool(closed)
    comparison = compare_frozen_panel_models(
        train=train, test=test, feature_names=feature_names,
        lower_better={name for name in feature_names if trainer._feature_direction(name) < 0}, top_n=5)
    model_gate_pass = chain_pass and bool(comparison["passing_models"])
    payload = {
        "schema": "us_observable_contract_pilot_v1", "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "CHAIN_PASS_CHALLENGER_FOUND" if model_gate_pass else
                  "CHAIN_PASS_MODEL_REJECTED" if chain_pass else "BLOCKED",
        "chain_status": "PASS" if chain_pass else "BLOCKED",
        "model_gate_status": "RESEARCH_PASS" if model_gate_pass else "REJECTED",
        "promotion_authorized": False,
        "scope": "isolated_research_only_not_production_not_model_promotion",
        "market": "US", "contract": contract,
        "lineage": {"evidence": str(args.evidence), "evidence_file_sha256": file_sha256(args.evidence),
                    "evidence_payload_sha256": envelope["sha256"]},
        "ticker_count": len(tickers), "train_sample_count": len(train),
        "holdout_candidate_count": len(test), "holdout_dates": sorted(holdout),
        "candidate_status_counts": dict(Counter(row["replay_status"] for row in candidates)),
        "per_trade_mismatch_count": mismatches,
        "top5": {"count": len(selected), "closed_count": len(closed),
                 "hit_rate": sum(value > 0 for value in returns) / len(returns) if returns else None,
                 "mean_net_return": float(np.mean(returns)) if returns else None,
                 "median_net_return": float(np.median(returns)) if returns else None},
        "model": {"family": "ridge", "alpha": 10.0, "feature_names": feature_names,
                  "coefficient_sha256": hashlib.sha256(np.asarray(model.coef_).tobytes()).hexdigest()},
        "frozen_panel_comparison": comparison,
        "candidates": candidates, "old_results_rewritten": False,
        "database_model_run_created": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    print(json.dumps({key: payload[key] for key in ("status", "ticker_count", "train_sample_count",
        "holdout_candidate_count", "candidate_status_counts", "per_trade_mismatch_count", "top5")},
        ensure_ascii=False))


if __name__ == "__main__":
    main()
