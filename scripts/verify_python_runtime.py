#!/usr/bin/env python3
"""Check the interpreter before installing dependencies; emit immutable evidence."""
import argparse
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import platform
import sys


def inspect_runtime(expected_python):
    failures = []
    actual = f"{sys.version_info.major}.{sys.version_info.minor}"
    if actual != expected_python:
        failures.append(f"Expected Python {expected_python}, got {actual}")
    for module in ("ssl", "sqlite3", "pyexpat", "venv", "ensurepip"):
        try:
            importlib.import_module(module)
        except Exception as error:
            failures.append(f"{module}: {type(error).__name__}: {error}")
    if sys.platform == "darwin" and not platform.mac_ver()[0]:
        failures.append("macOS runtime version unavailable")
    return {"status": "FAIL" if failures else "PASS", "python": sys.version,
            "executable": sys.executable, "platform": sys.platform,
            "expected_python": expected_python, "failures": failures,
            "scope": "Interpreter preflight only; not dependency install or application acceptance"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-python", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Receipt already exists")
    result = inspect_runtime(args.expected_python)
    result["checked_at"] = datetime.now(timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as handle:
        json.dump(result, handle, indent=2)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
