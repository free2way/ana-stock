from __future__ import annotations

from collections import defaultdict

from sqlalchemy import text
from sqlalchemy.orm import Session


def clean_model_history(
    db: Session,
    *,
    keep_model_runs_per_market: int = 20,
    keep_workspace_snapshots_per_type: int = 10,
    apply: bool = False,
) -> dict:
    keep_runs = max(1, int(keep_model_runs_per_market))
    keep_snapshots = max(1, int(keep_workspace_snapshots_per_type))
    runs = db.execute(
        text(
            """
            SELECT id, market FROM model_runs
            WHERE status = 'success' AND market IN ('CN', 'US')
            ORDER BY market, COALESCE(finished_at, created_at) DESC, id DESC
            """
        )
    ).mappings().all()
    by_market: dict[str, list[int]] = defaultdict(list)
    for row in runs:
        by_market[str(row["market"])].append(int(row["id"]))
    stale_run_ids = [run_id for values in by_market.values() for run_id in values[keep_runs:]]
    snapshot_ids = db.execute(
        text(
            """
            WITH ranked AS (
              SELECT id, ROW_NUMBER() OVER (PARTITION BY snapshot_type ORDER BY id DESC) AS rn
              FROM workspace_snapshots
              WHERE snapshot_type <> 'ai_daily_report_history'
            )
            SELECT id FROM ranked WHERE rn > :keep
            """
        ),
        {"keep": keep_snapshots},
    ).scalars().all()
    counts = {"predictions": 0, "prediction_details": 0, "prediction_explanations": 0, "prediction_trade_plans": 0}
    if stale_run_ids:
        params = {"ids": stale_run_ids}
        for table, column in (
            ("predictions", "id"),
            ("prediction_details", "prediction_id"),
            ("prediction_explanations", "prediction_id"),
            ("prediction_trade_plans", "prediction_id"),
        ):
            where = "model_run_id = ANY(CAST(:ids AS INTEGER[]))" if table == "predictions" else (
                "prediction_id IN (SELECT id FROM predictions WHERE model_run_id = ANY(CAST(:ids AS INTEGER[])))"
            )
            counts[table] = int(db.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {where}"), params).scalar() or 0)
    result = {
        "status": "success",
        "mode": "apply" if apply else "dry_run",
        "keep_model_runs_per_market": keep_runs,
        "keep_workspace_snapshots_per_type": keep_snapshots,
        "candidate_model_runs": len(stale_run_ids),
        "candidate_workspace_snapshots": len(snapshot_ids),
        **counts,
    }
    if not apply:
        result["message"] = "Storage-retention preview completed; no data was deleted."
        return result
    if stale_run_ids:
        params = {"ids": stale_run_ids}
        for table in ("prediction_explanations", "prediction_details", "prediction_trade_plans"):
            db.execute(text(f"DELETE FROM {table} WHERE prediction_id IN (SELECT id FROM predictions WHERE model_run_id = ANY(CAST(:ids AS INTEGER[])))"), params)
        db.execute(text("DELETE FROM predictions WHERE model_run_id = ANY(CAST(:ids AS INTEGER[]))"), params)
    if snapshot_ids:
        db.execute(text("DELETE FROM workspace_snapshots WHERE id = ANY(CAST(:ids AS INTEGER[]))"), {"ids": list(snapshot_ids)})
    db.commit()
    result["message"] = "Storage retention cleanup completed. Run VACUUM (ANALYZE) later to refresh statistics."
    return result
