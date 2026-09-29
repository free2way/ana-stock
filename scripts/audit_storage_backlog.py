from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import uuid

from sqlalchemy import text


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.db import SessionLocal  # noqa: E402
from app.services.time_utils import app_now_iso  # noqa: E402


GROUP_SQL = """
    SELECT
        COALESCE(mr.market, 'NULL') AS market,
        mr.status,
        CASE
            WHEN pa.status = 'verified' THEN 'verified'
            ELSE COALESCE(pa.status, 'none')
        END AS artifact_status,
        count(DISTINCT mr.id)::bigint AS run_count,
        count(p.id)::bigint AS prediction_count
    FROM model_runs AS mr
    LEFT JOIN prediction_artifacts AS pa ON pa.model_run_id = mr.id
    LEFT JOIN predictions AS p ON p.model_run_id = mr.id
    GROUP BY 1, 2, 3
    ORDER BY 1, 2, 3
"""


DETAIL_SQL = """
    SELECT
        COALESCE(mr.market, 'NULL') AS market,
        mr.status,
        CASE
            WHEN pa.status = 'verified' THEN 'verified'
            ELSE COALESCE(pa.status, 'none')
        END AS artifact_status,
        count(pd.id)::bigint AS detail_count
    FROM prediction_details AS pd
    JOIN predictions AS p ON p.id = pd.prediction_id
    JOIN model_runs AS mr ON mr.id = p.model_run_id
    LEFT JOIN prediction_artifacts AS pa ON pa.model_run_id = mr.id
    GROUP BY 1, 2, 3
    ORDER BY 1, 2, 3
"""


EXPLANATION_SQL = """
    SELECT
        COALESCE(mr.market, 'NULL') AS market,
        mr.status,
        CASE
            WHEN pa.status = 'verified' THEN 'verified'
            ELSE COALESCE(pa.status, 'none')
        END AS artifact_status,
        count(pe.id)::bigint AS explanation_count
    FROM prediction_explanations AS pe
    JOIN predictions AS p ON p.id = pe.prediction_id
    JOIN model_runs AS mr ON mr.id = p.model_run_id
    LEFT JOIN prediction_artifacts AS pa ON pa.model_run_id = mr.id
    GROUP BY 1, 2, 3
    ORDER BY 1, 2, 3
"""


def audit_storage_backlog() -> dict:
    with SessionLocal() as db:
        db.execute(text("SET LOCAL statement_timeout = '180s'"))
        database_bytes = int(
            db.scalar(text("SELECT pg_database_size(current_database())")) or 0
        )
        groups = [dict(row) for row in db.execute(text(GROUP_SQL)).mappings()]
        details = [dict(row) for row in db.execute(text(DETAIL_SQL)).mappings()]
        explanations = [
            dict(row) for row in db.execute(text(EXPLANATION_SQL)).mappings()
        ]
    detail_by_key = {
        (row["market"], row["status"], row["artifact_status"]): row
        for row in details
    }
    explanation_by_key = {
        (row["market"], row["status"], row["artifact_status"]): row
        for row in explanations
    }
    combined: list[dict] = []
    for row in groups:
        key = (row["market"], row["status"], row["artifact_status"])
        detail = detail_by_key.get(key) or {}
        explanation = explanation_by_key.get(key) or {}
        combined.append(
            {
                **row,
                "detail_count": int(detail.get("detail_count") or 0),
                "explanation_count": int(
                    explanation.get("explanation_count") or 0
                ),
            }
        )
    cn_verified_success = [
        row
        for row in combined
        if row["market"] == "CN"
        and row["status"] == "success"
        and row["artifact_status"] == "verified"
    ]
    cn_unverified_success = [
        row
        for row in combined
        if row["market"] == "CN"
        and row["status"] == "success"
        and row["artifact_status"] != "verified"
    ]
    us_rows = [row for row in combined if row["market"] == "US"]
    return {
        "audit_version": "storage-backlog-v1",
        "generated_at": app_now_iso(),
        "status": "pass",
        "database_bytes": database_bytes,
        "groups": combined,
        "summary": {
            "cn_verified_success_runs": sum(
                int(row["run_count"]) for row in cn_verified_success
            ),
            "cn_verified_success_predictions": sum(
                int(row["prediction_count"]) for row in cn_verified_success
            ),
            "cn_unverified_success_runs": sum(
                int(row["run_count"]) for row in cn_unverified_success
            ),
            "cn_unverified_success_predictions": sum(
                int(row["prediction_count"]) for row in cn_unverified_success
            ),
            "us_hold_runs": sum(int(row["run_count"]) for row in us_rows),
            "us_hold_predictions": sum(
                int(row["prediction_count"]) for row in us_rows
            ),
            "us_hold_details": sum(int(row["detail_count"]) for row in us_rows),
            "us_hold_explanations": sum(
                int(row["explanation_count"]) for row in us_rows
            ),
        },
        "database_mutated": False,
    }


def _write_new_json(path: Path, payload: dict) -> None:
    target = path.resolve()
    if target.exists():
        raise FileExistsError(f"Receipt already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only PostgreSQL hot-storage backlog audit."
    )
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    result = audit_storage_backlog()
    if args.receipt is not None:
        _write_new_json(args.receipt, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
