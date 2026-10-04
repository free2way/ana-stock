"""Create and verify the fixed local PostgreSQL acceptance test database.

Credentials are only ever read from ``PQW_TEST_DATABASE_URL`` (never from a
file written by this script, never echoed in output). The target database name
must end with ``_test`` so the fail-closed guards in ``tests/postgres_safety.py``
keep protecting destructive fixtures.

Usage:
    export PQW_TEST_DATABASE_URL='postgresql+psycopg://<user>:<pwd>@127.0.0.1:5432/pqw_test'
    .venv/bin/python scripts/setup_test_database.py
    .venv/bin/python scripts/setup_test_database.py --init-schema
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DATABASE = "pqw_test"
ADMIN_FALLBACKS = ("postgres", "template1")


def _masked(url) -> str:
    """Render a URL for humans with the password removed."""
    return url.render_as_string(hide_password=True)


def _missing_env_message() -> str:
    return (
        "PQW_TEST_DATABASE_URL is not set.\n"
        "Export the fixed local test database URL (password stays in your shell, never in a file):\n"
        "  export PQW_TEST_DATABASE_URL='postgresql+psycopg://<user>:<pwd>@127.0.0.1:5432/" + DEFAULT_DATABASE + "'\n"
        "Requirements enforced by tests/postgres_safety.py: local host, driver postgresql+psycopg, "
        "database name ending in '_test', and no URL query overrides."
    )


def _load_validated_url(expected_database: str):
    from tests.postgres_safety import require_test_database_url

    configured = require_test_database_url()
    url = make_url(configured)
    if url.database != expected_database:
        raise SystemExit(
            f"Configured test database {url.database!r} does not match the fixed acceptance database "
            f"{expected_database!r}. Re-export PQW_TEST_DATABASE_URL with the expected name."
        )
    return configured, url


def _admin_connection(url):
    last_error: Exception | None = None
    for admin_database in ADMIN_FALLBACKS:
        engine = create_engine(url.set(database=admin_database), future=True)
        try:
            connection = engine.execution_options(isolation_level="AUTOCOMMIT").connect()
        except Exception as error:  # noqa: BLE001 - report which admin DB failed
            engine.dispose()
            last_error = error
            continue
        return engine, connection
    raise SystemExit(
        f"Could not connect to an admin database ({', '.join(ADMIN_FALLBACKS)}) on the test server: "
        f"{type(last_error).__name__}: {last_error}"
    )


def ensure_database(url, expected_database: str) -> str:
    engine, connection = _admin_connection(url)
    try:
        exists = connection.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": expected_database}
        ).scalar_one_or_none()
        if exists:
            return "reused"
        quoted = connection.dialect.identifier_preparer.quote(expected_database)
        connection.execute(text(f"CREATE DATABASE {quoted}"))
        return "created"
    finally:
        connection.close()
        engine.dispose()


def verify_database(url, expected_database: str) -> dict:
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            actual = connection.execute(text("SELECT current_database()")).scalar_one()
            if actual != expected_database:
                raise SystemExit(
                    f"Connected database identity {actual!r} does not match expected {expected_database!r}"
                )
            table_count = connection.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
                )
            ).scalar_one()
            version = connection.execute(text("SHOW server_version")).scalar_one()
    finally:
        engine.dispose()
    return {"database": actual, "server_version": version, "public_table_count": int(table_count)}


def initialize_schema(url) -> None:
    # Reuse the same in-process binding the application test case uses.
    os.environ["PQW_DATABASE_URL"] = url.render_as_string(hide_password=False)
    from app.core import db as database
    from app.core.config import get_settings

    get_settings.cache_clear()
    database.configure_database()
    database.init_db()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=DEFAULT_DATABASE, help="Fixed test database name (default: pqw_test)")
    parser.add_argument("--init-schema", action="store_true", help="Create application tables via app.core.db.init_db()")
    parser.add_argument("--skip-create", action="store_true", help="Only verify an already existing database")
    args = parser.parse_args()

    if not os.environ.get("PQW_TEST_DATABASE_URL", "").strip():
        print(_missing_env_message(), file=sys.stderr)
        return 2
    if not args.database.endswith("_test") or args.database == "_test":
        parser.error("Refusing: test database name must end with '_test' and not be bare '_test'")

    configured, url = _load_validated_url(args.database)
    print(f"target={_masked(url)}")

    if args.skip_create:
        action = "skipped"
    else:
        action = ensure_database(url, args.database)
    info = verify_database(url, args.database)
    print(f"database_action={action}")
    print(f"server_version={info['server_version']} public_tables={info['public_table_count']}")

    if args.init_schema:
        initialize_schema(url)
        info = verify_database(url, args.database)
        print(f"schema_initialized public_tables={info['public_table_count']}")

    print("OK: test database is ready; credentials were read from the environment only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
