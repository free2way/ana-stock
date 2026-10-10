"""Auditable evidence for adjusted-view gaps no source can ever fill.

The adjusted-view coverage gate (:mod:`app.services.price_basis_contract`)
requires every price point of every label window to come from the rebuilt view.
A rebuild can only fill a gap when the raw *source* namespace the view is built
from actually carries the bar. When it does not, the ``(symbol, date)`` row is
genuinely unavailable -- not a view defect -- and demanding an unattainable
``coverage == 1.0`` would block the run forever (review comment #4:
"missing counted separately, never backfilled").

This module turns "the source has no data" into an *auditable fact* instead of
a hard-coded whitelist. For the market's declared single-basis source namespace
it reports, per requested ``(symbol, date)``:

* which requested bars the source is missing while the symbol is otherwise
  **live** in the source (``unavailable`` -- provably a source gap), and
* which requested symbols have **no** source coverage at all
  (``unproven`` -- a wholesale outage for one name must still fail the gate).

Design rules, kept deliberately strict so the gate cannot be widened by
accident:

* Only a symbol the source covers at all can contribute an exclusion; a symbol
  with zero source rows is never excused.
* Every lookup failure (missing namespace, unreadable parquet, engine error)
  yields **no** exclusions -- unknown is never treated as unavailable.
* The result carries the exact source glob and per-symbol counts so any
  reviewer can re-derive the exclusion straight from the raw files.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from app.services.adjusted_view_builder import us_adjusted_raw_glob
from app.services.market_lake import market_lake_root

# Cap the per-pair detail stored on run products: the counts are always
# complete, the sample rows are bounded so a run config cannot blow up.
DEFAULT_MAX_DETAIL = 100


@dataclass(frozen=True, slots=True)
class SourceAvailabilityEvidence:
    """Evidence that requested bars are (or are not) absent from the source."""

    market: str
    source_glob: str | None
    requested_pairs: int = 0
    live_symbols: tuple[str, ...] = ()
    source_missing_symbols: tuple[str, ...] = ()
    unavailable_pairs: frozenset[tuple[str, str]] = frozenset()
    present_pairs: frozenset[tuple[str, str]] = frozenset()
    unavailable_by_symbol: dict[str, int] = field(default_factory=dict)
    unproven_pairs: frozenset[tuple[str, str]] = frozenset()
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.error is None and bool(self.source_glob)

    def as_dict(self, *, max_detail: int = DEFAULT_MAX_DETAIL) -> dict:
        unavailable = sorted(self.unavailable_pairs)
        detail = [
            {"symbol": symbol, "date": trade_date}
            for symbol, trade_date in unavailable[: max(0, int(max_detail))]
        ]
        return {
            "market": self.market,
            "source_glob": self.source_glob,
            "requested_pairs": int(self.requested_pairs),
            "live_symbol_count": len(self.live_symbols),
            "source_missing_symbols": list(self.source_missing_symbols),
            "unavailable_pair_count": len(self.unavailable_pairs),
            "unproven_pair_count": len(self.unproven_pairs),
            "unavailable_by_symbol": dict(sorted(self.unavailable_by_symbol.items())),
            "unavailable_detail": detail,
            "detail_truncated": len(unavailable) > len(detail),
            "error": self.error,
        }


def _normalize_pairs(pairs) -> set[tuple[str, str]]:
    normalized: set[tuple[str, str]] = set()
    for symbol, trade_date in pairs:
        ticker = str(symbol or "").strip().upper()
        day = str(trade_date or "").strip()[:10]
        if ticker and day:
            normalized.add((ticker, day))
    return normalized


def source_namespace_glob(market: str | None, *, root: Path | None = None) -> str | None:
    """The raw source namespace the market's adjusted view is rebuilt from.

    ``US`` rebuilds from the single-basis Alpaca namespace
    (``_us_alpaca/raw/*.parquet``) and ``CN`` from the raw ``cn_daily`` lake.
    Any other market has no declared adjusted-view source.
    """

    normalized = str(market or "").strip().upper()
    base = Path(root) if root is not None else market_lake_root()
    if normalized == "US":
        return us_adjusted_raw_glob(base)
    if normalized == "CN":
        return str(base / "cn_daily" / "date=*" / "*.parquet")
    return None


def find_unavailable_pairs(
    market: str | None,
    pairs,
    *,
    root: Path | None = None,
    raw_glob: str | None = None,
) -> SourceAvailabilityEvidence:
    """Classify requested ``(symbol, date)`` pairs against the source namespace.

    Returns evidence whose ``unavailable_pairs`` are the requested bars that the
    source lacks while the symbol is live there. A symbol with no source rows at
    all lands in ``source_missing_symbols`` / ``unproven_pairs`` and is never
    excludable.
    """

    normalized = str(market or "").strip().upper()
    requested = _normalize_pairs(pairs)
    glob = raw_glob or source_namespace_glob(normalized, root=root)
    if not requested or glob is None:
        return SourceAvailabilityEvidence(
            market=normalized,
            source_glob=glob,
            requested_pairs=len(requested),
            error=None if glob is not None else "no_declared_source_namespace",
        )

    symbols = sorted({symbol for symbol, _ in requested})
    dates = sorted({trade_date for _, trade_date in requested})

    def _empty(error: str) -> SourceAvailabilityEvidence:
        return SourceAvailabilityEvidence(
            market=normalized,
            source_glob=glob,
            requested_pairs=len(requested),
            error=error,
        )

    try:
        live_rows = duckdb.sql(
            "SELECT DISTINCT symbol FROM read_parquet(?, hive_partitioning=true) "
            "WHERE symbol = ANY(?)",
            params=[glob, symbols],
        ).fetchall()
        present_rows = duckdb.sql(
            "SELECT DISTINCT symbol, CAST(date AS VARCHAR) AS d "
            "FROM read_parquet(?, hive_partitioning=true) "
            "WHERE symbol = ANY(?) AND CAST(date AS VARCHAR) IN (SELECT unnest(?))",
            params=[glob, symbols, dates],
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 - never widen the gate on error
        return _empty(f"{type(exc).__name__}: {exc}")

    live = {str(row[0]).strip().upper() for row in live_rows}
    present = {
        (str(row[0]).strip().upper(), str(row[1]).strip()[:10]) for row in present_rows
    }
    unavailable = {pair for pair in requested if pair[0] in live and pair not in present}
    unproven = {pair for pair in requested if pair[0] not in live}
    by_symbol: dict[str, int] = {}
    for symbol, _ in unavailable:
        by_symbol[symbol] = by_symbol.get(symbol, 0) + 1
    return SourceAvailabilityEvidence(
        market=normalized,
        source_glob=glob,
        requested_pairs=len(requested),
        live_symbols=tuple(sorted(live)),
        source_missing_symbols=tuple(sorted({symbol for symbol, _ in unproven})),
        unavailable_pairs=frozenset(unavailable),
        present_pairs=frozenset(present),
        unavailable_by_symbol=by_symbol,
        unproven_pairs=frozenset(unproven),
    )
