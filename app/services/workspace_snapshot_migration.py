from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.tables import WorkspaceSnapshot
from app.services.json_payload_artifacts import (
    ARTIFACT_MARKER,
    JsonPayloadArtifactStore,
    build_payload_envelope,
    canonical_json_bytes,
    payload_digest,
)


def migrate_workspace_snapshot_payload(
    db: Session,
    *,
    snapshot_id: int,
    apply: bool = False,
    artifact_root: Path | None = None,
) -> dict:
    row = db.scalar(
        select(WorkspaceSnapshot).where(WorkspaceSnapshot.id == int(snapshot_id))
    )
    if row is None:
        raise ValueError(f"Workspace snapshot {snapshot_id} was not found.")
    stored_payload = json.loads(row.payload_json)
    store = JsonPayloadArtifactStore(artifact_root)
    if isinstance(stored_payload, dict) and isinstance(stored_payload.get(ARTIFACT_MARKER), dict):
        restored = store.read(stored_payload[ARTIFACT_MARKER])
        return {
            "status": "already_externalized",
            "mode": "apply" if apply else "dry_run",
            "snapshot_id": int(row.id),
            "snapshot_type": row.snapshot_type,
            "snapshot_date": row.snapshot_date,
            "content_sha256": payload_digest(restored),
            "reference": stored_payload[ARTIFACT_MARKER],
            "postgresql_payload_bytes": len(row.payload_json.encode("utf-8")),
            "exact_match": True,
        }
    if not isinstance(stored_payload, dict):
        raise RuntimeError(f"Workspace snapshot {snapshot_id} payload is not a JSON object.")

    source_bytes = canonical_json_bytes(stored_payload)
    threshold = max(
        1024,
        int(get_settings().workspace_snapshot_inline_payload_max_bytes),
    )
    result = {
        "status": "dry_run",
        "mode": "apply" if apply else "dry_run",
        "snapshot_id": int(row.id),
        "snapshot_type": row.snapshot_type,
        "snapshot_date": row.snapshot_date,
        "source_payload_bytes": len(source_bytes),
        "threshold_bytes": threshold,
        "source_sha256": payload_digest(stored_payload),
        "eligible": len(source_bytes) > threshold,
        "applied": False,
    }
    if len(source_bytes) <= threshold or not apply:
        return result

    reference = store.write(stored_payload, namespace="workspace_snapshots")
    restored = store.read(reference)
    exact_match = payload_digest(restored) == result["source_sha256"]
    if not exact_match:
        raise RuntimeError("Workspace snapshot artifact failed semantic verification.")
    envelope = build_payload_envelope(stored_payload, reference)
    row.payload_json = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    try:
        db.commit()
        db.refresh(row)
    except Exception:
        db.rollback()
        raise
    result.update(
        {
            "status": "pass",
            "applied": True,
            "reference": reference,
            "artifact_sha256": payload_digest(restored),
            "exact_match": exact_match,
            "postgresql_payload_bytes_after": len(row.payload_json.encode("utf-8")),
            "postgresql_bytes_reduced": len(source_bytes) - len(row.payload_json.encode("utf-8")),
        }
    )
    return result
