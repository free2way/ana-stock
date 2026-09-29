from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys

from sqlalchemy import text


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.db import SessionLocal  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only audit of daily CN/US refresh, model, evaluation, and report jobs."
    )
    parser.add_argument("--date", default=date.today().isoformat())
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    target_date = date.fromisoformat(args.date)
    with SessionLocal() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        rows = [
            dict(row)
            for row in db.execute(
                text(
                    """
                    SELECT id, job_type, status, started_at, finished_at, message
                    FROM data_jobs
                    WHERE (started_at::timestamptz AT TIME ZONE 'Asia/Shanghai')::date = :target_date
                      AND (
                        job_type ILIKE '%cn%'
                        OR job_type ILIKE '%us%'
                        OR job_type ILIKE '%signal%'
                        OR job_type ILIKE '%model%'
                        OR job_type ILIKE '%daily_report%'
                        OR job_type ILIKE '%screener%'
                      )
                    ORDER BY id
                    """
                ),
                {"target_date": target_date},
            ).mappings()
        ]
        db.rollback()
    print(
        json.dumps(
            {"date": target_date.isoformat(), "job_count": len(rows), "jobs": rows},
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
