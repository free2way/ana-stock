"""Freeze a reproducible remediation baseline (P0A).

Read-only with respect to application data: writes only under the requested
output directory. Captures code revision (commit + dirty-tree patch hash),
runtime/dependency versions, the market-lake manifest, a consistent SQLite
snapshot when present, and the current evaluation artifacts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str]) -> str:
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    return (result.stdout or "") + (result.stderr or "")


CACHE_SUFFIXES = (".pyc", ".pyo", ".DS_Store")
CACHE_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


def _untracked_exclusion(relative: str) -> str | None:
    """Why a path is out of scope for the code-level content manifest."""

    if relative.startswith("data/"):
        # Lake partitions and artifacts carry their own manifests (lake files
        # SHA256 list + eval-artifacts index); hashing gigabytes here would
        # only bloat the freeze without adding provenance.
        return "data_artifacts_covered_by_lake_and_eval_manifests"
    if any(part in CACHE_PARTS for part in relative.split("/")) or relative.endswith(CACHE_SUFFIXES):
        return "cache_or_temp_file"
    return None


def collect_untracked_manifest() -> dict:
    """Content-level manifest for untracked source files (audit finding #5).

    `git status` only lists untracked paths; without content hashes the freeze
    cannot prove which version of the new code was evaluated. Every untracked
    source file (recursively for untracked directories) gets a SHA256 + size;
    data/cache entries are excluded with an explicit reason.
    """

    status_lines = [line for line in _run(["git", "status", "--porcelain"]).splitlines() if line.strip()]
    entries = [line[3:].strip() for line in status_lines if line.startswith("??")]
    files: dict[str, dict] = {}
    missing: list[str] = []
    excluded: dict[str, dict] = {}
    for entry in entries:
        path = (ROOT / entry).resolve()
        if not path.exists():
            missing.append(entry)
            continue
        candidates = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
        for candidate in candidates:
            relative = str(candidate.relative_to(ROOT))
            reason = _untracked_exclusion(relative)
            if reason:
                bucket = excluded.setdefault(reason, {"entries": 0, "files": 0})
                bucket["files"] += 1
                continue
            files[relative] = {"sha256": _sha256_file(candidate), "size": candidate.stat().st_size}
    for entry in entries:
        reason = _untracked_exclusion(entry if entry.endswith("/") else entry + "/")
        if reason and entry.endswith("/"):
            bucket = excluded.setdefault(reason, {"entries": 0, "files": 0})
            bucket["entries"] += 1
    canonical = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "entries": len(entries),
        "files": len(files),
        "missing": missing,
        "excluded": excluded,
        "content_sha256": _sha256_bytes(canonical),
        "file_hashes": files,
    }


def write_untracked_snapshot(manifest: dict, destination: Path) -> dict | None:
    """Deterministic tar.gz of the untracked files referenced by the manifest."""

    import tarfile

    paths = sorted(manifest.get("file_hashes") or {})
    if not paths:
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(destination, "w:gz") as archive:
        for relative in paths:
            path = ROOT / relative
            if not path.exists():
                continue
            info = archive.gettarinfo(str(path), arcname=relative)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            with path.open("rb") as handle:
                archive.addfile(info, handle)
    return {"path": str(destination), "sha256": _sha256_file(destination), "files": len(paths)}


def collect_docs_manifest() -> dict:
    """Content hashes for ``docs/``.

    ``docs/`` is git-ignored (``.gitignore``), so acceptance and remediation
    documents never show up in ``git status`` and would otherwise sit outside
    the frozen evidence (independent-review finding R6).
    """

    docs_root = ROOT / "docs"
    if not docs_root.is_dir():
        return {"file_count": 0, "files": {}, "content_sha256": _sha256_bytes(b"")}
    files = {
        str(path.relative_to(ROOT)): _sha256_file(path)
        for path in sorted(docs_root.rglob("*"))
        if path.is_file()
    }
    payload = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"file_count": len(files), "files": files, "content_sha256": _sha256_bytes(payload)}


def collect_code_revision() -> dict:
    commit = _run(["git", "rev-parse", "HEAD"]).strip()
    diff = subprocess.run(["git", "diff"], cwd=ROOT, capture_output=True, check=False).stdout
    staged = subprocess.run(["git", "diff", "--cached"], cwd=ROOT, capture_output=True, check=False).stdout
    status_lines = [line for line in _run(["git", "status", "--porcelain"]).splitlines() if line.strip()]
    untracked = [line for line in status_lines if line.startswith("??")]
    modified = [line for line in status_lines if not line.startswith("??")]
    manifest = collect_untracked_manifest()
    return {
        "commit": commit,
        "dirty_tree": bool(status_lines),
        "modified_count": len(modified),
        "untracked_count": len(untracked),
        "worktree_patch_sha256": _sha256_bytes(diff + staged),
        "untracked_manifest_sha256": manifest["content_sha256"],
        "untracked_file_count": manifest["files"],
        "untracked_manifest": manifest,
        "status": status_lines,
    }


def collect_runtime() -> dict:
    freeze = [line for line in _run([sys.executable, "-m", "pip", "freeze"]).splitlines() if line.strip()]
    requirement_hashes = {}
    for name in ("requirements.txt", "requirements-lock.txt", "requirements-macos-arm64-py312.lock"):
        path = ROOT / name
        if path.exists():
            requirement_hashes[name] = _sha256_file(path)
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "executable": sys.executable,
        "pip_freeze": freeze,
        "requirement_hashes": requirement_hashes,
    }


def collect_lake_manifest() -> dict:
    try:
        import duckdb  # type: ignore
    except ImportError:
        duckdb = None

    lake_root = ROOT / "data" / "lake"
    manifest: dict = {"root": str(lake_root), "markets": {}}
    if not lake_root.is_dir():
        return manifest
    for market_dir in sorted(path for path in lake_root.iterdir() if path.is_dir() and not path.name.startswith("_")):
        files = sorted(market_dir.glob("date=*/*.parquet"))
        market_entry: dict = {"file_count": len(files), "rows": None, "symbols": None, "files_sha256": {}}
        if files and duckdb is not None:
            glob = str(market_dir / "date=*" / "*.parquet")
            try:
                rows, symbols = duckdb.sql(
                    "SELECT count(*), count(DISTINCT symbol) FROM read_parquet(?, hive_partitioning=true)",
                    params=[glob],
                ).fetchone()
                market_entry["rows"] = int(rows)
                market_entry["symbols"] = int(symbols)
            except Exception as exc:  # pragma: no cover - diagnostic only
                market_entry["error"] = f"{type(exc).__name__}: {exc}"
        for path in files:
            market_entry["files_sha256"][str(path.relative_to(lake_root))] = _sha256_file(path)
        manifest["markets"][market_dir.name] = market_entry
    return manifest


def snapshot_sqlite(source: Path, destination: Path) -> dict | None:
    if not source.exists():
        return None
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_db, sqlite3.connect(destination) as target_db:
        source_db.backup(target_db)
    wal = source.with_name(source.name + "-wal")
    return {
        "source": str(source),
        "snapshot": str(destination),
        "sha256": _sha256_file(destination),
        "wal_present_at_freeze": wal.exists(),
    }


def collect_eval_artifacts(destination: Path) -> dict:
    artifacts_root = ROOT / "data" / "artifacts"
    if not artifacts_root.is_dir():
        return {"count": 0, "files": {}}
    destination.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    for path in sorted(artifacts_root.glob("*.json")):
        files[str(path.relative_to(ROOT))] = _sha256_file(path)
    if files:
        (destination / "eval-artifacts-index.json").write_text(
            json.dumps(files, indent=2, sort_keys=True), encoding="utf-8"
        )
    return {"count": len(files), "files": files}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    parser.add_argument("--output-dir", default=str(ROOT / "data" / "artifacts" / f"freeze-{stamp}"))
    parser.add_argument("--snapshot-untracked", action="store_true", help="also write untracked_snapshot.tar.gz")
    args = parser.parse_args()

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)

    code_revision = collect_code_revision()
    docs_manifest = collect_docs_manifest()
    baseline = {
        "schema_version": "quant_remediation_freeze_v3",
        "created_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        "code": code_revision,
        "docs": docs_manifest,
        "runtime": collect_runtime(),
        "lake": collect_lake_manifest(),
        "db": snapshot_sqlite(ROOT / "storage" / "app.db", output / "app.db.snapshot"),
        "eval_artifacts": collect_eval_artifacts(output / "eval-artifacts"),
    }
    untracked_manifest_path = output / "untracked_manifest.json"
    untracked_manifest_path.write_text(
        json.dumps(code_revision["untracked_manifest"], indent=2, sort_keys=True), encoding="utf-8"
    )
    if args.snapshot_untracked:
        baseline["untracked_snapshot"] = write_untracked_snapshot(
            code_revision["untracked_manifest"], output / "untracked_snapshot.tar.gz"
        )
    (output / "docs_manifest.json").write_text(
        json.dumps(docs_manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    baseline_path = output / "baseline.json"
    baseline_path.write_text(json.dumps(baseline, indent=2, sort_keys=True), encoding="utf-8")

    checksum_lines = []
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            checksum_lines.append(f"{_sha256_file(path)}  {path.relative_to(output)}")
    (output / "SHA256SUMS").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")

    lake = baseline["lake"]["markets"]
    summary = {
        "output_dir": str(output),
        "commit": baseline["code"]["commit"],
        "patch_sha256": baseline["code"]["worktree_patch_sha256"],
        "untracked_manifest_sha256": baseline["code"]["untracked_manifest_sha256"],
        "untracked_file_count": baseline["code"]["untracked_file_count"],
        "untracked_snapshot": (baseline.get("untracked_snapshot") or {}).get("sha256"),
        "dirty_tree": baseline["code"]["dirty_tree"],
        "lake": {name: {k: entry.get(k) for k in ("file_count", "rows", "symbols")} for name, entry in lake.items()},
        "db_snapshot": bool(baseline["db"]),
        "eval_artifact_count": baseline["eval_artifacts"]["count"],
        "docs_file_count": baseline["docs"]["file_count"],
        "docs_manifest_sha256": baseline["docs"]["content_sha256"],
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
