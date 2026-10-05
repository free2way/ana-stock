"""Independent, traceable storage for HiThink public featured-data/auction pulls.

Records are written under ``<artifacts_dir>/hithink_features/<name>/date=....json``
and are deliberately kept out of the main CN price lake so the v1 market-data basis
is never silently widened. Every record keeps the provider, endpoint/query identity,
fetch time and a content hash of the raw payload for point-in-time traceability.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from app.core.config import get_settings


HITHINK_FEATURE_SCHEMA_VERSION = "hithink_feature_v1"
HITHINK_FEATURE_SUBDIR = "hithink_features"
_REQUIRED_PROVENANCE_FIELDS = ("provider", "source_reference", "fetched_at")


class HithinkFeatureStoreError(RuntimeError):
    """Raised when a HiThink feature record cannot be persisted or read."""


@dataclass(frozen=True, slots=True)
class HithinkFeatureWriteResult:
    path: Path
    content_sha256: str
    reused_existing: bool


def hithink_feature_root(root: Path | None = None) -> Path:
    base = Path(root) if root is not None else Path(get_settings().artifacts_dir)
    return base / HITHINK_FEATURE_SUBDIR


def canonical_content_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_feature_name(name: str) -> str:
    text = str(name or "").strip()
    if not text or text in {".", ".."} or any(ch in text for ch in ("/", "\\", ":", "\0")):
        raise HithinkFeatureStoreError(f"invalid hithink feature name: {name!r}")
    return text


def _safe_slot(slot: str | None) -> str | None:
    if slot is None:
        return None
    text = str(slot).strip()
    if not text or not all(ch.isalnum() or ch in "_-" for ch in text):
        raise HithinkFeatureStoreError(f"invalid hithink feature slot: {slot!r}")
    return text


def _record_filename(trade_date: str, slot: str | None) -> str:
    return f"date={trade_date}{f'.{slot}' if slot else ''}.json"


def _read_record(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HithinkFeatureStoreError(f"unreadable hithink feature record: {path}") from exc
    if not isinstance(payload, dict):
        raise HithinkFeatureStoreError(f"hithink feature record is not an object: {path}")
    return payload


def persist_hithink_feature(
    *,
    name: str,
    trade_date: str | date,
    payload: Mapping[str, Any],
    root: Path | None = None,
    slot: str | None = None,
) -> HithinkFeatureWriteResult:
    """Persist one client pull with provenance and a content hash.

    Re-persisting identical content is idempotent; a differing payload for the
    same ``(name, date, slot)`` is refused rather than silently overwritten.
    """
    feature_name = _safe_feature_name(name)
    slot_value = _safe_slot(slot)
    day = trade_date.isoformat() if isinstance(trade_date, date) else str(trade_date or "")[:10]
    if not day:
        raise HithinkFeatureStoreError("trade_date is required")
    if not isinstance(payload, Mapping):
        raise HithinkFeatureStoreError("payload must be a mapping")
    for provenance_field in _REQUIRED_PROVENANCE_FIELDS:
        if not str(payload.get(provenance_field) or "").strip():
            raise HithinkFeatureStoreError(
                f"payload is missing provenance field: {provenance_field}"
            )
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise HithinkFeatureStoreError("payload is missing a raw 'data' object")
    content_sha256 = canonical_content_sha256(dict(data))
    record = {
        "schema_version": HITHINK_FEATURE_SCHEMA_VERSION,
        "feature_name": feature_name,
        "trade_date": day,
        "slot": slot_value,
        "provider": str(payload["provider"]),
        "endpoint": payload.get("endpoint"),
        "source_reference": str(payload["source_reference"]),
        "fetched_at": str(payload["fetched_at"]),
        "request_id": payload.get("request_id"),
        "request_ids": payload.get("request_ids"),
        "params": dict(payload.get("params") or {}),
        "content_sha256": content_sha256,
        "data": dict(data),
    }
    target_dir = hithink_feature_root(root) / feature_name
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / _record_filename(day, slot_value)
    if target.exists():
        existing = _read_record(target)
        if existing.get("content_sha256") != content_sha256:
            raise HithinkFeatureStoreError(
                f"refusing to overwrite hithink feature {target.name} with different content"
            )
        return HithinkFeatureWriteResult(
            path=target, content_sha256=content_sha256, reused_existing=True
        )
    serialized = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(serialized, encoding="utf-8")
    os.replace(temporary, target)
    return HithinkFeatureWriteResult(
        path=target, content_sha256=content_sha256, reused_existing=False
    )


def load_hithink_feature_records(
    *, root: Path | None = None, name: str | None = None
) -> list[dict]:
    """Load stored records, failing closed on a content-hash mismatch."""
    base = hithink_feature_root(root)
    directory_filter = _safe_feature_name(name) if name is not None else None
    records: list[dict] = []
    if not base.exists():
        return records
    for path in sorted(base.glob("*/date=*.json")):
        if directory_filter is not None and path.parent.name != directory_filter:
            continue
        record = _read_record(path)
        if record.get("content_sha256") != canonical_content_sha256(record.get("data")):
            raise HithinkFeatureStoreError(f"hithink feature content hash mismatch: {path}")
        records.append(record)
    return records
