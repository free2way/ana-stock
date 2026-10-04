"""Backfill missing CN lake partitions (whole-market holes) from TuShare.

Holes are whole-day coverage drops (e.g. 2026-02-26 / 2026-05-22 lost the
Shanghai symbols). The backfill uses ``pro.daily(trade_date=...)`` - one call
per hole day, same source as the original lake writes - and writes through the
canonical lake writer so provenance/shadow dual-write and validation apply.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.services.market_lake import daily_parquet_glob, write_daily_ohlcv_parquet  # noqa: E402

SUFFIX_MAP = {"SH": "SS", "SZ": "SZ", "BJ": "BJ"}


def lake_coverage(*, start: str) -> list[tuple[str, int]]:
    return [
        (str(day), int(count))
        for day, count in duckdb.sql(
            "SELECT CAST(date AS VARCHAR) AS d, count(*) FROM read_parquet(?, hive_partitioning=true) "
            "WHERE CAST(date AS DATE) >= CAST(? AS DATE) GROUP BY date ORDER BY date",
            params=[daily_parquet_glob("CN"), start],
        ).fetchall()
    ]


def hole_days(*, start: str, ratio: float) -> tuple[list[str], dict[str, int]]:
    coverage = lake_coverage(start=start)
    counts = sorted(count for _, count in coverage)
    median = counts[len(counts) // 2]
    holes = [day for day, count in coverage if count < median * ratio]
    return holes, {"median_rows": median, "days_checked": len(coverage)}


def missing_symbols(trade_date: str) -> list[str]:
    rows = duckdb.sql(
        """
        WITH symbols AS (
          SELECT DISTINCT symbol FROM read_parquet(?, hive_partitioning=true)
          WHERE CAST(date AS DATE) BETWEEN CAST(? AS DATE) - 10 AND CAST(? AS DATE) + 10
        ),
        present AS (
          SELECT symbol FROM read_parquet(?, hive_partitioning=true) WHERE CAST(date AS VARCHAR) = ?
        )
        SELECT symbol FROM symbols WHERE symbol NOT IN (SELECT symbol FROM present) ORDER BY symbol
        """,
        params=[daily_parquet_glob("CN"), trade_date, trade_date, daily_parquet_glob("CN"), trade_date],
    ).fetchall()
    return [str(row[0]) for row in rows]


def fetch_market_day(trade_date: str) -> dict[str, dict]:
    import tushare as ts  # type: ignore

    settings = get_settings()
    pro = ts.pro_api(settings.tushare_token)
    frame = pro.daily(trade_date=trade_date.replace("-", ""))
    rows: dict[str, dict] = {}
    if frame is None:
        return rows
    for record in frame.to_dict("records"):
        ts_code = str(record.get("ts_code") or "").upper()
        if "." not in ts_code:
            continue
        code, suffix = ts_code.split(".", 1)
        market_suffix = SUFFIX_MAP.get(suffix)
        if not market_suffix:
            continue
        try:
            rows[f"{code}.{market_suffix}"] = {
                "symbol": f"{code}.{market_suffix}",
                "open": float(record["open"]),
                "high": float(record["high"]),
                "low": float(record["low"]),
                "close": float(record["close"]),
                "volume": float(record["vol"]) * 100.0,
            }
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2025-03-01")
    parser.add_argument("--ratio", type=float, default=0.9)
    parser.add_argument("--dates", default=None, help="comma-separated hole days; auto-detect when omitted")
    parser.add_argument("--sleep", type=float, default=1.0)
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    if args.dates:
        holes = [item.strip() for item in args.dates.split(",") if item.strip()]
        detection = {"source": "explicit"}
    else:
        holes, detection = hole_days(start=args.start, ratio=args.ratio)
        detection["source"] = "auto"
    report: dict = {
        "schema_version": "cn_lake_gap_backfill_v1",
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "detection": detection,
        "hole_days": holes,
        "per_day": {},
    }
    for trade_date in holes:
        missing = missing_symbols(trade_date)
        market_rows = fetch_market_day(trade_date)
        rows = [market_rows[symbol] for symbol in missing if symbol in market_rows]
        report["per_day"][trade_date] = {
            "missing_symbols": len(missing),
            "fetched_symbols": len(rows),
            "unavailable": [symbol for symbol in missing if symbol not in market_rows][:20],
        }
        if rows:
            path = write_daily_ohlcv_parquet(
                market="CN",
                trade_date=trade_date,
                rows=[
                    {**row, "provider": "tushare", "source_reference": f"tushare:daily:{trade_date}"}
                    for row in rows
                ],
                merge_existing=True,
            )
            report["per_day"][trade_date]["written"] = len(rows)
            report["per_day"][trade_date]["path"] = str(path)
    if args.report:
        output = Path(args.report)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
