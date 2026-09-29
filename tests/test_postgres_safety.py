import os
import importlib
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from sqlalchemy import Column, Integer, MetaData, Table, make_url
from sqlalchemy.dialects.postgresql import dialect

from tests.postgres_safety import (
    assert_test_database, check_test_engine, require_test_database_url, truncate_test_tables,
    create_verified_test_engine,
    ApplicationPostgresTestCase,
)


TEST_URL = "postgresql+psycopg://fixture:fixture@127.0.0.1:55432/cleanup_test"


class PostgresSafetyTests(TestCase):
    def test_shared_application_fixture_restores_environment_after_setup_failure(self):
        class Fixture(ApplicationPostgresTestCase):
            pass
        with patch.dict(os.environ, {"PQW_TEST_DATABASE_URL": TEST_URL, "PQW_DATABASE_URL": "previous"}):
            before = dict(os.environ)
            with patch("app.core.db.configure_database") as configure, patch(
                    "tests.postgres_safety.check_test_engine", side_effect=RuntimeError("identity mismatch")), patch(
                    "app.core.db.init_db") as initialize:
                try:
                    with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                        Fixture.setUpClass()
                    self.assertEqual(TEST_URL, os.environ["PQW_DATABASE_URL"])
                finally:
                    Fixture.doClassCleanups()
                self.assertEqual(before, dict(os.environ))
                self.assertEqual(2, configure.call_count)
                initialize.assert_not_called()

    def test_storage_fixture_releases_engine_even_when_schema_setup_fails(self):
        for module_name, class_name in (
            ("tests.test_kronos_backtest", "KronosPhysicalStorageTests"),
            ("tests.test_external_model_physical_storage", "ExternalModelPhysicalStorageTests"),
            ("tests.test_prediction_publication_transaction", "PredictionPublicationTransactionTests"),
        ):
            with self.subTest(module=module_name):
                module = importlib.import_module(module_name)
                fixture = getattr(module, class_name)
                self.assertIn("setUp", fixture.__dict__)
                engine = MagicMock()
                with patch.object(module, "create_verified_test_engine", return_value=engine), patch.object(
                        module.Base.metadata, "create_all", side_effect=RuntimeError("schema failure")):
                    try:
                        with self.assertRaisesRegex(RuntimeError, "schema failure"):
                            fixture.setUpClass()
                    finally:
                        fixture.doClassCleanups()
                engine.dispose.assert_called_once_with()

    def test_remaining_storage_and_process_entrypoints_require_explicit_config(self):
        cases = [
            ("tests.test_kronos_backtest", "KronosPhysicalStorageTests"),
            ("tests.test_external_model_physical_storage", "ExternalModelPhysicalStorageTests"),
            ("tests.test_prediction_publication_transaction", "PredictionPublicationTransactionTests"),
            ("tests.test_stock_selection_decision_postgres_p0", "DecisionPostgresP0Tests"),
        ]
        with patch.dict(os.environ, {"PQW_TEST_DATABASE_URL": ""}), patch("tests.postgres_safety.create_engine") as create:
            for module_name, class_name in cases:
                with self.subTest(module=module_name):
                    module = importlib.import_module(module_name)
                    with self.assertRaises(RuntimeError):
                        getattr(module, class_name).setUpClass()
            with self.assertRaises(RuntimeError):
                module._crash_worker("before_cold_write", "isolated-not-run", "/unused")
            create.assert_not_called()

    def test_factory_disposes_on_identity_failure(self):
        engine = MagicMock()
        with patch.dict(os.environ, {"PQW_TEST_DATABASE_URL": TEST_URL}), patch(
                "tests.postgres_safety.create_engine", return_value=engine), patch(
                "tests.postgres_safety.check_test_engine", side_effect=RuntimeError("mismatch")):
            with self.assertRaisesRegex(RuntimeError, "mismatch"):
                create_verified_test_engine()
        engine.dispose.assert_called_once_with()

    def test_factory_returns_verified_engine(self):
        engine = MagicMock()
        with patch.dict(os.environ, {"PQW_TEST_DATABASE_URL": TEST_URL}), patch(
                "tests.postgres_safety.create_engine", return_value=engine) as create, patch(
                "tests.postgres_safety.check_test_engine") as check:
            self.assertIs(engine, create_verified_test_engine())
        create.assert_called_once_with(TEST_URL, future=True, pool_pre_ping=True)
        check.assert_called_once_with(engine)
        engine.dispose.assert_not_called()

    def test_destructive_fixture_entrypoints_reject_before_database_setup(self):
        for module_name, class_name, class_level in (
            ("tests.test_app", "AppFlowTests", False),
            ("tests.test_model_evaluation", "ModelEvaluationTests", True),
            ("tests.test_job_lineage", "JobLineageRepositoryTests", True),
        ):
            with self.subTest(module=module_name), patch.dict(os.environ, {"PQW_TEST_DATABASE_URL": ""}):
                module = importlib.import_module(module_name)
                fixture = getattr(module, class_name)
                target = "app.core.db.configure_database"
                before = dict(os.environ)
                with patch(target) as configure:
                    with self.assertRaises(RuntimeError):
                        fixture.setUpClass() if class_level else fixture().setUp()
                    configure.assert_not_called()
                self.assertEqual(before, dict(os.environ))

    def test_missing_production_remote_and_override_urls_are_rejected(self):
        for value in ("", "invalid", TEST_URL.replace("cleanup_test", "production"),
                      TEST_URL.replace("127.0.0.1", "remote.example"),
                      TEST_URL + "?host=remote.example", "sqlite:///:memory:"):
            with self.subTest(value=value), patch.dict(os.environ, {"PQW_TEST_DATABASE_URL": value}):
                with self.assertRaises(RuntimeError):
                    require_test_database_url()

    def test_engine_mismatch_rejected_before_connection(self):
        engine = MagicMock()
        engine.url = make_url(TEST_URL.replace("cleanup_test", "production"))
        with patch.dict(os.environ, {"PQW_TEST_DATABASE_URL": TEST_URL}):
            with self.assertRaises(RuntimeError):
                check_test_engine(engine)
        engine.connect.assert_not_called()

    def connection(self, actual="cleanup_test"):
        connection = MagicMock()
        connection.engine = SimpleNamespace(url=make_url(TEST_URL))
        connection.dialect = dialect()
        connection.execute.return_value.scalar_one.return_value = actual
        return connection

    def test_actual_database_mismatch_never_truncates(self):
        connection = self.connection("production")
        with patch.dict(os.environ, {"PQW_TEST_DATABASE_URL": TEST_URL}):
            with self.assertRaises(RuntimeError):
                truncate_test_tables(connection, MetaData())
        self.assertEqual(["SELECT current_database()"],
                         [str(c.args[0]) for c in connection.execute.call_args_list])

    def test_verified_connection_truncates_with_quoted_identifiers(self):
        connection = self.connection()
        metadata = MetaData()
        Table('odd"table', metadata, Column("id", Integer, primary_key=True))
        with patch.dict(os.environ, {"PQW_TEST_DATABASE_URL": TEST_URL}):
            truncate_test_tables(connection, metadata)
        self.assertEqual(["SELECT current_database()", 'TRUNCATE TABLE "odd""table" RESTART IDENTITY CASCADE'],
                         [str(c.args[0]) for c in connection.execute.call_args_list])

    def test_changed_configuration_rejected_before_any_sql(self):
        connection = self.connection()
        with patch.dict(os.environ, {"PQW_TEST_DATABASE_URL": TEST_URL.replace("cleanup_test", "other_test")}):
            with self.assertRaises(RuntimeError):
                assert_test_database(connection)
        connection.execute.assert_not_called()
