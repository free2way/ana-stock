#!/usr/bin/env python3
"""Bounded read-only CN lake audit; no DB, network, training or assumed fills."""
import argparse
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import duckdb
from app.services.market_lake import market_lake_root
from app.services.market_calendar import is_market_open_date
from app.services.market_freshness import latest_completed_market_date
from app.services.stock_selection.cn_execution_coverage import assess_cn_execution_coverage
from app.services.stock_selection.cn_execution_facts import load_facts_bundle, attach_matching_facts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--sessions", type=int, default=180)
    parser.add_argument("--ticker-limit", type=int, default=20)
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--facts-bundle", type=Path, help="Optional immutable BaoStock facts; enrich only matching OHLCV")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 10 <= args.sessions <= 520 or not 1 <= args.ticker_limit <= 100:
        parser.error("bounded audit requires 10..520 sessions and 1..100 tickers")
    if args.output.exists():
        parser.error("output exists; choose a new receipt path")
    end = date.fromisoformat(args.as_of)
    if args.as_of > latest_completed_market_date("CN") or not is_market_open_date("CN", args.as_of):
        parser.error("as-of must be a completed CN market session")
    days, cursor = [], end
    while len(days) < args.sessions:
        if is_market_open_date("CN", cursor.isoformat()):
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()
    paths = [market_lake_root() / "cn_daily" / f"date={day}" / "part.parquet" for day in days]
    existing = [p for p in paths if p.is_file()]
    if not paths[-1].is_file():
        parser.error("as-of partition missing; no silent fallback to an older snapshot")
    manifest = [{"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                 "size": p.stat().st_size, "mtime_ns": p.stat().st_mtime_ns} for p in existing]
    with duckdb.connect(config={"threads": 2, "memory_limit": "256MB"}) as db:
        # Deterministic current-survivor engineering sample, not a PIT stock universe.
        selected = [row[0] for row in db.execute("""
            SELECT DISTINCT symbol FROM read_parquet(?)
            WHERE (symbol LIKE '%.SS' AND substr(symbol,1,3) IN ('600','601','603','605','688'))
               OR (symbol LIKE '%.SZ' AND substr(symbol,1,3) IN ('000','001','002','003','300','301'))
            ORDER BY symbol LIMIT ?
        """, [str(paths[-1]), args.ticker_limit]).fetchall()]
        query = db.execute("""SELECT * FROM read_parquet(?, union_by_name=true, hive_partitioning=false)
                              WHERE symbol = ANY(?) ORDER BY symbol, date""", [[str(p) for p in existing], selected])
        columns = [col[0] for col in query.description]
        rows = [dict(zip(columns, row)) for row in query.fetchall()]
    for row in rows:
        row["date"] = str(row["date"])[:10]
    original_columns_counts = {col: sum(row.get(col) is not None for row in rows) for col in columns}
    facts_audit = None
    if args.facts_bundle:
        facts, facts_hash = load_facts_bundle(args.facts_bundle)
        rows, facts_audit = attach_matching_facts(rows, facts)
        facts_audit.update({"bundle_path": str(args.facts_bundle), "bundle_sha256": facts_hash})
    manifest_hash = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    source_reference = f"local_cn_lake_manifest:{manifest_hash}"
    if facts_audit:
        source_reference += f";execution_facts:{facts_audit['bundle_sha256']}"
    report = assess_cn_execution_coverage(rows, trading_dates=days, tickers=selected,
        horizon_days=args.horizon, source_reference=source_reference)
    report.update({"selection_mode": "current_survivor_lexicographic_engineering_sample_not_pit",
                   "selected_tickers": selected, "source_files": manifest,
                   "source_manifest_sha256": manifest_hash,
                   "execution_facts_join": facts_audit,
                   "missing_partitions": [str(p) for p in paths if not p.is_file()],
                   "column_non_null_counts": original_columns_counts})
    if any(Path(item["path"]).stat().st_mtime_ns != item["mtime_ns"] or
           hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest() != item["sha256"] for item in manifest):
        raise RuntimeError("source changed during audit; refusing evidence")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, sort_keys=True, indent=2, default=str)
        output.write("\n")
    print(json.dumps({key: value for key, value in report.items()
                      if key not in {"decisions", "training_evidence", "source_files"}}, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
