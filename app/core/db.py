from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.models.base import Base
from app.models import tables  # noqa: F401


settings = get_settings()


def _is_sqlite_url(database_url: str) -> bool:
    return make_url(database_url).get_backend_name() == "sqlite"


def _create_sqlite_engine(database_url: str):
    sqlite_engine = create_engine(
        database_url,
        future=True,
        pool_size=20,
        max_overflow=40,
        pool_timeout=max(30, int(float(settings.sqlite_timeout_seconds))),
        pool_pre_ping=True,
        connect_args={
            "check_same_thread": False,
            "timeout": float(settings.sqlite_timeout_seconds),
        },
    )

    @event.listens_for(sqlite_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute(f"PRAGMA busy_timeout={int(float(settings.sqlite_timeout_seconds) * 1000)}")
        cursor.close()

    return sqlite_engine


def _create_generic_engine(database_url: str):
    backend_name = make_url(database_url).get_backend_name()
    if backend_name == "postgresql":
        return _create_postgresql_engine(database_url)
    return create_engine(
        database_url,
        future=True,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        pool_timeout=30,
    )


def _build_postgresql_connect_args() -> dict:
    return {
        "connect_timeout": int(settings.postgres_connect_timeout_seconds),
        "application_name": settings.postgres_application_name,
        "options": (
            f"-c statement_timeout={int(settings.postgres_statement_timeout_ms)} "
            f"-c idle_in_transaction_session_timeout={int(settings.postgres_idle_transaction_timeout_ms)}"
        ),
    }


def _create_postgresql_engine(database_url: str):
    return create_engine(
        database_url,
        future=True,
        pool_pre_ping=True,
        pool_size=max(1, int(settings.postgres_pool_size)),
        max_overflow=max(0, int(settings.postgres_max_overflow)),
        pool_timeout=max(5, int(settings.postgres_pool_timeout_seconds)),
        pool_recycle=max(30, int(settings.postgres_pool_recycle_seconds)),
        connect_args=_build_postgresql_connect_args(),
    )


def _create_engine():
    database_url = settings.resolved_database_url
    if _is_sqlite_url(database_url):
        return _create_sqlite_engine(database_url)
    return _create_generic_engine(database_url)


engine = _create_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def configure_database() -> None:
    global settings, engine

    old_engine = engine
    settings = get_settings()
    engine = _create_engine()
    SessionLocal.configure(bind=engine)
    old_engine.dispose()


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _run_migrations()


def _run_migrations() -> None:
    inspector = inspect(engine)
    if "prediction_details" not in inspector.get_table_names():
        Base.metadata.create_all(bind=engine)
        inspector = inspect(engine)
    if "prediction_details" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("prediction_details")}
        with engine.begin() as connection:
            if "signal_label" not in columns:
                connection.execute(text("ALTER TABLE prediction_details ADD COLUMN signal_label TEXT"))
            if "signal_strength" not in columns:
                connection.execute(text("ALTER TABLE prediction_details ADD COLUMN signal_strength FLOAT"))
            if "expected_drawdown_20d" not in columns:
                connection.execute(text("ALTER TABLE prediction_details ADD COLUMN expected_drawdown_20d FLOAT"))
            if "model_reward_risk_ratio" not in columns:
                connection.execute(text("ALTER TABLE prediction_details ADD COLUMN model_reward_risk_ratio FLOAT"))
            if "target_horizon_days" not in columns:
                connection.execute(text("ALTER TABLE prediction_details ADD COLUMN target_horizon_days INTEGER"))
            if "universe_size" not in columns:
                connection.execute(text("ALTER TABLE prediction_details ADD COLUMN universe_size INTEGER"))
            if "percentile" not in columns:
                connection.execute(text("ALTER TABLE prediction_details ADD COLUMN percentile FLOAT"))
            if "conviction_bucket" not in columns:
                connection.execute(text("ALTER TABLE prediction_details ADD COLUMN conviction_bucket TEXT"))
            if "position_size_hint" not in columns:
                connection.execute(text("ALTER TABLE prediction_details ADD COLUMN position_size_hint TEXT"))
            if "entry_style" not in columns:
                connection.execute(text("ALTER TABLE prediction_details ADD COLUMN entry_style TEXT"))
    if "watchlist_items" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("watchlist_items")}
        if "sync_enabled" not in columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE watchlist_items ADD COLUMN sync_enabled INTEGER NOT NULL DEFAULT 0"))
    if "fundamental_snapshots" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("fundamental_snapshots")}
        if "dividend_yield" not in columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE fundamental_snapshots ADD COLUMN dividend_yield FLOAT"))
    if "technical_snapshots" not in inspector.get_table_names():
        Base.metadata.create_all(bind=engine)
    if "model_chart_signals" not in inspector.get_table_names():
        Base.metadata.create_all(bind=engine)
    if "prediction_trade_plans" not in inspector.get_table_names():
        Base.metadata.create_all(bind=engine)
        inspector = inspect(engine)
    if "prediction_trade_plans" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("prediction_trade_plans")}
        with engine.begin() as connection:
            if "stop_type" not in columns:
                connection.execute(text("ALTER TABLE prediction_trade_plans ADD COLUMN stop_type TEXT"))
            if "trailing_stop_pct" not in columns:
                connection.execute(text("ALTER TABLE prediction_trade_plans ADD COLUMN trailing_stop_pct FLOAT"))
            if "invalidation_reason" not in columns:
                connection.execute(text("ALTER TABLE prediction_trade_plans ADD COLUMN invalidation_reason TEXT"))
            if "execution_tags_json" not in columns:
                connection.execute(text("ALTER TABLE prediction_trade_plans ADD COLUMN execution_tags_json TEXT"))


def get_db_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
