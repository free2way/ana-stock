#!/usr/bin/env python3
"""Run an explicit DB-free P0 test allowlist and emit an immutable receipt.

This is engineering evidence only: no promotion, production restart or production
training, external notifications or PostgreSQL test classes are invoked.
Small synthetic Ridge/LambdaRank fits are included in the integration tests.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TESTS = (
    "tests.test_regime_p0_score_and_weights",
    "tests.test_regime_p0_executable_outcomes",
    "tests.test_regime_p0_trainer_integration",
    "tests.test_training_window",
    "tests.test_regime_p0_fill_cost_bridge",
    "tests.test_regime_p0_research_wiring",
    "tests.test_cn_execution_coverage",
    "tests.test_cn_execution_facts",
    "tests.test_regime_policy",
    "tests.test_screener_regime_diagnostics",
    "tests.test_p0_pipeline_diagnostics",
    "tests.test_regime_publication_integration",
    "tests.test_ai_daily_report_delivery",
    "tests.test_walk_forward_model_comparison",
    "tests.test_stock_selection_artifacts",
    "tests.test_research_factor_sets",
    "tests.test_risk_guardrails",
    "tests.test_trainer_point_in_time_protocol",
    "tests.test_executable_labels",
    "tests.test_ai_daily_report_rendering",
    "tests.test_model_signal_summary",
    "tests.test_production_research_data",
    "tests.test_stock_selection_sample_builder",
    "tests.test_stock_selection_contracts",
    "tests.test_final_decision_ledger",
    "tests.test_stock_selection_decision_transaction_p0",
    "tests.test_stock_selection_paper_simulation_p0",
    "tests.test_event_driven_backtest",
    # Never include ModelEvaluationTests: its setup truncates a PostgreSQL DB.
    "tests.test_model_evaluation.EvaluationWindowTests",
)


def _git(*args: str) -> str:
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def source_manifest() -> dict[str, str]:
    paths = set()
    for folder in ("app", "tests", "scripts"):
        paths.update((ROOT / folder).rglob("*.py"))
    for pattern in ("*.yml", "*.yaml"):
        paths.update((ROOT / ".github" / "workflows").rglob(pattern))
    paths.update(ROOT.glob("requirements*.lock"))
    for name in ("requirements.txt", "requirements-dev.txt", "pyproject.toml", "uv.lock"):
        if (ROOT / name).is_file():
            paths.add(ROOT / name)
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths) if "__pycache__" not in path.parts}


def write_contract_receipt(
    *,
    output: Path,
    tests: tuple[str, ...] = TESTS,
    schema_version: str = "regime_p0_engineering_receipt_v1",
    scope: str = "local_contracts_and_mocked_integration_only",
) -> int:
    if output.exists():
        raise FileExistsError("receipt already exists; choose a new path")
    started = datetime.now(timezone.utc).isoformat()
    before = source_manifest()
    log = io.StringIO()
    with patch("psycopg.connect", side_effect=AssertionError("P0 tests must not connect to PostgreSQL")), \
         patch("psycopg.Connection.connect", side_effect=AssertionError("P0 tests must not connect to PostgreSQL")):
        suite = unittest.defaultTestLoader.loadTestsFromNames(tests)
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    after = source_manifest()
    source_stable = before == after
    passed = result.wasSuccessful() and source_stable and not result.skipped
    receipt = {
        "schema_version": schema_version,
        "started_at": started, "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": "PASS" if passed else "FAIL",
        "scope": scope,
        "database_identity": "sqlite_in_memory_and_mocks_no_postgresql_connection",
        "postgres_connection_guard_enabled": True,
        "production_deployed": False, "model_promotion_approved": False,
        "git_commit": _git("rev-parse", "HEAD"), "git_dirty": bool(_git("status", "--porcelain")),
        "source_stable_during_tests": source_stable,
        "source_manifest_sha256": hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
        "source_files_sha256": before,
        "source_manifest_after_if_changed": after if not source_stable else None,
        "python_version": sys.version, "test_suites": list(tests),
        "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "skipped": len(result.skipped), "test_log": log.getvalue(),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(receipt, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
    print(log.getvalue(), end="")
    print(json.dumps({"status": receipt["status"], "tests_run": result.testsRun,
                      "receipt": str(output.resolve()),
                      "source_manifest_sha256": receipt["source_manifest_sha256"]}, ensure_ascii=False))
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New receipt path; existing files are never overwritten")
    args = parser.parse_args()
    try:
        return write_contract_receipt(output=args.output)
    except FileExistsError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
