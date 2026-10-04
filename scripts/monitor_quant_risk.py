"""Read-only quant risk monitor (data freshness / coverage / fail-closed / drift).

Produces a JSON + Markdown snapshot under ``data/artifacts/acceptance-20261002/``
(default; override with ``--output-dir``). The monitor never writes to the lake,
the database or any run record: it only reads them.

Sections:

* **data staleness** — the lake's latest traded date per market vs the latest
  completed session expected by the market calendar (reuses
  :mod:`app.services.market_freshness` and :func:`market_lake.get_latest_lake_trade_date`).
* **coverage** — the CN/US versioned adjusted-view probe (reuses
  :mod:`app.services.price_basis_contract`) and the count of stored corporate
  actions the event engine cannot model. When a previous monitor snapshot is
  present, the coverage/share and unsupported-count deltas are reported.
* **fail-closed** — how many times each fail-closed gate refused a gate in
  recent run config/summary and application logs (price-basis reject, coverage
  block, missing-reason/absent-view raw fallback block, unmodeled-action block).
* **model drift** — the OOS metrics of the two most recent same-named runs.

Every section degrades to an explicit ``insufficient_data`` status (with a
``note``) instead of inventing numbers when its source is missing.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA_VERSION = "quant-risk-monitor-v1"
DEFAULT_OUTPUT_DIR = ROOT / "data" / "artifacts" / "acceptance-20261002"
DEFAULT_MARKETS = ("CN", "US")
DEFAULT_LOG_PATHS = ("storage/ana-app.err.log", "storage/ana-app.out.log")
DEFAULT_RUN_LIMIT = 40
DEFAULT_LOG_TAIL_BYTES = 5_000_000

# Corporate-action types the event engine can actually model. Everything else
# (merger / spinoff / delisting / rights / adjustment_factor) is "unsupported".
ENGINE_MODELLED_ACTION_TYPES = {"split", "stock_dividend", "cash_dividend"}

FAIL_CLOSED_LOG_MARKERS = {
    "price_basis_reject": ("price-basis contract refused the run",),
    "coverage_block": (
        "refused an adjusted-view label run with incomplete coverage",
    ),
    "raw_fallback_gap_block": ("refused a CN/US label run without an adjusted view",),
    "unreadable_view_block": ("exists but is unreadable",),
    "unmodeled_block": ("Event-driven backtest refused to run",),
    "missing_optin_reason": ("was enabled without a reason",),
}

FAIL_CLOSED_CATEGORIES = tuple(FAIL_CLOSED_LOG_MARKERS)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_object(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Data staleness
# --------------------------------------------------------------------------- #
def _default_lake_latest_fn(market: str) -> str | None:
    from app.services.market_lake import get_latest_lake_trade_date

    return get_latest_lake_trade_date(market=market)


def collect_staleness(
    markets: tuple[str, ...] = DEFAULT_MARKETS,
    *,
    now: datetime | None = None,
    lake_latest_fn=None,
) -> dict:
    """Latest lake trade date per market vs the expected completed session."""

    from app.services.market_freshness import latest_completed_market_date

    lake_latest = lake_latest_fn or _default_lake_latest_fn
    per_market: dict[str, dict] = {}
    statuses: set[str] = set()
    for market in markets:
        expected: str | None = None
        actual: str | None = None
        error: str | None = None
        try:
            expected = latest_completed_market_date(market, now=now)
        except Exception as exc:  # calendar failure must not abort the monitor
            error = f"{type(exc).__name__}: {exc}"
        try:
            actual = lake_latest(market)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        lag_days: int | None = None
        if actual and expected:
            try:
                lag_days = (date.fromisoformat(expected) - date.fromisoformat(actual[:10])).days
            except ValueError:
                lag_days = None
        if error and not (actual or expected):
            status = "insufficient_data"
        elif not actual or not expected:
            status = "insufficient_data"
        elif actual[:10] >= expected:
            status = "fresh"
        else:
            status = "stale"
        statuses.add(status)
        per_market[market] = {
            "expected_as_of_date": expected,
            "lake_latest_trade_date": actual,
            "lag_days": lag_days,
            "status": status,
            "error": error,
        }
    if statuses == {"fresh"}:
        overall = "fresh"
    elif "stale" in statuses:
        overall = "stale"
    else:
        overall = "insufficient_data"
    return {"markets": per_market, "status": overall}


# --------------------------------------------------------------------------- #
# Coverage
# --------------------------------------------------------------------------- #
def _default_probe_fn(market: str, symbols):
    from app.services.price_basis_contract import probe_adjusted_view

    return probe_adjusted_view(market, symbols=symbols, read_bars=True)


def _default_symbols_fn(market: str) -> set[str]:
    from app.services.market_lake import list_lake_symbols

    return list_lake_symbols(market=market)


def _default_actions_fn(market: str):
    from app.services.corporate_actions import load_actions

    return load_actions(market)


def summarize_corporate_actions(records) -> dict:
    """Count stored actions the engine can vs cannot model, by type."""

    supported = 0
    unsupported = 0
    unsupported_by_type: Counter[str] = Counter()
    for record in records:
        action_type = str(getattr(record, "action_type", "") or "").strip().lower()
        if action_type in ENGINE_MODELLED_ACTION_TYPES:
            supported += 1
        else:
            unsupported += 1
            unsupported_by_type[action_type or "unknown"] += 1
    return {
        "loaded": supported + unsupported,
        "supported": supported,
        "unsupported": unsupported,
        "unsupported_by_type": dict(sorted(unsupported_by_type.items())),
    }


def collect_coverage(
    markets: tuple[str, ...] = DEFAULT_MARKETS,
    *,
    probe_fn=None,
    symbols_fn=None,
    actions_fn=None,
    previous: dict | None = None,
) -> dict:
    """Adjusted-view coverage + unsupported corporate-action counts per market."""

    probe = probe_fn or _default_probe_fn
    symbols_of = symbols_fn or _default_symbols_fn
    actions_of = actions_fn or _default_actions_fn
    per_market: dict[str, dict] = {}
    dataful = 0
    for market in markets:
        entry: dict = {"market": market}
        try:
            symbols = symbols_of(market)
        except Exception as exc:
            symbols = None
            entry["symbols_error"] = f"{type(exc).__name__}: {exc}"
        try:
            probe_result = probe(market, symbols or None)
            probe_dict = probe_result.as_dict() if hasattr(probe_result, "as_dict") else dict(probe_result)
            entry["adjusted_view"] = probe_dict
            dataful += 1
        except Exception as exc:
            entry["adjusted_view"] = None
            entry["adjusted_view_error"] = f"{type(exc).__name__}: {exc}"
        entry["symbol_count"] = len(symbols) if symbols is not None else None
        try:
            records = actions_of(market)
            entry["corporate_actions"] = summarize_corporate_actions(records)
            dataful += 1
        except Exception as exc:
            entry["corporate_actions"] = None
            entry["corporate_actions_error"] = f"{type(exc).__name__}: {exc}"
        per_market[market] = entry

    deltas = _coverage_deltas(per_market, previous)
    return {
        "markets": per_market,
        "deltas_vs_previous": deltas,
        "previous_snapshot": bool(previous),
        "status": "ok" if dataful else "insufficient_data",
    }


def _coverage_deltas(current: dict, previous: dict | None) -> dict:
    if not previous:
        return {"status": "baseline", "note": "no previous monitor snapshot to compare against"}
    old_markets = ((previous.get("coverage") or {}).get("markets")) or {}
    deltas: dict[str, dict] = {}
    for market, entry in current.items():
        old = old_markets.get(market) or {}
        old_view = (old.get("adjusted_view") or {}).get("coverage_share")
        new_view = (entry.get("adjusted_view") or {}).get("coverage_share")
        old_unsupported = (old.get("corporate_actions") or {}).get("unsupported")
        new_unsupported = (entry.get("corporate_actions") or {}).get("unsupported")
        deltas[market] = {
            "adjusted_coverage_share": _delta(new_view, old_view),
            "coverage_share_previous": old_view,
            "unsupported_corporate_actions": _delta(new_unsupported, old_unsupported),
            "unsupported_corporate_actions_previous": old_unsupported,
        }
    return {"status": "ok", "markets": deltas}


def _delta(new: object, old: object) -> float | int | None:
    if new is None or old is None:
        return None
    try:
        if isinstance(old, int) and isinstance(new, int):
            return int(new) - int(old)
        return float(new) - float(old)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Fail-closed accounting
# --------------------------------------------------------------------------- #
def _empty_counts() -> dict[str, int]:
    return {category: 0 for category in FAIL_CLOSED_CATEGORIES}


def _price_basis_contracts(blobs: list[dict]) -> list[dict]:
    found: list[dict] = []
    for blob in blobs:
        if not isinstance(blob, dict):
            continue
        for key in ("price_basis_contract", "prediction_price_basis_contract"):
            value = blob.get(key)
            if isinstance(value, dict):
                found.append(value)
    return found


def summarize_run_fail_closed(runs: list[dict]) -> dict:
    """Count fail-closed events recorded in recent run config/summary.

    ``runs`` is a list of ``{"source", "id", "name", "status", "config", "summary"}``
    already decoded to dicts (see :func:`load_run_records`).
    """

    counts = _empty_counts()
    optin_used = {"raw_fallback_allowed": 0, "unmodeled_opt_in": 0}
    audited_optins = 0
    scanned = 0
    for run in runs:
        config = _json_object(run.get("config"))
        summary = _json_object(run.get("summary"))
        blobs = [config, summary]
        scanned += 1
        for contract in _price_basis_contracts(blobs):
            if str(contract.get("decision") or "") != "reject":
                continue
            counts["price_basis_reject"] += 1
            reasons = contract.get("reasons") or []
            if "incomplete_adjusted_coverage" in reasons:
                counts["coverage_block"] += 1
            if "adjusted_view_absent" in reasons:
                counts["raw_fallback_gap_block"] += 1
            if "unreadable_view" in reasons:
                counts["unreadable_view_block"] += 1
        unmodeled = config.get("unmodeled_corporate_actions")
        if not isinstance(unmodeled, list):
            unmodeled = summary.get("unmodeled_corporate_actions")
        opted_in = config.get("unmodeled_opt_in")
        if opted_in is None:
            opted_in = summary.get("unmodeled_opt_in")
        if unmodeled and not opted_in:
            counts["unmodeled_block"] += 1
        if config.get("raw_fallback_allowed") or summary.get("raw_fallback_allowed"):
            optin_used["raw_fallback_allowed"] += 1
        if opted_in:
            optin_used["unmodeled_opt_in"] += 1
        if _find_optin_audit(blobs):
            audited_optins += 1
    return {
        "status": "ok",
        "runs_scanned": scanned,
        "counts": counts,
        "optin_used": optin_used,
        "runs_with_optin_audit": audited_optins,
    }


def _find_optin_audit(blobs: list[dict]) -> dict | None:
    for blob in blobs:
        if not isinstance(blob, dict):
            continue
        for key in ("raw_fallback_optin_audit", "unmodeled_corporate_actions_optin_audit"):
            value = blob.get(key)
            if isinstance(value, dict):
                return value
    return None


def scan_logs(paths, *, tail_bytes: int = DEFAULT_LOG_TAIL_BYTES) -> dict:
    """Count fail-closed markers in the tail of the given log files."""

    counts = _empty_counts()
    scanned: list[str] = []
    missing: list[str] = []
    for raw in paths:
        path = Path(raw)
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists() or not path.is_file():
            missing.append(str(path))
            continue
        try:
            text = _tail_text(path, tail_bytes)
        except OSError:
            missing.append(str(path))
            continue
        scanned.append(str(path))
        for category, markers in FAIL_CLOSED_LOG_MARKERS.items():
            hits = sum(text.count(marker) for marker in markers)
            counts[category] += hits
    return {
        "status": "ok" if scanned else "no_logs",
        "files_scanned": scanned,
        "missing_files": missing,
        "counts": counts,
    }


def _tail_text(path: Path, tail_bytes: int) -> str:
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > tail_bytes:
            handle.seek(size - tail_bytes)
        payload = handle.read()
    return payload.decode("utf-8", errors="replace")


def combine_fail_closed(runs: dict, logs: dict) -> dict:
    counts = _empty_counts()
    for source in (runs.get("counts") or {}, logs.get("counts") or {}):
        for key, value in source.items():
            if key in counts:
                counts[key] += int(value or 0)
    total = sum(counts.values())
    return {"counts": counts, "total": total}


# --------------------------------------------------------------------------- #
# Model drift
# --------------------------------------------------------------------------- #
DRIFT_HIT_RATE_THRESHOLD = 0.05
DRIFT_AVG_RETURN_THRESHOLD = 0.01


def assess_model_drift(evaluations: list[dict]) -> dict:
    """Compare the OOS metrics of the two most recent same-named runs.

    ``evaluations`` items are ``{"name", "run_id", "evaluation_id", "created_at",
    "metrics": [{"horizon_days", "metric_scope", "hit_rate", "avg_return",
    "sample_count"}]}``. Returns ``insufficient_data`` when no model name has two
    evaluated runs.
    """

    by_name: dict[str, dict[int, dict]] = {}
    for item in evaluations:
        name = str(item.get("name") or "").strip()
        run_id = item.get("run_id")
        metrics = [row for row in (item.get("metrics") or []) if isinstance(row, dict)]
        if not name or run_id is None or not metrics:
            continue
        bucket = by_name.setdefault(name, {})
        existing = bucket.get(int(run_id))
        # Keep the newest evaluation per run.
        if existing is None or int(item.get("evaluation_id") or 0) >= int(
            existing.get("evaluation_id") or 0
        ):
            bucket[int(run_id)] = item

    candidates = [
        (max(bucket), name, bucket)
        for name, bucket in by_name.items()
        if len(bucket) >= 2
    ]
    if not candidates:
        return {
            "status": "insufficient_data",
            "note": (
                "fewer than two same-named runs with persisted OOS evaluations; "
                "no drift can be computed"
            ),
            "model_names_seen": sorted(by_name),
            "models_compared": 0,
        }

    _, model_name, bucket = max(candidates)
    ordered = [bucket[run_id] for run_id in sorted(bucket, reverse=True)]
    latest, previous = ordered[0], ordered[1]
    latest_map = _metric_map(latest)
    previous_map = _metric_map(previous)
    horizons = sorted(set(latest_map) | set(previous_map))
    metric_deltas: dict[str, dict] = {}
    drift = False
    for horizon in horizons:
        new_row = latest_map.get(horizon, {})
        old_row = previous_map.get(horizon, {})
        hit_delta = _delta(new_row.get("hit_rate"), old_row.get("hit_rate"))
        ret_delta = _delta(new_row.get("avg_return"), old_row.get("avg_return"))
        metric_deltas[str(horizon)] = {
            "hit_rate_latest": new_row.get("hit_rate"),
            "hit_rate_previous": old_row.get("hit_rate"),
            "hit_rate_delta": hit_delta,
            "avg_return_latest": new_row.get("avg_return"),
            "avg_return_previous": old_row.get("avg_return"),
            "avg_return_delta": ret_delta,
            "sample_count_latest": new_row.get("sample_count"),
            "sample_count_previous": old_row.get("sample_count"),
        }
        if hit_delta is not None and abs(hit_delta) > DRIFT_HIT_RATE_THRESHOLD:
            drift = True
        if ret_delta is not None and abs(ret_delta) > DRIFT_AVG_RETURN_THRESHOLD:
            drift = True
    return {
        "status": "ok",
        "model_name": model_name,
        "models_compared": 1,
        "latest_run_id": int(latest["run_id"]),
        "previous_run_id": int(previous["run_id"]),
        "latest_evaluation_id": int(latest.get("evaluation_id") or 0),
        "previous_evaluation_id": int(previous.get("evaluation_id") or 0),
        "thresholds": {
            "hit_rate_abs": DRIFT_HIT_RATE_THRESHOLD,
            "avg_return_abs": DRIFT_AVG_RETURN_THRESHOLD,
        },
        "metrics": metric_deltas,
        "drift": drift,
    }


def _metric_map(item: dict) -> dict[int, dict]:
    result: dict[int, dict] = {}
    for row in item.get("metrics") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("metric_scope") or "overall") != "overall":
            continue
        try:
            horizon = int(row.get("horizon_days"))
        except (TypeError, ValueError):
            continue
        result[horizon] = row
    return result


# --------------------------------------------------------------------------- #
# DB loaders (read-only; degrade to insufficient_data on any failure)
# --------------------------------------------------------------------------- #
def load_run_records(limit: int = DEFAULT_RUN_LIMIT) -> dict:
    try:
        from app.core.db import SessionLocal
        from app.models.tables import ModelRun, StrategyRun
        from sqlalchemy import select
    except Exception as exc:
        return {"status": "insufficient_data", "note": f"db unavailable: {exc}", "runs": []}
    runs: list[dict] = []
    try:
        with SessionLocal() as db:
            model_rows = db.scalars(
                select(ModelRun).order_by(ModelRun.id.desc()).limit(limit)
            ).all()
            strategy_rows = db.scalars(
                select(StrategyRun).order_by(StrategyRun.id.desc()).limit(limit)
            ).all()
            for row in model_rows:
                runs.append(
                    {
                        "source": "model_run",
                        "id": row.id,
                        "name": row.name,
                        "status": row.status,
                        "config": _json_object(row.config_json),
                        "summary": {},
                    }
                )
            for row in strategy_rows:
                runs.append(
                    {
                        "source": "strategy_run",
                        "id": row.id,
                        "name": row.name,
                        "status": row.status,
                        "config": _json_object(row.config_json),
                        "summary": _json_object(row.summary_json),
                    }
                )
    except Exception as exc:
        return {"status": "insufficient_data", "note": f"db query failed: {exc}", "runs": []}
    if not runs:
        return {"status": "insufficient_data", "note": "no model/strategy runs found", "runs": []}
    return {"status": "ok", "runs": runs}


def load_model_evaluations(limit_runs: int = 40) -> dict:
    try:
        from app.core.db import SessionLocal
        from app.models.tables import ModelEvaluation, ModelEvaluationMetric, ModelRun
        from sqlalchemy import select
    except Exception as exc:
        return {"status": "insufficient_data", "note": f"db unavailable: {exc}", "evaluations": []}
    try:
        with SessionLocal() as db:
            rows = db.execute(
                select(ModelEvaluation, ModelRun.name)
                .join(ModelRun, ModelRun.id == ModelEvaluation.model_run_id)
                .order_by(ModelEvaluation.id.desc())
                .limit(max(1, int(limit_runs)) * 4)
            ).all()
            evaluations: list[dict] = []
            for evaluation, name in rows:
                metric_rows = db.scalars(
                    select(ModelEvaluationMetric).where(
                        ModelEvaluationMetric.model_evaluation_id == evaluation.id
                    )
                ).all()
                evaluations.append(
                    {
                        "name": name,
                        "run_id": evaluation.model_run_id,
                        "evaluation_id": evaluation.id,
                        "created_at": evaluation.created_at,
                        "metrics": [
                            {
                                "horizon_days": metric.horizon_days,
                                "metric_scope": metric.metric_scope,
                                "hit_rate": metric.hit_rate,
                                "avg_return": metric.avg_return,
                                "sample_count": metric.sample_count,
                            }
                            for metric in metric_rows
                        ],
                    }
                )
    except Exception as exc:
        return {"status": "insufficient_data", "note": f"db query failed: {exc}", "evaluations": []}
    if not evaluations:
        return {"status": "insufficient_data", "note": "no model evaluations found", "evaluations": []}
    return {"status": "ok", "evaluations": evaluations}


# --------------------------------------------------------------------------- #
# Report assembly
# --------------------------------------------------------------------------- #
def build_report(
    *,
    markets: tuple[str, ...] = DEFAULT_MARKETS,
    now: datetime | None = None,
    output_dir: Path,
    lake_latest_fn=None,
    probe_fn=None,
    symbols_fn=None,
    actions_fn=None,
    run_records: dict | None = None,
    evaluations: dict | None = None,
    log_paths=None,
) -> dict:
    previous = _read_json(output_dir / "quant-risk-monitor.json")
    staleness = collect_staleness(markets, now=now, lake_latest_fn=lake_latest_fn)
    coverage = collect_coverage(
        markets, probe_fn=probe_fn, symbols_fn=symbols_fn, actions_fn=actions_fn, previous=previous
    )
    run_records = run_records if run_records is not None else load_run_records()
    run_fail_closed = summarize_run_fail_closed(run_records.get("runs") or [])
    if run_records.get("status") != "ok":
        run_fail_closed = {
            "status": "insufficient_data",
            "note": run_records.get("note"),
            "runs_scanned": 0,
            "counts": _empty_counts(),
            "optin_used": {"raw_fallback_allowed": 0, "unmodeled_opt_in": 0},
            "runs_with_optin_audit": 0,
        }
    logs = scan_logs(log_paths or DEFAULT_LOG_PATHS)
    fail_closed = {
        "from_runs": run_fail_closed,
        "from_logs": logs,
        "combined": combine_fail_closed(run_fail_closed, logs),
        "status": "ok"
        if run_fail_closed["status"] == "ok" or logs["status"] == "ok"
        else "insufficient_data",
    }
    evaluations = evaluations if evaluations is not None else load_model_evaluations()
    if evaluations.get("status") != "ok":
        drift = {
            "status": "insufficient_data",
            "note": evaluations.get("note"),
            "models_compared": 0,
        }
    else:
        drift = assess_model_drift(evaluations.get("evaluations") or [])
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": (now or _utc_now()).astimezone(timezone.utc).isoformat(),
        "output_dir": str(output_dir),
        "markets": list(markets),
        "data_staleness": staleness,
        "coverage": coverage,
        "fail_closed": fail_closed,
        "model_drift": drift,
    }


def render_markdown(report: dict) -> str:
    lines: list[str] = []
    lines.append("# Quant Risk Monitor")
    lines.append("")
    lines.append(f"- schema: `{report.get('schema_version')}`")
    lines.append(f"- generated_at (UTC): `{report.get('generated_at')}`")
    lines.append(f"- markets: {', '.join(report.get('markets') or [])}")
    lines.append("")

    lines.append("## Data staleness")
    lines.append("")
    staleness = report.get("data_staleness") or {}
    lines.append(f"Status: **{staleness.get('status')}**")
    lines.append("")
    lines.append("| Market | Expected | Lake latest | Lag (days) | Status |")
    lines.append("| --- | --- | --- | --- | --- |")
    for market, entry in (staleness.get("markets") or {}).items():
        lines.append(
            f"| {market} | {entry.get('expected_as_of_date')} | "
            f"{entry.get('lake_latest_trade_date')} | {entry.get('lag_days')} | "
            f"{entry.get('status')} |"
        )
    lines.append("")

    lines.append("## Coverage")
    lines.append("")
    coverage = report.get("coverage") or {}
    lines.append(f"Status: **{coverage.get('status')}**")
    lines.append("")
    lines.append("| Market | View state | Coverage share | Symbols | Unsupported actions |")
    lines.append("| --- | --- | --- | --- | --- |")
    for market, entry in (coverage.get("markets") or {}).items():
        view = entry.get("adjusted_view") or {}
        actions = entry.get("corporate_actions") or {}
        lines.append(
            f"| {market} | {view.get('state')} | {view.get('coverage_share')} | "
            f"{entry.get('symbol_count')} | {actions.get('unsupported')} |"
        )
    deltas = (coverage.get("deltas_vs_previous") or {}).get("markets") or {}
    if deltas:
        lines.append("")
        lines.append("| Market | Coverage share delta | Unsupported actions delta |")
        lines.append("| --- | --- | --- |")
        for market, delta in deltas.items():
            lines.append(
                f"| {market} | {delta.get('adjusted_coverage_share')} | "
                f"{delta.get('unsupported_corporate_actions')} |"
            )
    lines.append("")

    lines.append("## Fail-closed events")
    lines.append("")
    fc = report.get("fail_closed") or {}
    lines.append(f"Status: **{fc.get('status')}**")
    lines.append("")
    lines.append("| Gate | From runs | From logs | Combined |")
    lines.append("| --- | --- | --- | --- |")
    run_counts = (fc.get("from_runs") or {}).get("counts") or {}
    log_counts = (fc.get("from_logs") or {}).get("counts") or {}
    combined = (fc.get("combined") or {}).get("counts") or {}
    for category in FAIL_CLOSED_CATEGORIES:
        lines.append(
            f"| {category} | {run_counts.get(category, 0)} | "
            f"{log_counts.get(category, 0)} | {combined.get(category, 0)} |"
        )
    lines.append("")
    lines.append(
        f"- runs scanned: {(fc.get('from_runs') or {}).get('runs_scanned')}"
    )
    lines.append(
        f"- log files scanned: {len((fc.get('from_logs') or {}).get('files_scanned') or [])}"
    )
    lines.append("")

    lines.append("## Model drift")
    lines.append("")
    drift = report.get("model_drift") or {}
    lines.append(f"Status: **{drift.get('status')}**")
    if drift.get("note"):
        lines.append("")
        lines.append(f"> {drift['note']}")
    if drift.get("status") == "ok":
        lines.append("")
        lines.append(
            f"Model `{drift.get('model_name')}`: run {drift.get('previous_run_id')} -> "
            f"{drift.get('latest_run_id')}; drift={drift.get('drift')}"
        )
        lines.append("")
        lines.append("| Horizon | hit_rate prev | hit_rate latest | delta | avg_return prev | avg_return latest | delta |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for horizon, metric in (drift.get("metrics") or {}).items():
            lines.append(
                f"| {horizon} | {metric.get('hit_rate_previous')} | "
                f"{metric.get('hit_rate_latest')} | {metric.get('hit_rate_delta')} | "
                f"{metric.get('avg_return_previous')} | {metric.get('avg_return_latest')} | "
                f"{metric.get('avg_return_delta')} |"
            )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--markets", default=",".join(DEFAULT_MARKETS))
    parser.add_argument("--run-limit", type=int, default=DEFAULT_RUN_LIMIT)
    parser.add_argument("--log", action="append", default=None, help="extra log path to scan")
    parser.add_argument("--json-name", default="quant-risk-monitor.json")
    parser.add_argument("--md-name", default="quant-risk-monitor.md")
    args = parser.parse_args(argv)

    markets = tuple(
        market.strip().upper() for market in str(args.markets).split(",") if market.strip()
    ) or DEFAULT_MARKETS
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    log_paths = list(DEFAULT_LOG_PATHS) + list(args.log or [])

    report = build_report(
        markets=markets,
        output_dir=output_dir,
        run_records=load_run_records(limit=max(1, int(args.run_limit))),
        evaluations=load_model_evaluations(),
        log_paths=log_paths,
    )
    json_path = output_dir / args.json_name
    md_path = output_dir / args.md_name
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "markdown": str(md_path), "summary": _headline(report)}, indent=2))
    return 0


def _headline(report: dict) -> dict:
    return {
        "data_staleness": (report.get("data_staleness") or {}).get("status"),
        "coverage": (report.get("coverage") or {}).get("status"),
        "fail_closed": (report.get("fail_closed") or {}).get("status"),
        "model_drift": (report.get("model_drift") or {}).get("status"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
