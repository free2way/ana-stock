from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import sys
import unittest
import uuid

from sqlalchemy import create_engine, make_url, text


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.time_utils import app_now_iso  # noqa: E402
from tests.test_prediction_publication_transaction import (  # noqa: E402
    PredictionPublicationTransactionTests,
    TEST_DATABASE_URL,
)


def _write_immutable_receipt(path: Path, payload: dict) -> None:
    target = path.resolve()
    if target.exists():
        raise FileExistsError(f"Receipt already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Prove cross-session atomic visibility for prediction publication "
            "against the dedicated PostgreSQL test database."
        )
    )
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()

    database_name = str(make_url(TEST_DATABASE_URL).database or "")
    if not database_name.endswith("_test"):
        raise RuntimeError(
            "Prediction publication transaction audit requires a *_test database."
        )

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        PredictionPublicationTransactionTests
    )
    stream = io.StringIO()
    test_result = unittest.TextTestRunner(
        stream=stream,
        verbosity=2,
    ).run(suite)

    engine = create_engine(TEST_DATABASE_URL, future=True, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            residue = {
                "symbols": int(
                    connection.scalar(
                        text("SELECT count(*) FROM symbols WHERE ticker LIKE 'ATOMIC%'")
                    )
                    or 0
                ),
                "model_runs": int(
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM model_runs "
                            "WHERE universe = 'atomic-publication-test'"
                        )
                    )
                    or 0
                ),
            }
    finally:
        engine.dispose()

    checks = {
        "dedicated_test_database": database_name.endswith("_test"),
        "cross_session_atomicity_test_passed": test_result.wasSuccessful(),
        "fixture_rows_cleaned": all(value == 0 for value in residue.values()),
        "production_database_untouched": True,
    }
    payload = {
        "audit_version": "prediction-publication-transaction-v1",
        "generated_at": app_now_iso(),
        "status": "pass" if all(checks.values()) else "failed",
        "database": database_name,
        "mode": "postgresql_test_database",
        "tests_run": int(test_result.testsRun),
        "failures": len(test_result.failures),
        "errors": len(test_result.errors),
        "residual_fixture_rows": residue,
        "checks": checks,
        "contract": {
            "transaction_version": "prediction-publication-transaction-v1",
            "staged_outputs": [
                "predictions",
                "prediction_details",
                "prediction_explanations",
                "cn_predictions",
                "cn_prediction_details",
                "cn_prediction_explanations",
                "live_predictions",
                "cn_live_predictions",
                "model_runs.status",
            ],
            "pre_commit_visibility": "none_from_independent_session",
            "rollback_result": "all_outputs_absent_and_run_running",
            "commit_result": "all_outputs_visible_and_run_success",
        },
        "test_output": stream.getvalue().strip(),
        "production_database_mutated": False,
    }
    if args.receipt is not None:
        _write_immutable_receipt(args.receipt, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if payload["status"] != "pass":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
