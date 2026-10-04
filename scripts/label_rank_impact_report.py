"""C-5/C-6 impact report: labels and rankings, raw basis vs adjusted view.

C-5 (label impact), per market and horizon:
  sample count, |delta label| > 1% share, sign flips (profit<->loss),
  samples starting on an ex-date, samples with an action inside the window.

C-6 (ranking impact), for the latest common date:
  Top-20/50/100 overlap and Spearman RankIC between momentum ranks computed on
  the raw basis versus the adjusted view; CN board distribution of the top set.

Outputs JSON (and CSV next to it).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402

from app.services.corporate_actions import load_actions  # noqa: E402

HORIZONS = (1, 3, 5, 20)
TOP_NS = (20, 50, 100)


def _raw_pattern(market: str, raw_glob: str | None) -> str:
    if raw_glob:
        return raw_glob
    if market == "US":
        return str(ROOT / "data" / "lake" / "_us_alpaca" / "raw" / "*.parquet")
    return str(ROOT / "data" / "lake" / "cn_daily" / "date=*" / "*.parquet")


def _adjusted_pattern(market: str, method: str) -> str:
    return str(ROOT / "data" / "lake" / "_adjusted_v2" / market.lower() / f"method={method}" / "adjusted.parquet")


def _joined_cte(raw_pattern: str, adjusted_pattern: str) -> str:
    return f"""
    WITH r AS (SELECT symbol, CAST(date AS DATE) AS d, close AS rc FROM read_parquet('{raw_pattern}') WHERE close > 0),
         a AS (SELECT symbol, CAST(date AS DATE) AS d, close AS ac FROM read_parquet('{adjusted_pattern}') WHERE close > 0),
         j AS (SELECT r.symbol, r.d, r.rc, a.ac FROM r JOIN a ON a.symbol = r.symbol AND a.d = r.d)
    """


def label_impact(market: str, method: str, raw_glob: str | None) -> dict:
    raw_pattern = _raw_pattern(market, raw_glob)
    adjusted_pattern = _adjusted_pattern(market, method)
    actions = load_actions(market)
    action_keys = sorted({(a.symbol, a.effective_date.isoformat()) for a in actions})
    horizons: dict[str, dict] = {}
    for horizon in HORIZONS:
        row = duckdb.sql(
            _joined_cte(raw_pattern, adjusted_pattern)
            + f"""
            , f AS (
              SELECT symbol, d, rc, ac,
                     lead(rc, {horizon}) OVER (PARTITION BY symbol ORDER BY d) AS rc_h,
                     lead(ac, {horizon}) OVER (PARTITION BY symbol ORDER BY d) AS ac_h
              FROM j
            )
            SELECT count(*) AS samples,
                   sum(CASE WHEN abs((ac_h/ac - 1) - (rc_h/rc - 1)) > 0.01 THEN 1 ELSE 0 END) AS delta_gt_1pct,
                   sum(CASE WHEN sign((ac_h/ac - 1)) <> sign((rc_h/rc - 1)) THEN 1 ELSE 0 END) AS sign_flips,
                   avg(abs((ac_h/ac - 1) - (rc_h/rc - 1))) AS mean_abs_delta
            FROM f WHERE rc_h IS NOT NULL AND rc > 0 AND ac > 0
            """
        ).fetchone()
        samples, delta_big, flips, mean_delta = row
        ex_date_samples = duckdb.sql(
            _joined_cte(raw_pattern, adjusted_pattern)
            + f"""
            , f AS (
              SELECT symbol, d, rc, ac,
                     lead(rc, {horizon}) OVER (PARTITION BY symbol ORDER BY d) AS rc_h
              FROM j
            ),
            ex AS (SELECT unnest($symbols) AS symbol, unnest($dates) AS d)
            SELECT count(*)
            FROM f JOIN ex ON f.symbol = ex.symbol AND CAST(f.d AS VARCHAR) = ex.d
            WHERE rc_h IS NOT NULL AND rc > 0 AND ac > 0
            """,
            params={"symbols": [key[0] for key in action_keys], "dates": [key[1] for key in action_keys]},
        ).fetchone()[0] if action_keys else 0
        horizons[str(horizon)] = {
            "samples": int(samples or 0),
            "delta_gt_1pct": int(delta_big or 0),
            "delta_gt_1pct_share": (float(delta_big) / float(samples)) if samples else None,
            "sign_flips": int(flips or 0),
            "sign_flip_share": (float(flips) / float(samples)) if samples else None,
            "mean_abs_delta": float(mean_delta) if mean_delta is not None else None,
            "samples_on_ex_date": int(ex_date_samples or 0),
        }
    return {
        "market": market,
        "method": method,
        "raw_pattern": raw_pattern,
        "actions_loaded": len(actions),
        "horizons": horizons,
    }


def ranking_impact(market: str, method: str, raw_glob: str | None, *, lookback: int = 20) -> dict:
    raw_pattern = _raw_pattern(market, raw_glob)
    adjusted_pattern = _adjusted_pattern(market, method)

    def momentum(pattern: str) -> dict[str, float]:
        rows = duckdb.sql(
            f"""
            WITH s AS (
              SELECT symbol, CAST(date AS DATE) AS d, close,
                     row_number() OVER (PARTITION BY symbol ORDER BY CAST(date AS DATE) DESC) AS rn
              FROM read_parquet('{pattern}') WHERE close > 0
            )
            SELECT a.symbol, a.close / b.close - 1.0 AS mom
            FROM s a JOIN s b ON a.symbol = b.symbol AND a.rn = 1 AND b.rn = {lookback + 1}
            WHERE a.close > 0 AND b.close > 0
            """
        ).fetchall()
        return {str(symbol): float(mom) for symbol, mom in rows if mom is not None}

    raw_mom = momentum(raw_pattern)
    adj_mom = momentum(adjusted_pattern)
    common = sorted(set(raw_mom) & set(adj_mom))
    raw_series = [raw_mom[symbol] for symbol in common]
    adj_series = [adj_mom[symbol] for symbol in common]

    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda index: values[index])
        out = [0.0] * len(values)
        index = 0
        while index < len(order):
            end = index
            while end + 1 < len(order) and values[order[end + 1]] == values[order[index]]:
                end += 1
            average = (index + end) / 2.0 + 1.0
            for position in range(index, end + 1):
                out[order[position]] = average
            index = end + 1
        return out

    def pearson(left: list[float], right: list[float]) -> float | None:
        n = len(left)
        if n < 3:
            return None
        mean_left = sum(left) / n
        mean_right = sum(right) / n
        cov = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right))
        var_left = sum((a - mean_left) ** 2 for a in left)
        var_right = sum((b - mean_right) ** 2 for b in right)
        if var_left <= 0 or var_right <= 0:
            return None
        return cov / (var_left * var_right) ** 0.5

    rank_ic = pearson(ranks(raw_series), ranks(adj_series))
    overlaps: dict[str, dict] = {}
    raw_desc = sorted(common, key=lambda symbol: raw_mom[symbol], reverse=True)
    adj_desc = sorted(common, key=lambda symbol: adj_mom[symbol], reverse=True)
    for top_n in TOP_NS:
        raw_top = set(raw_desc[:top_n])
        adj_top = set(adj_desc[:top_n])
        overlaps[str(top_n)] = {
            "overlap": len(raw_top & adj_top),
            "overlap_share": len(raw_top & adj_top) / top_n,
        }
    def boards_for(symbols_list: list[str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        if market != "CN":
            return counts
        for symbol in symbols_list:
            code = symbol.split(".", 1)[0]
            if symbol.upper().endswith(".BJ"):
                board = "BJ"
            elif code.startswith(("688", "689")):
                board = "STAR"
            elif code.startswith(("300", "301", "302")):
                board = "ChiNext"
            else:
                board = "Main"
            counts[board] = counts.get(board, 0) + 1
        return counts

    return {
        "market": market,
        "method": method,
        "lookback": lookback,
        "symbols": len(common),
        "rank_ic": rank_ic,
        "top_overlaps": overlaps,
        "raw_top100_boards": boards_for(raw_desc[:100]),
        "adjusted_top100_boards": boards_for(adj_desc[:100]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", choices=["CN", "US"], required=True)
    parser.add_argument("--method", default="qfq")
    parser.add_argument("--raw-glob", default=None)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    payload = {
        "schema_version": "label_rank_impact_v1",
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "market": args.market,
        "method": args.method,
        "label_impact": label_impact(args.market, args.method, args.raw_glob),
        "ranking_impact": ranking_impact(args.market, args.method, args.raw_glob),
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    csv_path = output.with_suffix(".csv")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["horizon", "samples", "delta_gt_1pct", "delta_gt_1pct_share", "sign_flips", "sign_flip_share", "mean_abs_delta", "samples_on_ex_date"])
        for horizon, item in payload["label_impact"]["horizons"].items():
            writer.writerow([
                horizon, item["samples"], item["delta_gt_1pct"], item["delta_gt_1pct_share"],
                item["sign_flips"], item["sign_flip_share"], item["mean_abs_delta"], item["samples_on_ex_date"],
            ])
    print(json.dumps({
        "market": args.market,
        "horizons": {
            horizon: {
                "samples": item["samples"],
                "delta_gt_1pct_share": item["delta_gt_1pct_share"],
                "sign_flips": item["sign_flips"],
            }
            for horizon, item in payload["label_impact"]["horizons"].items()
        },
        "rank_ic": payload["ranking_impact"]["rank_ic"],
        "top_overlaps": payload["ranking_impact"]["top_overlaps"],
        "boards": payload["ranking_impact"]["adjusted_top100_boards"],
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
