"""Bind a run manifest to concrete actions / adjusted-view snapshots (A2).

The audit (2026-10-03) noted that a static ``adjustment_version`` string does
not prove which corporate-action data or adjusted view a run consumed. These
helpers hash the actual stores so a manifest can cite them.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.core.config import get_settings

BASE_ADJUSTMENT_VERSION = "raw_prices_with_actions_v1"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def actions_snapshot_sha256(market: str, *, root: Path | None = None) -> str | None:
    base = Path(root) if root is not None else get_settings().data_dir / "corporate_actions"
    path = base / f"{str(market).strip().lower()}_actions.parquet"
    return _sha256_file(path) if path.exists() else None


def adjusted_view_manifest(market: str, *, method: str = "qfq", root: Path | None = None) -> dict:
    base = Path(root) if root is not None else get_settings().data_dir / "lake"
    directory = base / "_adjusted_v2" / str(market).strip().lower() / f"method={method}"
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        return {
            "method": method,
            "version": payload.get("adj_version") or payload.get("version") or payload.get("schema_version"),
            "parquet_sha256": payload.get("parquet_sha256"),
            "generated_at": payload.get("generated_at"),
        }
    parquet_path = directory / "adjusted.parquet"
    if parquet_path.exists():
        return {"method": method, "version": None, "parquet_sha256": _sha256_file(parquet_path), "generated_at": None}
    return {"method": method, "version": None, "parquet_sha256": None, "generated_at": None}


def adjustment_version_binding(market: str, *, method: str = "qfq") -> dict:
    """Composite version + component hashes for run manifests."""

    actions_sha = actions_snapshot_sha256(market)
    view = adjusted_view_manifest(market, method=method)
    view_sha = view.get("parquet_sha256")
    composite = BASE_ADJUSTMENT_VERSION
    if actions_sha or view_sha:
        composite = (
            f"{BASE_ADJUSTMENT_VERSION}:actions={(actions_sha or 'missing')[:12]}:"
            f"view={(view_sha or 'missing')[:12]}"
        )
    return {
        "adjustment_version": composite,
        "actions_snapshot_sha256": actions_sha,
        "adjusted_view_sha256": view_sha,
        "adjusted_view": view,
    }
