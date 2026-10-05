"""Single canonical round-trip cost basis for selection / evaluation paths.

Before this module the model-evaluation entry point charged a cost while the
selection-quality ledger (``selection_quality_snapshot``) and the close-to-close
factor-experiment summary reported *gross* hit rates at an implicit zero cost.
The two views were therefore not comparable: a source could look like a winner
only because its hit flag ignored the round trip.

Every path that reports a hit rate or a return now resolves its cost through
:func:`canonical_round_trip_cost_bps` and echoes both the value it used and
:data:`CANONICAL_COST_SOURCE` in its summary, so a reader can never be misled
into believing a cost-free number is net.

Default basis and provenance
----------------------------
``PQW_SELECTION_CANONICAL_ROUND_TRIP_COST_BPS`` defaults to **50 bps**.  That is
the existing majority convention in this repository:

* ``Settings.trainer_round_trip_cost_bps`` (P0 #3 realistic CN ladder) defaults
  to 50bps -- "commission ~2.5bps per leg plus stamp/transfer load and next-open
  market-order slippage" (``app/core/config.py``).
* ``model_evaluation.DEFAULT_COST_SENSITIVITY_LADDER_BPS`` evaluates the
  scheduled nominal at 50bps (optimistic 20 / nominal 50 / punitive 80).
* The documented P0 cost bridge uses a 40-50bps flat round trip for the legacy
  fixed-fee label family (``docs/regime-aware-hit-rate-p0-cost-bridge-execution-*.md``).

A single value is applied to each market (per-market uniqueness is structural:
one canonical number per market, not a per-symbol table).  Override it with the
environment variable when a market's statutory load changes.
"""
from __future__ import annotations

from app.core.config import get_settings

CANONICAL_COST_ENV = "PQW_SELECTION_CANONICAL_ROUND_TRIP_COST_BPS"
CANONICAL_COST_SOURCE = (
    "settings.selection_canonical_round_trip_cost_bps "
    "(default 50bps: trainer P0#3 nominal / cost-sensitivity ladder nominal)"
)


def canonical_round_trip_cost_bps(market: str | None = None) -> float:
    """Return the canonical round-trip cost in bps for ``market``.

    ``market`` is accepted so callers state their market explicitly and a future
    per-market override can be added without changing call sites; today every
    market resolves to the single validated setting.
    """

    value = float(get_settings().selection_canonical_round_trip_cost_bps)
    return value


def canonical_cost_fields(market: str | None = None) -> dict[str, object]:
    """Summary fragment declaring the cost used and where it came from."""

    return {
        "cost_bps": canonical_round_trip_cost_bps(market),
        "cost_basis": "round_trip",
        "cost_source": CANONICAL_COST_SOURCE,
        "cost_env": CANONICAL_COST_ENV,
    }
