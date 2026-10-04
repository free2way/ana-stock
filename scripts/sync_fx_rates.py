#!/usr/bin/env python3
"""Sync the S-12 operator FX table from SAFE RMB central parity (AKShare).

Source (free, official): ``ak.currency_boc_safe()`` — 国家外汇管理局人民币汇率
中间价.  Each column quotes CNY per **100** units of the foreign currency, so the
raw values are divided by 100 to match the ``app/services/fx_rates.py`` contract
(CNY per 1 unit of the foreign currency).

Outputs
-------
* ``AppSetting`` key ``fx_rate_table`` — the single latest table consumed by
  ``app/services/fx_rates.py`` (``{base, as_of, source, rates}``).
* Versioned snapshots under ``<artifacts>/fx_rates/`` — ``AppSetting`` keeps only
  the latest value, so history lives here as
  ``fx_rate_table_as_of_<date>.json`` (table + ``content_sha256``) plus an
  append-only ``history.json`` index of every ``(as_of, source, sha256)``.

Usage
-----
    python scripts/sync_fx_rates.py                       # latest SAFE quote
    python scripts/sync_fx_rates.py --as-of 2026-09-30    # exact/historical date
    python scripts/sync_fx_rates.py --as-of 2026-09-30 --dry-run
    python scripts/sync_fx_rates.py --as-of 2026-09-30 --no-db   # artifact only

``--as-of`` accepts any calendar date: if SAFE published no quote that day
(weekend/holiday) the most recent earlier quote is used and the report marks
``exact_match=false``.  Re-running for the same quote date is idempotent: the
same bytes, hash and history entry are produced.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.fx_rates import FX_RATE_TABLE_KEY  # noqa: E402

SOURCE_LABEL = "safe_rmb_central_parity/akshare.currency_boc_safe"
DATE_COLUMN = "日期"
# Foreign currency -> SAFE column name.  SAFE quotes 100 foreign units -> CNY.
CURRENCY_COLUMNS = {"USD": "美元", "HKD": "港元"}
BASE_CURRENCY = "CNY"
RATE_DECIMALS = 6
ARTIFACT_SCHEMA_VERSION = "fx_rate_table_snapshot_v1"
HISTORY_SCHEMA_VERSION = "fx_rate_history_v1"
DEFAULT_ARTIFACT_SUBDIR = "fx_rates"
DEFAULT_HISTORY_FILENAME = "history.json"


class FxSyncError(RuntimeError):
    """Raised for any unrecoverable fetch/normalization failure."""


def _clean_date(value) -> str:
    if value is None:
        raise FxSyncError("SAFE frame has an empty quote date")
    text = str(value).strip()
    if not text:
        raise FxSyncError("SAFE frame has an empty quote date")
    return text.replace("/", "-")[:10]


def _per_unit_rate(value, currency: str, column: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise FxSyncError(f"non-numeric {currency} ({column}) quote: {value!r}") from None
    if not math.isfinite(number) or number <= 0:
        raise FxSyncError(f"invalid {currency} ({column}) quote: {value!r}")
    return round(number / 100.0, RATE_DECIMALS)


def fetch_safe_midrates_with_attempts(*, retries: int = 4, base_delay: float = 2.0):
    """Fetch ``ak.currency_boc_safe()`` with bounded retries.

    The local MITM proxy (``HTTP_PROXY=127.0.0.1:7890``) and safesvc hosts are
    known to emit transient ``SSL``/``ProxyError``; akshare's requests clients
    honor the proxy env vars.  We retry a small number of times and never fall
    back to a cached/fabricated value — a hard failure is reported as such.

    Returns ``(frame, attempts_used)``.
    """
    import akshare as ak  # type: ignore

    max_attempts = max(1, retries)
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            frame = ak.currency_boc_safe()
        except Exception as exc:  # transient network / proxy / SSL
            last_error = exc
        else:
            if frame is not None and not frame.empty:
                return frame, attempt
            last_error = FxSyncError("currency_boc_safe returned an empty frame")
        if attempt < max_attempts:
            time.sleep(base_delay * attempt)
    raise FxSyncError(
        f"currency_boc_safe failed after {max_attempts} attempts: "
        f"{type(last_error).__name__}: {last_error}"
    )


def fetch_safe_midrates(*, retries: int = 4, base_delay: float = 2.0):
    frame, _attempts = fetch_safe_midrates_with_attempts(retries=retries, base_delay=base_delay)
    return frame



def _normalized_rows(frame) -> list[dict]:
    raw_columns = getattr(frame, "columns", None)
    columns = set(raw_columns) if raw_columns is not None else set()
    if DATE_COLUMN not in columns:
        raise FxSyncError(f"SAFE frame is missing the {DATE_COLUMN!r} column")
    missing = [column for column in CURRENCY_COLUMNS.values() if column not in columns]
    if missing:
        raise FxSyncError(f"SAFE frame is missing currency columns: {', '.join(missing)}")
    rows: list[dict] = []
    for record in frame.to_dict("records"):
        try:
            quote_date = _clean_date(record.get(DATE_COLUMN))
        except FxSyncError:
            continue
        rates: dict[str, float] = {}
        valid = True
        for currency, column in CURRENCY_COLUMNS.items():
            try:
                rates[currency] = _per_unit_rate(record.get(column), currency, column)
            except FxSyncError:
                valid = False
                break
        if valid:
            rows.append({"date": quote_date, "rates": rates})
    if not rows:
        raise FxSyncError("SAFE frame contains no usable USD/HKD quotes")
    rows.sort(key=lambda item: item["date"])
    return rows


def build_fx_table_payload(frame, *, as_of: str | None = None) -> tuple[dict, dict]:
    """Normalize a SAFE frame into the ``fx_rate_table`` payload (pure).

    Returns ``(table, meta)`` where ``table`` is exactly
    ``{base, as_of, source, rates}`` and ``meta`` records
    ``requested_as_of``/``quote_date``/``exact_match``/``rows``.
    """
    rows = _normalized_rows(frame)
    requested = (str(as_of).strip()[:10] if as_of else None) or None
    if requested:
        candidates = [row for row in rows if row["date"] <= requested]
        if not candidates:
            raise FxSyncError(
                f"no SAFE quote on or before {requested}; earliest available is {rows[0]['date']}"
            )
        selected = candidates[-1]
    else:
        selected = rows[-1]
    quote_date = selected["date"]
    table = {
        "base": BASE_CURRENCY,
        "as_of": quote_date,
        "source": SOURCE_LABEL,
        "rates": dict(selected["rates"]),
    }
    meta = {
        "requested_as_of": requested,
        "quote_date": quote_date,
        "exact_match": requested is None or requested == quote_date,
        "rows": len(rows),
        "first_quote_date": rows[0]["date"],
        "last_quote_date": rows[-1]["date"],
        "raw_100_unit": {
            currency: round(rate * 100.0, 6) for currency, rate in selected["rates"].items()
        },
        "source": SOURCE_LABEL,
    }
    return table, meta


def table_content_hash(table: dict) -> str:
    """Stable content hash of the canonical table (order-insensitive)."""
    canonical = json.dumps(table, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def default_artifact_dir() -> Path:
    from app.core.config import get_settings

    return get_settings().artifacts_dir / DEFAULT_ARTIFACT_SUBDIR


def _load_history(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": HISTORY_SCHEMA_VERSION, "entries": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema_version": HISTORY_SCHEMA_VERSION, "entries": []}
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        return {"schema_version": HISTORY_SCHEMA_VERSION, "entries": []}
    return payload


def write_artifact(
    table: dict,
    *,
    artifact_dir: Path,
    meta: dict | None = None,
    created_at: str | None = None,
) -> dict:
    """Write the versioned snapshot + history index idempotently.

    Re-running for an identical table leaves the existing snapshot bytes and
    ``created_at`` untouched and does not duplicate the history entry.
    """
    artifact_dir = Path(artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    content_sha256 = table_content_hash(table)
    snapshot_path = artifact_dir / f"fx_rate_table_as_of_{table['as_of']}.json"
    history_path = artifact_dir / DEFAULT_HISTORY_FILENAME
    timestamp = created_at or datetime.now(tz=timezone.utc).isoformat()

    written = False
    if snapshot_path.exists():
        try:
            existing = json.loads(snapshot_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = {}
        if existing.get("content_sha256") != content_sha256:
            written = True
    else:
        written = True
    if written:
        payload = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "as_of": table["as_of"],
            "base": table["base"],
            "source": table["source"],
            "content_sha256": content_sha256,
            "created_at": timestamp,
            "table": table,
            "meta": meta or {},
        }
        snapshot_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )

    history = _load_history(history_path)
    entry = {
        "as_of": table["as_of"],
        "base": table["base"],
        "source": table["source"],
        "content_sha256": content_sha256,
        "snapshot": snapshot_path.name,
        "created_at": timestamp,
        "rates": table["rates"],
    }
    entries = [
        item
        for item in history["entries"]
        if not (item.get("as_of") == entry["as_of"] and item.get("content_sha256") == content_sha256)
    ]
    entries.append(entry)
    entries.sort(key=lambda item: (str(item.get("as_of") or ""), str(item.get("content_sha256") or "")))
    history = {"schema_version": HISTORY_SCHEMA_VERSION, "entries": entries}
    history_path.write_text(
        json.dumps(history, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    return {
        "snapshot_path": str(snapshot_path),
        "history_path": str(history_path),
        "content_sha256": content_sha256,
        "written": written,
        "history_entries": len(entries),
    }


def write_app_setting(table: dict) -> None:
    """Persist the latest table through the existing AppSetting read/write path."""
    from app.core.db import SessionLocal, init_db
    from app.services.repository import AppSettingRepository

    init_db()
    with SessionLocal() as db:
        AppSettingRepository(db).set(
            FX_RATE_TABLE_KEY, json.dumps(table, ensure_ascii=False, sort_keys=True)
        )


def sync_fx_rates(
    *,
    as_of: str | None = None,
    frame=None,
    artifact_dir: Path | None = None,
    write_setting: bool = True,
    retries: int = 4,
    base_delay: float = 2.0,
) -> dict:
    """Fetch (or accept) a SAFE frame, normalize it and persist both outputs."""
    errors: list[str] = []
    fetch_attempts = 0
    if frame is None:
        frame, fetch_attempts = fetch_safe_midrates_with_attempts(
            retries=retries, base_delay=base_delay
        )

    table, meta = build_fx_table_payload(frame, as_of=as_of)
    result = {
        "status": "ok",
        "table": table,
        "meta": meta,
        "fetch_attempts": fetch_attempts,
        "artifact": None,
        "setting_written": False,
        "errors": errors,
    }
    target_dir = Path(artifact_dir) if artifact_dir is not None else default_artifact_dir()
    result["artifact"] = write_artifact(table, artifact_dir=target_dir, meta=meta)
    if write_setting:
        write_app_setting(table)
        result["setting_written"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--as-of", default=None, help="quote date YYYY-MM-DD (default: latest SAFE row)")
    parser.add_argument("--artifact-dir", default=None, help="override the versioned snapshot directory")
    parser.add_argument("--no-db", action="store_true", help="write only the versioned artifact, not AppSetting")
    parser.add_argument("--dry-run", action="store_true", help="fetch + normalize, write nothing")
    parser.add_argument("--retries", type=int, default=4, help="fetch attempts before giving up (default 4)")
    parser.add_argument("--base-delay", type=float, default=2.0, help="linear backoff seconds between retries")
    parser.add_argument("--output", default=None, help="optional path for the JSON report")
    args = parser.parse_args(argv)

    try:
        if args.dry_run:
            frame, attempts = fetch_safe_midrates_with_attempts(
                retries=max(1, args.retries), base_delay=max(0.0, args.base_delay)
            )
            table, meta = build_fx_table_payload(frame, as_of=args.as_of)
            report = {
                "status": "dry_run",
                "table": table,
                "meta": meta,
                "fetch_attempts": attempts,
                "artifact": None,
                "setting_written": False,
                "errors": [],
            }
        else:
            report = sync_fx_rates(
                as_of=args.as_of,
                artifact_dir=Path(args.artifact_dir) if args.artifact_dir else None,
                write_setting=not args.no_db,
                retries=max(1, args.retries),
                base_delay=max(0.0, args.base_delay),
            )
    except FxSyncError as exc:
        report = {"status": "failed", "error": str(exc)}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    payload = {
        "schema_version": "fx_rate_sync_report_v1",
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "report": report,
    }
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
