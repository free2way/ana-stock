"""Run-scoped corporate-action coverage audit: the canonical producer.

The unified promotion gate (``app.services.stock_selection.promotion_gate_v2``,
check ``corporate_action_coverage``) asks one question of a candidate run: are
there corporate actions inside the run's *traded window* that the price pipeline
does not model? Historically only the event-driven backtest runner answered it,
and only for *strategy* runs (``backtesting.runner._load_market_corporate_actions``
persists ``unmodeled_corporate_actions`` / ``unmodeled_opt_in`` on a
``strategy_runs`` config). A *training* run therefore reached the gate with no
audit at all and was reported as ``NOT_ENOUGH_EVIDENCE`` — a missing producer,
not a failed check.

This module holds the single canonical rule (``modeled_action``) plus the window
helper, so the runner and the trainer cannot drift apart, and exposes
``assess_corporate_action_coverage`` for the trainer to persist on the run.

Honesty rules:

* An *absent* action store is never reported as "0 unmodeled events". Absence of
  data is not evidence of absence, so the result is marked ``audited=False`` with
  a machine-readable ``missing_reason`` and the caller must record that reason
  instead of a pass.
* Read failures are treated the same way (``audited=False``); an audit that
  cannot be trusted must not become promotion evidence.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable, Mapping

from app.services.corporate_actions import actions_path, load_actions

#: Action types the price pipeline models on the run's behalf: ``split`` /
#: ``stock_dividend`` become price factors, a ``cash_dividend`` keeps its cash
#: amount. Everything else (``rights`` / ``delisting`` / ``adjustment_factor`` /
#: ``merger`` / ``spinoff``) changes the event-day price in a way this pipeline
#: does not model and must be surfaced instead of silently dropped.
MODELED_ACTION_TYPES = frozenset({"split", "stock_dividend", "cash_dividend"})

#: Minimum forward tail appended to the traded window so an action effective just
#: after the last signal (i.e. inside the holding period) is still audited.
DEFAULT_HOLDING_TAIL_DAYS = 14


def modeled_action(
    action_type: object, *, factor: float | None = None, cash_amount: float | None = None
) -> bool:
    """Canonical "does the price pipeline model this action?" rule.

    Mirrors the historical inline rule in
    :meth:`EventDrivenBacktestRunner._load_market_corporate_actions`: a split-like
    event needs a truthy factor, a cash dividend needs a recorded cash amount.
    """

    kind = str(action_type or "").strip().lower()
    if kind in {"split", "stock_dividend"}:
        return bool(factor)
    if kind == "cash_dividend":
        return cash_amount is not None
    return False


def corporate_action_window_end(end_date: str, *, holding_days: int = 0) -> str:
    """Last date audited: the traded window end plus the holding-period tail."""

    tail = max(DEFAULT_HOLDING_TAIL_DAYS, int(holding_days or 0) * 3 + 7)
    start = date.fromisoformat(str(end_date)[:10])
    return (start + timedelta(days=tail)).isoformat()


def assess_corporate_action_coverage(
    *,
    market: str,
    symbols: Iterable[str] | None,
    start_date: str,
    end_date: str,
    holding_days: int = 0,
) -> dict[str, object]:
    """Audit stored corporate actions inside a run's traded window.

    Returns gate-ready fields when the store is present::

        {"audited": True, "unmodeled_corporate_actions": [...],
         "unmodeled_opt_in": False, "loaded": n, "modeled": n, "unmodeled": n,
         "window_start": ..., "window_end": ..., "symbol_count": n}

    or ``{"audited": False, "missing_reason": "..."}`` when the store is absent
    or unreadable. The caller decides how to record each shape; this function
    never invents coverage.
    """

    market_code = str(market or "").strip().upper()
    if not market_code:
        raise ValueError("market is required for a corporate-action coverage audit")
    wanted = {str(item).strip().upper() for item in (symbols or ()) if str(item).strip()}
    window_start = str(start_date)[:10]
    window_end = corporate_action_window_end(end_date, holding_days=holding_days)

    store_path = actions_path(market_code)
    if not store_path.exists():
        return {
            "audited": False,
            "missing_reason": (
                f"no corporate-action store for market {market_code} "
                f"({store_path.name}); coverage cannot be claimed from absence of data"
            ),
        }
    try:
        records = load_actions(market_code)
    except Exception as exc:  # noqa: BLE001 - an unreadable store must not pass
        return {
            "audited": False,
            "missing_reason": (
                f"corporate-action store for market {market_code} could not be read: {exc}"
            ),
        }

    loaded = 0
    modeled = 0
    unmodeled: list[dict[str, str]] = []
    for record in records:
        if wanted and record.symbol not in wanted:
            continue
        effective = record.effective_date.isoformat()
        if effective < window_start or effective > window_end:
            continue
        loaded += 1
        if modeled_action(
            record.action_type, factor=record.factor, cash_amount=record.cash_amount
        ):
            modeled += 1
            continue
        unmodeled.append(
            {
                "symbol": record.symbol,
                "action_type": str(record.action_type or "").strip().lower(),
                "effective_date": effective,
            }
        )
    unmodeled.sort(
        key=lambda item: (item["effective_date"], item["symbol"], item["action_type"])
    )
    return {
        "audited": True,
        "unmodeled_corporate_actions": unmodeled,
        # The trainer has no unmodeled-corporate-action opt-in; recording False
        # keeps the gate fail-closed if an unmodeled event is ever found.
        "unmodeled_opt_in": False,
        "loaded": loaded,
        "modeled": modeled,
        "unmodeled": len(unmodeled),
        "window_start": window_start,
        "window_end": window_end,
        "symbol_count": len(wanted),
    }


def coverage_evidence_fields(
    coverage: Mapping[str, object],
) -> dict[str, object]:
    """Map an audit result onto the run-config keys the gate reads.

    An unaudited store yields an explicit missing reason instead of the two
    evidence keys, so ``promotion_gate_v2`` keeps reporting
    ``NOT_ENOUGH_EVIDENCE`` rather than a fabricated pass.
    """

    if coverage.get("audited"):
        return {
            "unmodeled_corporate_actions": list(
                coverage.get("unmodeled_corporate_actions") or ()
            ),
            "unmodeled_opt_in": bool(coverage.get("unmodeled_opt_in")),
        }
    return {
        "corporate_action_coverage_missing_reason": str(
            coverage.get("missing_reason") or "corporate-action coverage unavailable"
        )
    }


__all__ = [
    "DEFAULT_HOLDING_TAIL_DAYS",
    "MODELED_ACTION_TYPES",
    "assess_corporate_action_coverage",
    "corporate_action_window_end",
    "coverage_evidence_fields",
    "modeled_action",
]
