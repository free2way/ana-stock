"""Write/verify a lake integrity manifest (row counts + per-partition SHA256).

Used as an operational control around test suites and batch jobs: any writer
that mutates canonical v1 or the provenance shadow shows up as a manifest diff.

    python scripts/verify_lake_manifest.py --write   # snapshot current state
    python scripts/verify_lake_manifest.py --check   # report drift (exit 1)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import duckdb  # noqa: E402

from app.services.market_lake import market_lake_root  # noqa: E402

DEFAULT_MANIFEST = ROOT / "data" / "artifacts" / "acceptance-20261002" / "lake-manifest-current.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect() -> dict:
    root = market_lake_root()
    payload: dict = {"generated_at": datetime.now(tz=timezone.utc).isoformat(), "markets": {}}
    for market in ("cn", "us"):
        parts: dict[str, dict] = {}
        for path in sorted((root / f"{market}_daily").glob("date=*/part.parquet")):
            rows = duckdb.sql("SELECT count(*) FROM read_parquet(?)", params=[str(path)]).fetchone()[0]
            parts[path.parent.name] = {"rows": int(rows), "sha256": _sha256(path)}
        shadow_parts: dict[str, dict] = {}
        for path in sorted((root / "_lake_v2" / f"{market}_daily").glob("date=*/part.parquet")):
            shadow_parts[path.parent.name] = {"sha256": _sha256(path)}
        payload["markets"][market] = {
            "partitions": len(parts),
            "rows": sum(item["rows"] for item in parts.values()),
            "files": parts,
            "shadow_files": shadow_parts,
        }
    return payload


def diff(old: dict, new: dict) -> dict:
    changes: dict = {"added": [], "removed": [], "changed": [], "shadow_changes": []}
    for market in ("cn", "us"):
        old_files = (old.get("markets", {}).get(market, {}) or {}).get("files", {})
        new_files = (new.get("markets", {}).get(market, {}) or {}).get("files", {})
        for key in sorted(set(new_files) - set(old_files)):
            changes["added"].append(f"{market}/{key}")
        for key in sorted(set(old_files) - set(new_files)):
            changes["removed"].append(f"{market}/{key}")
        for key in sorted(set(old_files) & set(new_files)):
            if old_files[key] != new_files[key]:
                changes["changed"].append(
                    {
                        "partition": f"{market}/{key}",
                        "old_rows": old_files[key]["rows"],
                        "new_rows": new_files[key]["rows"],
                    }
                )
        old_shadow = (old.get("markets", {}).get(market, {}) or {}).get("shadow_files", {})
        new_shadow = (new.get("markets", {}).get(market, {}) or {}).get("shadow_files", {})
        for key in sorted(set(old_shadow) | set(new_shadow)):
            if old_shadow.get(key) != new_shadow.get(key):
                changes["shadow_changes"].append(f"{market}/{key}")
    changes["drift"] = bool(
        changes["added"] or changes["removed"] or changes["changed"] or changes["shadow_changes"]
    )
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", default=None, help="write the diff JSON here when checking")
    args = parser.parse_args()

    current = collect()
    if args.write or not args.check:
        Path(args.manifest).parent.mkdir(parents=True, exist_ok=True)
        Path(args.manifest).write_text(json.dumps(current, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps({
            "manifest": args.manifest,
            "cn_rows": current["markets"]["cn"]["rows"],
            "us_rows": current["markets"]["us"]["rows"],
        }, indent=2))
        return 0

    old = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    changes = diff(old, current)
    if args.output:
        Path(args.output).write_text(json.dumps(changes, indent=2), encoding="utf-8")
    print(json.dumps({
        "drift": changes["drift"],
        "added": len(changes["added"]),
        "removed": len(changes["removed"]),
        "changed": len(changes["changed"]),
        "shadow_changes": len(changes["shadow_changes"]),
        "cn_rows": current["markets"]["cn"]["rows"],
        "us_rows": current["markets"]["us"]["rows"],
    }, indent=2))
    return 1 if changes["drift"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
