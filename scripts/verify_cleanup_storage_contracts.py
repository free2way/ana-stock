#!/usr/bin/env python3
"""Run a bounded storage integration suite against an explicit disposable PG."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import io
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TESTS = (
    "tests.test_scheduled_execution_contract",
    "tests.test_postgres_safety_integration",
    "tests.test_prediction_publication_transaction",
    "tests.test_external_model_physical_storage",
    "tests.test_kronos_backtest",
    "tests.test_model_evaluation",
    "tests.test_job_lineage",
)

PAGE_TESTS = (
    "tests.test_app.AppFlowTests.test_watchlist_market_order_is_cn_hk_us",
    "tests.test_app.AppFlowTests.test_watchlist_page_shows_market_sections",
)


def guarded_page_thread_start(original):
    def start(thread):
        target = getattr(thread, "_target", None)
        portal = (getattr(target, "__module__", "") == "anyio.from_thread"
                  and getattr(target, "__qualname__", "") == "start_blocking_portal.<locals>.run_blocking_portal")
        worker = (type(thread).__module__ == "anyio._backends._asyncio"
                  and type(thread).__name__ == "WorkerThread")
        if not (portal or worker):
            raise AssertionError("Page contracts allow only AnyIO request threads")
        return original(thread)
    return start


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-data-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--include-page-contracts", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error("Receipt already exists")
    from tests.postgres_safety import create_verified_test_engine, require_test_database_url
    from scripts.verify_regime_p0_contracts import source_manifest
    url = require_test_database_url()
    expected = args.expected_data_directory.resolve()
    # Only a deliberately identified temporary cluster is eligible.
    if not str(expected).startswith("/private/tmp/ana-cleanup-pg-"):
        parser.error("Expected data directory must be an ana-cleanup-pg-* temporary cluster")
    started = datetime.now(timezone.utc).isoformat()
    engine = create_verified_test_engine()
    try:
        with engine.connect() as connection:
            actual = connection.exec_driver_sql("SHOW data_directory").scalar_one()
            if Path(actual).resolve() != expected:
                raise RuntimeError("Disposable cluster directory identity mismatch")
            version = connection.exec_driver_sql("SELECT version()").scalar_one()
    finally:
        engine.dispose()
    before = source_manifest()
    previous_cwd = Path.cwd()
    log = io.StringIO()
    with TemporaryDirectory(prefix="ana-cleanup-storage-") as temp, ExitStack() as guards:
        environment = {key: value for key, value in os.environ.items() if not key.startswith("PQW_")}
        environment.update(PQW_DATABASE_URL=url, PQW_TEST_DATABASE_URL=url,
                           MPLCONFIGDIR=str(Path(temp) / "mpl"))
        for name in ("STORAGE", "DATA", "RAW_DATA", "NORMALIZED_DATA", "QLIB_DATA", "ARTIFACTS"):
            environment[f"PQW_{name}_DIR"] = str(Path(temp) / name.lower())
        guards.enter_context(patch.dict(os.environ, environment, clear=True))
        # No workspace .env files, real schedulers, Python outbound networking.
        os.chdir(temp)
        guards.callback(os.chdir, previous_cwd)
        for target in ("socket.socket.connect", "socket.socket.connect_ex"):
            guards.enter_context(patch(target, side_effect=AssertionError("Storage suite forbids network/thread side effects")))
        if args.include_page_contracts:
            guards.enter_context(patch("threading.Thread.start", new=guarded_page_thread_start(threading.Thread.start)))
        else:
            guards.enter_context(patch("threading.Thread.start", side_effect=AssertionError("Storage suite forbids threads")))
        selected_tests = (*TESTS, *(PAGE_TESTS if args.include_page_contracts else ()))
        suite = unittest.defaultTestLoader.loadTestsFromNames(selected_tests)
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
        # libpq uses native sockets; all tested engines are explicitly bound to
        # the verified local URL. This is a bounded allowlist, not a sandbox.
    after = source_manifest()
    passed = result.wasSuccessful() and not result.skipped and before == after
    receipt = {
        "status": "PASS" if passed else "FAIL", "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "scope": "isolated_storage_allowlist_not_full_app_or_ci",
        "cluster_data_directory": str(expected), "postgres_version": version,
        "python_version": sys.version, "python_executable": sys.executable,
        "installed_packages": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
        "production_accessed": False, "test_suites": selected_tests,
        "tests_run": result.testsRun, "failures": len(result.failures),
        "errors": len(result.errors), "skipped": len(result.skipped),
        "source_stable": before == after, "source_files_sha256": before,
        "test_log": log.getvalue(),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as handle:
        json.dump(receipt, handle, indent=2)
    print(log.getvalue())
    print(json.dumps({"status": receipt["status"], "tests_run": result.testsRun, "receipt": str(output)}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
