from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from sqlalchemy import select


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.db import SessionLocal  # noqa: E402
from app.models.tables import DataJob, JobRunAttempt  # noqa: E402
from app.services.json_payload_artifacts import canonical_json_bytes  # noqa: E402
from app.services.repository import DataJobRepository  # noqa: E402
from app.services.time_utils import app_now_iso  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish and verify one real CN large job-result artifact canary."
    )
    parser.add_argument("--rows", type=int, default=5000)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    row_count = max(1000, int(args.rows))
    payload = {
        "status": "success",
        "market": "CN",
        "rows_written": row_count,
        "output_summary": {"market": "CN", "rows_written": row_count},
        "rows": [
            {
                "ticker": f"{index:06d}.SZ",
                "as_of_date": "2026-08-22",
                "score": round(index / row_count, 8),
            }
            for index in range(row_count)
        ],
    }
    payload_sha256 = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    with SessionLocal() as db:
        repository = DataJobRepository(db)
        job = repository.create_job(
            job_type="storage_job_payload_canary_cn",
            status="running",
            params={"market": "CN", "canary": True},
            message="Verifying compressed job-result persistence.",
        )
        completed = repository.complete_job(
            int(job.id),
            status="success",
            message="Compressed job-result canary completed.",
            result=payload,
        )
        if completed is None:
            raise RuntimeError("Canary job disappeared during completion.")
        stored = db.get(DataJob, int(job.id))
        if stored is None:
            raise RuntimeError("Canary job was not persisted.")
        params = json.loads(stored.params_json or "{}")
        reference = params.get("result_artifact") or {}
        summary = repository._serialize_job(stored, hydrate_result_artifact=False)
        detail = repository.get_job_detail(int(job.id)) or {}
        hydrated_payload = detail.get("result") or {}
        hydrated_sha256 = hashlib.sha256(
            canonical_json_bytes(hydrated_payload)
        ).hexdigest()
        attempt = db.scalar(
            select(JobRunAttempt)
            .where(JobRunAttempt.job_id == int(job.id))
            .order_by(JobRunAttempt.attempt_no.desc())
            .limit(1)
        )
        attempt_summary = json.loads(attempt.summary_json or "{}") if attempt else {}
        checks = {
            "postgresql_has_no_full_result": "result" not in params,
            "postgresql_has_artifact_reference": bool(reference),
            "list_uses_summary": summary.get("result_source")
            == "postgresql_summary",
            "detail_hydrates_artifact": detail.get("result_source")
            == "compressed_artifact",
            "payload_sha256_exact": hydrated_sha256 == payload_sha256,
            "attempt_has_summary_only": "rows" not in attempt_summary
            and int(attempt_summary.get("rows_count") or 0) == row_count,
            "artifact_is_compressed": reference.get("compression") == "gzip",
        }
        result = {
            "canary_version": "job-payload-artifact-canary-v1",
            "generated_at": app_now_iso(),
            "status": "pass" if all(checks.values()) else "failed",
            "job_id": int(job.id),
            "market": "CN",
            "row_count": row_count,
            "payload_sha256": payload_sha256,
            "hydrated_sha256": hydrated_sha256,
            "postgresql_params_bytes": len((stored.params_json or "").encode("utf-8")),
            "artifact_reference": reference,
            "result_summary": params.get("result_summary"),
            "attempt_summary": attempt_summary,
            "checks": checks,
            "database_rows_added": {"data_jobs": 1, "job_run_attempts": 1},
            "database_rows_deleted": 0,
        }

    serialized = json.dumps(result, ensure_ascii=False, indent=2, default=str)
    target = args.receipt.resolve()
    if target.exists():
        raise FileExistsError(f"Receipt already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(serialized + "\n", encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    print(serialized)
    if result["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
