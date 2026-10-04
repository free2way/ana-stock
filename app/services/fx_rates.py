"""Fail-closed cross-currency aggregation for the portfolio ledger (S-12).

There is no live FX provider in this repository.  Rather than inventing a rate
(or silently summing CNY + USD + HKD as if they were the same unit), this
module resolves rates from an operator-supplied, versioned table:

* ``source`` — a human-readable label for the table's provenance;
* ``as_of``  — the quote date; required so a stale table is visible;
* ``base``   — the reporting currency (default ``CNY``);
* ``rates``  — currency -> units of ``base`` per 1 unit of that currency.

With no table, same-currency portfolios still aggregate normally; mixed-currency
portfolios are reported as ``fx_unavailable`` with the affected markets listed
and excluded from the blended total.  No rate is ever fabricated.

Staleness policy (annotate, never silently drop): a table whose ``as_of`` is
older than :data:`FX_MAX_AGE_DAYS` still converts — dropping a valid but old
quote would re-introduce a silent fail-closed — but every aggregation summary
carries ``fx_stale`` / ``fx_age_days`` / ``fx_reference_date`` and lists
``stale_fx_table`` in ``fx_warnings`` so the condition is machine-visible.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone

from app.core.db import SessionLocal
from app.services.repository import AppSettingRepository


FX_RATE_TABLE_KEY = "fx_rate_table"
DEFAULT_BASE_CURRENCY = "CNY"
SUPPORTED_CURRENCIES = ("CNY", "USD", "HKD")
# A quote older than this many days is still used, but flagged as stale.
FX_MAX_AGE_DAYS = 30
MARKET_CURRENCY = {"CN": "CNY", "US": "USD", "HK": "HKD"}
# Markets with a local price/lake source.  HK is present in the currency map but
# has no lake partition, so it must be reported as data-unavailable rather than
# quietly treated like a US name (S-12).
PRICE_SUPPORTED_MARKETS = frozenset({"CN", "US"})


def currency_for_market(market: str | None) -> str | None:
    return MARKET_CURRENCY.get(str(market or "").strip().upper())


def market_data_supported(market: str | None) -> bool:
    return str(market or "").strip().upper() in PRICE_SUPPORTED_MARKETS


def _empty_table() -> dict:
    return {
        "available": False,
        "base": None,
        "as_of": None,
        "source": None,
        "rates": {},
    }


def _parse_iso_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def fx_table_freshness(
    table: dict | None,
    *,
    reference_date: str | date | None = None,
    max_age_days: int = FX_MAX_AGE_DAYS,
) -> dict:
    """Report how old the table's ``as_of`` is relative to ``reference_date``.

    ``reference_date`` defaults to today (UTC).  An unparseable ``as_of`` yields
    ``age_days=None`` and ``stale=False``; availability is validated separately
    by :func:`load_fx_rate_table`, so this helper never invents staleness.
    """
    quoted = _parse_iso_date((table or {}).get("as_of"))
    reference = _parse_iso_date(reference_date) if reference_date is not None else datetime.now(timezone.utc).date()
    if reference is None:
        reference = datetime.now(timezone.utc).date()
    if quoted is None:
        return {
            "as_of": None,
            "reference_date": reference.isoformat(),
            "age_days": None,
            "max_age_days": max_age_days,
            "stale": False,
        }
    age_days = (reference - quoted).days
    return {
        "as_of": quoted.isoformat(),
        "reference_date": reference.isoformat(),
        "age_days": age_days,
        "max_age_days": max_age_days,
        "stale": age_days > max_age_days,
    }


def load_fx_rate_table() -> dict:
    """Load the operator-supplied FX table.  Invalid/absent tables are unavailable."""
    try:
        with SessionLocal() as db:
            raw = AppSettingRepository(db).get(FX_RATE_TABLE_KEY)
    except Exception:
        return _empty_table()
    if not raw:
        return _empty_table()
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return _empty_table()
    if not isinstance(payload, dict):
        return _empty_table()
    base = str(payload.get("base") or "").strip().upper()
    as_of = str(payload.get("as_of") or "").strip()[:10]
    source = str(payload.get("source") or "").strip()
    raw_rates = payload.get("rates")
    if base not in SUPPORTED_CURRENCIES or not as_of or not source or not isinstance(raw_rates, dict):
        return _empty_table()
    rates: dict[str, float] = {}
    for currency, value in raw_rates.items():
        code = str(currency or "").strip().upper()
        if code not in SUPPORTED_CURRENCIES or code == base:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            return _empty_table()
        if not math.isfinite(number) or number <= 0:
            return _empty_table()
        rates[code] = number
    return {
        "available": True,
        "base": base,
        "as_of": as_of,
        "source": source,
        "rates": rates,
    }


def resolve_fx_rate(from_currency: str | None, to_currency: str | None, table: dict | None) -> float | None:
    """Return the multiplier converting ``from`` into ``to``, or None if unknown."""
    source = str(from_currency or "").strip().upper()
    target = str(to_currency or "").strip().upper()
    if not source or not target:
        return None
    if source == target:
        return 1.0
    resolved = table or _empty_table()
    if not resolved.get("available"):
        return None
    base = str(resolved.get("base") or "").strip().upper()
    rates = resolved.get("rates") or {}
    if source != base and source not in rates:
        return None
    if target != base and target not in rates:
        return None
    source_in_base = 1.0 if source == base else float(rates[source])
    target_in_base = 1.0 if target == base else float(rates[target])
    if source_in_base <= 0 or target_in_base <= 0:
        return None
    return source_in_base / target_in_base


def convert_amount(amount: float | None, from_currency: str | None, to_currency: str | None, table: dict | None) -> tuple[float | None, float | None]:
    rate = resolve_fx_rate(from_currency, to_currency, table)
    if rate is None:
        return None, None
    if amount is None:
        return None, rate
    return float(amount) * rate, rate


def normalize_currency_aggregation(
    rows: list[dict],
    *,
    value_key: str,
    cost_key: str | None = None,
    table: dict | None = None,
    reference_date: str | date | None = None,
    max_age_days: int = FX_MAX_AGE_DAYS,
) -> dict:
    """Annotate rows with base-currency values and return the aggregation summary.

    Every row receives ``currency``, ``fx_rate``, ``fx_unavailable`` and
    ``{value_key}_base``.  ``total_base`` only sums convertible rows, so a
    missing rate can never be reported as a real blended total.

    A stale-but-valid table is annotated (``fx_stale``/``fx_warnings``) rather
    than rejected; ``reference_date`` exists so callers (and tests) can pin the
    clock instead of depending on wall time.
    """
    resolved = table if table is not None else load_fx_rate_table()
    freshness = fx_table_freshness(resolved, reference_date=reference_date, max_age_days=max_age_days)
    currencies = {
        currency_for_market(row.get("market"))
        for row in rows
        if currency_for_market(row.get("market"))
    }
    single_currency = next(iter(currencies)) if len(currencies) == 1 else None
    if single_currency:
        base = single_currency
    elif resolved.get("available") and resolved.get("base"):
        base = str(resolved["base"]).upper()
    else:
        base = DEFAULT_BASE_CURRENCY

    total_base = 0.0
    total_cost_base = 0.0
    unavailable_markets: list[str] = []
    for row in rows:
        market = str(row.get("market") or "").strip().upper()
        currency = currency_for_market(market)
        row["currency"] = currency
        amount = row.get(value_key)
        if currency is None:
            row["fx_rate"] = None
            row["fx_unavailable"] = True
            row[f"{value_key}_base"] = None
            if market and market not in unavailable_markets:
                unavailable_markets.append(market)
            continue
        converted, rate = convert_amount(amount, currency, base, resolved)
        row["fx_rate"] = rate
        row["fx_unavailable"] = converted is None
        row[f"{value_key}_base"] = converted
        if converted is None:
            if market not in unavailable_markets:
                unavailable_markets.append(market)
            continue
        total_base += converted
        if cost_key:
            cost_value = row.get(cost_key)
            if cost_value is not None:
                total_cost_base += float(cost_value) * float(rate)

    if not unavailable_markets:
        fx_status = "ok"
    elif total_base:
        fx_status = "partial"
    else:
        fx_status = "unavailable"
    warnings = ["stale_fx_table"] if freshness["stale"] else []
    return {
        "base_currency": base,
        "fx_status": fx_status,
        "fx_unavailable_markets": unavailable_markets,
        "fx_unavailable": bool(unavailable_markets),
        "fx_as_of": resolved.get("as_of"),
        "fx_source": resolved.get("source"),
        "fx_table_available": bool(resolved.get("available")),
        "fx_stale": freshness["stale"],
        "fx_age_days": freshness["age_days"],
        "fx_max_age_days": freshness["max_age_days"],
        "fx_reference_date": freshness["reference_date"],
        "fx_warnings": warnings,
        "total_base": round(total_base, 4),
        "total_cost_base": round(total_cost_base, 4) if cost_key else None,
    }
