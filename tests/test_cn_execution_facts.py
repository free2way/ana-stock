from copy import deepcopy
from datetime import date
import json
import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from app.services.stock_selection.cn_execution_facts import (
    FIELDS, attach_matching_facts, collect_baostock_facts, digest,
    load_facts_bundle, normalize_response, validate_request,
)
from app.services.stock_selection.cn_execution_coverage import assess_cn_execution_coverage
from scripts import collect_cn_execution_facts as cli
from scripts import audit_cn_execution_coverage as audit_cli


class Result:
    error_code = "0"
    fields = FIELDS.split(",")

    def __init__(self, rows):
        self.rows = iter(rows)

    def next(self):
        self.current = next(self.rows, None)
        return self.current is not None

    def get_row_data(self):
        return self.current


class CNExecutionFactsTests(TestCase):
    def setUp(self):
        self.rows = [[day, "sz.000001", "10", "10.2", "9.8", "10", "10", "1000", "3", "1", "0"]
                     for day in ("2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11")]
        self.response = {"ticker": "000001.SZ", "fields": FIELDS.split(","), "rows": self.rows}
        self.request = dict(tickers=["000001.SZ"], start_date="2026-09-08", end_date="2026-09-11")

    def normalized(self, response=None):
        return normalize_response(self.response if response is None else response,
                                  start_date=self.request["start_date"], end_date=self.request["end_date"])

    def collect(self, sdk=None):
        sdk = sdk or SimpleNamespace(login=Mock(return_value=SimpleNamespace(error_code="0")),
            logout=Mock(), query_history_k_data_plus=Mock(return_value=Result(self.rows)))
        with patch("app.services.stock_selection.cn_execution_facts.importlib.metadata.version", return_value="fixture"):
            return collect_baostock_facts(**self.request, sdk=sdk)

    def load(self, envelope):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "facts.json"
            path.write_text(json.dumps(envelope), encoding="utf-8")
            return load_facts_bundle(path)

    def test_explicit_raw_request_and_historical_flags_preserved(self):
        facts = self.normalized()
        self.assertEqual("raw", facts[0]["price_basis"])
        self.assertFalse(facts[0]["suspended"])
        self.assertFalse(facts[0]["is_st"])
        self.assertIsNone(facts[0]["corporate_action_status"])
        self.assertIsNone(facts[0]["upper_limit"])
        self.assertIsNone(facts[0]["lower_limit"])

    def test_adjusted_or_wrong_symbol_response_rejected(self):
        for field, value in (("adjustflag", "2"), ("code", "sh.600000")):
            response = deepcopy(self.response)
            response["rows"][0][response["fields"].index(field)] = value
            with self.assertRaises(ValueError):
                self.normalized(response)

    def test_unknown_flag_never_becomes_normal_trading(self):
        response = deepcopy(self.response)
        response["rows"][0][-2:] = ["", "not-a-flag"]
        fact = self.normalized(response)[0]
        self.assertIsNone(fact["suspended"])
        self.assertIsNone(fact["is_st"])

    def test_explicit_suspension_and_st_are_historical(self):
        response = deepcopy(self.response)
        response["rows"][0][-2:] = ["0", "1"]
        fact = self.normalized(response)[0]
        self.assertTrue(fact["suspended"])
        self.assertTrue(fact["is_st"])

    def test_duplicate_outside_dates_and_invalid_numeric_rejected(self):
        variants = []
        duplicate = deepcopy(self.response)
        duplicate["rows"].append(duplicate["rows"][0])
        variants.append(duplicate)
        for index, value in ((0, "2026-09-07"), (2, "nan"), (7, "-1"), (2, True), (3, "9")):
            response = deepcopy(self.response)
            response["rows"][0][index] = value
            variants.append(response)
        for response in variants:
            with self.assertRaises(ValueError):
                self.normalized(response)

    def test_scope_excludes_bj_us_hk_etf_and_oversized_requests(self):
        for ticker in ("430001.BJ", "AAPL", "00700.HK", "510300.SS", "600000.SH", "600000.SZ"):
            with self.assertRaises(ValueError):
                validate_request([ticker], "2026-09-08", "2026-09-11")
        for tickers, start, end in (([], "2026-09-08", "2026-09-11"),
            (["000001.SZ"] * 2, "2026-09-08", "2026-09-11"),
            (["000001.SZ"], "2020-01-01", "2026-09-11"),
            (["000001.SZ"], "2026-09-11", "2026-09-08")):
            with self.assertRaises(ValueError):
                validate_request(tickers, start, end)

    def test_bundle_reproduces_source_and_hash(self):
        bundle = self.collect()
        facts, fingerprint = self.load(bundle)
        self.assertEqual(self.normalized(), facts)
        self.assertEqual(bundle["sha256"], fingerprint)
        self.assertEqual("SUCCESS", bundle["payload"]["status"])

    def test_modified_hash_or_rehashed_invented_facts_rejected(self):
        for rehash in (False, True):
            bundle = self.collect()
            bundle["payload"]["records"][0]["upper_limit"] = 11
            if rehash:
                bundle["sha256"] = digest(bundle["payload"])
            with self.assertRaises(ValueError):
                self.load(bundle)

    def test_login_failure_does_not_call_query_or_claim_empty_success(self):
        sdk = SimpleNamespace(login=Mock(return_value=SimpleNamespace(error_code="100")),
                              logout=Mock(), query_history_k_data_plus=Mock())
        bundle = self.collect(sdk)
        self.assertEqual("UNAVAILABLE", bundle["payload"]["status"])
        self.assertEqual([], bundle["payload"]["records"])
        sdk.query_history_k_data_plus.assert_not_called()
        sdk.logout.assert_called_once()

    def test_query_failure_is_not_no_events(self):
        sdk = SimpleNamespace(login=Mock(return_value=SimpleNamespace(error_code="0")),
            logout=Mock(), query_history_k_data_plus=Mock(side_effect=RuntimeError("secret-like text")))
        bundle = self.collect(sdk)
        self.assertEqual("UNAVAILABLE", bundle["payload"]["status"])
        self.assertNotIn("secret-like text", json.dumps(bundle))
        self.assertEqual([], self.load(bundle)[0])

    def test_empty_response_does_not_confirm_coverage(self):
        self.rows.clear()
        bundle = self.collect()
        self.assertEqual("UNAVAILABLE", bundle["payload"]["status"])
        self.assertEqual("EMPTY", bundle["payload"]["queries"][0]["status"])

    def test_matching_prices_join_without_overwriting_original(self):
        facts = self.normalized()
        rows = [{k: v for k, v in fact.items() if k in ("date", "symbol", "open", "high", "low", "close", "volume")}
                for fact in facts]
        original = deepcopy(rows)
        enriched, summary = attach_matching_facts(rows, facts)
        self.assertEqual(original, rows)
        self.assertEqual(4, summary["counts"]["matched_rows"])
        result = assess_cn_execution_coverage(enriched,
            trading_dates=[date.fromisoformat(row["date"]) for row in rows],
            tickers=["000001.SZ"], horizon_days=3, source_reference="fixture")
        self.assertEqual("BLOCKED", result["status"])
        self.assertEqual(1, result["status_counts"]["unknown"])
        self.assertNotIn("raw_price_basis_unverified", result["reason_counts"])
        self.assertNotIn("daily_suspension_state_unknown", result["reason_counts"])
        self.assertIn("daily_price_limits_unknown", result["reason_counts"])
        self.assertIn("corporate_action_state_unknown", result["reason_counts"])

    def test_mismatched_ohlcv_does_not_attach_and_revokes_existing_claim(self):
        facts = self.normalized()
        row = {**facts[0], "close": 10.01}
        enriched, summary = attach_matching_facts([row], facts)
        self.assertEqual(1, summary["counts"]["conflicting_rows"])
        self.assertIsNone(enriched[0]["price_basis"])
        self.assertIsNone(enriched[0]["execution_source_reference"])
        self.assertEqual(10.01, enriched[0]["close"])

    def test_conflicting_flags_block_and_missing_rows_remain_unknown(self):
        facts = self.normalized()
        rows = [{**facts[0], "suspended": True}, {**facts[1], "symbol": "600000.SS"}]
        _, summary = attach_matching_facts(rows, facts)
        self.assertEqual(1, summary["counts"]["conflicting_rows"])
        self.assertEqual(1, summary["counts"]["missing_fact_rows"])

    def test_duplicate_facts_rejected(self):
        facts = self.normalized()
        with self.assertRaises(ValueError):
            attach_matching_facts([], facts + facts)

    def test_cli_timeout_leaves_failure_receipt_not_empty_success(self):
        with TemporaryDirectory() as temp:
            output = Path(temp) / "failure.json"
            argv = ["collect", "--tickers", "000001.SZ", "--start", "2026-09-08", "--end", "2026-09-11", "--output", str(output)]
            with patch("sys.argv", argv), patch.object(cli, "latest_completed_market_date", return_value="2026-09-18"), \
                 patch.object(cli.subprocess, "run", side_effect=subprocess.TimeoutExpired("worker", 45)), redirect_stdout(io.StringIO()):
                self.assertEqual(2, cli.main())
            payload = json.loads(output.read_text())["payload"]
            self.assertEqual("UNAVAILABLE", payload["status"])
            self.assertEqual("TimeoutExpired", payload["error_type"])
            with self.assertRaises(ValueError):
                load_facts_bundle(output)

    def test_cli_existing_receipt_never_overwritten_or_requests_sent(self):
        with TemporaryDirectory() as temp:
            output = Path(temp) / "existing.json"
            output.write_text("immutable")
            argv = ["collect", "--tickers", "000001.SZ", "--start", "2026-09-08", "--end", "2026-09-11", "--output", str(output)]
            with patch("sys.argv", argv), patch.object(cli, "latest_completed_market_date", return_value="2026-09-18"), \
                 patch.object(cli.subprocess, "run") as worker, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    cli.main()
                worker.assert_not_called()
            self.assertEqual("immutable", output.read_text())

    def test_cli_future_date_rejected_before_worker(self):
        argv = ["collect", "--tickers", "000001.SZ", "--start", "2026-09-18", "--end", "2026-09-21", "--output", "unused.json"]
        with patch("sys.argv", argv), patch.object(cli, "latest_completed_market_date", return_value="2026-09-18"), \
             patch.object(cli.subprocess, "run") as worker, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main()
            worker.assert_not_called()

    def test_sdk_request_is_raw_daily_and_logs_out_on_query_error(self):
        sdk = SimpleNamespace(login=Mock(return_value=SimpleNamespace(error_code="0")),
            logout=Mock(), query_history_k_data_plus=Mock(side_effect=ValueError("error")))
        self.collect(sdk)
        sdk.query_history_k_data_plus.assert_called_once_with("sz.000001", FIELDS,
            start_date="2026-09-08", end_date="2026-09-11", frequency="d", adjustflag="3")
        sdk.logout.assert_called_once()

    def test_stage_diagnostics_filter_arbitrary_sdk_error_text(self):
        self.assertEqual(["login_started"], cli.stage_markers(
            b"secret-like text\nCN_FACTS_STAGE:login_started\nCN_FACTS_STAGE:unknown\n"))

    def test_rehashed_status_and_missing_provenance_rejected(self):
        for field, value in (("status", "UNAVAILABLE"), ("collected_at", "2026-09-20T12:00:00"), ("sdk_version", "")):
            bundle = self.collect()
            bundle["payload"][field] = value
            bundle["sha256"] = digest(bundle["payload"])
            with self.assertRaises(ValueError):
                self.load(bundle)

    def test_empty_claim_cannot_hide_rows(self):
        bundle = self.collect()
        bundle["payload"]["queries"][0]["status"] = "EMPTY"
        bundle["payload"]["records"] = []
        bundle["payload"]["status"] = "UNAVAILABLE"
        bundle["sha256"] = digest(bundle["payload"])
        with self.assertRaises(ValueError):
            self.load(bundle)

    def test_real_parquet_audit_cli_joins_fixture_facts_without_mutating_lake(self):
        import polars as pl
        days = ("2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11",
                "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18")
        self.rows = [[day, *self.rows[0][1:]] for day in days]
        self.request.update(start_date=days[0], end_date=days[-1])
        bundle = self.collect()
        with TemporaryDirectory() as temp:
            root = Path(temp)
            originals = {}
            for fact in bundle["payload"]["records"]:
                path = root / "cn_daily" / f"date={fact['date']}" / "part.parquet"
                path.parent.mkdir(parents=True)
                row = {key: fact[key] for key in ("date", "symbol", "open", "high", "low", "close", "volume")}
                pl.DataFrame([row]).write_parquet(path)
                originals[path] = path.read_bytes()
            source = root / "facts.json"
            source.write_text(json.dumps(bundle))
            output = root / "audit.json"
            argv = ["audit", "--as-of", days[-1], "--sessions", "10", "--ticker-limit", "1",
                    "--horizon", "3", "--facts-bundle", str(source), "--output", str(output)]
            with patch("sys.argv", argv), patch.object(audit_cli, "market_lake_root", return_value=root), \
                 patch.object(audit_cli, "latest_completed_market_date", return_value=days[-1]), redirect_stdout(io.StringIO()):
                audit_cli.main()
            report = json.loads(output.read_text())
            self.assertEqual(10, report["execution_facts_join"]["counts"]["matched_rows"])
            self.assertIn(bundle["sha256"], report["source_reference"])
            self.assertEqual(7, report["status_counts"]["unknown"])
            self.assertEqual("BLOCKED", report["status"])
            self.assertEqual("NOT_RUN", report["model_backtest_status"])
            for path, original in originals.items():
                self.assertEqual(original, path.read_bytes())
