"""The LightGBM evaluation read must not idle in an open transaction.

Materialising the joined prediction sample set is client-side CPU work: the
read transaction stays open with no server round trip while SQLAlchemy builds
the rows.  With the global ``idle_in_transaction_session_timeout`` (60s) that
work can outlive the guard, the server terminates the connection, and the
A-share AI daily-report stage fails.  The loader therefore has to bound its own
read transaction explicitly.
"""
import unittest
from unittest.mock import patch

from app.services import template_evaluation as service


class _FakeDialect:
    def __init__(self, name):
        self.name = name


class _FakeBind:
    def __init__(self, dialect_name):
        self.dialect = _FakeDialect(dialect_name)


class _FakeSession:
    def __init__(self, dialect_name):
        self.statements = []
        self._bind = _FakeBind(dialect_name)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get_bind(self):
        return self._bind

    def execute(self, statement, *args, **kwargs):
        self.statements.append(str(statement))
        return self

    def scalars(self, statement, *args, **kwargs):
        self.statements.append(str(statement))
        return iter(())


class TemplateEvaluationTransactionTests(unittest.TestCase):
    def _run_loader(self, dialect_name):
        session = _FakeSession(dialect_name)
        with patch.object(service, "SessionLocal", return_value=session), \
             patch.object(service, "get_or_set",
                          side_effect=lambda *args, loader, **kwargs: loader()):
            result = service.build_lightgbm_prediction_evaluation(
                market="ALL", recent_runs=8, top_n=40,
            )
        return session.statements, result

    def test_postgresql_loader_bounds_its_read_transaction(self):
        statements, result = self._run_loader("postgresql")
        self.assertTrue(statements)
        self.assertEqual(
            "SET LOCAL idle_in_transaction_session_timeout = '600s'",
            statements[0],
        )
        self.assertEqual(0, result["run_count"])

    def test_non_postgresql_loader_issues_no_postgres_setting(self):
        statements, _ = self._run_loader("sqlite")
        self.assertTrue(statements)
        self.assertFalse(
            [statement for statement in statements if statement.startswith("SET LOCAL")]
        )


if __name__ == "__main__":
    unittest.main()
