"""Fail-closed guards for destructive local PostgreSQL test fixtures."""
import os
from unittest import TestCase

from sqlalchemy import create_engine, make_url, text


def require_test_database_url() -> str:
    configured = os.environ.get("PQW_TEST_DATABASE_URL", "").strip()
    try:
        url = make_url(configured)
    except Exception:
        raise RuntimeError("Set an explicit local PQW_TEST_DATABASE_URL") from None
    if (url.drivername != "postgresql+psycopg"
            or url.host not in {"localhost", "127.0.0.1", "::1"}
            or not str(url.database or "").endswith("_test")
            or url.database == "_test" or url.query):
        raise RuntimeError("Tests require a local postgresql+psycopg *_test database without URL query overrides")
    return configured


def assert_test_database(connection) -> None:
    expected = make_url(require_test_database_url())
    if connection.engine.url != expected:
        raise RuntimeError("Test database engine does not match the explicit test URL")
    actual = connection.execute(text("SELECT current_database()")).scalar_one()
    if actual != expected.database:
        raise RuntimeError("Connected database identity does not match the explicit test database")


def check_test_engine(engine) -> None:
    # Reject a mismatched binding before opening any connection.
    if engine.url != make_url(require_test_database_url()):
        raise RuntimeError("Test engine binding does not match the explicit test URL")
    with engine.connect() as connection:
        assert_test_database(connection)


def create_verified_test_engine():
    engine = create_engine(require_test_database_url(), future=True, pool_pre_ping=True)
    try:
        check_test_engine(engine)
    except BaseException:
        engine.dispose()
        raise
    return engine


def truncate_test_tables(connection, metadata) -> None:
    assert_test_database(connection)
    for table in reversed(metadata.sorted_tables):
        quoted = connection.dialect.identifier_preparer.format_table(table)
        connection.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))


class ApplicationPostgresTestCase(TestCase):
    """Shared application binding and per-example reset for explicit test DBs."""

    @classmethod
    def setUpClass(cls):
        url = require_test_database_url()
        from app.core import db as database
        from app.core.config import get_settings
        snapshot = dict(os.environ)

        def restore():
            os.environ.clear()
            os.environ.update(snapshot)
            get_settings.cache_clear()
            database.configure_database()

        # Unlike tearDownClass, this runs even if initialization raises.
        cls.addClassCleanup(restore)
        os.environ["PQW_DATABASE_URL"] = url
        get_settings.cache_clear()
        database.configure_database()
        check_test_engine(database.engine)
        database.init_db()

    def setUp(self):
        from app.core.db import SessionLocal
        from app.models.base import Base
        with SessionLocal() as db:
            truncate_test_tables(db.connection(), Base.metadata)
            db.commit()
