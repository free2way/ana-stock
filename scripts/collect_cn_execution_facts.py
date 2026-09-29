#!/usr/bin/env python3
"""Opt-in anonymous BaoStock probe; immutable sidecar only, no production writes."""
import argparse
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.services.stock_selection.cn_execution_facts import collect_baostock_facts, digest, validate_request
from app.services.market_freshness import latest_completed_market_date


def stage_markers(stderr):
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    allowed = {"login_started", "login_succeeded", "query_started", "query_finished", "logout_started", "logout_finished"}
    return [line.removeprefix("CN_FACTS_STAGE:") for line in (stderr or "").splitlines()
            if line.startswith("CN_FACTS_STAGE:") and line.removeprefix("CN_FACTS_STAGE:") in allowed]


def main():
    if sys.argv[1:] == ["--worker"]:
        request = json.load(sys.stdin)
        socket.setdefaulttimeout(10)
        with redirect_stdout(io.StringIO()):
            result = collect_baostock_facts(**request,
                progress=lambda stage: print("CN_FACTS_STAGE:" + stage, file=sys.stderr, flush=True))
        print(json.dumps(result, allow_nan=False))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", required=True, nargs="+")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    validate_request(args.tickers, args.start, args.end)
    if args.end > latest_completed_market_date("CN"):
        parser.error("end must not exceed the latest completed CN session")
    if not 1 <= args.timeout <= 60:
        parser.error("worker timeout must be 1..60 seconds")
    if args.output.exists():
        parser.error("receipt exists; choose a new output path")
    request = {"tickers": args.tickers, "start_date": args.start, "end_date": args.end}
    try:
        process = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker"],
            input=json.dumps(request), capture_output=True, text=True, timeout=args.timeout, check=True)
        result = json.loads(process.stdout)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, ValueError) as exc:
        payload = {"schema_version": "cn_execution_facts_probe_failure_v1", "market": "CN",
                   "provider": "baostock", "status": "UNAVAILABLE", "request": request,
                   "collected_at": datetime.now(timezone.utc).isoformat(),
                   "error_type": type(exc).__name__, "stages": stage_markers(getattr(exc, "stderr", None)),
                   "model_backtest_status": "NOT_RUN"}
        result = {"payload": payload, "sha256": digest(payload)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")
    payload = result["payload"]
    print(json.dumps({"status": payload["status"], "record_count": len(payload.get("records", [])),
                      "queries": [{k: v for k, v in q.items() if k != "response"} for q in payload.get("queries", [])],
                      "error_type": payload.get("error_type"), "sha256": result["sha256"],
                      "stages": payload.get("stages", []),
                      "output": str(args.output)}))
    return 0 if payload["status"] == "SUCCESS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
