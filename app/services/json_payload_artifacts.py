from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import uuid

from app.core.config import get_settings


ARTIFACT_MARKER = "_payload_artifact"
SUMMARY_MARKER = "_payload_summary"
SUMMARY_MAX_STRING_CHARS = 4_096


def _bounded_summary_scalar(value):
    if not isinstance(value, str) or len(value) <= SUMMARY_MAX_STRING_CHARS:
        return value
    omitted = len(value) - SUMMARY_MAX_STRING_CHARS
    return f"{value[:SUMMARY_MAX_STRING_CHARS]}\n...[{omitted} characters omitted]"


def canonical_json_bytes(payload: dict) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def payload_digest(payload: dict) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def summarize_payload(payload: dict, *, max_scalar_keys: int = 40) -> dict:
    summary: dict = {}
    for key, value in payload.items():
        if len(summary) >= max(1, int(max_scalar_keys)):
            break
        if value is None or isinstance(value, (str, int, float, bool)):
            summary[str(key)] = _bounded_summary_scalar(value)
        elif isinstance(value, (list, tuple, set)):
            summary[f"{key}_count"] = len(value)
        elif isinstance(value, dict):
            summary[f"{key}_keys"] = sorted(str(item) for item in value.keys())[:40]
    summary["top_level_key_count"] = len(payload)
    return summary


def summarize_job_result(payload: dict) -> dict:
    summary = summarize_payload(payload, max_scalar_keys=80)
    for key in ("output_summary", "quality_summary", "counts", "stats", "summary"):
        value = payload.get(key)
        if not isinstance(value, dict):
            continue
        summary[key] = {
            str(child_key): _bounded_summary_scalar(child_value)
            for child_key, child_value in value.items()
            if child_value is None or isinstance(child_value, (str, int, float, bool))
        }
    return summary


class JsonPayloadArtifactStore:
    def __init__(self, artifact_root: Path | None = None) -> None:
        settings = get_settings()
        self.artifact_root = Path(artifact_root or settings.artifacts_dir).resolve()
        self.schema_version = settings.json_payload_artifact_schema_version

    def write(self, payload: dict, *, namespace: str) -> dict:
        if not isinstance(payload, dict):
            raise TypeError("JSON payload artifacts require an object payload.")
        normalized_namespace = str(namespace or "").strip().replace("/", "_")
        if not normalized_namespace or normalized_namespace in {".", ".."}:
            raise ValueError("A safe payload-artifact namespace is required.")
        content = canonical_json_bytes(payload)
        content_sha256 = hashlib.sha256(content).hexdigest()
        compressed = gzip.compress(content, compresslevel=9, mtime=0)
        file_sha256 = hashlib.sha256(compressed).hexdigest()
        relative_path = Path("payloads") / normalized_namespace / content_sha256[:2] / (
            f"{content_sha256}.json.gz"
        )
        target_path = (self.artifact_root / relative_path).resolve()
        if not target_path.is_relative_to(self.artifact_root):
            raise ValueError("Payload artifact path escaped the configured artifact root.")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            if hashlib.sha256(target_path.read_bytes()).hexdigest() != file_sha256:
                raise RuntimeError(f"Existing payload artifact failed integrity: {target_path}")
        else:
            temporary_path = target_path.with_name(
                f".{target_path.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                temporary_path.write_bytes(compressed)
                os.replace(temporary_path, target_path)
            finally:
                temporary_path.unlink(missing_ok=True)
        return {
            "schema_version": self.schema_version,
            "relative_path": relative_path.as_posix(),
            "content_sha256": content_sha256,
            "file_sha256": file_sha256,
            "uncompressed_bytes": len(content),
            "compressed_bytes": len(compressed),
            "compression": "gzip",
        }

    def read(self, reference: dict) -> dict:
        if str(reference.get("schema_version") or "") != self.schema_version:
            raise RuntimeError("Unsupported JSON payload artifact schema version.")
        relative_path = Path(str(reference.get("relative_path") or ""))
        if relative_path.is_absolute():
            raise ValueError("Payload artifact reference must be relative.")
        artifact_path = (self.artifact_root / relative_path).resolve()
        if not artifact_path.is_relative_to(self.artifact_root):
            raise ValueError("Payload artifact reference escaped the configured artifact root.")
        compressed = artifact_path.read_bytes()
        if hashlib.sha256(compressed).hexdigest() != str(reference.get("file_sha256") or ""):
            raise RuntimeError("Payload artifact file SHA-256 mismatch.")
        content = gzip.decompress(compressed)
        if hashlib.sha256(content).hexdigest() != str(reference.get("content_sha256") or ""):
            raise RuntimeError("Payload artifact content SHA-256 mismatch.")
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise RuntimeError("Payload artifact content is not a JSON object.")
        return payload


def build_payload_envelope(payload: dict, reference: dict) -> dict:
    return {
        ARTIFACT_MARKER: reference,
        SUMMARY_MARKER: summarize_payload(payload),
    }


def resolve_payload_envelope(payload: dict | None, *, artifact_root: Path | None = None) -> tuple[dict | None, str]:
    if not isinstance(payload, dict) or not isinstance(payload.get(ARTIFACT_MARKER), dict):
        return payload, "postgresql_inline"
    resolved = JsonPayloadArtifactStore(artifact_root).read(payload[ARTIFACT_MARKER])
    return resolved, "compressed_artifact"
