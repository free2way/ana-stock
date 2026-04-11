from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.database_migration import DatabaseMigrationService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Copy app data from one SQLAlchemy database URL to another.")
    parser.add_argument(
        "--mode",
        choices=("migrate", "validate"),
        default="migrate",
        help="Use 'migrate' to copy rows or 'validate' to compare table counts. Default: migrate.",
    )
    parser.add_argument("--source-url", required=True, help="SQLAlchemy database URL for the source database.")
    parser.add_argument("--target-url", required=True, help="SQLAlchemy database URL for the target database.")
    parser.add_argument("--chunk-size", type=int, default=1000, help="Rows per insert batch. Default: 1000.")
    parser.add_argument(
        "--truncate-target",
        action="store_true",
        help="Delete existing rows in the target tables before copying.",
    )
    return parser


if __name__ == "__main__":
    arguments = build_parser().parse_args()
    service = DatabaseMigrationService()
    if arguments.mode == "validate":
        summary = service.validate(
            source_url=arguments.source_url,
            target_url=arguments.target_url,
        )
        print(
            f"Validation {'passed' if summary.matches else 'failed'} "
            f"({summary.source_backend} -> {summary.target_backend})."
        )
        for item in summary.comparisons:
            status = "OK" if item.matches else "MISMATCH"
            print(f"- {item.table_name}: source={item.source_count} target={item.target_count} [{status}]")
        if summary.skipped_tables:
            print("Skipped missing tables:")
            for table_name in summary.skipped_tables:
                print(f"  - {table_name}")
        raise SystemExit(0 if summary.matches else 1)

    summary = service.migrate(
        source_url=arguments.source_url,
        target_url=arguments.target_url,
        chunk_size=arguments.chunk_size,
        truncate_target=arguments.truncate_target,
    )
    print(f"Migrated {summary.total_rows} row(s) into {summary.target_backend}.")
    for table_name, row_count in summary.copied_rows.items():
        print(f"- {table_name}: {row_count}")
    if summary.skipped_tables:
        print("Skipped missing source tables:")
        for table_name in summary.skipped_tables:
            print(f"  - {table_name}")
