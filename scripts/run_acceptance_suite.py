"""Reproducible acceptance pipeline for the PostgreSQL test suite.

Runs the full ``unittest discover`` suite in verbose mode as an isolated child
process, records a per-test status ledger (pass / fail / error / skip /
expected_failure / unexpected_success / not_collected) with module and
duration, and diffs the failing-name set item by item against a baseline log.

Credentials are read from ``PQW_TEST_DATABASE_URL`` only and are redacted from
captured output before anything is written to disk.

Usage:
    export PQW_TEST_DATABASE_URL='.../pqw_test'
    .venv/bin/python scripts/run_acceptance_suite.py
    .venv/bin/python scripts/run_acceptance_suite.py --quick
    .venv/bin/python scripts/run_acceptance_suite.py --baseline data/artifacts/acceptance-20261002/regression-with-postgres-final.log
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unittest
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_ARTIFACT_DIR = ROOT / "data" / "artifacts" / "acceptance-20261002"
DEFAULT_BASELINE = DEFAULT_ARTIFACT_DIR / "regression-with-postgres-final.log"
DEFAULT_START_DIR = "tests"
DEFAULT_PATTERN = "test*.py"
# Small, DB-aware smoke subset for --quick; override with --quick-modules.
QUICK_MODULES = ("tests.test_postgres_safety", "tests.test_health_readiness")

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_ERROR = "error"
STATUS_SKIP = "skip"
STATUS_EXPECTED_FAILURE = "expected_failure"
STATUS_UNEXPECTED_SUCCESS = "unexpected_success"
FAILING_STATUSES = {STATUS_FAIL, STATUS_ERROR}
_STATUS_PRIORITY = {
    STATUS_PASS: 0,
    STATUS_SKIP: 1,
    STATUS_EXPECTED_FAILURE: 2,
    STATUS_FAIL: 3,
    STATUS_ERROR: 4,
    STATUS_UNEXPECTED_SUCCESS: 5,
}

HEADER_RE = re.compile(r"^(FAIL|ERROR): (\S+) \(([^)]+)\)")
RAN_RE = re.compile(r"^Ran (\d+) tests? in ")
SUMMARY_RE = re.compile(r"^(FAILED|OK)\b")
FAILED_COUNTS_RE = re.compile(r"failures=(\d+)")
ERROR_COUNTS_RE = re.compile(r"errors=(\d+)")


# --------------------------------------------------------------------------
# Child mode: run the suite in-process with a recording result class.
# --------------------------------------------------------------------------
class LedgerTextResult(unittest.TextTestResult):
    """Verbose TextTestResult that also records one row per test."""

    def __init__(self, stream, descriptions, verbosity):
        super().__init__(stream, descriptions, verbosity)
        self.records: "OrderedDict[str, dict]" = OrderedDict()
        self._started: dict[str, float] = {}

    def _record(self, test, status: str) -> None:
        test_id = test.id()
        started = self._started.get(test_id)
        duration_ms = None
        if started is not None:
            duration_ms = round((time.perf_counter() - started) * 1000.0, 3)
        record = self.records.get(test_id)
        if record is None:
            self.records[test_id] = {
                "id": test_id,
                "module": test.__class__.__module__,
                "class": test.__class__.__name__,
                "name": getattr(test, "_testMethodName", test_id.rsplit(".", 1)[-1]),
                "status": status,
                "duration_ms": duration_ms,
            }
        elif _STATUS_PRIORITY[status] > _STATUS_PRIORITY[record["status"]]:
            record["status"] = status

    def startTest(self, test):
        self._started[test.id()] = time.perf_counter()
        super().startTest(test)

    def _finish(self, test):
        self._started.pop(test.id(), None)

    def addSuccess(self, test):
        super().addSuccess(test)
        self._record(test, STATUS_PASS)

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, STATUS_FAIL)

    def addError(self, test, err):
        super().addError(test, err)
        self._record(test, STATUS_ERROR)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._record(test, STATUS_SKIP)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._record(test, STATUS_EXPECTED_FAILURE)

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._record(test, STATUS_UNEXPECTED_SUCCESS)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is None:
            return
        if issubclass(err[0], test.failureException):
            self._record(test, STATUS_FAIL)
        else:
            self._record(test, STATUS_ERROR)


def _count_tests(suite) -> int:
    return suite.countTestCases()


def run_child(args) -> int:
    """Load and run the suite, then write the machine-readable sidecar."""
    started_at = time.time()
    loader = unittest.TestLoader()
    discovery_error = None
    if args.quick:
        names = [name.strip() for name in (args.quick_modules or "").split(",") if name.strip()]
        if not names:
            names = list(QUICK_MODULES)
        suite = loader.loadTestsFromNames(names)
        selected = names
        mode = "quick"
    else:
        try:
            suite = loader.discover(DEFAULT_START_DIR, pattern=DEFAULT_PATTERN, top_level_dir=None)
        except Exception as error:  # noqa: BLE001 - record discovery failures in the ledger
            discovery_error = f"{type(error).__name__}: {error}"
            suite = unittest.TestSuite()
        selected = None
        mode = "full"

    collected_total = _count_tests(suite)
    collected_ids = sorted(_iter_test_ids(suite))
    runner = unittest.TextTestRunner(stream=sys.stdout, verbosity=2, resultclass=LedgerTextResult)
    if discovery_error:
        sys.stdout.write(f"DISCOVERY ERROR: {discovery_error}\n")
        run_result = runner.run(unittest.TestSuite())
    else:
        run_result = runner.run(suite)
    sys.stdout.flush()

    records = [run_result.records[key] for key in sorted(run_result.records)]
    run_total = run_result.testsRun
    not_collected = sorted(set(collected_ids) - set(run_result.records))
    payload = {
        "mode": mode,
        "selected": selected,
        "started_at": datetime.fromtimestamp(started_at, tz=timezone.utc).isoformat(),
        "duration_seconds": round(time.time() - started_at, 3),
        "collected_total": collected_total,
        "run_total": run_total,
        "not_collected": not_collected,
        "not_collected_total": len(not_collected),
        "discovery_error": discovery_error,
        "summary": {
            "testsRun": run_total,
            "failures": len(run_result.failures),
            "errors": len(run_result.errors),
            "skipped": len(run_result.skipped),
            "expectedFailures": len(run_result.expectedFailures),
            "unexpectedSuccesses": len(run_result.unexpectedSuccesses),
        },
        "tests": records,
    }
    with open(args.emit_json, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    return 0


def _iter_test_ids(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _iter_test_ids(test)
        else:
            yield test.id()


# --------------------------------------------------------------------------
# Baseline parsing and comparison.
# --------------------------------------------------------------------------
def parse_baseline_log(path: Path) -> dict:
    failing: dict[str, str] = {}
    ran = None
    fail_count = error_count = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = HEADER_RE.match(line)
        if match:
            kind, method_name, full_id = match.group(1), match.group(2), match.group(3)
            test_id = full_id.strip() or method_name
            failing[test_id] = "fail" if kind == "FAIL" else "error"
            continue
        ran_match = RAN_RE.match(line)
        if ran_match:
            ran = int(ran_match.group(1))
            continue
        if SUMMARY_RE.match(line):
            fm = FAILED_COUNTS_RE.search(line)
            em = ERROR_COUNTS_RE.search(line)
            if fm:
                fail_count = int(fm.group(1))
            if em:
                error_count = int(em.group(1))
    return {
        "path": str(path),
        "sha256": _sha256_file(path),
        "ran": ran,
        "reported_failures": fail_count,
        "reported_errors": error_count,
        "failing": failing,
        "failing_total": len(failing),
        "parsed": True,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compare_to_baseline(records: list[dict], baseline: dict, scope: set[str]) -> dict:
    current = {row["id"]: row["status"] for row in records if row["status"] in FAILING_STATUSES}
    base = {key: value for key, value in baseline["failing"].items() if key in scope}
    added = sorted(set(current) - set(base))
    removed = sorted(set(base) - set(current))
    base_common = set(base) & set(current)
    changed = [
        {"id": test_id, "baseline": base[test_id], "current": current[test_id]}
        for test_id in sorted(base_common)
        if base[test_id] != current[test_id]
    ]
    unchanged = sorted(test_id for test_id in base_common if base[test_id] == current[test_id])
    return {
        "scope": "selected subset (quick)" if baseline.get("scope_note") else "all executed tests",
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged_failing": unchanged,
        "counts": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "unchanged_failing": len(unchanged),
        },
    }


# --------------------------------------------------------------------------
# Credential redaction.
# --------------------------------------------------------------------------
def build_redactor() -> Callable[[str], str]:
    from sqlalchemy.engine import make_url

    raw = os.environ.get("PQW_TEST_DATABASE_URL", "").strip()
    replacements: list[tuple[str, str]] = []
    if raw:
        try:
            url = make_url(raw)
            masked = url.render_as_string(hide_password=True)
            replacements.append((raw, masked))
            if url.password and len(url.password) >= 4:
                replacements.append((url.password, "***"))
        except Exception:  # noqa: BLE001 - fall back to whole-string redaction
            replacements.append((raw, "<redacted-test-url>"))

    def redact(text: str) -> str:
        for secret, safe in replacements:
            if secret:
                text = text.replace(secret, safe)
        return text

    return redact


# --------------------------------------------------------------------------
# Parent mode: orchestrate, redact, ledger, markdown.
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--quick", action="store_true", help="Run a small smoke subset instead of the full suite")
    parser.add_argument("--quick-modules", default="", help="Comma-separated test modules for --quick")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--label", default="", help="Optional label embedded in artifact file names")
    parser.add_argument("--fail-on-added", action="store_true", help="Exit non-zero when new failures vs baseline appear")
    parser.add_argument("--emit-json", default="", help=argparse.SUPPRESS)
    return parser


def _timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%dT%H%M%S")


def _require_test_url() -> str:
    value = os.environ.get("PQW_TEST_DATABASE_URL", "").strip()
    if not value:
        raise SystemExit(
            "PQW_TEST_DATABASE_URL is not set. Export the fixed test database URL first, e.g.\n"
            "  export PQW_TEST_DATABASE_URL='postgresql+psycopg://<user>:<pwd>@127.0.0.1:5432/pqw_test'\n"
            "Create/verify it with: .venv/bin/python scripts/setup_test_database.py"
        )
    return value


def run_parent(args) -> int:
    _require_test_url()
    redact = build_redactor()
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = _timestamp()
    suffix = f"-{args.label}" if args.label else ""
    log_path = output_dir / f"regression-with-postgres-acceptance-{stamp}{suffix}.log"
    sidecar_path = output_dir / f".acceptance-sidecar-{stamp}{suffix}.json"

    child_command = [sys.executable, str(Path(__file__).resolve()), "--emit-json", str(sidecar_path)]
    if args.quick:
        child_command += ["--quick", "--quick-modules", args.quick_modules]
    display_command = "python -m unittest discover -s tests -v" + (" (quick subset)" if args.quick else "")
    print(f"[acceptance] mode={'quick' if args.quick else 'full'} command={display_command}")
    print(f"[acceptance] log={log_path.name} sidecar={sidecar_path.name}")

    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    started = time.time()
    with log_path.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            child_command,
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            safe = redact(line)
            log_handle.write(safe)
            log_handle.flush()
            sys.stdout.write(safe)
            sys.stdout.flush()
        return_code = process.wait()
    wall_seconds = round(time.time() - started, 3)
    if return_code != 0 or not sidecar_path.exists():
        print(f"[acceptance] child failed (rc={return_code}); see {log_path}", file=sys.stderr)
        return 1

    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar_path.unlink(missing_ok=True)
    records = payload["tests"]

    baseline = None
    comparison = None
    if args.baseline and args.baseline.exists():
        baseline = parse_baseline_log(args.baseline)
        if args.quick:
            baseline["scope_note"] = "quick"
        scope = {row["id"] for row in records}
        comparison = compare_to_baseline(records, baseline, scope)

    status_counts = Counter(row["status"] for row in records)
    failing_records = [row for row in records if row["status"] in FAILING_STATUSES]
    failing_by_module = Counter(row["module"] for row in failing_records)

    ledger = {
        "schema_version": "acceptance-ledger-v1",
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "run": {
            "mode": payload["mode"],
            "selected": payload["selected"],
            "display_command": display_command,
            "python": sys.version.split()[0],
            "python_executable": sys.executable,
            "start_dir": DEFAULT_START_DIR,
            "pattern": DEFAULT_PATTERN,
            "log_path": str(log_path),
            "log_sha256": _sha256_file(log_path),
            "duration_seconds": payload["duration_seconds"],
            "wall_seconds": wall_seconds,
            "collected_total": payload["collected_total"],
            "run_total": payload["run_total"],
            "not_collected_total": payload["not_collected_total"],
            "not_collected": payload["not_collected"],
            "discovery_error": payload["discovery_error"],
            "unittest_summary": payload["summary"],
            "counts": {
                STATUS_PASS: status_counts.get(STATUS_PASS, 0),
                STATUS_FAIL: status_counts.get(STATUS_FAIL, 0),
                STATUS_ERROR: status_counts.get(STATUS_ERROR, 0),
                STATUS_SKIP: status_counts.get(STATUS_SKIP, 0),
                STATUS_EXPECTED_FAILURE: status_counts.get(STATUS_EXPECTED_FAILURE, 0),
                STATUS_UNEXPECTED_SUCCESS: status_counts.get(STATUS_UNEXPECTED_SUCCESS, 0),
            },
        },
        "baseline": baseline,
        "comparison": comparison,
        "failing_by_module": dict(failing_by_module.most_common()),
        "tests": records,
    }
    ledger_path = output_dir / f"acceptance-ledger-{stamp}{suffix}.json"
    ledger_path.write_text(json.dumps(ledger, indent=2, ensure_ascii=False), encoding="utf-8")
    markdown_path = output_dir / f"acceptance-ledger-{stamp}{suffix}.md"
    markdown_path.write_text(render_markdown(ledger), encoding="utf-8")

    print(f"[acceptance] ledger={ledger_path}")
    print(f"[acceptance] summary={markdown_path}")
    if comparison:
        c = comparison["counts"]
        print(
            f"[acceptance] added={c['added']} removed={c['removed']} "
            f"changed={c['changed']} unchanged_failing={c['unchanged_failing']}"
        )
        if args.fail_on_added and c["added"] > 0:
            return 2
    return 0


def render_markdown(ledger: dict) -> str:
    run = ledger["run"]
    counts = run["counts"]
    lines: list[str] = []
    lines.append("# 验收账本（acceptance ledger）")
    lines.append("")
    lines.append(f"- 生成时间：`{ledger['generated_at']}`")
    lines.append(f"- 模式：`{run['mode']}`；命令：`{run['display_command']}`")
    lines.append(f"- Python：`{run['python']}`（{run['python_executable']}）")
    lines.append(f"- 运行日志：`{run['log_path']}`")
    lines.append(f"- 运行日志 SHA256：`{run['log_sha256']}`")
    lines.append(f"- 运行耗时：{run['duration_seconds']}s（子进程 wall {run['wall_seconds']}s）")
    lines.append("")
    lines.append("## 逐测试状态计数")
    lines.append("")
    lines.append("| 状态 | 数量 |")
    lines.append("|---|---|")
    for key in (
        STATUS_PASS,
        STATUS_FAIL,
        STATUS_ERROR,
        STATUS_SKIP,
        STATUS_EXPECTED_FAILURE,
        STATUS_UNEXPECTED_SUCCESS,
    ):
        lines.append(f"| {key} | {counts.get(key, 0)} |")
    lines.append(f"| 执行总数 run | {run['run_total']} |")
    lines.append(f"| discovery 收集数 | {run['collected_total']} |")
    lines.append(f"| not_collected | {run['not_collected_total']} |")
    lines.append("")
    lines.append(
        "> `not_collected` 定义：`unittest` discovery 收集到的测试 id 集合减去本次实际执行并记录到的 id 集合。"
        "全量单进程运行下应为 0；非 0 表示收集与执行不一致（例如导入期异常），需人工排查。"
    )
    lines.append("")
    if run.get("discovery_error"):
        lines.append(f"- **DISCOVERY ERROR**：`{run['discovery_error']}`")
        lines.append("")

    baseline = ledger.get("baseline")
    comparison = ledger.get("comparison")
    lines.append("## 与基线逐项比较")
    lines.append("")
    if not baseline:
        lines.append("未提供可用基线日志，跳过名集比较。")
    else:
        lines.append(f"- 基线日志：`{baseline['path']}`")
        lines.append(f"- 基线日志 SHA256：`{baseline['sha256']}`")
        lines.append(
            f"- 基线：Ran {baseline['ran']} / FAIL {baseline['reported_failures']} / ERROR {baseline['reported_errors']}"
            f"（解析失败名 {baseline['failing_total']}）"
        )
        if comparison:
            c = comparison["counts"]
            lines.append(
                f"- 新增失败 added = **{c['added']}**；已修复 removed = **{c['removed']}**；"
                f"类型变化 changed = **{c['changed']}**；仍失败 unchanged = {c['unchanged_failing']}"
            )
            lines.append("")
            if comparison["added"]:
                lines.append("### 新增失败（added）")
                lines.append("")
                for test_id in comparison["added"]:
                    lines.append(f"- `{test_id}`")
                lines.append("")
            if comparison["changed"]:
                lines.append("### 状态类型变化（changed）")
                lines.append("")
                for row in comparison["changed"]:
                    lines.append(f"- `{row['id']}`：{row['baseline']} → {row['current']}")
                lines.append("")
            if comparison["removed"]:
                lines.append("### 相对基线已不再失败（removed）")
                lines.append("")
                for test_id in comparison["removed"]:
                    lines.append(f"- `{test_id}`")
                lines.append("")
    lines.append("## 失败按模块分布")
    lines.append("")
    lines.append("| 模块 | 失败/错误 |")
    lines.append("|---|---|")
    for module, count in ledger["failing_by_module"].items():
        lines.append(f"| `{module}` | {count} |")
    lines.append("")
    lines.append("## 失败明细")
    lines.append("")
    failing = [row for row in ledger["tests"] if row["status"] in FAILING_STATUSES]
    lines.append(f"共 {len(failing)} 项：")
    lines.append("")
    lines.append("| 状态 | 测试 | 模块 | 用时 ms |")
    lines.append("|---|---|---|---|")
    for row in failing:
        lines.append(f"| {row['status']} | `{row['id']}` | `{row['module']}` | {row['duration_ms']} |")
    lines.append("")
    lines.append("## 口径与限制")
    lines.append("")
    lines.append("- 基线段日志为非 verbose 输出（仅 `FAIL:`/`ERROR:` 块头），因此基线侧只能还原失败/错误名集，无法还原通过/跳过项。")
    lines.append("- 逐项比较基于完整测试 id（`module.Class.method`）；`changed` 指同一 id 在基线与本次之间 `fail`↔`error` 变化。")
    lines.append("- 账本除时间字段（`generated_at`、`duration_seconds`、`duration_ms`）外，同一提交复跑应逐字节一致（测试按 id 排序）。")
    lines.append("- 凭证仅来自 `PQW_TEST_DATABASE_URL`，日志在落盘前已做脱敏。")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.emit_json:
        return run_child(args)
    return run_parent(args)


if __name__ == "__main__":
    raise SystemExit(main())
