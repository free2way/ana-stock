from __future__ import annotations

from app.core.db import SessionLocal
from app.services.market_lake import market_lake_root
from app.services.repository import SymbolRepository
from app.services.us_trade_universe import is_known_us_non_common_security

import polars as pl


def _known_non_common_symbols() -> tuple[set[str], dict[str, int]]:
    with SessionLocal() as db:
        symbols = [
            row
            for row in SymbolRepository(db).list_symbols()
            if str(row.market or "").upper() == "US"
        ]
    drop: set[str] = set()
    reasons: dict[str, int] = {}
    for symbol in symbols:
        metadata_text = " ".join(
            str(value or "")
            for value in (symbol.name, symbol.exchange, symbol.sector, symbol.industry)
        )
        should_drop, reason = is_known_us_non_common_security(
            symbol.ticker,
            symbol.name,
            metadata_text=metadata_text,
        )
        if not should_drop:
            continue
        drop.add(str(symbol.ticker or "").upper())
        reason_key = reason or "known_non_common_security"
        reasons[reason_key] = reasons.get(reason_key, 0) + 1
    return drop, dict(sorted(reasons.items(), key=lambda item: (-item[1], item[0])))


def main() -> None:
    drop_symbols, reasons = _known_non_common_symbols()
    root = market_lake_root() / "us_daily"
    files = sorted(root.glob("date=*/part.parquet"))
    rows_before = 0
    rows_after = 0
    files_touched = 0
    rows_removed = 0
    for path in files:
        frame = pl.read_parquet(path)
        before = frame.height
        rows_before += before
        if before <= 0 or not drop_symbols:
            rows_after += before
            continue
        filtered = frame.filter(~pl.col("symbol").cast(pl.Utf8).str.to_uppercase().is_in(drop_symbols))
        after = filtered.height
        rows_after += after
        if after != before:
            filtered.write_parquet(path, compression="zstd")
            files_touched += 1
            rows_removed += before - after
    print(
        {
            "known_non_common_symbols": len(drop_symbols),
            "reason_counts": reasons,
            "files_scanned": len(files),
            "files_touched": files_touched,
            "rows_before": rows_before,
            "rows_after": rows_after,
            "rows_removed": rows_removed,
        }
    )


if __name__ == "__main__":
    main()
