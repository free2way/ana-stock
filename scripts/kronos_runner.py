#!/usr/bin/env python3
"""External Kronos runner for Personal Quant Workbench.

The main app calls this script through PQW_KRONOS_RUNNER_COMMAND. Keep it in a
separate Python environment so PyTorch/HuggingFace dependencies do not affect the
web app runtime.
"""

from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path


TOKENIZER_BY_MODEL = {
    "NeoQuasar/Kronos-mini": "NeoQuasar/Kronos-Tokenizer-2k",
    "NeoQuasar/Kronos-small": "NeoQuasar/Kronos-Tokenizer-base",
    "NeoQuasar/Kronos-base": "NeoQuasar/Kronos-Tokenizer-base",
}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        _emit_error(f"Invalid JSON input: {exc}")
        return 2

    try:
        rows = run_predictions(payload)
    except Exception as exc:
        _emit_error(str(exc))
        return 1

    print(json.dumps({"rows": rows}, ensure_ascii=False))
    return 0


def run_predictions(payload: dict) -> list[dict]:
    _prepare_kronos_import_path()

    import numpy as np
    import pandas as pd
    import torch
    from model import Kronos, KronosPredictor, KronosTokenizer

    seed = int(payload.get("seed") or 42)
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.backends.mps.is_available():
        torch.mps.manual_seed(seed)

    model_name = str(payload.get("model_name") or "NeoQuasar/Kronos-mini").strip()
    tokenizer_name = str(payload.get("tokenizer_name") or TOKENIZER_BY_MODEL.get(model_name) or "NeoQuasar/Kronos-Tokenizer-base")
    device = str(payload.get("device") or "cpu").strip()
    horizon_days = max(1, int(payload.get("horizon_days") or 3))
    candidates = [item for item in (payload.get("candidates") or []) if isinstance(item, dict)]
    if not candidates:
        return []

    tokenizer = KronosTokenizer.from_pretrained(tokenizer_name)
    model = Kronos.from_pretrained(model_name)
    predictor = KronosPredictor(model, tokenizer, max_context=_max_context_for_model(model_name), device=device)

    prepared = []
    df_list = []
    x_timestamp_list = []
    y_timestamp_list = []
    for candidate in candidates:
        prepared_item = _prepare_candidate_frame(candidate, horizon_days=horizon_days, pd=pd)
        if prepared_item is None:
            continue
        prepared.append(prepared_item)
        df_list.append(prepared_item["x_df"])
        x_timestamp_list.append(prepared_item["x_timestamp"])
        y_timestamp_list.append(prepared_item["y_timestamp"])

    if not prepared:
        return []
    _align_batch_context_lengths(prepared)
    df_list = [item["x_df"] for item in prepared]
    x_timestamp_list = [item["x_timestamp"] for item in prepared]
    y_timestamp_list = [item["y_timestamp"] for item in prepared]

    pred_df_list = predictor.predict_batch(
        df_list=df_list,
        x_timestamp_list=x_timestamp_list,
        y_timestamp_list=y_timestamp_list,
        pred_len=horizon_days,
        T=float(payload.get("temperature") or 0.8),
        top_p=float(payload.get("top_p") or 0.9),
        sample_count=max(1, int(payload.get("sample_count") or 3)),
        verbose=False,
    )

    rows = []
    for item, pred_df in zip(prepared, pred_df_list, strict=False):
        rows.append(_summarize_prediction(item, pred_df))
    return rows


def _prepare_kronos_import_path() -> None:
    repo_path = str(os.environ.get("PQW_KRONOS_REPO_PATH") or "").strip()
    if not repo_path:
        return
    path = Path(repo_path).expanduser().resolve()
    if path.exists() and str(path) not in sys.path:
        sys.path.insert(0, str(path))


def _max_context_for_model(model_name: str) -> int:
    return 2048 if model_name.endswith("Kronos-mini") else 512


def _prepare_candidate_frame(candidate: dict, *, horizon_days: int, pd):
    history = [row for row in (candidate.get("history") or []) if isinstance(row, dict)]
    if len(history) < 8:
        return None
    frame = pd.DataFrame(history)
    if "date" not in frame.columns:
        return None
    frame["timestamps"] = pd.to_datetime(frame["date"])
    frame = frame.sort_values("timestamps").drop_duplicates(subset=["timestamps"], keep="last")
    for column in ("open", "high", "low", "close", "volume"):
        if column not in frame.columns:
            if column == "volume":
                frame[column] = 0.0
            else:
                return None
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
    if "amount" not in frame.columns:
        frame["amount"] = frame["close"] * frame["volume"]
    else:
        frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce").fillna(frame["close"] * frame["volume"])
    frame = frame[frame["close"] > 0].copy()
    if len(frame) < 8:
        return None
    last_ts = frame["timestamps"].iloc[-1]
    y_timestamp = pd.Series(pd.bdate_range(start=last_ts + pd.Timedelta(days=1), periods=horizon_days))
    return {
        "ticker": str(candidate.get("ticker") or "").strip().upper(),
        "market": str(candidate.get("market") or "").strip().upper(),
        "latest_close": float(frame["close"].iloc[-1]),
        "x_df": frame[["open", "high", "low", "close", "volume", "amount"]].copy(),
        "x_timestamp": frame["timestamps"].copy(),
        "y_timestamp": y_timestamp,
    }


def _align_batch_context_lengths(items: list[dict]) -> None:
    # Kronos predict_batch requires all historical contexts in one batch to use
    # the same length. We keep the most recent overlapping window.
    target_len = min(len(item["x_df"]) for item in items)
    for item in items:
        item["x_df"] = item["x_df"].tail(target_len).reset_index(drop=True)
        item["x_timestamp"] = item["x_timestamp"].tail(target_len).reset_index(drop=True)


def _summarize_prediction(item: dict, pred_df) -> dict:
    latest_close = float(item["latest_close"] or 0.0)
    closes = [float(value) for value in list(pred_df.get("close", [])) if value is not None]
    highs = [float(value) for value in list(pred_df.get("high", [])) if value is not None]
    lows = [float(value) for value in list(pred_df.get("low", [])) if value is not None]
    if latest_close <= 0 or not closes:
        return {
            "ticker": item["ticker"],
            "decision": "Kronos 无有效预测",
            "reason": "Prediction output did not contain usable closes.",
        }
    return_1d = _pct(closes[0], latest_close)
    return_3d = _pct(closes[-1], latest_close)
    max_high_return = max(_pct(value, latest_close) for value in (highs or closes))
    max_drawdown = min(_pct(value, latest_close) for value in (lows or closes))
    path_score = _path_score(return_3d=return_3d, max_high_return=max_high_return, max_drawdown=max_drawdown)
    decision = _decision(path_score=path_score, return_3d=return_3d, max_drawdown=max_drawdown)
    return {
        "ticker": item["ticker"],
        "market": item["market"],
        "kronos_score": round(path_score, 2),
        "expected_return_1d_pct": round(return_1d, 2),
        "expected_return_3d_pct": round(return_3d, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
        "max_high_return_pct": round(max_high_return, 2),
        "decision": decision,
        "reason": (
            f"3D expected return {return_3d:.2f}%, max high {max_high_return:.2f}%, "
            f"path drawdown {max_drawdown:.2f}%."
        ),
    }


def _pct(value: float, base: float) -> float:
    if base == 0:
        return 0.0
    return ((value / base) - 1.0) * 100.0


def _path_score(*, return_3d: float, max_high_return: float, max_drawdown: float) -> float:
    score = 50.0 + return_3d * 5.0 + max_high_return * 1.8 + max_drawdown * 2.6
    return max(0.0, min(100.0, score))


def _decision(*, path_score: float, return_3d: float, max_drawdown: float) -> str:
    if path_score >= 68 and return_3d > 0 and max_drawdown > -5:
        return "Kronos 支持"
    if path_score <= 42 or max_drawdown <= -8:
        return "Kronos 不支持"
    return "Kronos 中性"


def _emit_error(message: str) -> None:
    print(json.dumps({"status": "failed", "error": message}, ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
