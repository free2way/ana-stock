from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import MetaData, create_engine, func, inspect, select, text

from app.models.base import Base
from app.models import tables  # noqa: F401


def _table_names_in_dependency_order() -> list[str]:
    return [table.name for table in Base.metadata.sorted_tables]


@dataclass(slots=True)
class DatabaseMigrationSummary:
    source_url: str
    target_url: str
    copied_rows: dict[str, int]
    skipped_tables: list[str]
    target_backend: str

    @property
    def total_rows(self) -> int:
        return sum(self.copied_rows.values())


@dataclass(slots=True)
class TableCountComparison:
    table_name: str
    source_count: int
    target_count: int

    @property
    def matches(self) -> bool:
        return self.source_count == self.target_count


@dataclass(slots=True)
class DatabaseValidationSummary:
    source_url: str
    target_url: str
    comparisons: list[TableCountComparison]
    skipped_tables: list[str]
    source_backend: str
    target_backend: str

    @property
    def matches(self) -> bool:
        return all(item.matches for item in self.comparisons)


class DatabaseMigrationService:
    def migrate(
        self,
        *,
        source_url: str,
        target_url: str,
        chunk_size: int = 1000,
        truncate_target: bool = False,
    ) -> DatabaseMigrationSummary:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive.")
        if source_url == target_url:
            raise ValueError("source_url and target_url must be different.")

        source_engine = create_engine(source_url, future=True)
        target_engine = create_engine(target_url, future=True)
        copied_rows: dict[str, int] = {}
        skipped_tables: list[str] = []

        try:
            Base.metadata.create_all(bind=target_engine)
            target_backend = target_engine.url.get_backend_name()
            source_metadata = MetaData()
            source_metadata.reflect(bind=source_engine)
            available_tables = set(source_metadata.tables.keys())
            ordered_tables = _table_names_in_dependency_order()

            if truncate_target:
                with target_engine.begin() as target_connection:
                    self._truncate_tables(
                        target_connection,
                        table_names=[name for name in reversed(ordered_tables) if name in available_tables],
                        backend=target_backend,
                    )

            for table_name in ordered_tables:
                if table_name not in available_tables:
                    skipped_tables.append(table_name)
                    continue

                source_table = source_metadata.tables[table_name]
                target_table = Base.metadata.tables[table_name]
                copied_rows[table_name] = self._copy_table_rows(
                    source_engine=source_engine,
                    target_engine=target_engine,
                    source_table=source_table,
                    target_table=target_table,
                    chunk_size=chunk_size,
                )

            if target_backend == "postgresql":
                with target_engine.begin() as target_connection:
                    self._reset_postgres_sequences(target_connection)
        finally:
            source_engine.dispose()
            target_engine.dispose()

        return DatabaseMigrationSummary(
            source_url=source_url,
            target_url=target_url,
            copied_rows=copied_rows,
            skipped_tables=skipped_tables,
            target_backend=target_backend,
        )

    def validate(
        self,
        *,
        source_url: str,
        target_url: str,
    ) -> DatabaseValidationSummary:
        if source_url == target_url:
            raise ValueError("source_url and target_url must be different.")

        source_engine = create_engine(source_url, future=True)
        target_engine = create_engine(target_url, future=True)
        comparisons: list[TableCountComparison] = []
        skipped_tables: list[str] = []

        try:
            source_backend = source_engine.url.get_backend_name()
            target_backend = target_engine.url.get_backend_name()
            source_metadata = MetaData()
            target_metadata = MetaData()
            source_metadata.reflect(bind=source_engine)
            target_metadata.reflect(bind=target_engine)
            source_tables = set(source_metadata.tables.keys())
            target_tables = set(target_metadata.tables.keys())

            for table_name in _table_names_in_dependency_order():
                if table_name not in source_tables or table_name not in target_tables:
                    skipped_tables.append(table_name)
                    continue

                source_table = source_metadata.tables[table_name]
                target_table = target_metadata.tables[table_name]

                with source_engine.connect() as source_connection:
                    source_count = int(source_connection.execute(select(func.count()).select_from(source_table)).scalar_one())
                with target_engine.connect() as target_connection:
                    target_count = int(target_connection.execute(select(func.count()).select_from(target_table)).scalar_one())

                comparisons.append(
                    TableCountComparison(
                        table_name=table_name,
                        source_count=source_count,
                        target_count=target_count,
                    )
                )
        finally:
            source_engine.dispose()
            target_engine.dispose()

        return DatabaseValidationSummary(
            source_url=source_url,
            target_url=target_url,
            comparisons=comparisons,
            skipped_tables=skipped_tables,
            source_backend=source_backend,
            target_backend=target_backend,
        )

    def _copy_table_rows(
        self,
        *,
        source_engine,
        target_engine,
        source_table,
        target_table,
        chunk_size: int,
    ) -> int:
        row_count = 0
        with source_engine.connect() as source_connection, target_engine.begin() as target_connection:
            result = source_connection.execute(select(source_table))
            while True:
                rows = result.fetchmany(chunk_size)
                if not rows:
                    break
                payload = [dict(row._mapping) for row in rows]
                if not payload:
                    continue
                target_connection.execute(target_table.insert(), payload)
                row_count += len(payload)
        return row_count

    def _truncate_tables(self, target_connection, *, table_names: list[str], backend: str) -> None:
        if not table_names:
            return
        if backend == "postgresql":
            joined = ", ".join(table_names)
            target_connection.execute(text(f"TRUNCATE TABLE {joined} RESTART IDENTITY CASCADE"))
            return
        for table_name in table_names:
            target_connection.execute(text(f"DELETE FROM {table_name}"))

    def _reset_postgres_sequences(self, target_connection) -> None:
        inspector = inspect(target_connection)
        for table_name in inspector.get_table_names():
            primary_key = inspector.get_pk_constraint(table_name).get("constrained_columns") or []
            if len(primary_key) != 1:
                continue
            column_name = primary_key[0]
            sequence_name = target_connection.execute(
                text("SELECT pg_get_serial_sequence(:table_name, :column_name) AS sequence_name"),
                {"table_name": table_name, "column_name": column_name},
            ).scalar_one_or_none()
            if not sequence_name:
                continue
            target_connection.execute(
                text(
                    f"""
                    SELECT setval(
                        '{sequence_name}',
                        COALESCE((SELECT MAX({column_name}) FROM {table_name}), 1),
                        (SELECT COUNT(*) > 0 FROM {table_name})
                    )
                    """
                )
            )
