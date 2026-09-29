"""The cleanup suite is opt-in, offline, and cannot silently replace existing tests."""
import socket
import os
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from scripts import verify_regime_p1_p2_contracts as runner
from scripts import verify_cleanup_storage_contracts as storage_runner
from scripts import verify_python_runtime as runtime_check


class CleanupRunnerTests(TestCase):
    def test_runtime_preflight_reports_import_failure_without_masking_it(self):
        def load(name):
            if name == "pyexpat":
                raise ImportError("incompatible test library")
        actual = f"{runtime_check.sys.version_info.major}.{runtime_check.sys.version_info.minor}"
        with patch.object(runtime_check.importlib, "import_module", side_effect=load), patch.object(
                runtime_check.platform, "mac_ver", return_value=("14.0", (), "arm64")):
            result = runtime_check.inspect_runtime(actual)
        self.assertEqual("FAIL", result["status"])
        self.assertTrue(any("pyexpat" in error for error in result["failures"]))

    def test_runtime_preflight_rejects_wrong_version(self):
        with patch.object(runtime_check.importlib, "import_module"), patch.object(
                runtime_check.platform, "mac_ver", return_value=("14.0", (), "arm64")):
            self.assertEqual("FAIL", runtime_check.inspect_runtime("0.0")["status"])

    def test_runtime_preflight_accepts_matching_healthy_interpreter(self):
        actual = f"{runtime_check.sys.version_info.major}.{runtime_check.sys.version_info.minor}"
        with patch.object(runtime_check.importlib, "import_module"), patch.object(
                runtime_check.platform, "mac_ver", return_value=("14.0", (), "arm64")):
            self.assertEqual("PASS", runtime_check.inspect_runtime(actual)["status"])

    def test_direct_dataframe_and_form_dependencies_are_declared(self):
        root = Path(__file__).resolve().parents[1]
        declared = {line.split(">=", 1)[0] for line in (root / "requirements.txt").read_text().splitlines()}
        self.assertIn("pandas", declared)
        self.assertIn("python-multipart", declared)

    def test_page_guard_rejects_application_threads_even_with_framework_name(self):
        original = Mock()
        start = storage_runner.guarded_page_thread_start(original)
        for name in ("market-refresh", "AnyIO worker thread", "asyncio-portal-test"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(AssertionError, "only AnyIO"):
                    start(threading.Thread(target=lambda: None, name=name))
        original.assert_not_called()

    def test_page_guard_allows_framework_worker_without_starting_a_real_thread(self):
        from anyio._backends._asyncio import WorkerThread
        original = Mock(return_value="started")
        worker = WorkerThread.__new__(WorkerThread)
        self.assertEqual("started", storage_runner.guarded_page_thread_start(original)(worker))
        original.assert_called_once_with(worker)

    def test_page_guard_allows_exact_portal_target(self):
        original = Mock()
        def portal():
            pass
        portal.__module__ = "anyio.from_thread"
        portal.__qualname__ = "start_blocking_portal.<locals>.run_blocking_portal"
        thread = threading.Thread(target=portal)
        storage_runner.guarded_page_thread_start(original)(thread)
        original.assert_called_once_with(thread)

    def test_storage_runner_rejects_non_temporary_cluster_before_connecting(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {
                "PQW_TEST_DATABASE_URL": "postgresql+psycopg://fixture@127.0.0.1:55479/cleanup_test"}), patch(
                "sys.argv", ["verify", "--expected-data-directory", "/production/cluster",
                             "--output", str(Path(directory) / "receipt.json")]), patch(
                "tests.postgres_safety.create_verified_test_engine") as connect:
            with self.assertRaises(SystemExit) as raised:
                storage_runner.main()
            self.assertEqual(2, raised.exception.code)
            connect.assert_not_called()

    def test_default_suite_and_scope_are_unchanged(self):
        with patch("sys.argv", ["verify", "--output", "unused.json"]), \
             patch.object(runner, "write_contract_receipt", return_value=0) as write:
            self.assertEqual(0, runner.main())
            self.assertEqual((*runner.P0_TESTS, *runner.P1_P2_TESTS), write.call_args.kwargs["tests"])
            self.assertEqual("p0_regression_plus_p1_a6_a7_a8_and_p2_a9_local_contracts_no_production_switch",
                             write.call_args.kwargs["scope"])

    def test_cleanup_adds_tests_and_blocks_both_socket_connect_methods(self):
        def verify(**kwargs):
            self.assertEqual((*runner.P0_TESTS, *runner.P1_P2_TESTS, *runner.CLEANUP_TESTS), kwargs["tests"])
            with socket.socket() as connection:
                for method in (connection.connect, connection.connect_ex):
                    with self.assertRaisesRegex(AssertionError, "must not open network"):
                        method(("127.0.0.1", 1))
            return 0
        with patch("sys.argv", ["verify", "--output", "unused.json", "--include-cleanup-contracts"]), \
             patch.object(runner, "write_contract_receipt", side_effect=verify):
            self.assertEqual(0, runner.main())

    def test_failure_status_is_preserved(self):
        with patch("sys.argv", ["verify", "--output", "unused.json", "--include-cleanup-contracts"]), \
             patch.object(runner, "write_contract_receipt", return_value=1):
            self.assertEqual(1, runner.main())
