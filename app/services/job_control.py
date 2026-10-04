"""In-process control plane for background :class:`DataJob` runs.

The web process (:mod:`app.api.routes.jobs`) runs long market jobs on daemon
threads.  This module gives those threads a cheap, lock-protected way to:

* observe a **cooperative cancellation** request (``POST /jobs/{id}/cancel``);
* enforce a **wall-clock deadline** (a watchdog requests cancellation and marks
  the run as timed out);
* publish **heartbeats** so the UI/DB can tell a live run from a wedged one.

It holds no state on disk and does not import the DB layer, so it is safe to
import and unit test in isolation.  Durable state (heartbeat/cancel/timeout
flags) is persisted separately in ``data_jobs.params_json["job_runtime"]`` by
the route layer, so **no database schema migration is required**.
"""

from __future__ import annotations

import threading
from time import monotonic
from typing import Callable


class JobCancelled(RuntimeError):
    """Raised by a worker at a checkpoint after a cancellation request."""


_STATE_LOCK = threading.RLock()
_STATE: dict[int, dict] = {}


def register(job_id: int, *, timeout_seconds: float | None = None) -> dict:
    """Start tracking a run; returns the fresh control state.

    If a cancellation was already requested for this id (e.g. the user clicked
    cancel between job creation and thread start) it is preserved.
    """

    now = monotonic()
    with _STATE_LOCK:
        pending = _STATE.get(int(job_id)) or {}
        state = {
            "job_id": int(job_id),
            "started_monotonic": now,
            "deadline_monotonic": (now + float(timeout_seconds)) if timeout_seconds else None,
            "cancel_requested": bool(pending.get("cancel_requested")),
            "cancel_reason": pending.get("cancel_reason"),
            "timed_out": bool(pending.get("timed_out")),
            "last_heartbeat_monotonic": now,
            "heartbeat_count": 0,
        }
        _STATE[int(job_id)] = state
    return state


def clear(job_id: int) -> None:
    with _STATE_LOCK:
        _STATE.pop(int(job_id), None)


def snapshot(job_id: int) -> dict | None:
    with _STATE_LOCK:
        state = _STATE.get(int(job_id))
        return dict(state) if state is not None else None


def heartbeat(job_id: int) -> None:
    with _STATE_LOCK:
        state = _STATE.get(int(job_id))
        if state is None:
            return
        state["last_heartbeat_monotonic"] = monotonic()
        state["heartbeat_count"] = int(state.get("heartbeat_count", 0)) + 1


def request_cancel(job_id: int, *, reason: str | None = None, timed_out: bool = False) -> bool:
    """Request cooperative cancellation.

    Returns ``True`` when the run is tracked in this process.  An untracked id
    still gets a state entry so the request is not silently lost (the durable
    flag is also written to ``job_runtime`` by the route layer).
    """

    with _STATE_LOCK:
        existing = _STATE.get(int(job_id))
        if existing is None:
            now = monotonic()
            existing = {
                "job_id": int(job_id),
                "started_monotonic": now,
                "deadline_monotonic": None,
                "cancel_requested": False,
                "cancel_reason": None,
                "timed_out": False,
                "last_heartbeat_monotonic": now,
                "heartbeat_count": 0,
            }
            _STATE[int(job_id)] = existing
            tracked = False
        else:
            tracked = True
        existing["cancel_requested"] = True
        if reason:
            existing["cancel_reason"] = reason
        if timed_out:
            existing["timed_out"] = True
        return tracked


def is_cancel_requested(job_id: int) -> bool:
    with _STATE_LOCK:
        state = _STATE.get(int(job_id))
        return bool(state and state.get("cancel_requested"))


def has_timed_out(job_id: int) -> bool:
    with _STATE_LOCK:
        state = _STATE.get(int(job_id))
        if state is None:
            return False
        if state.get("timed_out"):
            return True
        deadline = state.get("deadline_monotonic")
        return deadline is not None and monotonic() >= deadline


def check_cancelled(job_id: int) -> None:
    """Raise :class:`JobCancelled` if cancellation/deadline has been reached."""

    with _STATE_LOCK:
        state = _STATE.get(int(job_id))
        if state is None:
            return
        timed_out = bool(state.get("timed_out"))
        if not timed_out:
            deadline = state.get("deadline_monotonic")
            timed_out = deadline is not None and monotonic() >= deadline
        cancel_requested = bool(state.get("cancel_requested")) or timed_out
        reason = state.get("cancel_reason")
    if cancel_requested:
        raise JobCancelled(reason or ("Job timed out." if timed_out else "Job cancellation requested."))


def elapsed_seconds(job_id: int) -> float | None:
    with _STATE_LOCK:
        state = _STATE.get(int(job_id))
        if state is None:
            return None
        return max(0.0, monotonic() - float(state.get("started_monotonic", monotonic())))


def cancellation_checker(job_id: int) -> Callable[[], None]:
    """Return a zero-arg callable suitable for passing into service loops."""

    return lambda: check_cancelled(job_id)
