"""Append-only experiment registry (E-7) with hash-chain tamper detection.

Every promotion attempt appends one JSON line; each entry carries
``prev_hash``/``entry_hash`` so any edit, deletion or reordering breaks the
chain and is refused on load. The promotion manifest carries the attempt
counts and the dataset hash consulted from this registry.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "stock_selection_experiment_registry_v1"


class RegistryTamperedError(RuntimeError):
    """Raised when the registry hash chain does not validate."""


def default_registry_path(root: Path | None = None) -> Path:
    base = Path(root) if root is not None else Path("data") / "experiments"
    return base / "stock_selection_registry.jsonl"


def _entry_hash(previous_hash: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{previous_hash}|{canonical}".encode("utf-8")).hexdigest()


def load_attempts(path: Path | None = None) -> list[dict[str, Any]]:
    """Read and validate the registry; raises RegistryTamperedError on any break."""

    registry = Path(path) if path is not None else default_registry_path()
    if not registry.exists():
        return []
    entries: list[dict[str, Any]] = []
    previous_hash = ""
    for line_number, line in enumerate(registry.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RegistryTamperedError(f"registry line {line_number} is not valid JSON") from exc
        stored_hash = str(entry.pop("entry_hash", ""))
        recorded_prev = str(entry.get("prev_hash") or "")
        if recorded_prev != previous_hash:
            raise RegistryTamperedError(f"registry line {line_number} breaks the hash chain")
        if stored_hash != _entry_hash(previous_hash, entry):
            raise RegistryTamperedError(f"registry line {line_number} was modified")
        entries.append({**entry, "entry_hash": stored_hash})
        previous_hash = stored_hash
    return entries


def record_attempt(
    *,
    model_key: str,
    dataset_hash: str,
    protocol_id: str,
    verdict: str,
    metrics: dict[str, Any] | None = None,
    rejected: bool | None = None,
    path: Path | None = None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Append one attempt; validates the existing chain before appending."""

    registry = Path(path) if path is not None else default_registry_path()
    existing = load_attempts(registry)
    previous_hash = existing[-1]["entry_hash"] if existing else ""
    is_rejected = bool(rejected) if rejected is not None else str(verdict).upper() not in {"PASS", "PROMOTE"}
    entry = {
        "schema_version": SCHEMA_VERSION,
        "recorded_at": recorded_at or datetime.now(tz=timezone.utc).isoformat(),
        "model_key": str(model_key),
        "dataset_hash": str(dataset_hash),
        "protocol_id": str(protocol_id),
        "verdict": str(verdict),
        "rejected": is_rejected,
        "metrics": metrics or {},
        "prev_hash": previous_hash,
    }
    entry["entry_hash"] = _entry_hash(previous_hash, entry)
    registry.parent.mkdir(parents=True, exist_ok=True)
    with registry.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    return entry


def attempt_stats(*, model_key: str | None = None, path: Path | None = None) -> dict[str, Any]:
    """Historical attempt counts and the latest dataset hash (E-7 report fields)."""

    entries = load_attempts(path)
    if model_key:
        entries = [entry for entry in entries if entry.get("model_key") == str(model_key)]
    latest = entries[-1] if entries else None
    return {
        "attempts": len(entries),
        "rejected_count": sum(1 for entry in entries if entry.get("rejected")),
        "dataset_hash": latest.get("dataset_hash") if latest else None,
        "latest_verdict": latest.get("verdict") if latest else None,
        "registry_tail_hash": latest.get("entry_hash") if latest else None,
        "schema_version": SCHEMA_VERSION,
    }
