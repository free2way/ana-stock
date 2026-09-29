from __future__ import annotations

import re

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection


_MARKET_FACT_PARENT_SUFFIXES = (
    "live_predictions",
    "predictions",
    "model_chart_signals",
    "fundamental_snapshots",
    "point_in_time_features",
    "technical_snapshots",
)
MARKET_FACT_SYMBOL_CONSTRAINTS = {
    f"{market.lower()}_{suffix}": f"fk_{market.lower()}_{suffix}_symbol_market"
    for market in ("CN", "HK", "US")
    for suffix in _MARKET_FACT_PARENT_SUFFIXES
}
SYMBOL_MARKET_UNIQUE_CONSTRAINT = "uq_symbols_id_market"
_SAFE_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


def _has_composite_symbol_market_fk(items: list[dict]) -> bool:
    return any(
        list(item.get("constrained_columns") or []) == ["symbol_id", "market"]
        and str(item.get("referred_table") or "") == "symbols"
        and list(item.get("referred_columns") or []) == ["id", "market"]
        and str((item.get("options") or {}).get("ondelete") or "").upper()
        == "CASCADE"
        for item in items
    )


def inspect_market_fact_constraints(connection: Connection) -> dict:
    inspector = inspect(connection)
    table_names = set(inspector.get_table_names())
    symbol_unique = any(
        list(item.get("column_names") or []) == ["id", "market"]
        for item in inspector.get_unique_constraints("symbols")
    )
    tables: dict[str, dict] = {}
    for table_name, desired_name in MARKET_FACT_SYMBOL_CONSTRAINTS.items():
        if table_name not in table_names:
            tables[table_name] = {
                "exists": False,
                "constraint_name": desired_name,
                "composite_fk_present": False,
                "simple_symbol_fk_names": [],
                "mismatch_rows": None,
                "compliant": False,
            }
            continue
        foreign_keys = inspector.get_foreign_keys(table_name)
        simple_names = [
            str(item.get("name") or "")
            for item in foreign_keys
            if list(item.get("constrained_columns") or []) == ["symbol_id"]
            and str(item.get("referred_table") or "") == "symbols"
        ]
        mismatch_rows = int(
            connection.scalar(
                text(
                    f'SELECT count(*) FROM "{table_name}" AS fact '
                    'LEFT JOIN symbols AS symbol ON symbol.id = fact.symbol_id '
                    'WHERE symbol.id IS NULL OR symbol.market IS DISTINCT FROM fact.market'
                )
            )
            or 0
        )
        composite_present = _has_composite_symbol_market_fk(foreign_keys)
        tables[table_name] = {
            "exists": True,
            "constraint_name": desired_name,
            "composite_fk_present": composite_present,
            "simple_symbol_fk_names": simple_names,
            "mismatch_rows": mismatch_rows,
            "compliant": composite_present and mismatch_rows == 0,
        }
    return {
        "symbols_unique_present": symbol_unique,
        "tables": tables,
        "status": (
            "pass"
            if symbol_unique and all(item["compliant"] for item in tables.values())
            else "failed"
        ),
    }


def apply_market_fact_constraints(connection: Connection) -> dict:
    before = inspect_market_fact_constraints(connection)
    mismatch_tables = [
        name
        for name, item in before["tables"].items()
        if item["mismatch_rows"] not in (None, 0)
    ]
    if mismatch_tables:
        raise RuntimeError(
            "Cannot add market fact composite foreign keys; symbol/market mismatches: "
            + ", ".join(sorted(mismatch_tables))
        )
    connection.execute(text("SET LOCAL lock_timeout = '10s'"))
    changed: list[str] = []
    if not before["symbols_unique_present"]:
        connection.execute(
            text(
                f"ALTER TABLE symbols ADD CONSTRAINT {SYMBOL_MARKET_UNIQUE_CONSTRAINT} "
                "UNIQUE (id, market)"
            )
        )
        changed.append(SYMBOL_MARKET_UNIQUE_CONSTRAINT)
    for table_name, constraint_name in MARKET_FACT_SYMBOL_CONSTRAINTS.items():
        item = before["tables"][table_name]
        if not item["exists"]:
            continue
        if not _SAFE_IDENTIFIER.fullmatch(table_name) or not _SAFE_IDENTIFIER.fullmatch(
            constraint_name
        ):
            raise ValueError("Unsafe market fact constraint identifier.")
        if not item["composite_fk_present"]:
            connection.execute(
                text(
                    f'ALTER TABLE "{table_name}" ADD CONSTRAINT "{constraint_name}" '
                    "FOREIGN KEY (symbol_id, market) REFERENCES symbols(id, market) "
                    "ON DELETE CASCADE NOT VALID"
                )
            )
            connection.execute(
                text(
                    f'ALTER TABLE "{table_name}" VALIDATE CONSTRAINT "{constraint_name}"'
                )
            )
            changed.append(constraint_name)
        for simple_name in item["simple_symbol_fk_names"]:
            if not _SAFE_IDENTIFIER.fullmatch(simple_name):
                raise ValueError("Unsafe legacy symbol constraint identifier.")
            connection.execute(
                text(f'ALTER TABLE "{table_name}" DROP CONSTRAINT "{simple_name}"')
            )
            changed.append(f"drop:{simple_name}")
    after = inspect_market_fact_constraints(connection)
    return {
        "status": after["status"],
        "changed": changed,
        "before": before,
        "after": after,
    }
