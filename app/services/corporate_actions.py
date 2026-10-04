"""Corporate-action records, storage and jump-reconciliation helpers.

Offline half of B-7: provider fetchers (TuShare adj_factor / splits/dividends,
Polygon splits/dividends) and the adjusted-view rebuild (C1) consume this
store. Records are validated, de-duplicated by natural key and kept in a
per-market Parquet file so every later step can cite a revision.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
import hashlib
import json

import polars as pl

SUPPORTED_ACTION_TYPES = {
    "split",
    "cash_dividend",
    "stock_dividend",
    "rights",
    "delisting",
    # TuShare adj_factor jumps combine splits and dividends; the derived
    # record keeps only the exact factor and never pretends to know the type.
    "adjustment_factor",
    # Alpaca corporate actions beyond splits/dividends. A merger completion
    # ends trading (cash and/or share consideration); a spinoff re-prices the
    # parent on the ex-date. Both are event-day explanations, not price factors.
    "merger",
    "spinoff",
}

ACTIONS_SCHEMA: dict[str, pl.DataType] = {
    "market": pl.String,
    "symbol": pl.String,
    "action_type": pl.String,
    "effective_date": pl.String,
    "announced_date": pl.String,
    "factor": pl.Float64,
    "cash_amount": pl.Float64,
    "currency": pl.String,
    "source": pl.String,
    "source_reference": pl.String,
    "revision_id": pl.String,
    "ingested_at": pl.String,
}

def _parse_iso_date(value: object, *, field: str) -> date:
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date, got `{value}`") from exc


@dataclass(frozen=True, slots=True)
class CorporateActionRecord:
    market: str
    symbol: str
    action_type: str
    effective_date: date
    factor: float | None = None
    cash_amount: float | None = None
    currency: str = ""
    announced_date: date | None = None
    source: str = ""
    source_reference: str = ""
    revision_id: str = ""
    ingested_at: str = ""

    def __post_init__(self) -> None:
        market = str(self.market or "").strip().upper()
        symbol = str(self.symbol or "").strip().upper()
        action_type = str(self.action_type or "").strip().lower()
        if not market or not symbol:
            raise ValueError("corporate action requires market and symbol")
        if action_type not in SUPPORTED_ACTION_TYPES:
            raise ValueError(f"unsupported action_type `{self.action_type}`")
        if self.factor is not None and (not isinstance(self.factor, float) or self.factor <= 0):
            raise ValueError("factor must be a positive float when provided")
        if self.cash_amount is not None and (
            not isinstance(self.cash_amount, float) or self.cash_amount < 0
        ):
            raise ValueError("cash_amount must be a non-negative float when provided")
        if action_type in {"split", "adjustment_factor"} and self.factor is None:
            raise ValueError(f"{action_type} requires factor")
        if action_type == "cash_dividend" and self.cash_amount is None:
            raise ValueError("cash_dividend requires cash_amount")
        if self.announced_date is not None and self.announced_date > self.effective_date:
            raise ValueError("announced_date cannot follow effective_date")
        object.__setattr__(self, "market", market)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "action_type", action_type)
        if not self.revision_id:
            object.__setattr__(self, "revision_id", self.compute_revision_id())
        if not self.ingested_at:
            object.__setattr__(self, "ingested_at", datetime.now(tz=timezone.utc).isoformat())

    @property
    def natural_key(self) -> tuple[str, str, str, str]:
        return (self.market, self.symbol, self.action_type, self.effective_date.isoformat())

    def compute_revision_id(self) -> str:
        """Content hash of the full revision (audit finding #4).

        announced_date / currency / source_reference are part of the identity:
        a corrected announcement date or a re-sourced value is a NEW revision.
        """

        payload = json.dumps(
            {
                "market": self.market,
                "symbol": self.symbol,
                "action_type": self.action_type,
                "effective_date": self.effective_date.isoformat(),
                "announced_date": self.announced_date.isoformat() if self.announced_date else None,
                "factor": self.factor,
                "cash_amount": self.cash_amount,
                "currency": self.currency,
                "source": self.source,
                "source_reference": self.source_reference,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_row(self) -> dict:
        return {
            "market": self.market,
            "symbol": self.symbol,
            "action_type": self.action_type,
            "effective_date": self.effective_date.isoformat(),
            "announced_date": self.announced_date.isoformat() if self.announced_date else None,
            "factor": float(self.factor) if self.factor is not None else None,
            "cash_amount": float(self.cash_amount) if self.cash_amount is not None else None,
            "currency": self.currency,
            "source": self.source,
            "source_reference": self.source_reference,
            "revision_id": self.revision_id,
            "ingested_at": self.ingested_at,
        }


def normalize_action(row: dict, *, market: str | None = None) -> CorporateActionRecord:
    """Validate one provider row into a CorporateActionRecord."""

    market_code = str(row.get("market") or market or "").strip().upper()
    factor = row.get("factor")
    cash_amount = row.get("cash_amount")
    return CorporateActionRecord(
        market=market_code,
        symbol=str(row.get("symbol") or "").strip().upper(),
        action_type=str(row.get("action_type") or "").strip().lower(),
        effective_date=_parse_iso_date(row.get("effective_date"), field="effective_date"),
        factor=float(factor) if factor not in (None, "") else None,
        cash_amount=float(cash_amount) if cash_amount not in (None, "") else None,
        currency=str(row.get("currency") or "").strip().upper(),
        announced_date=(
            _parse_iso_date(row.get("announced_date"), field="announced_date")
            if row.get("announced_date") not in (None, "")
            else None
        ),
        source=str(row.get("source") or "").strip(),
        source_reference=str(row.get("source_reference") or "").strip(),
    )


def actions_path(market: str, *, root: Path | None = None) -> Path:
    if root is None:
        from app.core.config import get_settings

        root = get_settings().data_dir / "corporate_actions"
    return Path(root) / f"{str(market).strip().lower()}_actions.parquet"


def write_actions(market: str, records: list[CorporateActionRecord], *, root: Path | None = None) -> Path:
    market_code = str(market or "").strip().upper()
    rows = [record.to_row() for record in records]
    if any(row["market"] != market_code for row in rows):
        raise ValueError("record market does not match the target store")
    path = actions_path(market_code, root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    incoming = pl.DataFrame(rows, schema=ACTIONS_SCHEMA, orient="row")
    if path.exists():
        existing = pl.read_parquet(path).cast(ACTIONS_SCHEMA)
        incoming = pl.concat([existing, incoming], how="vertical_relaxed").cast(ACTIONS_SCHEMA)
    # Append-only (audit finding #4): same natural key with different content is
    # a revision and is kept. Identical content re-ingested collapses to one row
    # (idempotency), independently of the revision hash implementation.
    incoming = (
        incoming.sort(["effective_date", "symbol", "action_type", "ingested_at", "revision_id"])
        .unique(
            subset=[
                "market", "symbol", "action_type", "effective_date",
                "announced_date", "factor", "cash_amount", "currency",
                "source", "source_reference",
            ],
            keep="last",
        )
        .sort(["effective_date", "symbol", "action_type", "ingested_at"])
    )
    temporary = path.with_name(f".{path.name}.tmp")
    incoming.write_parquet(temporary, compression="zstd")
    temporary.replace(path)
    return path


def load_actions(
    market: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    symbols: list[str] | None = None,
    as_of: str | None = None,
    root: Path | None = None,
) -> list[CorporateActionRecord]:
    """Latest revision per natural key, or the state visible at ``as_of``.

    The store keeps every revision; ``as_of`` reproduces what a run at that
    ingestion time would have seen (revision audit trail).
    """

    path = actions_path(market, root=root)
    if not path.exists():
        return []
    frame = pl.read_parquet(path).cast(ACTIONS_SCHEMA)
    # ``ingested_at`` is an ISO-8601 string that may mix UTC offsets, so compare
    # and order by the parsed instant, never lexicographically (review finding).
    frame = frame.with_columns(
        pl.col("ingested_at").str.to_datetime(time_zone="UTC", strict=False).alias("_ingested_at_utc")
    )
    if as_of:
        as_of_instant = pl.lit(str(as_of)).str.to_datetime(time_zone="UTC", strict=False)
        frame = frame.filter(pl.col("_ingested_at_utc") <= as_of_instant)
    frame = (
        frame.sort(["_ingested_at_utc", "revision_id"], nulls_last=False)
        .unique(subset=["market", "symbol", "action_type", "effective_date"], keep="last")
        .drop("_ingested_at_utc")
    )
    if start_date:
        frame = frame.filter(pl.col("effective_date") >= str(start_date)[:10])
    if end_date:
        frame = frame.filter(pl.col("effective_date") <= str(end_date)[:10])
    if symbols:
        wanted = {str(item).strip().upper() for item in symbols}
        frame = frame.filter(pl.col("symbol").is_in(sorted(wanted)))
    records: list[CorporateActionRecord] = []
    for row in frame.iter_rows(named=True):
        records.append(
            CorporateActionRecord(
                market=str(row["market"]),
                symbol=str(row["symbol"]),
                action_type=str(row["action_type"]),
                effective_date=_parse_iso_date(row["effective_date"], field="effective_date"),
                factor=row["factor"],
                cash_amount=row["cash_amount"],
                currency=str(row["currency"] or ""),
                announced_date=(
                    _parse_iso_date(row["announced_date"], field="announced_date")
                    if row["announced_date"]
                    else None
                ),
                source=str(row["source"] or ""),
                source_reference=str(row["source_reference"] or ""),
                revision_id=str(row["revision_id"] or ""),
                ingested_at=str(row["ingested_at"] or ""),
            )
        )
    return sorted(records, key=lambda item: (item.effective_date, item.symbol, item.action_type))


def explains_close_jump(
    *,
    previous_close: float,
    close: float,
    actions: list[CorporateActionRecord],
    symbol: str | None = None,
    band: float = 0.10,
) -> bool:
    """Whether an ex-date price step is explained by a known action on that day.

    Same-day actions combine: the ex-reference close is
    ``(previous_close - total_cash) / product(split factors)`` (e.g. a cash
    dividend paid together with a bonus share issue). The comparison is made
    against exchange-rounded limit prices (0.01, half-up), so a close exactly at
    the limit price on the ex-date counts as explained.

    ``band`` allows the same-session market move on top of the reference price
    (an ex-date that also rose 4% is still the same corporate action). When
    ``symbol`` is provided, only that symbol's actions are considered.
    """

    if previous_close <= 0 or close <= 0:
        return False
    candidates = actions
    if symbol:
        wanted = str(symbol).strip().upper()
        candidates = [action for action in actions if action.symbol == wanted]
    if not candidates:
        return False
    reference = float(previous_close)
    saw_action = False
    for action in candidates:
        if action.action_type in {"delisting", "merger", "spinoff"}:
            # Event-day explanations: a merger completion ends trading (the
            # final step is the consideration price, not a band move) and a
            # spinoff re-prices the parent (value leaves with the new entity).
            # Both are whitelisted events with their own records.
            return True
        if action.factor and action.action_type in {"split", "stock_dividend", "rights", "adjustment_factor"}:
            factor = float(action.factor)
            if factor > 0:
                reference /= factor
                saw_action = True
        elif action.action_type == "cash_dividend" and action.cash_amount is not None:
            reference -= float(action.cash_amount)
            saw_action = True
    if not saw_action or reference <= 0:
        return False
    reference = _round_tick(reference)
    limit_up = _round_tick(reference * (1.0 + band))
    limit_down = _round_tick(reference * (1.0 - band))
    # Official ex-reference prices can differ from the factor-derived one by a
    # small rounding margin (bonus-share ratio rounding, tax conventions), so
    # allow two ticks or 0.2% - whichever is larger.
    tolerance = max(0.02, 0.002 * reference)
    return limit_down - tolerance <= close <= limit_up + tolerance


def _round_tick(value: float) -> float:
    """Exchange prices tick at 0.01 with half-up rounding."""

    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def record_payload(record: CorporateActionRecord) -> dict:
    payload = asdict(record)
    payload["effective_date"] = record.effective_date.isoformat()
    payload["announced_date"] = record.announced_date.isoformat() if record.announced_date else None
    return payload
