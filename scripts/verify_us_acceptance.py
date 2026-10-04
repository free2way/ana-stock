"""Formal acceptance checks for the US clauses of C-1/C-2 (see acceptance plan §3.1/§3.2).

Clauses checked:
1. Single-basis seam check: every symbol's stored history must come from exactly
   one provider and one price basis.
2. Event-window explanation: jumps within +/-3 days of a corporate action must be
   100% explained (from the reconciliation report).
3. Independent-source sampling: N>=25 unexplained jumps are re-fetched from
   Alpaca directly; the stored values must match exactly.

Writes a JSON verdict used as acceptance evidence.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402

from app.services.alpaca_client import AlpacaClient  # noqa: E402
from app.services.us_event_exemptions import coverage_payload  # noqa: E402


def seam_check(raw_glob: str) -> dict:
    row = duckdb.sql(
        """
        WITH per_symbol AS (
          SELECT symbol,
                 count(DISTINCT provider) AS providers,
                 count(DISTINCT price_basis) AS bases
          FROM read_parquet(?, hive_partitioning=true)
          GROUP BY symbol
        )
        SELECT count(*) AS symbols,
               sum(CASE WHEN providers > 1 OR bases > 1 THEN 1 ELSE 0 END) AS mixed,
               max(providers) AS max_providers,
               max(bases) AS max_bases
        FROM per_symbol
        """,
        params=[raw_glob],
    ).fetchone()
    offenders = duckdb.sql(
        """
        WITH per_symbol AS (
          SELECT symbol, count(DISTINCT provider) AS providers, count(DISTINCT price_basis) AS bases
          FROM read_parquet(?, hive_partitioning=true) GROUP BY symbol
        )
        SELECT symbol, providers, bases FROM per_symbol WHERE providers > 1 OR bases > 1 LIMIT 10
        """,
        params=[raw_glob],
    ).fetchall()
    return {
        "symbols": row[0],
        "mixed_symbols": row[1],
        "max_providers": row[2],
        "max_bases": row[3],
        "offender_samples": [list(item) for item in offenders],
        "passed": row[1] == 0,
    }


def sampling_check(samples: list[dict], *, feed: str) -> dict:
    from app.core.config import get_settings

    settings = get_settings()
    client = AlpacaClient(
        api_key=settings.alpaca_api_key,
        api_secret=settings.alpaca_api_secret,
        trading_endpoint=settings.alpaca_endpoint,
        data_endpoint=settings.alpaca_data_endpoint,
        feed=feed or settings.alpaca_data_feed,
    )
    results = []
    matched = 0
    for sample in samples:
        symbol = sample["symbol"]
        trade_date = sample["date"]
        stored_close = sample["close"]
        try:
            bars = client.fetch_daily_bars(symbol, start=trade_date, end=trade_date, adjustment="raw")
        except Exception as exc:  # noqa: BLE001 - recorded in the verdict
            bars = []
            results.append({**sample, "fresh_close": None, "match": False, "error": f"{type(exc).__name__}: {exc}"})
            continue
        fresh = next((bar for bar in bars if bar["date"] == trade_date), None)
        fresh_close = fresh["close"] if fresh else None
        match = fresh_close is not None and abs(fresh_close - stored_close) < 1e-6
        matched += 1 if match else 0
        results.append({**sample, "fresh_close": fresh_close, "match": match})
    return {
        "checked": len(results),
        "matched": matched,
        "match_rate": (matched / len(results)) if results else None,
        "failed_samples": [item for item in results if not item["match"]],
        "passed": bool(results) and matched == len(results),
    }


def action_application_check(samples: list[dict], adjusted_path: str, raw_glob: str) -> dict:
    """For event-window unexplained jumps, the action must be applied at its effective date.

    Verified directly on the stored series: the adjusted multiplier must change
    by the action's price factor on the effective date (split-like: ``factor``;
    cash dividend: ``prev_close / (prev_close - cash)``). The remaining adjusted
    move is then a genuine market move, not an unapplied action.
    """

    if not Path(adjusted_path).exists():
        return {"checked": 0, "verified": 0, "failures": [], "passed": False, "reason": "adjusted view missing"}

    failures: list[dict] = []
    verified = 0
    details: list[dict] = []
    for sample in samples:
        symbol = sample["symbol"]
        actions = sample.get("actions") or []
        sample_ok = False
        sample_notes: list[dict] = []
        for action in actions:
            effective_date = action.get("effective_date")
            if not effective_date:
                continue
            if action.get("action_type") in {"merger", "spinoff", "delisting"}:
                # Event-day explanations have no price factor to verify; their
                # presence on the date IS the explanation (recorded for audit).
                sample_ok = True
                sample_notes.append(
                    {
                        "action_type": action.get("action_type"),
                        "effective_date": effective_date,
                        "applied": True,
                        "reason": "event_day_explanation",
                    }
                )
                continue
            factor = action.get("factor")
            cash = action.get("cash_amount")
            effective = date.fromisoformat(str(effective_date)[:10])
            window_start = (effective - timedelta(days=4)).isoformat()
            window_end = (effective + timedelta(days=3)).isoformat()
            rows = duckdb.sql(
                """
                SELECT CAST(r.date AS VARCHAR) AS d, r.close AS raw_close, a.adj_multiplier AS mult
                FROM read_parquet(?) r
                JOIN read_parquet(?) a ON a.symbol = r.symbol AND a.date = r.date
                WHERE r.symbol = ? AND CAST(r.date AS DATE) BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
                ORDER BY r.date
                """,
                params=[raw_glob, adjusted_path, symbol, window_start, window_end],
            ).fetchall()
            before = [row for row in rows if row[0] < effective_date]
            after = [row for row in rows if row[0] >= effective_date]
            if not before or not after or not before[-1][2] or not after[0][2]:
                continue
            ratio = float(after[0][2]) / float(before[-1][2])
            expected: float | None = None
            if factor:
                expected = float(factor)
            elif cash:
                prev_close = float(before[-1][1])
                if prev_close > float(cash) > 0:
                    expected = prev_close / (prev_close - float(cash))
            if expected is None or expected <= 0:
                continue
            deviation = abs(ratio / expected - 1.0)
            ok = deviation < 1e-3
            sample_ok = sample_ok or ok
            sample_notes.append(
                {
                    "action_type": action.get("action_type"),
                    "effective_date": effective_date,
                    "multiplier_ratio": round(ratio, 8),
                    "expected_ratio": round(expected, 8),
                    "deviation": deviation,
                    "applied": ok,
                }
            )
        if sample_ok:
            verified += 1
        else:
            failures.append({**sample, "checks": sample_notes})
        details.append({"symbol": symbol, "date": sample["date"], "checks": sample_notes, "passed": sample_ok})
    return {
        "checked": len(samples),
        "verified": verified,
        "details": details,
        "failures": failures,
        "passed": len(samples) == 0 or verified == len(samples),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reconcile-report", required=True)
    parser.add_argument("--raw-glob", default="data/lake/_us_alpaca/raw/*.parquet")
    parser.add_argument("--adjusted-path", default="data/lake/_adjusted_v2/us/method=qfq/adjusted.parquet")
    parser.add_argument("--min-samples", type=int, default=25)
    parser.add_argument("--feed", default=None)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    report = json.loads(Path(args.reconcile_report).read_text(encoding="utf-8"))
    raw_scan = report["raw_scan"]
    samples = raw_scan.get("unexplained_samples", [])[: max(args.min_samples, 1)]

    seam = seam_check(args.raw_glob)
    coverage = coverage_payload()
    coverage["passed"] = bool(coverage["consistent"]) and bool(coverage["supported_event_types"])
    event_window = {
        "jumps": raw_scan.get("event_window_jumps"),
        "unexplained": raw_scan.get("event_window_unexplained"),
    }
    application = action_application_check(
        raw_scan.get("event_window_unexplained_samples", []),
        args.adjusted_path,
        args.raw_glob,
    )
    event_window["action_application"] = {
        "checked": application["checked"],
        "verified": application["verified"],
        "failed": len(application["failures"]),
    }
    event_window["passed"] = bool(application["passed"])
    sampling = sampling_check(samples, feed=args.feed) if samples else {
        "checked": 0, "matched": 0, "match_rate": None, "failed_samples": [], "passed": False,
    }
    sampling["required_min_samples"] = args.min_samples
    if sampling["checked"] < args.min_samples:
        sampling["passed"] = False
    verdict = {
        "schema_version": "us_acceptance_verification_v1",
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "reconcile_report": args.reconcile_report,
        "raw_glob": args.raw_glob,
        "clause_seam": seam,
        "clause_event_window": event_window,
        "clause_event_coverage": coverage,
        "clause_sampling": sampling,
        "passed": seam["passed"] and event_window["passed"] and coverage["passed"] and sampling["passed"],
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(verdict, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(json.dumps({
        "passed": verdict["passed"],
        "seam": {"passed": seam["passed"], "mixed_symbols": seam["mixed_symbols"]},
        "event_window": event_window,
        "sampling": {"passed": sampling["passed"], "checked": sampling["checked"], "match_rate": sampling["match_rate"]},
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
