from __future__ import annotations

import json
from pathlib import Path
import sys

from sqlalchemy import text


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.db import SessionLocal  # noqa: E402
from app.services.stock_selection.data_contracts import (  # noqa: E402
    audit_source_data_contracts,
)


SOURCE_AGGREGATE_SQL = """
SELECT 'CN' AS market, source, count(*) AS row_count,
       count(*) FILTER (
           WHERE ingested_time < created_at - interval '1 day'
       ) AS suspected_backdated_ingestion_count,
       min(event_time) AS earliest_event_time,
       max(created_at) AS latest_created_at
FROM cn_point_in_time_features
GROUP BY source
UNION ALL
SELECT 'US' AS market, source, count(*) AS row_count,
       count(*) FILTER (
           WHERE ingested_time < created_at - interval '1 day'
       ) AS suspected_backdated_ingestion_count,
       min(event_time) AS earliest_event_time,
       max(created_at) AS latest_created_at
FROM us_point_in_time_features
GROUP BY source
UNION ALL
SELECT 'HK' AS market, source, count(*) AS row_count,
       count(*) FILTER (
           WHERE ingested_time < created_at - interval '1 day'
       ) AS suspected_backdated_ingestion_count,
       min(event_time) AS earliest_event_time,
       max(created_at) AS latest_created_at
FROM hk_point_in_time_features
GROUP BY source
ORDER BY market, source
"""


def main() -> None:
    with SessionLocal() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        rows = [dict(row) for row in db.execute(text(SOURCE_AGGREGATE_SQL)).mappings()]
        report = audit_source_data_contracts(rows)
        db.rollback()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str, sort_keys=True))
    if report["verdict"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
