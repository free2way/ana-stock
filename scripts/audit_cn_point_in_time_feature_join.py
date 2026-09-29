from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date, datetime, time
import hashlib
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.db import SessionLocal
from app.services.market_lake import list_lake_symbols, list_lake_trade_dates
from app.services.repository import PointInTimeFeatureSnapshotRepository
from app.services.stock_selection.feature_availability import (
    adapt_point_in_time_feature_snapshots,
)
from app.services.stock_selection.point_in_time_features import (
    DEFAULT_MAX_AGE_DAYS,
    PointInTimeFeatureJoinConfig,
    build_point_in_time_feature_join,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit the CN as-of fundamental join without training or production writes."
    )
    parser.add_argument("--date-count", type=int, default=10)
    parser.add_argument("--minimum-coverage", type=float, default=0.60)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.date_count <= 0:
        raise ValueError("date-count must be positive")
    trade_dates = tuple(
        sorted(
            date.fromisoformat(item)
            for item in list_lake_trade_dates(market="CN")[: args.date_count]
        )
    )
    tickers = tuple(sorted(list_lake_symbols(market="CN")))
    if not trade_dates or not tickers:
        raise RuntimeError("CN market lake has no dates or symbols")
    latest_cutoff = datetime.combine(
        trade_dates[-1],
        time(hour=16),
        tzinfo=ZoneInfo("Asia/Shanghai"),
    )
    with SessionLocal() as db:
        repository = PointInTimeFeatureSnapshotRepository(db)
        rows = repository.list_history_for_market("CN")
        repository_as_of_coverage = repository.summarize_market_coverage_as_of(
            "CN",
            cutoff=latest_cutoff,
            required_features=tuple(name for name, _ in DEFAULT_MAX_AGE_DAYS),
            max_age_days=dict(DEFAULT_MAX_AGE_DAYS),
            minimum_cross_section_coverage=args.minimum_coverage,
        )
    adapted = adapt_point_in_time_feature_snapshots(rows, market="CN")
    identity = "\n".join(
        f"{item.record_id}:{item.revision_id}:{item.knowledge_time.isoformat()}"
        for item in adapted.records
    )
    source_version = (
        f"cn_pit_store_v1:{len(adapted.records)}:"
        f"{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"
    )
    result = build_point_in_time_feature_join(
        adapted.records,
        universe_by_date={item: tickers for item in trade_dates},
        source_version=source_version,
        config=PointInTimeFeatureJoinConfig(
            market="CN",
            minimum_cross_section_coverage=args.minimum_coverage,
        ),
    )
    payload = {
        "status": "success",
        "scope": "shadow_research_only",
        "source_row_count": adapted.source_row_count,
        "emitted_value_count": adapted.emitted_value_count,
        "rejected_row_count": adapted.rejected_row_count,
        "revision_history_preserved": adapted.revision_history_preserved,
        "universe_symbol_count": len(tickers),
        "feature_set_version": result.feature_set_version,
        "enabled_date_count": len(result.enabled_dates),
        "disabled_date_count": len(result.disabled_dates),
        "selected_record_count": result.selected_record_count,
        "latest_repository_as_of_coverage": repository_as_of_coverage,
        "date_coverage": [asdict(item) for item in result.date_coverage],
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
