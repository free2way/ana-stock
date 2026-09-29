"""Read-only integrity and candidate-orphan audit for frozen selection artifacts."""
from __future__ import annotations

import json
import time

from sqlalchemy import select

from app.services.json_payload_artifacts import JsonPayloadArtifactStore
from app.services.stock_selection.decision_ledger import DECISION_MODELS, PUBLICATION_MODELS


def audit_final_decision_artifacts(*, db, store=None, orphan_grace_seconds: int = 300) -> dict:
    artifact_store = store or JsonPayloadArtifactStore()
    root = artifact_store.artifact_root
    referenced_paths: set[str] = set()
    checked = 0
    errors: list[dict] = []
    for group, models in (("decision", DECISION_MODELS), ("publication", PUBLICATION_MODELS)):
        for market, model in models.items():
            references = db.scalars(select(model.artifact_reference_json)).all()
            for raw in references:
                checked += 1
                try:
                    reference = json.loads(raw)
                    if not isinstance(reference, dict):
                        raise ValueError("artifact reference is not an object")
                    relative_path = str(reference.get("relative_path") or "")
                    if not relative_path:
                        raise ValueError("artifact reference has no relative path")
                    referenced_paths.add(relative_path)
                    artifact_store.read(reference)
                except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
                    errors.append({"group": group, "market": market,
                                   "reason": type(exc).__name__})

    candidate_orphans: list[str] = []
    recent_unreferenced = 0
    files_scanned = 0
    for namespace in ("stock_selection_final_decisions", "stock_selection_publications"):
        directory = root / "payloads" / namespace
        if not directory.is_dir():
            continue
        for path in directory.rglob("*.json.gz"):
            files_scanned += 1
            relative_path = path.relative_to(root).as_posix()
            if relative_path in referenced_paths:
                continue
            if time.time() - path.stat().st_mtime < max(0, int(orphan_grace_seconds)):
                recent_unreferenced += 1
            else:
                candidate_orphans.append(relative_path)
    return {
        "status": "pass" if not errors and not candidate_orphans else "review_required",
        "references_checked": checked,
        "unique_referenced_files": len(referenced_paths),
        "files_scanned": files_scanned,
        "reference_errors": errors,
        "candidate_orphans": sorted(candidate_orphans),
        "recent_unreferenced_count": recent_unreferenced,
        "orphan_grace_seconds": max(0, int(orphan_grace_seconds)),
        "deletion_performed": False,
    }


__all__ = ["audit_final_decision_artifacts"]
