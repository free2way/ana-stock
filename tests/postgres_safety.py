"""Fail-closed guards for destructive local PostgreSQL test fixtures."""
import os
import time
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


# PostgreSQL SQLSTATEs that abort the current transaction but are transient and
# therefore safe to retry: 40P01 deadlock_detected, 40001 serialization_failure.
TRUNCATE_RETRY_ATTEMPTS = 3
TRUNCATE_RETRY_BASE_DELAY_SECONDS = 0.25
_RETRYABLE_LOCK_SQLSTATES = frozenset({"40P01", "40001"})


def _is_retryable_lock_error(error: BaseException) -> bool:
    original = getattr(error, "orig", None)
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    return sqlstate in _RETRYABLE_LOCK_SQLSTATES


def truncate_test_tables_with_retry(engine, metadata) -> None:
    """Truncate ``metadata``'s tables, retrying transient lock contention.

    ``truncate_test_tables`` runs inside the caller's transaction, and a
    deadlock aborts that whole transaction, so a retry has to open a fresh one
    (retrying on the aborted connection cannot succeed). Only deadlock and
    serialization failures are retried, with exponential backoff; anything else
    -- including a deadlock that keeps recurring past the attempt budget --
    is re-raised so a real failure is never hidden.
    """
    for attempt in range(TRUNCATE_RETRY_ATTEMPTS):
        try:
            with engine.begin() as connection:
                truncate_test_tables(connection, metadata)
            return
        except Exception as error:  # noqa: BLE001 - retried only when transient
            if attempt + 1 >= TRUNCATE_RETRY_ATTEMPTS or not _is_retryable_lock_error(error):
                raise
            time.sleep(TRUNCATE_RETRY_BASE_DELAY_SECONDS * (2 ** attempt))


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
        from app.core import db as database
        from app.models.base import Base
        truncate_test_tables_with_retry(database.engine, Base.metadata)
