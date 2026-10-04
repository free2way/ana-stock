"""Derive corporate-action factor events from TuShare ``adj_factor`` series.

TuShare's cumulative adjustment factor changes on every ex-date (splits,
stock dividends and cash dividends alike). A factor jump on day D means the
raw close on D is ``previous_close / ratio`` for ratio = factor[D]/factor[D-1],
which is exactly the event model the adjusted view consumes. The derived
records keep the factor and never claim to know the economic type.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
import json
import math

import polars as pl

from app.services.corporate_actions import CorporateActionRecord

FACTOR_EPSILON = 1e-6

FACTOR_SERIES_SCHEMA: dict[str, pl.DataType] = {
    "market": pl.String,
    "symbol": pl.String,
    "trade_date": pl.String,
    "adj_factor": pl.Float64,
    "source": pl.String,
    "ingested_at": pl.String,
}


def _parse_date(value: object) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValueError(f"invalid factor trade_date `{value}`") from exc


def normalize_factor_rows(rows: list[dict], *, market: str = "CN", source: str = "tushare_adj_factor") -> list[dict]:
    market_code = str(market or "").strip().upper()
    normalized: dict[tuple[str, str], dict] = {}
    ingested_at = datetime.now(tz=timezone.utc).isoformat()
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        trade_date = str(row.get("trade_date") or "")[:10]
        try:
            factor = float(row.get("adj_factor"))
        except (TypeError, ValueError):
            continue
        if not symbol or not trade_date or not math.isfinite(factor) or factor <= 0:
            continue
        try:
            _parse_date(trade_date)
        except ValueError:
            continue
        normalized[(symbol, trade_date)] = {
            "market": market_code,
            "symbol": symbol,
            "trade_date": trade_date,
            "adj_factor": factor,
            "source": str(row.get("source") or source),
            "ingested_at": str(row.get("ingested_at") or ingested_at),
        }
    return sorted(normalized.values(), key=lambda item: (item["symbol"], item["trade_date"]))


def derive_factor_actions(
    rows: list[dict],
    *,
    market: str = "CN",
    source: str = "tushare_adj_factor",
) -> list[CorporateActionRecord]:
    market_code = str(market or "").strip().upper()
    series: dict[str, list[dict]] = defaultdict(list)
    for row in normalize_factor_rows(rows, market=market_code, source=source):
        series[row["symbol"]].append(row)
    records: list[CorporateActionRecord] = []
    for symbol, items in series.items():
        items.sort(key=lambda item: item["trade_date"])
        for previous, current in zip(items, items[1:]):
            ratio = float(current["adj_factor"]) / float(previous["adj_factor"])
            if abs(ratio - 1.0) <= FACTOR_EPSILON:
                continue
            records.append(
                CorporateActionRecord(
                    market=market_code,
                    symbol=symbol,
                    action_type="adjustment_factor",
                    effective_date=_parse_date(current["trade_date"]),
                    factor=ratio,
                    currency="CNY" if market_code == "CN" else "",
                    source=source,
                    source_reference=f"tushare:adj_factor:{current['trade_date']}",
                )
            )
    return sorted(records, key=lambda item: (item.effective_date, item.symbol))


def derive_dividend_actions(
    rows: list[dict],
    *,
    market: str = "CN",
    source: str = "tushare_dividend",
) -> list[CorporateActionRecord]:
    """Turn implemented dividend plans into factor / cash action records."""

    market_code = str(market or "").strip().upper()
    records: list[CorporateActionRecord] = []
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        ex_date = str(row.get("ex_date") or "")[:10]
        if not symbol or not ex_date:
            continue
        try:
            effective = _parse_date(ex_date)
        except ValueError:
            continue
        reference = str(
            row.get("source_reference")
            or f"tushare:dividend:{symbol}:{str(row.get('end_date') or '')[:8]}"
        )
        stock_ratio = row.get("stk_div")
        try:
            stock_ratio = float(stock_ratio) if stock_ratio not in (None, "") else 0.0
        except (TypeError, ValueError):
            stock_ratio = 0.0
        if stock_ratio > 0:
            records.append(
                CorporateActionRecord(
                    market=market_code,
                    symbol=symbol,
                    action_type="stock_dividend",
                    effective_date=effective,
                    factor=1.0 + stock_ratio,
                    announced_date=(
                        _parse_date(str(row.get("ann_date"))[:10]) if row.get("ann_date") else None
                    ),
                    currency="CNY" if market_code == "CN" else "",
                    source=source,
                    source_reference=reference,
                )
            )
        cash = row.get("cash_div_tax")
        try:
            cash = float(cash) if cash not in (None, "") else 0.0
        except (TypeError, ValueError):
            cash = 0.0
        if cash > 0:
            records.append(
                CorporateActionRecord(
                    market=market_code,
                    symbol=symbol,
                    action_type="cash_dividend",
                    effective_date=effective,
                    cash_amount=cash,
                    announced_date=(
                        _parse_date(str(row.get("ann_date"))[:10]) if row.get("ann_date") else None
                    ),
                    currency="CNY" if market_code == "CN" else "",
                    source=source,
                    source_reference=reference,
                )
            )
    return sorted(records, key=lambda item: (item.effective_date, item.symbol, item.action_type))


DIVIDEND_ROWS_SCHEMA: dict[str, pl.DataType] = {
    "market": pl.String,
    "symbol": pl.String,
    "end_date": pl.String,
    "ex_date": pl.String,
    "cash_div_tax": pl.Float64,
    "stk_div": pl.Float64,
    "record_date": pl.String,
    "ann_date": pl.String,
    "source": pl.String,
    "source_reference": pl.String,
    "ingested_at": pl.String,
}


def dividend_rows_path(*, root: Path | None = None) -> Path:
    if root is None:
        from app.core.config import get_settings

        root = get_settings().data_dir / "corporate_actions"
    return Path(root) / "cn_dividend_rows.parquet"


def write_dividend_rows(rows: list[dict], *, root: Path | None = None, market: str = "CN") -> Path:
    market_code = str(market or "").strip().upper()
    ingested_at = datetime.now(tz=timezone.utc).isoformat()
    normalized = []
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        ex_date = str(row.get("ex_date") or "")[:10]
        if not symbol or not ex_date:
            continue
        normalized.append(
            {
                "market": market_code,
                "symbol": symbol,
                "end_date": str(row.get("end_date") or "")[:8],
                "ex_date": ex_date,
                "cash_div_tax": float(row.get("cash_div_tax") or 0.0),
                "stk_div": float(row.get("stk_div") or 0.0),
                "record_date": str(row.get("record_date") or "")[:10] or None,
                "ann_date": str(row.get("ann_date") or "")[:10] or None,
                "source": str(row.get("source") or "tushare_dividend"),
                "source_reference": str(
                    row.get("source_reference")
                    or f"tushare:dividend:{symbol}:{str(row.get('end_date') or '')[:8]}"
                ),
                "ingested_at": str(row.get("ingested_at") or ingested_at),
            }
        )
    path = dividend_rows_path(root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    incoming = pl.DataFrame(normalized, schema=DIVIDEND_ROWS_SCHEMA, orient="row")
    if path.exists():
        existing = pl.read_parquet(path).cast(DIVIDEND_ROWS_SCHEMA)
        incoming = pl.concat([existing, incoming], how="vertical_relaxed").cast(DIVIDEND_ROWS_SCHEMA)
    incoming = (
        incoming.sort(["symbol", "ex_date", "ingested_at"])
        .unique(subset=["symbol", "ex_date", "source_reference"], keep="last")
        .sort(["ex_date", "symbol"])
    )
    temporary = path.with_name(f".{path.name}.tmp")
    incoming.write_parquet(temporary, compression="zstd")
    temporary.replace(path)
    return path


def load_dividend_rows(*, root: Path | None = None) -> list[dict]:
    path = dividend_rows_path(root=root)
    if not path.exists():
        return []
    return pl.read_parquet(path).cast(DIVIDEND_ROWS_SCHEMA).to_dicts()


def factor_series_path(*, root: Path | None = None) -> Path:
    if root is None:
        from app.core.config import get_settings

        root = get_settings().data_dir / "corporate_actions"
    return Path(root) / "cn_adj_factors.parquet"


def write_factor_series(rows: list[dict], *, root: Path | None = None, market: str = "CN") -> Path:
    normalized = normalize_factor_rows(rows, market=market)
    path = factor_series_path(root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    incoming = pl.DataFrame(normalized, schema=FACTOR_SERIES_SCHEMA, orient="row")
    if path.exists():
        existing = pl.read_parquet(path).cast(FACTOR_SERIES_SCHEMA)
        incoming = pl.concat([existing, incoming], how="vertical_relaxed").cast(FACTOR_SERIES_SCHEMA)
    incoming = (
        incoming.sort(["symbol", "trade_date", "ingested_at"])
        .unique(subset=["symbol", "trade_date"], keep="last")
        .sort(["symbol", "trade_date"])
    )
    temporary = path.with_name(f".{path.name}.tmp")
    incoming.write_parquet(temporary, compression="zstd")
    temporary.replace(path)
    return path


def load_factor_rows(*, root: Path | None = None) -> list[dict]:
    path = factor_series_path(root=root)
    if not path.exists():
        return []
    return pl.read_parquet(path).cast(FACTOR_SERIES_SCHEMA).to_dicts()


def summarize_factor_rows(rows: list[dict]) -> dict:
    normalized = normalize_factor_rows(rows)
    symbols = {row["symbol"] for row in normalized}
    dates = sorted({row["trade_date"] for row in normalized})
    return {
        "rows": len(normalized),
        "symbols": len(symbols),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "payload_sha256": __import__("hashlib").sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16],
    }
