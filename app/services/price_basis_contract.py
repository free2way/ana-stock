"""Shared price-basis contract for train / inference / backtest entry points.

The versioned adjusted view (``data/lake/_adjusted_v2/<market>/method=qfq``)
is the single trustworthy price basis for labels and provenance. Before this
module each entry point re-invented its own view detection:

* the trainer probed the view, tracked ``absent`` / ``present`` /
  ``unreadable`` and gated the run (fail closed);
* the backtest runner only hashed the view for its manifest;
* prediction producers had no shared notion of the view state at all.

That let the same corrupt or partial view produce *different* decisions on the
three paths, and let prediction products omit the basis they were built on.
This module is the one place that:

1. probes the view into a three-state :class:`AdjustedViewProbe`
   (``absent`` / ``present`` / ``unreadable`` with path + reason) and computes
   coverage / missing statistics;
2. turns that probe plus the entry-point requirements into a single
   :class:`PriceBasisDecision` (``allow_adjusted`` /
   ``allow_raw_with_authorization`` / ``reject``) with enumerable reasons;
3. serialises the decision + audit fields into a plain dict so every entry can
   persist the *same* contract fields in its product.

Design notes
------------
* ``unreadable`` is never downgraded to "no view": a file that exists but
  cannot be parsed is a corrupt view, not an absent one. It is fail closed for
  every entry that depends on the adjusted basis.
* The adjusted view is not applicable outside ``CN`` / ``US`` and for the
  reconciled raw-replay label profile; those runs are truthfully ``raw``.
* Coverage gating is the label-construction concern (train) and the
  prediction-basis concern (inference). The backtest engine replays raw bars
  plus corporate actions and does not read the adjusted view arithmetically,
  so it gates coverage only when the model run itself declared a full-coverage
  basis; it always fails closed on an unreadable view and on a version
  mismatch.
* Only an *explicit* opt-in may fall back to raw when the view is absent. The
  authorization source is recorded on the decision so downstream products can
  never claim an adjusted basis that was not actually used.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import duckdb

from app.services import adjusted_view_store, adjustment_snapshot

DEFAULT_METHOD = "qfq"
ADJUSTED_VIEW_MARKETS = ("CN", "US")
ENTRY_POINTS = ("train", "inference", "backtest")

# Enumerated entry points / decisions / reject reasons. Kept as plain strings
# (not enum.Enum) so the serialised audit dict is JSON-native everywhere.
ENTRY_TRAIN = "train"
ENTRY_INFERENCE = "inference"
ENTRY_BACKTEST = "backtest"

DECISION_ALLOW_ADJUSTED = "allow_adjusted"
DECISION_ALLOW_RAW_WITH_AUTHORIZATION = "allow_raw_with_authorization"
DECISION_REJECT = "reject"

REASON_UNREADABLE_VIEW = "unreadable_view"
REASON_VIEW_VERSION_MISMATCH = "view_version_mismatch"
REASON_ADJUSTED_VIEW_ABSENT = "adjusted_view_absent"
REASON_INCOMPLETE_ADJUSTED_COVERAGE = "incomplete_adjusted_coverage"

# fallback_policy values recorded on the decision.
FALLBACK_NONE = "none"
FALLBACK_EXPLICIT_RAW = "explicit_raw_fallback"
FALLBACK_FAIL_CLOSED = "fail_closed"
FALLBACK_NOT_APPLICABLE = "not_applicable"
FALLBACK_RAW_WITH_ACTIONS = "raw_with_actions"

# The single authorization switch reused by every entry point (documented in
# the module docstring: entry-specific opt-in flags are intentionally avoided
# so the three paths cannot drift apart).
RAW_FALLBACK_AUTHORIZATION_SOURCE = "PQW_TRAINER_ALLOW_RAW_FALLBACK"

_ADJUSTED_FIELDS = ("open", "high", "low", "close")


def _finite_positive(value: object) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


@dataclass(frozen=True, slots=True)
class AdjustedViewProbe:
    """Result of probing the versioned adjusted view for a market."""

    market: str | None
    state: str  # "absent" | "present" | "unreadable"
    path: str | None = None
    error: str | None = None
    version: str | None = None
    view_sha256: str | None = None
    actions_sha256: str | None = None
    requested_rows: int = 0
    served_rows: int = 0
    missing_rows: int = 0
    missing_symbols: tuple[str, ...] = ()
    coverage_share: float = 1.0

    @property
    def applicable(self) -> bool:
        return str(self.market or "").strip().upper() in ADJUSTED_VIEW_MARKETS

    @property
    def present(self) -> bool:
        return self.state == "present"

    @property
    def unreadable(self) -> bool:
        return self.state == "unreadable"

    def as_dict(self) -> dict:
        return {
            "market": self.market,
            "state": self.state,
            "path": self.path,
            "error": self.error,
            "version": self.version,
            "view_sha256": self.view_sha256,
            "actions_sha256": self.actions_sha256,
            "requested_rows": self.requested_rows,
            "served_rows": self.served_rows,
            "missing_rows": self.missing_rows,
            "missing_symbols": list(self.missing_symbols),
            "coverage_share": round(float(self.coverage_share), 8),
        }


def _empty_probe(market: str | None, *, state: str = "absent", **kwargs) -> AdjustedViewProbe:
    return AdjustedViewProbe(market=market, state=state, **kwargs)


def _path_is_readable(path: Path) -> tuple[bool, str | None]:
    """Cheap readability check: one bounded read_parquet (binds/parses schema)."""

    try:
        duckdb.sql(
            "SELECT 1 FROM read_parquet(?) LIMIT 1", params=[str(path)]
        ).fetchone()
    except Exception as exc:  # corrupt / not a parquet / schema mismatch
        return False, f"{type(exc).__name__}: {exc} (view={path})"
    return True, None


def read_adjusted_view_with_probe(
    market: str | None,
    *,
    method: str = DEFAULT_METHOD,
    symbols: set[str] | None = None,
    rows: list[dict] | None = None,
    root: Path | None = None,
    read_bars: bool = True,
) -> tuple[dict[str, dict[str, dict[str, float]]], AdjustedViewProbe]:
    """Load the adjusted view once and return ``(bars, probe)``.

    ``symbols`` / ``rows`` scope the coverage statistics to the caller's
    universe: pass ``rows`` to get row-level missing counts (the trainer
    attaches per row) or ``symbols`` for a symbol-level coverage share. Pass
    ``read_bars=False`` when the caller only needs the view state / manifest
    hash (e.g. an import that does not consume adjusted prices): the view is
    still checked for corruption with one bounded read, but no bars are
    materialised.
    """

    normalized = str(market or "").strip().upper()
    if normalized not in ADJUSTED_VIEW_MARKETS:
        return {}, _empty_probe(normalized or None)

    path: Path | None = None
    try:
        path = adjusted_view_store.adjusted_view_path(normalized, method=method, root=root)
        view_exists = path.exists()
    except Exception as exc:  # path resolution itself failed
        return {}, AdjustedViewProbe(
            market=normalized,
            state="unreadable",
            error=f"{type(exc).__name__}: {exc}",
        )

    if not read_bars:
        if not view_exists:
            return {}, AdjustedViewProbe(
                market=normalized,
                state="absent",
                path=str(path),
                version=None,
                view_sha256=None,
                actions_sha256=adjustment_snapshot.actions_snapshot_sha256(normalized),
            )
        readable, error = _path_is_readable(path)
        if not readable:
            return {}, AdjustedViewProbe(
                market=normalized,
                state="unreadable",
                path=str(path),
                error=error,
            )
        manifest = adjustment_snapshot.adjusted_view_manifest(
            normalized, method=method, root=root
        )
        return {}, AdjustedViewProbe(
            market=normalized,
            state="present",
            path=str(path),
            version=manifest.get("version"),
            view_sha256=manifest.get("parquet_sha256"),
            actions_sha256=adjustment_snapshot.actions_snapshot_sha256(normalized),
        )

    try:
        bars = adjusted_view_store.load_adjusted_bars(
            normalized, method=method, symbols=symbols, root=root
        )
    except Exception as exc:  # corrupt / unreadable, never "absent"
        location = f" (view={path})" if path is not None else ""
        return {}, AdjustedViewProbe(
            market=normalized,
            state="unreadable",
            path=str(path) if path is not None else None,
            error=f"{type(exc).__name__}: {exc}{location}",
        )

    state = "present" if (bars or view_exists) else "absent"
    manifest = adjustment_snapshot.adjusted_view_manifest(
        normalized, method=method, root=root
    )
    actions_sha = adjustment_snapshot.actions_snapshot_sha256(normalized)
    probe_kwargs = {
        "path": str(path),
        "version": manifest.get("version"),
        "view_sha256": manifest.get("parquet_sha256"),
        "actions_sha256": actions_sha,
    }
    if not bars or state != "present":
        return bars, AdjustedViewProbe(market=normalized, state=state, **probe_kwargs)

    missing_symbols: set[str] = set()
    requested_rows = 0
    served_rows = 0
    if rows:
        requested_rows = len(rows)
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            trade_date = str(row.get("date") or "")[:10]
            payload = bars.get(symbol, {}).get(trade_date)
            usable = bool(payload) and all(
                _finite_positive(payload.get(name)) for name in _ADJUSTED_FIELDS
            )
            if usable:
                served_rows += 1
            else:
                missing_symbols.add(symbol)
        missing_rows = requested_rows - served_rows
        coverage_share = served_rows / requested_rows if requested_rows else 1.0
    elif symbols:
        requested = {str(item).strip().upper() for item in symbols if str(item).strip()}
        served = requested & set(bars)
        missing_symbols = requested - served
        missing_rows = 0
        coverage_share = len(served) / len(requested) if requested else 1.0
    else:
        missing_rows = 0
        coverage_share = 1.0
    return bars, AdjustedViewProbe(
        market=normalized,
        state=state,
        requested_rows=requested_rows,
        served_rows=served_rows,
        missing_rows=missing_rows,
        missing_symbols=tuple(sorted(missing_symbols)),
        coverage_share=coverage_share,
        **probe_kwargs,
    )


def probe_adjusted_view(
    market: str | None,
    *,
    method: str = DEFAULT_METHOD,
    symbols: set[str] | None = None,
    rows: list[dict] | None = None,
    root: Path | None = None,
    read_bars: bool = True,
) -> AdjustedViewProbe:
    """Probe-only shorthand for callers that do not need the loaded bars."""

    return read_adjusted_view_with_probe(
        market, method=method, symbols=symbols, rows=rows, root=root, read_bars=read_bars
    )[1]


@dataclass(frozen=True, slots=True)
class PriceBasisRequirements:
    """Per-entry inputs to the shared decision table.

    ``requires_adjusted_prices`` is False for runs where the adjusted view is
    genuinely not applicable (non CN/US, or the raw-replay label profile) —
    those are truthfully raw and never rejected. ``adjusted_count`` /
    ``raw_fallback_count`` / ``dropped_missing_adjusted_count`` let the trainer
    gate on *labeled-sample* coverage instead of raw row coverage.
    """

    entry_point: str = ENTRY_TRAIN
    requires_adjusted_prices: bool = True
    require_full_coverage: bool = True
    allow_raw_fallback: bool = False
    authorization_source: str | None = None
    expected_view_sha256: str | None = None
    adjusted_count: int = 0
    raw_fallback_count: int = 0
    dropped_missing_adjusted_count: int = 0
    # Backtest replays raw bars + corporate actions: it binds the view by hash
    # and records coverage, but must not reject a complete backtest solely
    # because the view lacks an unrelated symbol.
    gates_coverage: bool = True


@dataclass(frozen=True, slots=True)
class PriceBasisDecision:
    entry_point: str
    decision: str
    reasons: tuple[str, ...]
    applicable: bool
    view_state: str
    view_sha256: str | None
    actions_sha256: str | None
    coverage_share: float
    missing_rows: int
    missing_symbols: tuple[str, ...]
    fallback_policy: str
    authorized_by: str | None
    require_full_coverage: bool
    version_match: bool | None
    expected_view_sha256: str | None
    adjusted_count: int
    raw_fallback_count: int
    dropped_missing_adjusted_count: int
    label_price_basis: str

    @property
    def allowed(self) -> bool:
        return self.decision != DECISION_REJECT

    @property
    def used_adjusted_basis(self) -> bool:
        return self.decision == DECISION_ALLOW_ADJUSTED and self.view_state == "present"

    def error_message(self, *, market: str | None = None, error: str | None = None) -> str:
        """Human-explainable refusal message enumerating the violated reasons."""

        prefix = f"[{self.entry_point}] price-basis contract refused the run"
        detail = "; ".join(self.reasons) if self.reasons else "reject"
        if market:
            prefix = f"{prefix} (market={str(market).strip().upper()})"
        if error:
            prefix = f"{prefix}: {detail}; view_error={error}"
        else:
            prefix = f"{prefix}: {detail}"
        return prefix + ". Rebuild the adjusted view (scripts/rebuild_adjusted_view.py) or explicitly authorize raw fallback."

    def audit_fields(self) -> dict:
        """Serialisable contract + audit fields for entry-point products."""

        return {
            "entry_point": self.entry_point,
            "decision": self.decision,
            "reasons": list(self.reasons),
            "applicable": self.applicable,
            "view_state": self.view_state,
            "view_sha256": self.view_sha256,
            "actions_sha256": self.actions_sha256,
            "coverage_share": round(float(self.coverage_share), 8),
            "missing_rows": self.missing_rows,
            "missing_symbols": list(self.missing_symbols),
            "fallback_policy": self.fallback_policy,
            "authorized_by": self.authorized_by,
            "require_full_coverage": bool(self.require_full_coverage),
            "version_match": self.version_match,
            "expected_view_sha256": self.expected_view_sha256,
            "adjusted_count": self.adjusted_count,
            "raw_fallback_count": self.raw_fallback_count,
            "dropped_missing_adjusted_count": self.dropped_missing_adjusted_count,
            "label_price_basis": self.label_price_basis,
        }


def _resolve_coverage(probe: AdjustedViewProbe, requirements: PriceBasisRequirements) -> float:
    candidate_count = (
        int(requirements.adjusted_count)
        + int(requirements.raw_fallback_count)
        + int(requirements.dropped_missing_adjusted_count)
    )
    if candidate_count > 0:
        return int(requirements.adjusted_count) / candidate_count
    return float(probe.coverage_share)


def _label_price_basis(
    *,
    applicable: bool,
    state: str,
    requirements: PriceBasisRequirements,
    coverage_share: float,
) -> str:
    if not applicable:
        return "raw"
    candidate_count = (
        int(requirements.adjusted_count)
        + int(requirements.raw_fallback_count)
        + int(requirements.dropped_missing_adjusted_count)
    )
    if candidate_count == 0:
        return "adjusted_view" if state == "present" else f"mixed:{0.0:.8f}"
    if int(requirements.adjusted_count) == candidate_count and state == "present":
        return "adjusted_view"
    return f"mixed:{coverage_share:.8f}"


def decide_price_basis(
    entry_point: str,
    probe: AdjustedViewProbe,
    requirements: PriceBasisRequirements | None = None,
) -> PriceBasisDecision:
    """The single decision table shared by train / inference / backtest.

    Order of checks (first match wins):

    1. not applicable (non CN/US or raw-replay profile) -> allow raw;
    2. ``unreadable`` view -> reject ``unreadable_view``;
    3. recorded expected view hash differs from the current view -> reject
       ``view_version_mismatch``;
    4. ``absent`` view -> allow raw only with explicit authorization, else
       reject ``adjusted_view_absent``;
    5. incomplete coverage (when required) -> reject
       ``incomplete_adjusted_coverage``;
    6. otherwise -> allow the adjusted basis.
    """

    resolved_entry = str(entry_point or "").strip().lower()
    if resolved_entry not in ENTRY_POINTS:
        raise ValueError(f"Unknown price-basis entry point: {entry_point!r}")
    requirements = requirements or PriceBasisRequirements(entry_point=resolved_entry)
    applicable = bool(probe.applicable and requirements.requires_adjusted_prices)
    coverage_share = _resolve_coverage(probe, requirements)
    authorized_by = (
        (requirements.authorization_source or RAW_FALLBACK_AUTHORIZATION_SOURCE)
        if requirements.allow_raw_fallback
        else None
    )
    expected = requirements.expected_view_sha256 or None
    version_match: bool | None = None
    if expected is not None:
        version_match = probe.view_sha256 == expected

    def _build(decision: str, reasons: tuple[str, ...], fallback: str) -> PriceBasisDecision:
        label = _label_price_basis(
            applicable=applicable,
            state=probe.state,
            requirements=requirements,
            coverage_share=coverage_share,
        )
        if label == "adjusted_view" and decision == DECISION_REJECT:
            # A rejected run must never carry a truthful-looking adjusted label.
            label = "raw"
        return PriceBasisDecision(
            entry_point=resolved_entry,
            decision=decision,
            reasons=reasons,
            applicable=applicable,
            view_state=probe.state,
            view_sha256=probe.view_sha256,
            actions_sha256=probe.actions_sha256,
            coverage_share=coverage_share,
            missing_rows=probe.missing_rows,
            missing_symbols=probe.missing_symbols,
            fallback_policy=fallback,
            authorized_by=authorized_by,
            require_full_coverage=bool(requirements.require_full_coverage),
            version_match=version_match,
            expected_view_sha256=expected,
            adjusted_count=int(requirements.adjusted_count),
            raw_fallback_count=int(requirements.raw_fallback_count),
            dropped_missing_adjusted_count=int(requirements.dropped_missing_adjusted_count),
            label_price_basis=label,
        )

    if not applicable:
        return _build(DECISION_ALLOW_RAW_WITH_AUTHORIZATION, (), FALLBACK_NOT_APPLICABLE)

    if probe.unreadable:
        return _build(DECISION_REJECT, (REASON_UNREADABLE_VIEW,), FALLBACK_FAIL_CLOSED)

    if version_match is False:
        return _build(DECISION_REJECT, (REASON_VIEW_VERSION_MISMATCH,), FALLBACK_FAIL_CLOSED)

    if probe.state == "absent":
        if requirements.allow_raw_fallback:
            return _build(
                DECISION_ALLOW_RAW_WITH_AUTHORIZATION, (), FALLBACK_EXPLICIT_RAW
            )
        return _build(DECISION_REJECT, (REASON_ADJUSTED_VIEW_ABSENT,), FALLBACK_FAIL_CLOSED)

    if (
        requirements.gates_coverage
        and requirements.require_full_coverage
        and coverage_share < 1.0
    ):
        return _build(DECISION_REJECT, (REASON_INCOMPLETE_ADJUSTED_COVERAGE,), FALLBACK_FAIL_CLOSED)

    return _build(DECISION_ALLOW_ADJUSTED, (), FALLBACK_NONE)


def build_contract(
    entry_point: str,
    market: str | None,
    *,
    rows: list[dict] | None = None,
    symbols: set[str] | None = None,
    expected_view_sha256: str | None = None,
    require_full_coverage: bool = True,
    allow_raw_fallback: bool = False,
    authorization_source: str | None = None,
    gates_coverage: bool = True,
    adjusted_count: int = 0,
    raw_fallback_count: int = 0,
    dropped_missing_adjusted_count: int = 0,
) -> tuple[PriceBasisDecision, AdjustedViewProbe]:
    """Probe the view and decide in one call (convenience for entry points)."""

    probe = probe_adjusted_view(market, symbols=symbols, rows=rows)
    decision = decide_price_basis(
        entry_point,
        probe,
        PriceBasisRequirements(
            entry_point=entry_point,
            requires_adjusted_prices=True,
            require_full_coverage=require_full_coverage,
            allow_raw_fallback=allow_raw_fallback,
            authorization_source=authorization_source,
            expected_view_sha256=expected_view_sha256,
            adjusted_count=adjusted_count,
            raw_fallback_count=raw_fallback_count,
            dropped_missing_adjusted_count=dropped_missing_adjusted_count,
            gates_coverage=gates_coverage,
        ),
    )
    return decision, probe
