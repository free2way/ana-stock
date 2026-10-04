"""Structured, accountable records for fail-closed operator opt-ins.

Two safety gates are opt-in: the trainer's raw-label fallback
(``PQW_TRAINER_ALLOW_RAW_FALLBACK``) and the backtest runner's acceptance of
unmodeled corporate actions (``allow_unmodeled_corporate_actions``). Historically
each was persisted as a bare boolean, which records *that* an operator waived a
gate but not *who* did it, *when*, over *what scope*, or *why*.

This module upgrades both records to a structured object and, crucially, refuses
to honour an opt-in that carries no reason (fail closed): an unexplained waiver
is not auditable and must not be trusted. The reason may come from the run
parameter or from ``PQW_OPTIN_REASON``; the operator defaults to ``unknown`` but
is always recorded explicitly (from the run parameter, ``PQW_OPTIN_OPERATOR``,
or the ``unknown`` fallback) so "no operator recorded" is distinguishable from
"operator field absent".

Unified strictness (both entry points)
--------------------------------------
Both opt-ins (the trainer's raw-label fallback and the runner's unmodeled
corporate-action acceptance) share one rule: **enabling the opt-in requires a
reason**. The requirement deliberately does *not* depend on whether the waiver
was exercised on the run in question — a standing/global setting is still an
operator decision that must be attributable, and whether it "was needed" is a
data-dependent judgement that must not decide auditability. Trainer and runner
therefore follow the exact same semantics; there is no per-call, per-run way to
downgrade the check.

The legacy boolean keys are kept by every caller for backwards compatibility;
the structured record is written under a separate key so old consumers are
unaffected.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

OPTIN_OPERATOR_ENV = "PQW_OPTIN_OPERATOR"
OPTIN_REASON_ENV = "PQW_OPTIN_REASON"
DEFAULT_OPERATOR = "unknown"

OPERATOR_SOURCE_PARAMETER = "run_parameter"
OPERATOR_SOURCE_ENV = f"env:{OPTIN_OPERATOR_ENV}"
OPERATOR_SOURCE_DEFAULT = "default"


class MissingOptinReasonError(RuntimeError):
    """Raised when an opt-in would be honoured without an auditable reason."""


def _clean(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _utc_iso(decided_at: datetime | None = None) -> str:
    moment = decided_at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _resolve_operator(operator: object) -> tuple[str, str]:
    explicit = _clean(operator)
    if explicit:
        return explicit, OPERATOR_SOURCE_PARAMETER
    from_env = _clean(os.environ.get(OPTIN_OPERATOR_ENV))
    if from_env:
        return from_env, OPERATOR_SOURCE_ENV
    return DEFAULT_OPERATOR, OPERATOR_SOURCE_DEFAULT


def _resolve_reason(reason: object) -> str:
    explicit = _clean(reason)
    if explicit:
        return explicit
    return _clean(os.environ.get(OPTIN_REASON_ENV))


def build_optin_audit(
    *,
    enabled: bool,
    source: str,
    scope: dict | None = None,
    operator: object = None,
    reason: object = None,
    decided_at: datetime | None = None,
) -> dict:
    """Build the structured audit record for one opt-in decision.

    ``enabled`` reflects whether the opt-in flag was set. Whenever it is enabled
    a non-empty ``reason`` is mandatory (run parameter or
    :data:`OPTIN_REASON_ENV`); otherwise :class:`MissingOptinReasonError` is
    raised so the caller fails closed before allowing the run. The same rule
    applies to both opt-in entry points (trainer raw-label fallback and runner
    unmodeled corporate-action acceptance): enabling the opt-in requires a
    reason, irrespective of whether that run actually exercises the waiver. A
    disabled opt-in records ``reason=None`` and raises nothing.
    """

    resolved_operator, operator_source = _resolve_operator(operator)
    resolved_reason = _resolve_reason(reason)
    record = {
        "source": str(source),
        "enabled": bool(enabled),
        "operator": resolved_operator,
        "operator_source": operator_source,
        "decided_at": _utc_iso(decided_at),
        "scope": {str(key): value for key, value in (scope or {}).items()},
        "reason": resolved_reason or None,
    }
    if enabled and not resolved_reason:
        raise MissingOptinReasonError(
            f"opt-in `{source}` was enabled without a reason; refusing fail-closed. "
            f"Record why the safety gate is being waived via the run parameter or "
            f"{OPTIN_REASON_ENV}."
        )
    return record
