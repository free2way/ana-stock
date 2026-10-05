"""Offline tests for the public HiThink featured-data/auction integration.

Covers: thin endpoint wrappers and their provenance, fail-closed behaviour,
traceable storage with content hashes, PIT-strict sentiment feature construction,
and an opt-in real-network smoke test (skipped unless PQW_HITHINK_SMOKE=1).
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import os
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.services.hithink_feature_store import (
    HithinkFeatureStoreError,
    canonical_content_sha256,
    load_hithink_feature_records,
    persist_hithink_feature,
)
from app.services.hithink_finance_client import HithinkFinanceAPIError, HithinkFinanceClient
from app.services.stock_selection.sentiment_features import (
    FEATURE_NAMES,
    HithinkSentimentObservation,
    SentimentFeatureError,
    build_sentiment_features,
    build_sentiment_features_from_store,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _payload(data, *, code=0, message="success", request_id="req-fixture"):
    return {"code": code, "message": message, "request_id": request_id, "data": data}


def _settings():
    return SimpleNamespace(
        hithink_finance_api_key="fixture-secret",
        hithink_finance_base_url="https://example.invalid",
        hithink_finance_timeout_seconds=1.0,
        hithink_finance_max_retries=0,
        hithink_finance_min_request_interval_seconds=0.0,
    )


def _client_with(payloads):
    """Patch urlopen and return (client, captured_request_urls)."""
    urls: list[str] = []

    def fake_urlopen(request, *args, **kwargs):  # noqa: ANN001
        urls.append(request.full_url)
        item = payloads.pop(0) if isinstance(payloads, list) else payloads
        return _FakeResponse(item)

    return urls, fake_urlopen


def _obs(name, day, data, *, slot=None, fetched="2026-09-30T16:05:00+08:00", ref=None):
    return HithinkSentimentObservation(
        feature_name=name,
        trade_date=date.fromisoformat(day),
        provider="hithink",
        source_reference=ref or f"hithink:{name}:fixture",
        fetched_at=fetched,
        data=data,
        slot=slot,
    )


def _cutoff(day: str, hour: int, minute: int = 0) -> datetime:
    return datetime(
        date.fromisoformat(day).year,
        date.fromisoformat(day).month,
        date.fromisoformat(day).day,
        hour,
        minute,
        tzinfo=SHANGHAI,
    )


# --------------------------------------------------------------------------- #
# Client wrappers: endpoints, provenance, fail-closed
# --------------------------------------------------------------------------- #
class HithinkFeaturedClientTests(unittest.TestCase):
    def _call(self, method_name, payload, **kwargs):
        urls, fake = _client_with(payload)
        with patch("app.services.hithink_finance_client.get_settings", return_value=_settings()), patch(
            "app.services.hithink_finance_client.urlopen", side_effect=fake
        ):
            client = HithinkFinanceClient()
            result = getattr(client, method_name)(**kwargs)
        return client, result, urls

    def test_limit_up_pool_wrapper_attaches_provenance_and_params(self):
        data = {"timestamp": 1791211759447, "pagination": {"total": 1, "pages": 1, "size": 50, "page": 1},
                "item": [{"thscode": "600825.SH", "continue_day_cnt": 7}]}
        _, result, urls = self._call(
            "fetch_limit_up_pool",
            _payload(data),
            trade_date="2026-09-30",
            size=50,
            sort_field="continue_day_cnt",
        )
        self.assertEqual("hithink", result["provider"])
        self.assertEqual("special-data/limit-up-pool", result["endpoint"])
        self.assertIn("limit-up-pool", result["source_reference"])
        self.assertIn("sort_field=continue_day_cnt", result["source_reference"])
        self.assertIn("date_ms=1790697600000", result["source_reference"])
        self.assertEqual("req-fixture", result["request_id"])
        datetime.fromisoformat(result["fetched_at"])
        self.assertEqual(data, result["data"])
        self.assertIn("/api/a-share/special-data/limit-up-pool?", urls[0])
        self.assertNotIn("fixture-secret", urls[0])

    def test_each_featured_wrapper_hits_the_documented_path(self):
        cases = {
            "fetch_limit_down_pool": ("special-data/limit-down-pool", {"trade_date": "2026-09-30"}),
            "fetch_limit_break_pool": ("special-data/limit-break-pool", {}),
            "fetch_limit_up_ladder": ("special-data/limit-up-ladder", {}),
            "fetch_skyrocket_list": ("special-data/skyrocket-list", {"period": "hour"}),
            "fetch_hot_stock_list": ("special-data/hot-stock-list", {"period": "day"}),
            "fetch_hot_stock_list_history": (
                "special-data/hot-stock-list-history",
                {"trade_date": "2026-06-21"},
            ),
            "fetch_hot_stock_rank_trend": (
                "special-data/hot-stock-rank-trend",
                {"ticker": "300034.SZ", "start_date": "2026-06-21", "end_date": "2026-07-01"},
            ),
            "fetch_dragon_tiger_list": ("special-data/dragon-tiger-list", {"board_type": "org", "trade_date": "2026-07-01"}),
            "fetch_anomaly_analysis_list": ("special-data/anomaly-analysis-list", {"tag_codes": ["LIMIT_UP", "sharp_fall"]}),
            "fetch_anomaly_analysis_stock": ("special-data/anomaly-analysis-stock", {"tickers": ["600519.SS"]}),
            "fetch_auction_short_term_benchmark": ("auction/short-term-benchmark", {"trade_date": "2026-08-14"}),
        }
        for method_name, (endpoint, kwargs) in cases.items():
            with self.subTest(method=method_name):
                _, result, urls = self._call(method_name, _payload({"timestamp": 1, "item": []}), **kwargs)
                self.assertEqual(endpoint, result["endpoint"])
                self.assertEqual("hithink", result["provider"])
                self.assertIn(f"/api/a-share/{endpoint}", urls[0])
                self.assertTrue(result["source_reference"].startswith(f"hithink:{endpoint}:"))

    def test_auction_snapshot_is_provenance_tagged(self):
        data = {
            "timestamp": 1786689000000,
            "auction_phase": "closed",
            "data_status": "final",
            "total": 1,
            "item": [{"thscode": "600519.SH", "auction_pct": 0.35}],
        }
        _, result, _ = self._call("fetch_auction_snapshot", _payload(data), tickers=["600519.SS"], stage="final")
        self.assertEqual("auction/snapshot", result["endpoint"])
        self.assertEqual("stage=final;thscodes=600519.SH", result["source_reference"].split(":", 2)[2])
        self.assertEqual(data["item"], result["data"]["item"])
        self.assertEqual("final", result["data"]["data_status"])

    def test_auction_snapshot_chunks_over_100_tickers(self):
        first = _payload({"timestamp": 1, "auction_phase": "closed", "data_status": "final",
                          "total": 1, "item": [{"thscode": "600000.SH", "auction_pct": 0.1}]})
        second = _payload({"timestamp": 2, "auction_phase": "closed", "data_status": "final",
                           "total": 1, "item": [{"thscode": "600100.SH", "auction_pct": 0.2}]})
        tickers = [f"{600000 + index}.SS" for index in range(150)]
        _, result, urls = self._call("fetch_auction_snapshot", [first, second], tickers=tickers)
        self.assertEqual(2, len(urls))
        self.assertEqual(2, len(result["data"]["item"]))
        self.assertEqual(2, result["data"]["total"])
        self.assertIsNone(result["request_id"])
        self.assertEqual(2, len(result["request_ids"]))
        self.assertEqual(2, result["data"]["timestamp"])

    def test_business_error_is_fail_closed_and_hides_the_key(self):
        urls, fake = _client_with(_payload(None, code=2003, message="permission denied"))
        with patch("app.services.hithink_finance_client.get_settings", return_value=_settings()), patch(
            "app.services.hithink_finance_client.urlopen", side_effect=fake
        ):
            with self.assertRaises(HithinkFinanceAPIError) as caught:
                HithinkFinanceClient().fetch_hot_stock_list()
        self.assertIn("permission denied", str(caught.exception))
        self.assertNotIn("fixture-secret", str(caught.exception))

    def test_empty_data_payload_is_fail_closed(self):
        _, fake = _client_with(_payload({}))
        with patch("app.services.hithink_finance_client.get_settings", return_value=_settings()), patch(
            "app.services.hithink_finance_client.urlopen", side_effect=fake
        ):
            with self.assertRaises(HithinkFinanceAPIError):
                HithinkFinanceClient().fetch_limit_up_ladder()

    def test_invalid_arguments_fail_closed_without_network(self):
        client = HithinkFinanceClient.__new__(HithinkFinanceClient)
        with self.assertRaises(ValueError):
            client.fetch_limit_up_pool(size=500)
        with self.assertRaises(ValueError):
            client.fetch_limit_up_pool(sort_field="not_a_field")
        with self.assertRaises(ValueError):
            client.fetch_limit_up_pool(page=0)
        with self.assertRaises(ValueError):
            client.fetch_hot_stock_list(period="minute")
        with self.assertRaises(ValueError):
            client.fetch_dragon_tiger_list(board_type="retail")
        with self.assertRaises(ValueError):
            client.fetch_anomaly_analysis_list(tag_codes="NOT_A_TAG")
        with self.assertRaises(ValueError):
            client.fetch_anomaly_analysis_stock(tickers=[f"{600000 + i}.SS" for i in range(51)])
        with self.assertRaises(ValueError):
            client.fetch_auction_snapshot(tickers=[], stage="final")
        with self.assertRaises(ValueError):
            client.fetch_auction_snapshot(tickers=["600519.SS"], stage="midday")
        with self.assertRaises(ValueError):
            client.fetch_hot_stock_rank_trend(ticker="600519.SS", start_date="2026-07-01", end_date="2026-06-21")
        with self.assertRaises(ValueError):
            client.fetch_hot_stock_list_history(trade_date=None)


# --------------------------------------------------------------------------- #
# Traceable storage
# --------------------------------------------------------------------------- #
class HithinkFeatureStoreTests(unittest.TestCase):
    def _envelope(self, data=None):
        return {
            "provider": "hithink",
            "endpoint": "special-data/limit-up-pool",
            "source_reference": "hithink:special-data/limit-up-pool:date_ms=1",
            "fetched_at": "2026-09-30T16:05:00+08:00",
            "request_id": "req-1",
            "params": {"date_ms": 1},
            "data": data if data is not None else {"timestamp": 1, "item": [{"thscode": "600825.SH"}]},
        }

    def test_persist_writes_dated_json_with_provenance_and_hash(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            result = persist_hithink_feature(
                name="limit_up_pool", trade_date="2026-09-30", payload=self._envelope(), root=root
            )
            expected = root / "hithink_features" / "limit_up_pool" / "date=2026-09-30.json"
            self.assertEqual(expected, result.path)
            self.assertFalse(result.reused_existing)
            record = json.loads(expected.read_text(encoding="utf-8"))
            self.assertEqual("hithink", record["provider"])
            self.assertEqual("hithink:special-data/limit-up-pool:date_ms=1", record["source_reference"])
            self.assertEqual("2026-09-30T16:05:00+08:00", record["fetched_at"])
            self.assertEqual(canonical_content_sha256(record["data"]), record["content_sha256"])

    def test_persist_is_idempotent_and_refuses_conflicting_content(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            persist_hithink_feature(name="hot_stock_list", trade_date="2026-09-30", payload=self._envelope(), root=root)
            reused = persist_hithink_feature(name="hot_stock_list", trade_date="2026-09-30", payload=self._envelope(), root=root)
            self.assertTrue(reused.reused_existing)
            conflicting = self._envelope(data={"timestamp": 2, "item": []})
            with self.assertRaises(HithinkFeatureStoreError):
                persist_hithink_feature(name="hot_stock_list", trade_date="2026-09-30", payload=conflicting, root=root)

    def test_slot_disambiguates_variants(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            persist_hithink_feature(name="auction_snapshot", trade_date="2026-09-30", payload=self._envelope(), root=root, slot="live")
            persist_hithink_feature(name="auction_snapshot", trade_date="2026-09-30", payload=self._envelope(data={"timestamp": 3, "item": []}), root=root, slot="final")
            names = sorted(path.name for path in (root / "hithink_features" / "auction_snapshot").glob("*.json"))
            self.assertEqual(["date=2026-09-30.final.json", "date=2026-09-30.live.json"], names)

    def test_persist_rejects_missing_provenance_or_data(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(HithinkFeatureStoreError):
                persist_hithink_feature(name="x", trade_date="2026-09-30", payload={"data": {}}, root=root)
            broken = {**self._envelope(), "data": None}
            with self.assertRaises(HithinkFeatureStoreError):
                persist_hithink_feature(name="x", trade_date="2026-09-30", payload=broken, root=root)
            with self.assertRaises(HithinkFeatureStoreError):
                persist_hithink_feature(name="../escape", trade_date="2026-09-30", payload=self._envelope(), root=root)

    def test_load_fails_closed_on_content_hash_mismatch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            written = persist_hithink_feature(name="limit_up_pool", trade_date="2026-09-30", payload=self._envelope(), root=root)
            tampered = json.loads(written.path.read_text(encoding="utf-8"))
            tampered["data"]["item"] = []
            written.path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaises(HithinkFeatureStoreError):
                load_hithink_feature_records(root=root)


# --------------------------------------------------------------------------- #
# PIT-strict sentiment features
# --------------------------------------------------------------------------- #
class SentimentFeatureTests(unittest.TestCase):
    def test_featured_data_not_available_before_post_close(self):
        pool = _obs("limit_up_pool", "2026-09-30", {"timestamp": 1, "item": [
            {"thscode": "600825.SH", "continue_day_cnt": 7, "seal_money": 100.0, "price_change_ratio_pct": 9.9},
        ]})
        universe = {date(2026, 9, 30): ["600825.SS", "000001.SZ"]}
        early = build_sentiment_features([pool], universe_by_date=universe, cutoff_by_date={date(2026, 9, 30): _cutoff("2026-09-30", 10)})
        self.assertIsNone(early.features_by_key[(date(2026, 9, 30), "600825.SS")]["limit_up_flag"])
        self.assertIsNone(early.features_by_key[(date(2026, 9, 30), "000001.SZ")]["limit_up_flag"])

        late = build_sentiment_features([pool], universe_by_date=universe, cutoff_by_date={date(2026, 9, 30): _cutoff("2026-09-30", 16, 30)})
        listed = late.features_by_key[(date(2026, 9, 30), "600825.SS")]
        absent = late.features_by_key[(date(2026, 9, 30), "000001.SZ")]
        self.assertEqual(1.0, listed["limit_up_flag"])
        self.assertEqual(7.0, listed["limit_up_streak"])
        # exhaustive pool: absent symbol is a true 0 membership, but a price field stays unknown (None)
        self.assertEqual(0.0, absent["limit_up_flag"])
        self.assertEqual(0.0, absent["limit_up_streak"])
        self.assertIsNone(absent["limit_up_change_pct"])

    def test_preopen_uses_previous_session_eod_not_same_session(self):
        prev = _obs("limit_up_pool", "2026-09-30", {"timestamp": 1, "item": [{"thscode": "600825.SH", "continue_day_cnt": 7}]}, ref="hithink:pool:prev")
        same = _obs("limit_up_pool", "2026-10-09", {"timestamp": 2, "item": [{"thscode": "600825.SH", "continue_day_cnt": 1}]}, ref="hithink:pool:same-day")
        universe = {date(2026, 10, 9): ["600825.SS"]}
        build = build_sentiment_features(
            [prev, same], universe_by_date=universe, cutoff_by_date={date(2026, 10, 9): _cutoff("2026-10-09", 9)}
        )
        self.assertEqual(7.0, build.features_by_key[(date(2026, 10, 9), "600825.SS")]["limit_up_streak"])
        self.assertEqual("hithink:pool:prev", build.selected_observations_by_date[date(2026, 10, 9)]["limit_up_pool"])

    def test_auction_only_available_at_or_after_auction_time(self):
        auction = _obs(
            "auction_snapshot",
            "2026-10-09",
            {"timestamp": 1, "data_status": "final", "auction_phase": "closed", "item": [
                {"thscode": "600519.SH", "auction_pct": 0.35, "auction_volume_ratio": 1.12, "auction_price": 1421.0},
            ]},
            slot="final",
        )
        universe = {date(2026, 10, 9): ["600519.SS"]}
        before = build_sentiment_features([auction], universe_by_date=universe, cutoff_by_date={date(2026, 10, 9): _cutoff("2026-10-09", 9)})
        self.assertIsNone(before.features_by_key[(date(2026, 10, 9), "600519.SS")]["auction_pct"])
        after = build_sentiment_features([auction], universe_by_date=universe, cutoff_by_date={date(2026, 10, 9): _cutoff("2026-10-09", 9, 30)})
        row = after.features_by_key[(date(2026, 10, 9), "600519.SS")]
        self.assertAlmostEqual(0.35, row["auction_pct"])
        self.assertAlmostEqual(0.392, row["auction_strength"])
        self.assertIn("auction_snapshot", after.selected_observations_by_date[date(2026, 10, 9)])

    def test_live_auction_uses_fetch_time_and_not_ready_is_unusable(self):
        live = _obs(
            "auction_snapshot",
            "2026-10-09",
            {"timestamp": 1, "data_status": "ready", "item": [{"thscode": "600519.SH", "auction_pct": 0.35}]},
            slot="live",
            fetched="2026-10-09T09:19:00+08:00",
        )
        universe = {date(2026, 10, 9): ["600519.SS"]}
        at_09_15 = build_sentiment_features([live], universe_by_date=universe, cutoff_by_date={date(2026, 10, 9): _cutoff("2026-10-09", 9, 15)})
        self.assertIsNone(at_09_15.features_by_key[(date(2026, 10, 9), "600519.SS")]["auction_pct"])
        at_09_20 = build_sentiment_features([live], universe_by_date=universe, cutoff_by_date={date(2026, 10, 9): _cutoff("2026-10-09", 9, 20)})
        self.assertAlmostEqual(0.35, at_09_20.features_by_key[(date(2026, 10, 9), "600519.SS")]["auction_pct"])

        not_ready = _obs(
            "auction_snapshot",
            "2026-10-09",
            {"timestamp": 1, "data_status": "not_ready", "item": [{"thscode": "600519.SH", "auction_pct": 0.99}]},
            slot="final",
        )
        gated = build_sentiment_features([not_ready], universe_by_date=universe, cutoff_by_date={date(2026, 10, 9): _cutoff("2026-10-09", 10)})
        self.assertIsNone(gated.features_by_key[(date(2026, 10, 9), "600519.SS")]["auction_pct"])

    def test_missing_source_returns_none_not_zero(self):
        universe = {date(2026, 10, 9): ["600519.SS"]}
        build = build_sentiment_features([], universe_by_date=universe, cutoff_by_date={date(2026, 10, 9): _cutoff("2026-10-09", 17)})
        row = build.features_by_key[(date(2026, 10, 9), "600519.SS")]
        self.assertTrue(all(value is None for value in row.values()))
        self.assertEqual("hithink_sentiment_features_v1", build.schema_version)

    def test_hot_rank_percentile_and_unlisted_is_unknown(self):
        items = [{"thscode": f"300{index:03d}.SZ", "rank": index, "rank_change": 1, "heat": "100"} for index in range(1, 31)]
        hot = _obs("hot_stock_list", "2026-09-30", {"timestamp": 1, "item": items})
        universe = {date(2026, 9, 30): ["300001.SZ", "300030.SZ", "600000.SS"]}
        build = build_sentiment_features([hot], universe_by_date=universe, cutoff_by_date={date(2026, 9, 30): _cutoff("2026-09-30", 17)})
        self.assertAlmostEqual(1.0, build.features_by_key[(date(2026, 9, 30), "300001.SZ")]["hot_rank_score"])
        self.assertAlmostEqual(1.0 / 30.0, build.features_by_key[(date(2026, 9, 30), "300030.SZ")]["hot_rank_score"])
        unlisted = build.features_by_key[(date(2026, 9, 30), "600000.SS")]
        self.assertIsNone(unlisted["hot_rank"])
        self.assertIsNone(unlisted["hot_rank_score"])

    def test_ladder_and_dragon_tiger_and_anomaly_extraction(self):
        ladder = _obs("limit_up_ladder", "2026-09-30", {"timestamp": 1, "window": {"date_list": ["20260930"]}, "item": [
            {"date": "20260930", "boards": {
                "two_board": [{"thscode": "603200.SH", "board_num": 2, "sign_level": 1, "seal_nextday": False}],
                "seven_over": [{"thscode": "600825.SH", "board_num": 7, "sign_level": 0, "seal_nextday": True}],
            }},
        ]})
        dragon = _obs("dragon_tiger_list", "2026-09-30", {"timestamp": 1, "stock_items": [
            {"thscode": "000002.SZ", "net_value": 100.0, "net_rate": 0.02, "hot_rank": 1, "range_days": 3},
        ]})
        anomaly = _obs("anomaly_analysis_list", "2026-09-30", {"timestamp": 1, "item": [
            {"thscode": "600825.SH", "tag_name": "涨停"},
            {"thscode": "600825.SH", "tag_name": "快速拉升"},
        ]})
        universe = {date(2026, 9, 30): ["603200.SS", "600825.SS", "000002.SZ"]}
        build = build_sentiment_features(
            [ladder, dragon, anomaly], universe_by_date=universe, cutoff_by_date={date(2026, 9, 30): _cutoff("2026-09-30", 17)}
        )
        rows = build.features_by_key
        self.assertEqual(2.0, rows[(date(2026, 9, 30), "603200.SS")]["ladder_board_num"])
        self.assertEqual(7.0, rows[(date(2026, 9, 30), "600825.SS")]["ladder_board_num"])
        self.assertEqual(1.0, rows[(date(2026, 9, 30), "000002.SZ")]["dragon_tiger_flag"])
        self.assertEqual(100.0, rows[(date(2026, 9, 30), "000002.SZ")]["dragon_tiger_net_value"])
        self.assertEqual(2.0, rows[(date(2026, 9, 30), "600825.SS")]["anomaly_count"])
        self.assertEqual(1.0, rows[(date(2026, 9, 30), "600825.SS")]["anomaly_limit_up_flag"])
        self.assertEqual(0.0, rows[(date(2026, 9, 30), "603200.SS")]["dragon_tiger_flag"])
        # next-day seal outcome must never leak in as a feature
        self.assertNotIn("seal_nextday", FEATURE_NAMES)

    def test_cutoff_coverage_is_enforced(self):
        universe = {date(2026, 9, 30): ["600000.SS"]}
        with self.assertRaises(SentimentFeatureError):
            build_sentiment_features([], universe_by_date=universe, cutoff_by_date={})
        with self.assertRaises(SentimentFeatureError):
            build_sentiment_features([], universe_by_date=universe, cutoff_by_date={date(2026, 10, 1): _cutoff("2026-10-01", 9)})
        with self.assertRaises(SentimentFeatureError):
            build_sentiment_features(
                [],
                universe_by_date=universe,
                cutoff_by_date={date(2026, 9, 30): datetime(2026, 9, 30, 9)},
            )
        with self.assertRaises(SentimentFeatureError):
            build_sentiment_features([], universe_by_date={}, cutoff_by_date={})

    def test_unknown_record_feature_name_is_fail_closed(self):
        from app.services.stock_selection.sentiment_features import observations_from_records

        with self.assertRaises(SentimentFeatureError):
            observations_from_records([{"feature_name": "news", "trade_date": "2026-09-30",
                                        "provider": "hithink", "source_reference": "x", "fetched_at": "y", "data": {}}])

    def test_store_roundtrip_builds_features(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            envelope = {
                "provider": "hithink",
                "endpoint": "special-data/hot-stock-list",
                "source_reference": "hithink:special-data/hot-stock-list:period=day",
                "fetched_at": "2026-09-30T16:05:00+08:00",
                "request_id": "req-1",
                "params": {"period": "day"},
                "data": {"timestamp": 1, "item": [{"thscode": "300308.SZ", "rank": 1}]},
            }
            persist_hithink_feature(name="hot_stock_list", trade_date="2026-09-30", payload=envelope, root=root)
            build = build_sentiment_features_from_store(
                root=root,
                universe_by_date={date(2026, 9, 30): ["300308.SZ"]},
                cutoff_by_date={date(2026, 9, 30): _cutoff("2026-09-30", 17)},
            )
            self.assertEqual(1.0, build.features_by_key[(date(2026, 9, 30), "300308.SZ")]["hot_rank"])
            self.assertIn("hot-stock-list", build.selected_observations_by_date[date(2026, 9, 30)]["hot_stock_list"])

    def test_source_version_tracks_selected_provenance(self):
        obs_a = _obs("limit_up_pool", "2026-09-30", {"timestamp": 1, "item": []}, ref="hithink:pool:a")
        obs_b = _obs("limit_up_pool", "2026-09-30", {"timestamp": 1, "item": []}, ref="hithink:pool:b")
        universe = {date(2026, 9, 30): ["600000.SS"]}
        cutoffs = {date(2026, 9, 30): _cutoff("2026-09-30", 17)}
        version_a = build_sentiment_features([obs_a], universe_by_date=universe, cutoff_by_date=cutoffs).source_version
        version_b = build_sentiment_features([obs_b], universe_by_date=universe, cutoff_by_date=cutoffs).source_version
        self.assertNotEqual(version_a, version_b)


@unittest.skipUnless(os.environ.get("PQW_HITHINK_SMOKE") == "1", "set PQW_HITHINK_SMOKE=1 to hit the live API")
class HithinkLiveSmokeTests(unittest.TestCase):
    def test_live_auction_snapshot_smoke(self):
        client = HithinkFinanceClient()
        if not client.is_configured():
            self.skipTest("PQW_HITHINK_FINANCE_API_KEY is not configured")
        result = client.fetch_auction_snapshot(tickers=["600519.SS", "000001.SZ"], stage="final")
        self.assertEqual("hithink", result["provider"])
        self.assertIn("auction/snapshot", result["source_reference"])
        self.assertIsInstance(result["data"]["item"], list)


if __name__ == "__main__":
    unittest.main()
