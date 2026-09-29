from __future__ import annotations

import os
import unittest

from app.services.postgres_backup_restore import (
    compare_critical_counts,
    database_url_for,
    postgres_cli_connection,
    validate_restore_database_name,
)


class PostgresBackupRestoreTests(unittest.TestCase):
    def test_cli_connection_keeps_password_out_of_arguments(self) -> None:
        args, env = postgres_cli_connection("postgresql+psycopg://alice:secret@127.0.0.1:5432/quant")

        self.assertEqual(
            ["--dbname", "quant", "--host", "127.0.0.1", "--port", "5432", "--username", "alice"],
            args,
        )
        self.assertNotIn("secret", " ".join(args))
        self.assertEqual("secret", env["PGPASSWORD"])

    def test_cli_connection_does_not_invent_password(self) -> None:
        original = os.environ.get("PGPASSWORD")
        try:
            os.environ.pop("PGPASSWORD", None)
            _args, env = postgres_cli_connection("postgresql://alice@localhost/quant")
            self.assertNotIn("PGPASSWORD", env)
        finally:
            if original is not None:
                os.environ["PGPASSWORD"] = original

    def test_database_url_switches_database_without_losing_driver(self) -> None:
        result = database_url_for("postgresql+psycopg://alice:secret@localhost/quant", "restore_db")

        self.assertEqual("postgresql+psycopg://alice:secret@localhost/restore_db", result)

    def test_restore_database_name_is_strictly_scoped(self) -> None:
        self.assertEqual(
            "quant_restore_acceptance_20260822",
            validate_restore_database_name("quant_restore_acceptance_20260822"),
        )
        for invalid in ("quant", "postgres", "quant_restore_acceptance_bad-name", "../quant"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_restore_database_name(invalid)

    def test_count_parity_reports_exact_differences(self) -> None:
        passed = compare_critical_counts({"predictions": 2}, {"predictions": 2})
        failed = compare_critical_counts({"predictions": 2}, {"predictions": 1})

        self.assertEqual("pass", passed["status"])
        self.assertTrue(passed["exact_match"])
        self.assertEqual("fail", failed["status"])
        self.assertEqual({"expected": 2, "actual": 1}, failed["differences"]["predictions"])


if __name__ == "__main__":
    unittest.main()
