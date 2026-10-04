from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import requests

from app.services.stock_selection.data_readiness import (
    DataReadinessConfig,
    assess_data_readiness,
)
from app.services.stock_selection.historical_universe_contract import (
    HISTORICAL_UNIVERSE_CONTRACT_SCHEMA_VERSION,
    build_historical_universe_contract,
    default_historical_universe_contract_path,
    load_historical_universe_contract,
    load_historical_universe_contract_for_market,
    resolve_historical_universe_contract_path,
)
from scripts.build_historical_universe_contract import _download_sw_industry_bytes

ALL_DIMENSIONS = (
    "historical_security_master_verified",
    "historical_membership_verified",
    "delisting_history_verified",
    "historical_industry_verified",
    "universe_revision_history_verified",
)


def _valid_payload(market: str = "CN") -> dict:
    return {
        "schema_version": HISTORICAL_UNIVERSE_CONTRACT_SCHEMA_VERSION,
        "market": market,
        "dimensions": {name: True for name in ALL_DIMENSIONS},
    }


def _master_rows() -> dict:
    return {
        "listed": [
            {"ts_code": "000001.SZ", "name": "平安银行", "list_date": "19910403"},
            {"ts_code": "600000.SH", "name": "浦发银行", "list_date": "19991110"},
        ],
        "paused": [],
        "delisted": [{"ts_code": "000002.SZ", "name": "万科A", "delist_date": "20250101"}],
        "name_changes": [
            {"ts_code": "000001.SZ", "name": "深发展A", "start_date": "19910403", "end_date": "20120601"},
            {"ts_code": "000001.SZ", "name": "平安银行", "start_date": "20120601", "end_date": None},
            {"ts_code": "000002.SZ", "name": "万科A", "start_date": "19910101", "end_date": None},
        ],
        "cross_check": {"active_tickers": ["000001.SZ", "600000.SS"]},
    }


class HistoricalUniverseContractLoaderTests(TestCase):
    def test_missing_artifact_is_none_and_keeps_gate_fail_closed(self) -> None:
        with TemporaryDirectory() as temporary_name:
            path = Path(temporary_name) / "absent.json"
            self.assertIsNone(load_historical_universe_contract(path, market="CN"))
            self.assertIsNone(
                load_historical_universe_contract_for_market(
                    market="CN", artifacts_dir=Path(temporary_name)
                )
            )

    def test_present_artifact_is_loaded_with_all_dimensions(self) -> None:
        with TemporaryDirectory() as temporary_name:
            path = Path(temporary_name) / "contract.json"
            path.write_text(json.dumps(_valid_payload()), encoding="utf-8")
            loaded = load_historical_universe_contract(path, market="CN")

        self.assertEqual({name: True for name in ALL_DIMENSIONS}, loaded)

    def test_partial_dimensions_default_to_false(self) -> None:
        with TemporaryDirectory() as temporary_name:
            path = Path(temporary_name) / "contract.json"
            payload = _valid_payload()
            payload["dimensions"] = {"delisting_history_verified": True}
            path.write_text(json.dumps(payload), encoding="utf-8")
            loaded = load_historical_universe_contract(path, market="CN")

        assert loaded is not None
        self.assertTrue(loaded["delisting_history_verified"])
        self.assertFalse(loaded["historical_membership_verified"])
        self.assertFalse(loaded["historical_security_master_verified"])

    def test_invalid_or_mismatched_artifact_raises(self) -> None:
        with TemporaryDirectory() as temporary_name:
            path = Path(temporary_name) / "contract.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_historical_universe_contract(path, market="CN")

            path.write_text(json.dumps({"schema_version": "wrong"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_historical_universe_contract(path, market="CN")

            path.write_text(json.dumps(_valid_payload(market="US")), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_historical_universe_contract(path, market="CN")

    def test_resolution_prefers_explicit_then_settings_then_default(self) -> None:
        artifacts_dir = Path("/tmp/artifacts")
        default = default_historical_universe_contract_path(artifacts_dir)
        self.assertEqual(
            default,
            resolve_historical_universe_contract_path(artifacts_dir=artifacts_dir),
        )
        self.assertEqual(
            Path("/tmp/settings.json"),
            resolve_historical_universe_contract_path(
                artifacts_dir=artifacts_dir, settings_path="/tmp/settings.json"
            ),
        )
        self.assertEqual(
            Path("/tmp/explicit.json"),
            resolve_historical_universe_contract_path(
                artifacts_dir=artifacts_dir,
                explicit_path="/tmp/explicit.json",
                settings_path="/tmp/settings.json",
            ),
        )

    def test_loaded_contract_satisfies_the_readiness_gate(self) -> None:
        metrics = {
            f"S{index:03d}": {"history_days": 252, "duplicate_conflict_days": 0}
            for index in range(10)
        }
        with TemporaryDirectory() as temporary_name:
            path = Path(temporary_name) / "contract.json"
            path.write_text(json.dumps(_valid_payload("US")), encoding="utf-8")
            contract = load_historical_universe_contract(path, market="US")

        report = assess_data_readiness(
            metrics,
            security_types={ticker: "equity" for ticker in metrics},
            metadata_present={ticker: True for ticker in metrics},
            industries={ticker: "TECH" for ticker in metrics},
            config=DataReadinessConfig(
                market="US",
                minimum_eligible_symbols=10,
                minimum_history_coverage=1.0,
                minimum_metadata_coverage=1.0,
                minimum_industry_coverage=1.0,
                require_historical_universe_contract=True,
            ),
            historical_universe_contract=contract,
        )
        self.assertTrue(report.passed)


class HistoricalUniverseContractBuilderTests(TestCase):
    def test_missing_permission_never_backfills_true(self) -> None:
        payload = build_historical_universe_contract(
            market="CN",
            listed=[],
            paused=[],
            delisted=[],
            name_changes=[],
            endpoint_errors={
                "stock_basic_delisted": "PermissionError: 没有接口访问权限",
                "stock_basic_listed": "PermissionError: 没有接口访问权限",
                "namechange": "PermissionError: 没有接口访问权限",
            },
        )
        self.assertEqual({name: False for name in ALL_DIMENSIONS}, payload["dimensions"])
        self.assertTrue(all(payload["missing_reasons"][name] for name in ALL_DIMENSIONS))
        self.assertIn("PermissionError", payload["provider_endpoint_errors"]["stock_basic_delisted"])

    def test_membership_industry_and_revision_stay_false_even_with_master_evidence(self) -> None:
        payload = build_historical_universe_contract(market="CN", **_master_rows())
        dimensions = payload["dimensions"]
        self.assertTrue(dimensions["delisting_history_verified"])
        self.assertTrue(dimensions["historical_security_master_verified"])
        self.assertFalse(dimensions["historical_membership_verified"])
        self.assertFalse(dimensions["historical_industry_verified"])
        self.assertFalse(dimensions["universe_revision_history_verified"])
        self.assertIn("index_member", payload["missing_reasons"]["historical_membership_verified"])

    def test_active_store_overlap_is_recorded_as_warning_not_hidden(self) -> None:
        rows = _master_rows()
        rows["cross_check"] = {"active_tickers": ["000001.SZ", "000002.SZ"]}
        payload = build_historical_universe_contract(market="CN", **rows)
        self.assertTrue(payload["dimensions"]["delisting_history_verified"])
        delisting = payload["evidence"]["delisting_history"]
        self.assertEqual(1, delisting["db_active_overlap_count"])
        self.assertEqual(["000002.SZ"], delisting["db_active_overlap_symbols"])
        self.assertTrue(any("store_staleness" in item for item in payload["warnings"]))

    def test_delisting_requires_real_dated_rows(self) -> None:
        payload = build_historical_universe_contract(
            market="CN",
            listed=[{"ts_code": "000001.SZ", "name": "A", "list_date": "19910403"}],
            delisted=[{"ts_code": "000002.SZ", "name": "B", "delist_date": ""}],
            cross_check={"active_tickers": ["000001.SZ"]},
        )
        self.assertFalse(payload["dimensions"]["delisting_history_verified"])
        self.assertEqual(1, payload["evidence"]["delisting_history"]["missing_delist_date_count"])

    def test_missing_namechange_endpoint_blocks_master_verification(self) -> None:
        rows = _master_rows()
        rows["name_changes"] = []
        rows["errors"] = {"namechange": "PermissionError: no access"}
        payload = build_historical_universe_contract(
            market="CN",
            listed=rows["listed"],
            paused=rows["paused"],
            delisted=rows["delisted"],
            name_changes=rows["name_changes"],
            cross_check=rows["cross_check"],
            endpoint_errors=rows["errors"],
        )
        self.assertFalse(payload["dimensions"]["historical_security_master_verified"])
        self.assertIn("namechange", payload["missing_reasons"]["historical_security_master_verified"])


class HistoricalUniverseContractIndustryTests(TestCase):
    """The industry dimension is driven by real effective-dated SWS history."""

    _CODES = {"110101": "种子", "440101": "银行", "480301": "股份制银行Ⅲ"}

    def _industry_rows(self, *, unknown_share: float = 0.0) -> list[dict]:
        rows: list[dict] = []
        known = sorted(self._CODES)
        for index in range(120):
            if index % 3 == 0:
                ts_code = f"{600000 + index}.SH"
            elif index % 3 == 1:
                ts_code = f"{1 + index:06d}.SZ"
            else:
                ts_code = f"{830000 + index}.BJ"
            for offset, year in enumerate(range(2005, 2016)):
                use_unknown = unknown_share > 0 and (index + offset) % 2 == 0
                code = "999999" if use_unknown else known[(index + offset) % len(known)]
                rows.append(
                    {
                        "ts_code": ts_code,
                        "effective_date": f"{year}0601",
                        "industry_code": code,
                    }
                )
        return rows

    def test_effective_dated_industry_history_verifies_only_that_dimension(self) -> None:
        payload = build_historical_universe_contract(
            market="CN",
            industry_rows=self._industry_rows(),
            industry_code_names=self._CODES,
        )
        dimensions = payload["dimensions"]
        self.assertTrue(dimensions["historical_industry_verified"])
        self.assertFalse(dimensions["historical_security_master_verified"])
        self.assertFalse(dimensions["historical_membership_verified"])
        self.assertFalse(dimensions["delisting_history_verified"])
        self.assertFalse(dimensions["universe_revision_history_verified"])
        self.assertNotIn("historical_industry_verified", payload["missing_reasons"])
        evidence = payload["evidence"]["historical_industry"]
        self.assertEqual(120, evidence["symbol_count"])
        self.assertEqual(1.0, evidence["industry_code_mapping_rate"])
        self.assertEqual("20050601", evidence["min_effective_date"])
        self.assertEqual("20150601", evidence["max_effective_date"])
        self.assertEqual({"SH", "SZ", "BJ"}, set(evidence["exchange_counts"]))
        self.assertEqual(5, len(evidence["sample_rows"]))

    def test_industry_provider_failure_stays_false_with_reason(self) -> None:
        payload = build_historical_universe_contract(
            market="CN",
            industry_rows=[],
            industry_code_names={},
            endpoint_errors={"sws_industry_hist": "SSLError: certificate verify failed"},
        )
        self.assertFalse(payload["dimensions"]["historical_industry_verified"])
        reason = payload["missing_reasons"]["historical_industry_verified"]
        self.assertIn("sws_industry_hist", reason)
        self.assertIn("certificate verify failed", reason)

    def test_industry_mapping_rate_below_threshold_stays_false(self) -> None:
        payload = build_historical_universe_contract(
            market="CN",
            industry_rows=self._industry_rows(unknown_share=1.0),
            industry_code_names=self._CODES,
        )
        self.assertFalse(payload["dimensions"]["historical_industry_verified"])
        reason = payload["missing_reasons"]["historical_industry_verified"]
        self.assertIn("unknown industry-code mapping rate", reason)
        evidence = payload["evidence"]["historical_industry"]
        self.assertLess(evidence["industry_code_mapping_rate"], 0.80)
        self.assertGreater(evidence["unknown_industry_code_mapping_rate"], 0.20)

    def test_industry_non_monotonic_dates_block_verification(self) -> None:
        rows = self._industry_rows()
        rows[0]["effective_date"] = "20300101"
        payload = build_historical_universe_contract(
            market="CN",
            industry_rows=rows,
            industry_code_names=self._CODES,
        )
        self.assertFalse(payload["dimensions"]["historical_industry_verified"])
        reason = payload["missing_reasons"]["historical_industry_verified"]
        self.assertIn("not monotonic", reason)


class SwsIndustryDownloadRobustnessTests(TestCase):
    """The SWS XLS download must degrade explicitly, never silently."""

    def test_insecure_proxy_fallback_is_used_and_caveat_recorded(self) -> None:
        verify_calls: list[bool] = []

        class _Response:
            status_code = 200
            content = b"xls-bytes"

        class _Session:
            headers: dict = {}

            def __enter__(self) -> "_Session":
                return self

            def __exit__(self, *_args: object) -> bool:
                return False

            def get(self, url: str, timeout: float | None = None, **kwargs: object) -> "_Response":
                verify = bool(kwargs.get("verify", True))
                verify_calls.append(verify)
                if verify:
                    raise requests.exceptions.SSLError("certificate verify failed")
                return _Response()

        errors: dict[str, str] = {}
        caveats: list[str] = []
        with patch("requests.Session", _Session), patch("time.sleep", lambda *_a: None):
            content = _download_sw_industry_bytes(
                errors=errors, caveats=caveats, label="sws_industry_hist", timeout=1.0
            )

        self.assertEqual(b"xls-bytes", content)
        self.assertEqual({}, errors)
        self.assertEqual(["sws_xls_tls_verification_disabled"], caveats)
        self.assertEqual([True, True, True, True, False], verify_calls)

    def test_total_download_failure_is_recorded_not_silent(self) -> None:
        class _Session:
            headers: dict = {}

            def __enter__(self) -> "_Session":
                return self

            def __exit__(self, *_args: object) -> bool:
                return False

            def get(self, url: str, timeout: float | None = None, **kwargs: object) -> object:
                raise requests.exceptions.ConnectionError("proxy refused")

        errors: dict[str, str] = {}
        caveats: list[str] = []
        with patch("requests.Session", _Session), patch("time.sleep", lambda *_a: None):
            content = _download_sw_industry_bytes(
                errors=errors, caveats=caveats, label="sws_industry_hist", timeout=1.0
            )

        self.assertIsNone(content)
        self.assertEqual([], caveats)
        self.assertIn("sws_industry_hist", errors)
        self.assertIn("ConnectionError", errors["sws_industry_hist"])


class HistoricalUniverseContractInjectorTests(TestCase):
    """The readiness gate must read the conventional artifact path when present."""

    def test_conventional_path_is_picked_up_by_market_loader(self) -> None:
        with TemporaryDirectory() as temporary_name:
            artifacts_dir = Path(temporary_name)
            target = default_historical_universe_contract_path(artifacts_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(_valid_payload("CN")), encoding="utf-8")

            loaded = load_historical_universe_contract_for_market(
                market="CN", artifacts_dir=artifacts_dir
            )
            explicit_override = load_historical_universe_contract_for_market(
                market="CN",
                artifacts_dir=artifacts_dir,
                explicit_path=Path(temporary_name) / "does-not-exist.json",
            )

        self.assertEqual({name: True for name in ALL_DIMENSIONS}, loaded)
        self.assertIsNone(explicit_override)
