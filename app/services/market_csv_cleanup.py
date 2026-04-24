from __future__ import annotations

from pathlib import Path

from app.core.config import get_settings
from app.services.market_lake import list_lake_symbols


CSV_DELETE_CONFIRMATION = "DELETE_CSV"


def cleanup_market_csv_files(*, dry_run: bool = True, confirm: str | None = None, markets: list[str] | None = None) -> dict:
    settings = get_settings()
    market_set = {str(item).strip().upper() for item in (markets or ["CN", "US"]) if str(item).strip()}
    covered_symbols: set[str] = set()
    for market in market_set:
        covered_symbols.update(list_lake_symbols(market=market))

    candidates = _covered_csv_candidates(covered_symbols)
    protected_count = _count_all_market_csv_files() - len(candidates)
    if not dry_run and confirm != CSV_DELETE_CONFIRMATION:
        return {
            "status": "blocked",
            "message": f"Deletion blocked. Pass confirm={CSV_DELETE_CONFIRMATION} to remove covered CSV files.",
            "dry_run": dry_run,
            "covered_symbols": len(covered_symbols),
            "delete_candidates": len(candidates),
            "protected_count": protected_count,
            "deleted_count": 0,
            "examples": [str(path) for path in candidates[:12]],
        }

    deleted: list[str] = []
    failed: list[dict] = []
    if not dry_run:
        for path in candidates:
            try:
                path.unlink()
                deleted.append(str(path))
            except Exception as exc:
                failed.append({"path": str(path), "error": str(exc)})

    return {
        "status": "success" if not failed else "partial",
        "message": (
            f"Dry run: {len(candidates)} covered CSV file(s) can be removed; {protected_count} remain protected."
            if dry_run
            else f"Deleted {len(deleted)} covered CSV file(s); {len(failed)} failed; {protected_count} remain protected."
        ),
        "dry_run": dry_run,
        "covered_symbols": len(covered_symbols),
        "delete_candidates": len(candidates),
        "protected_count": protected_count,
        "deleted_count": len(deleted),
        "failed_count": len(failed),
        "examples": [str(path) for path in candidates[:12]],
        "failed": failed[:12],
    }


def _covered_csv_candidates(covered_symbols: set[str]) -> list[Path]:
    settings = get_settings()
    candidates: list[Path] = []
    for directory in (settings.raw_data_dir, settings.normalized_data_dir):
        for path in sorted(directory.glob("*.csv")):
            if path.stem.upper() in covered_symbols:
                candidates.append(path)
    return candidates


def _count_all_market_csv_files() -> int:
    settings = get_settings()
    return sum(1 for directory in (settings.raw_data_dir, settings.normalized_data_dir) for _ in directory.glob("*.csv"))
