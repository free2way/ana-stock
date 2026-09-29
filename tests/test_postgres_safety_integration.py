"""Explicit opt-in PostgreSQL guard integration; never use an existing data DB."""
import os
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import Column, Integer, MetaData, Table, func, select

from tests.postgres_safety import create_verified_test_engine, truncate_test_tables


class PostgresSafetyIntegrationTests(TestCase):
    def setUp(self):
        self.engine = create_verified_test_engine()
        self.addCleanup(self.engine.dispose)
        self.metadata = MetaData()
        self.table = Table("cleanup_guard_" + uuid4().hex, self.metadata,
                           Column("id", Integer, primary_key=True))
        self.metadata.create_all(self.engine)
        self.addCleanup(self.metadata.drop_all, self.engine)
        with self.engine.begin() as connection:
            connection.execute(self.table.insert().values(id=1))

    def test_verified_truncate_is_transactional_and_can_commit(self):
        with self.engine.connect() as connection:
            transaction = connection.begin()
            truncate_test_tables(connection, self.metadata)
            self.assertEqual(0, connection.scalar(select(func.count()).select_from(self.table)))
            transaction.rollback()
            self.assertEqual(1, connection.scalar(select(func.count()).select_from(self.table)))
            connection.rollback()
            with connection.begin():
                truncate_test_tables(connection, self.metadata)
        with self.engine.connect() as connection:
            self.assertEqual(0, connection.scalar(select(func.count()).select_from(self.table)))

    def test_config_change_refuses_to_delete_existing_test_rows(self):
        with self.engine.begin() as connection, patch.dict(os.environ, {"PQW_TEST_DATABASE_URL": ""}):
            with self.assertRaises(RuntimeError):
                truncate_test_tables(connection, self.metadata)
            self.assertEqual(1, connection.scalar(select(func.count()).select_from(self.table)))
