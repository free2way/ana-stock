from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.models.base import Base
from app.models import tables  # noqa: F401


settings = get_settings()
engine = create_engine(f"sqlite:///{settings.sqlite_path}", future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def configure_database() -> None:
    global settings, engine

    old_engine = engine
    settings = get_settings()
    engine = create_engine(f"sqlite:///{settings.sqlite_path}", future=True)
    SessionLocal.configure(bind=engine)
    old_engine.dispose()


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _run_migrations()


def _run_migrations() -> None:
    inspector = inspect(engine)
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


def get_db_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
