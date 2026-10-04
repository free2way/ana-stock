"""AKShare (EastMoney) dividend / bonus-share rows for A-share actions.

Bulk per fiscal period, no token and no hourly bucket. Columns are per 10
shares and pre-tax; they are converted to the per-share convention used by the
action store. Values without an ex-date or not marked implemented are kept out
of the factor derivation.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

SUPPORTED_PROGRESS = {"实施分配", "实施"}


def _clean(value) -> str | None:
    text = str(value).strip() if value is not None else ""
    if not text or text.lower() in {"nan", "none", "--"}:
        return None
    return text


def _to_float(value) -> float | None:
    text = _clean(value)
    if text is None:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def app_ticker_from_code(code: str) -> str:
    digits = str(code or "").strip()
    if len(digits) != 6 or not digits.isdigit():
        return ""
    if digits.startswith(("6", "9")):
        return f"{digits}.SS"
    if digits.startswith(("0", "2", "3")):
        return f"{digits}.SZ"
    if digits.startswith(("4", "8")):
        return f"{digits}.BJ"
    return ""


def normalize_fhps_rows(frame_rows: list[dict], *, period: str) -> list[dict]:
    """Convert EastMoney fhps rows into the dividend-row store schema."""

    ingested_at = datetime.now(tz=timezone.utc).isoformat()
    rows: list[dict] = []
    for row in frame_rows:
        code = _clean(row.get("代码"))
        symbol = app_ticker_from_code(code or "")
        ex_date = _clean(row.get("除权除息日"))
        progress = _clean(row.get("方案进度"))
        if not symbol or not ex_date or progress not in SUPPORTED_PROGRESS:
            continue
        stock_total_per_10 = _to_float(row.get("送转股份-送转总比例")) or 0.0
        cash_per_10 = _to_float(row.get("现金分红-现金分红比例")) or 0.0
        if stock_total_per_10 <= 0 and cash_per_10 <= 0:
            continue
        rows.append(
            {
                "symbol": symbol,
                "end_date": str(period)[:8],
                "ex_date": ex_date[:10],
                "cash_div_tax": round(cash_per_10 / 10.0, 8),
                "stk_div": round(stock_total_per_10 / 10.0, 8),
                "record_date": (_clean(row.get("股权登记日")) or "")[:10] or None,
                "ann_date": (_clean(row.get("预案公告日")) or "")[:10] or None,
                "source": "akshare_fhps",
                "source_reference": f"akshare:fhps:{str(period)[:8]}:{code}",
                "ingested_at": ingested_at,
            }
        )
    return rows


def fetch_fhps_rows(period: str) -> list[dict]:
    """Network call; import akshare lazily so unit tests stay offline."""

    import akshare as ak  # type: ignore

    frame = ak.stock_fhps_em(date=str(period)[:8])
    if frame is None or frame.empty:
        return []
    return normalize_fhps_rows(frame.to_dict("records"), period=str(period)[:8])
