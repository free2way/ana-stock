from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.json_payload_artifacts import (
    ARTIFACT_MARKER,
    SUMMARY_MARKER,
    JsonPayloadArtifactStore,
    canonical_json_bytes,
    resolve_payload_envelope,
)


SETTING_VALUE_WRAPPER = "_app_setting_json_value"


def app_setting_storage_report(db: Session, *, max_inline_bytes: int) -> dict:
    threshold = max(1024, int(max_inline_bytes))
    row = db.execute(
        text(
            "SELECT COUNT(*) AS setting_count, "
            "COUNT(*) FILTER (WHERE octet_length(value) > :threshold) "
            "AS oversized_inline_count, "
            "COALESCE(MAX(octet_length(value)), 0) AS maximum_inline_bytes "
            "FROM app_settings"
        ),
        {"threshold": threshold},
    ).mappings().one()
    return {
        "threshold_bytes": threshold,
        "setting_count": int(row["setting_count"] or 0),
        "oversized_inline_count": int(row["oversized_inline_count"] or 0),
        "maximum_inline_bytes": int(row["maximum_inline_bytes"] or 0),
    }


def encode_app_setting_value(
    value: str,
    *,
    max_inline_bytes: int,
    artifact_root: Path | None = None,
) -> tuple[str, str]:
    raw = str(value)
    threshold = max(1024, int(max_inline_bytes))
    if len(raw.encode("utf-8")) <= threshold:
        return raw, "postgresql_inline"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "App setting values above the inline limit must contain valid JSON."
        ) from exc
    artifact_payload = (
        payload if isinstance(payload, dict) else {SETTING_VALUE_WRAPPER: payload}
    )
    store = JsonPayloadArtifactStore(artifact_root)
    reference = store.write(artifact_payload, namespace="app_settings")
    restored = store.read(reference)
    if canonical_json_bytes(restored) != canonical_json_bytes(artifact_payload):
        raise RuntimeError("App setting artifact failed semantic verification.")
    envelope = {
        ARTIFACT_MARKER: reference,
        SUMMARY_MARKER: {
            "externalized": True,
            "json_type": type(payload).__name__,
            "top_level_key_count": len(payload) if isinstance(payload, dict) else None,
            "top_level_keys": (
                sorted(str(key) for key in payload)[:40]
                if isinstance(payload, dict)
                else []
            ),
        },
    }
    stored = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    if len(stored.encode("utf-8")) > threshold:
        raise RuntimeError("App setting artifact envelope exceeds the inline limit.")
    return stored, "compressed_artifact"


def decode_app_setting_value(
    stored_value: str,
    *,
    artifact_root: Path | None = None,
) -> tuple[str, str]:
    raw = str(stored_value)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw, "postgresql_inline"
    resolved, source = resolve_payload_envelope(payload, artifact_root=artifact_root)
    if source == "postgresql_inline":
        return raw, source
    summary = payload.get(SUMMARY_MARKER) if isinstance(payload, dict) else None
    value = (
        resolved[SETTING_VALUE_WRAPPER]
        if isinstance(resolved, dict)
        and set(resolved) == {SETTING_VALUE_WRAPPER}
        and isinstance(summary, dict)
        and summary.get("json_type") != "dict"
        else resolved
    )
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str),
        source,
    )
