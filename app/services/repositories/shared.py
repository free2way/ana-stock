"""Shared constants and low-level helpers used by all repository domains."""

import json
import time
from datetime import UTC, date, datetime

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.tables import ModelRun, Symbol
from app.services.market_storage_routing import (
    legacy_mirror_write_enabled,
    physical_fact_write_markets,
    physical_only_cutover_active,
    physical_snapshot_models,
)
from app.services.time_utils import app_now_iso


DECOMMISSIONED_CN_REVIEW_JOB_TYPE = "cn" + "_close" + "_review"

# Operational candidate views must not infer the production champion from the
# largest model-run id. Challenger and historical-research runs share the same
# audit tables but are never serving models until an explicit champion switch.
PRODUCTION_SIGNAL_MODEL_TYPES = ("lightgbm_multifactor",)

JOB_MESSAGE_MAX_CHARS = 16_000

def _bounded_job_message(message: str | None) -> str | None:
    if message is None:
        return None
    normalized = str(message)
    if len(normalized) <= JOB_MESSAGE_MAX_CHARS:
        return normalized
    tail_size = 2_000
    omitted = len(normalized) - JOB_MESSAGE_MAX_CHARS
    marker = (
        f"\n...[{omitted} characters omitted; full error is stored in the result artifact]...\n"
    )
    head_size = JOB_MESSAGE_MAX_CHARS - tail_size - len(marker)
    return (
        f"{normalized[:head_size]}"
        f"{marker}"
        f"{normalized[-tail_size:]}"
    )

def utc_now_iso() -> str:
    return app_now_iso()

def _physical_snapshot_tables_for_market(market: str | None):
    market_code = str(market or "").strip().upper()
    if market_code not in physical_fact_write_markets():
        return None
    return physical_snapshot_models(market_code)

def _legacy_snapshot_writes_enabled(
    db: Session,
    physical_tables,
    *,
    market: str | None = "CN",
) -> bool:
    if physical_tables is None:
        return True
    return legacy_mirror_write_enabled(
        db,
        market=market,
        configured=bool(
            getattr(
                get_settings(),
                "market_physical_snapshot_dual_write_legacy",
                True,
            )
        ),
    )

def _symbol_market(db: Session, symbol_id: int) -> str:
    market = str(
        db.scalar(select(Symbol.market).where(Symbol.id == int(symbol_id))) or ""
    ).strip().upper()
    if market not in {"CN", "US"}:
        return market
    return market

def _assert_legacy_prediction_write_allowed(
    db: Session,
    *,
    model_run_id: int,
) -> None:
    """Fail closed once a market has switched to physical-only publication.

    Legacy repositories remain available during the observed dual-write phase
    and for rollback.  After the database cutover marker is active, however,
    an old importer must not silently repopulate the shared fact tables.
    """

    market = str(
        db.scalar(
            select(ModelRun.market).where(ModelRun.id == int(model_run_id))
        )
        or ""
    ).strip().upper()
    if market in physical_fact_write_markets() and physical_only_cutover_active(
        db,
        market,
    ):
        raise RuntimeError(
            f"Legacy shared prediction writes are disabled for {market} model "
            f"run {int(model_run_id)} after physical-only cutover."
        )

def _physical_date(value: str | date | None, *, nullable: bool = False) -> date | None:
    if value is None or not str(value).strip():
        if nullable:
            return None
        raise ValueError("Physical market facts require a valid date.")
    try:
        if isinstance(value, datetime):
            return value.date()
        return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValueError(f"Invalid physical market-fact date: {value!r}") from exc

def _physical_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = _safe_parse_iso(str(value))
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Invalid timezone-aware market-fact timestamp: {value!r}")
    return parsed

def _loads_json_object(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None

def _loads_json_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []

def _safe_parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None

def _job_duration_seconds(started_at: str | None, finished_at: str | None) -> int | None:
    started = _safe_parse_iso(started_at)
    finished = _safe_parse_iso(finished_at)
    if started is None or finished is None:
        return None
    if started.tzinfo is None and finished.tzinfo is not None:
        started = started.replace(tzinfo=finished.tzinfo)
    elif started.tzinfo is not None and finished.tzinfo is None:
        finished = finished.replace(tzinfo=started.tzinfo)
    elif started.tzinfo is not None and finished.tzinfo is not None:
        started = started.astimezone(UTC)
        finished = finished.astimezone(UTC)
    return max(0, int((finished - started).total_seconds()))

def ticker_query_candidates(ticker: str) -> list[str]:
    normalized = ticker.strip().upper()
    candidates = [normalized]
    if normalized.endswith(".HK"):
        core = normalized[:-3]
        if core.isdigit():
            raw = core.lstrip("0") or "0"
            for width in (4, 5):
                candidate = f"{raw.zfill(width)}.HK"
                if candidate not in candidates:
                    candidates.append(candidate)
    return candidates

def chunked_ids(values: list[int], size: int = 500) -> list[list[int]]:
    if size < 1:
        size = 1
    return [values[index : index + size] for index in range(0, len(values), size)]

def chunked_rows(values: list[dict], size: int = 100) -> list[list[dict]]:
    if size < 1:
        size = 1
    return [values[index : index + size] for index in range(0, len(values), size)]

def market_sort_case(column):
    return case(
        (column == "CN", 0),
        (column == "HK", 1),
        (column == "US", 2),
        else_=9,
    )

def _is_database_locked_error(exc: Exception) -> bool:
    return "database is locked" in str(exc).lower()

def _sleep_for_lock_retry(attempt: int) -> None:
    time.sleep(min(0.2 * attempt, 1.0))
