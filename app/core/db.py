from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.models.base import Base
from app.models import tables  # noqa: F401


settings = get_settings()


def _create_generic_engine(database_url: str):
    backend_name = make_url(database_url).get_backend_name()
    if backend_name == "postgresql":
        return _create_postgresql_engine(database_url)
    raise RuntimeError(f"Unsupported database backend: {backend_name}. PostgreSQL is required.")


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
    if "model_evaluations" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("model_evaluations")}
        with engine.begin() as connection:
            if "oos_sample_count" not in columns:
                connection.execute(text("ALTER TABLE model_evaluations ADD COLUMN oos_sample_count INTEGER NOT NULL DEFAULT 0"))
            if "oos_coverage_days" not in columns:
                connection.execute(text("ALTER TABLE model_evaluations ADD COLUMN oos_coverage_days INTEGER NOT NULL DEFAULT 0"))
            if "purge_gap_days" not in columns:
                connection.execute(text("ALTER TABLE model_evaluations ADD COLUMN purge_gap_days INTEGER"))
            if "benchmark_avg_return" not in columns:
                connection.execute(text("ALTER TABLE model_evaluations ADD COLUMN benchmark_avg_return FLOAT"))
            if "universe_version" not in columns:
                connection.execute(text("ALTER TABLE model_evaluations ADD COLUMN universe_version TEXT"))
            if "activation_status" not in columns:
                connection.execute(text("ALTER TABLE model_evaluations ADD COLUMN activation_status TEXT NOT NULL DEFAULT 'observation'"))
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
    _run_market_snapshot_type_migrations()
    _run_market_fact_constraint_migrations()
    _run_storage_maintenance_migrations()
    _run_index_migrations(inspector)


def _run_market_snapshot_type_migrations() -> None:
    targets = {
        "cn_fundamental_snapshots": {
            "report_date": "DATE",
            "listing_date": "DATE",
            "created_at": "TIMESTAMP WITH TIME ZONE",
            "updated_at": "TIMESTAMP WITH TIME ZONE",
        },
        "us_fundamental_snapshots": {
            "report_date": "DATE",
            "listing_date": "DATE",
            "created_at": "TIMESTAMP WITH TIME ZONE",
            "updated_at": "TIMESTAMP WITH TIME ZONE",
        },
        "hk_fundamental_snapshots": {
            "report_date": "DATE",
            "listing_date": "DATE",
            "created_at": "TIMESTAMP WITH TIME ZONE",
            "updated_at": "TIMESTAMP WITH TIME ZONE",
        },
        "cn_point_in_time_features": {
            "event_time": "TIMESTAMP WITH TIME ZONE",
            "available_time": "TIMESTAMP WITH TIME ZONE",
            "ingested_time": "TIMESTAMP WITH TIME ZONE",
            "created_at": "TIMESTAMP WITH TIME ZONE",
        },
        "us_point_in_time_features": {
            "event_time": "TIMESTAMP WITH TIME ZONE",
            "available_time": "TIMESTAMP WITH TIME ZONE",
            "ingested_time": "TIMESTAMP WITH TIME ZONE",
            "created_at": "TIMESTAMP WITH TIME ZONE",
        },
        "hk_point_in_time_features": {
            "event_time": "TIMESTAMP WITH TIME ZONE",
            "available_time": "TIMESTAMP WITH TIME ZONE",
            "ingested_time": "TIMESTAMP WITH TIME ZONE",
            "created_at": "TIMESTAMP WITH TIME ZONE",
        },
        "cn_technical_snapshots": {
            "as_of_date": "DATE",
            "created_at": "TIMESTAMP WITH TIME ZONE",
            "updated_at": "TIMESTAMP WITH TIME ZONE",
        },
        "us_technical_snapshots": {
            "as_of_date": "DATE",
            "created_at": "TIMESTAMP WITH TIME ZONE",
            "updated_at": "TIMESTAMP WITH TIME ZONE",
        },
        "hk_technical_snapshots": {
            "as_of_date": "DATE",
            "created_at": "TIMESTAMP WITH TIME ZONE",
            "updated_at": "TIMESTAMP WITH TIME ZONE",
        },
    }
    current_inspector = inspect(engine)
    table_names = set(current_inspector.get_table_names())
    statements: list[str] = []
    for table_name, columns in targets.items():
        if table_name not in table_names:
            continue
        current_types = {
            str(column["name"]): column["type"]
            for column in current_inspector.get_columns(table_name)
        }
        for column_name, sql_type in columns.items():
            current_type = current_types.get(column_name)
            current_type_name = str(current_type or "").upper()
            desired_matches = (
                current_type_name == "DATE"
                if sql_type == "DATE"
                else "TIMESTAMP" in current_type_name
                and bool(getattr(current_type, "timezone", False))
            )
            if desired_matches:
                continue
            statements.append(
                f"ALTER TABLE {table_name} ALTER COLUMN {column_name} "
                f"TYPE {sql_type} USING NULLIF({column_name}::text, '')::{sql_type}"
            )
    if not statements:
        return
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL lock_timeout = '10s'"))
        for statement in statements:
            connection.execute(text(statement))


def _run_storage_maintenance_migrations() -> None:
    """Keep table-level autovacuum policy present on upgrades and fresh installs."""

    from app.services.storage_maintenance import apply_storage_maintenance

    with engine.begin() as connection:
        result = apply_storage_maintenance(connection)
    if result["status"] != "pass":
        raise RuntimeError(
            "PostgreSQL storage maintenance migration failed: "
            f"missing_tables={result['missing_tables']}"
        )


def _run_market_fact_constraint_migrations() -> None:
    """Enforce symbol/market identity in every physical market fact table."""

    from app.services.market_fact_constraints import apply_market_fact_constraints

    with engine.begin() as connection:
        result = apply_market_fact_constraints(connection)
    if result["status"] != "pass":
        raise RuntimeError("PostgreSQL market fact composite-FK migration failed.")


def _run_index_migrations(inspector) -> None:
    table_names = set(inspector.get_table_names())
    statements: list[str] = []
    if "predictions" in table_names:
        statements.extend(
            [
                (
                    "CREATE INDEX IF NOT EXISTS ix_predictions_symbol_trade_model "
                    "ON predictions (symbol_id, trade_date DESC, model_run_id DESC, id DESC)"
                ),
                (
                    "CREATE INDEX IF NOT EXISTS ix_predictions_run_date_score "
                    "ON predictions (model_run_id, trade_date, score DESC)"
                ),
            ]
        )
    if "data_jobs" in table_names:
        statements.append(
            "CREATE INDEX IF NOT EXISTS ix_data_jobs_type_status_started "
            "ON data_jobs (job_type, status, started_at DESC)"
        )
    if "job_run_dependencies" in table_names:
        statements.extend(
            [
                "CREATE INDEX IF NOT EXISTS ix_job_run_dependencies_job_id "
                "ON job_run_dependencies (job_id, id DESC)",
                "CREATE INDEX IF NOT EXISTS ix_job_run_dependencies_upstream_id "
                "ON job_run_dependencies (upstream_job_id, id DESC)",
            ]
        )
    if "job_run_attempts" in table_names:
        statements.append(
            "CREATE INDEX IF NOT EXISTS ix_job_run_attempts_job_attempt "
            "ON job_run_attempts (job_id, attempt_no DESC)"
        )
    if "market_refresh_batches" in table_names:
        statements.append(
            "CREATE INDEX IF NOT EXISTS ix_market_refresh_batches_market_id "
            "ON market_refresh_batches (market, id DESC)"
        )
    if "model_evaluations" in table_names:
        statements.append(
            "CREATE INDEX IF NOT EXISTS ix_model_evaluations_run_market_id "
            "ON model_evaluations (model_run_id, market, id DESC)"
        )
    if "model_evaluation_metrics" in table_names:
        statements.append(
            "CREATE INDEX IF NOT EXISTS ix_model_evaluation_metrics_eval_horizon "
            "ON model_evaluation_metrics (model_evaluation_id, horizon_days, metric_scope)"
        )
    if "workspace_snapshots" in table_names:
        statements.extend(
            [
                (
                    "CREATE INDEX IF NOT EXISTS ix_workspace_snapshots_type_id "
                    "ON workspace_snapshots (snapshot_type, id DESC)"
                ),
                (
                    "CREATE INDEX IF NOT EXISTS ix_workspace_snapshots_type_date "
                    "ON workspace_snapshots (snapshot_type, snapshot_date DESC)"
                ),
            ]
        )
    if "point_in_time_feature_snapshots" in table_names:
        statements.extend(
            [
                (
                    "CREATE INDEX IF NOT EXISTS ix_pit_features_symbol_name_available "
                    "ON point_in_time_feature_snapshots "
                    "(symbol_id, feature_name, available_time)"
                ),
                (
                    "CREATE INDEX IF NOT EXISTS ix_pit_features_source_record_revision "
                    "ON point_in_time_feature_snapshots "
                    "(source, source_record_id, revision_id)"
                ),
            ]
        )
    if "strategy_orders" in table_names:
        statements.extend(
            [
                "CREATE INDEX IF NOT EXISTS ix_strategy_orders_run_date ON strategy_orders (strategy_run_id, effective_date, id)",
                "CREATE INDEX IF NOT EXISTS ix_strategy_orders_run_ticker ON strategy_orders (strategy_run_id, ticker, id)",
            ]
        )
    if "strategy_fills" in table_names:
        statements.append(
            "CREATE INDEX IF NOT EXISTS ix_strategy_fills_run_date ON strategy_fills (strategy_run_id, fill_date, id)"
        )
    if "strategy_rejects" in table_names:
        statements.append(
            "CREATE INDEX IF NOT EXISTS ix_strategy_rejects_run_reason ON strategy_rejects (strategy_run_id, reject_reason, id)"
        )
    if "strategy_portfolio_states" in table_names:
        statements.append(
            "CREATE INDEX IF NOT EXISTS ix_strategy_portfolio_states_run_date ON strategy_portfolio_states (strategy_run_id, trade_date)"
        )
    if not statements:
        return
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def get_db_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
