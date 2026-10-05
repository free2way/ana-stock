"""Registration, factory-loading, and PIT tests for the sentiment factor family.

The sentiment features are forward-only and are registered for research and
experiment consumption; they must stay out of the frozen research candidates and
the production training defaults.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from zoneinfo import ZoneInfo

from app.services.hithink_feature_store import persist_hithink_feature
from app.services.stock_selection.factor_pipeline import FactorDirection
from app.services.stock_selection.factor_sets import (
    get_research_factor_set,
    registered_factor_sets,
    research_factor_sets,
    sentiment_factor_set,
)
from app.services.stock_selection.feature_availability import (
    feature_availability_manifest,
    sentiment_feature_availability_entries,
)
from app.services.stock_selection.sentiment_features import (
    FEATURE_NAMES,
    HithinkSentimentObservation,
    SENTIMENT_FACTOR_SET_KEY,
    SentimentFeatureError,
    build_sentiment_feature_matrix,
    sentiment_feature_catalog_version,
    sentiment_feature_definitions,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _cutoff(day: str, hour: int, minute: int = 0) -> datetime:
    parsed = date.fromisoformat(day)
    return datetime(parsed.year, parsed.month, parsed.day, hour, minute, tzinfo=SHANGHAI)


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


class SentimentRegistrationMetadataTests(TestCase):
    def test_every_feature_has_complete_registration_metadata(self) -> None:
        definitions = sentiment_feature_definitions()

        self.assertEqual(set(FEATURE_NAMES), set(definitions))
        self.assertEqual(38, len(definitions))
        for name, definition in definitions.items():
            with self.subTest(feature=name):
                self.assertEqual(name, definition.name)
                self.assertEqual("hithink", definition.source)
                self.assertTrue(definition.forward_only)
                self.assertEqual("trailing_one_year", definition.coverage_window)
                self.assertIn(definition.availability_slot, {"eod", "auction"})
                self.assertTrue(definition.information_family)
                self.assertTrue(definition.missing_policy)

        eod = {d.name for d in definitions.values() if d.availability_slot == "eod"}
        auction = {d.name for d in definitions.values() if d.availability_slot == "auction"}
        self.assertEqual(10, len(auction))
        self.assertEqual(28, len(eod))
        self.assertEqual(set(FEATURE_NAMES), eod | auction)

    def test_availability_time_matches_eod_post_close_and_auction_pre_open(self) -> None:
        definitions = sentiment_feature_definitions()

        self.assertEqual("16:00", definitions["limit_up_flag"].available_time_local)
        self.assertEqual("eod", definitions["limit_up_flag"].availability_slot)
        self.assertEqual("09:25", definitions["auction_pct"].available_time_local)
        self.assertEqual("auction", definitions["auction_strength"].availability_slot)
        for definition in definitions.values():
            if definition.availability_slot == "eod":
                self.assertEqual("16:00", definition.available_time_local)
            else:
                self.assertEqual("09:25", definition.available_time_local)

    def test_missing_semantics_distinguish_exhaustive_zero_from_unknown(self) -> None:
        definitions = sentiment_feature_definitions()

        self.assertEqual(
            "zero_when_absent_from_exhaustive_family",
            definitions["limit_up_flag"].missing_policy,
        )
        self.assertEqual(
            "none_when_absent_even_in_exhaustive_family",
            definitions["limit_up_time_minutes"].missing_policy,
        )
        self.assertEqual(
            "none_when_absent_from_source", definitions["hot_rank"].missing_policy
        )

    def test_catalog_version_is_deterministic(self) -> None:
        self.assertEqual(
            sentiment_feature_catalog_version(), sentiment_feature_catalog_version()
        )


class SentimentFactoryRegistrationTests(TestCase):
    def test_factory_loads_the_sentiment_factor_set(self) -> None:
        factor_set = get_research_factor_set(SENTIMENT_FACTOR_SET_KEY)

        self.assertEqual(SENTIMENT_FACTOR_SET_KEY, factor_set.key)
        self.assertEqual(tuple(FEATURE_NAMES), factor_set.feature_names)
        self.assertEqual(38, len(factor_set.specs))
        self.assertEqual(factor_set.version(), sentiment_factor_set().version())
        self.assertTrue(
            all(
                spec.direction in {FactorDirection.HIGHER_BETTER, FactorDirection.LOWER_BETTER}
                for spec in factor_set.specs
            )
        )

    def test_sentiment_set_stays_out_of_the_frozen_research_candidates(self) -> None:
        self.assertNotIn(SENTIMENT_FACTOR_SET_KEY, research_factor_sets())
        self.assertIn(SENTIMENT_FACTOR_SET_KEY, registered_factor_sets())

    def test_unknown_factor_set_error_still_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown factor_set_key"):
            get_research_factor_set("missing_v1")


class SentimentAvailabilityManifestTests(TestCase):
    def test_sentiment_features_are_registered_in_the_availability_manifest(self) -> None:
        manifest = feature_availability_manifest()

        self.assertEqual(set(FEATURE_NAMES), set(manifest))
        for name, entry in manifest.items():
            with self.subTest(feature=name):
                self.assertEqual("hithink", entry.source)
                self.assertTrue(entry.forward_only)
                self.assertEqual("trailing_one_year", entry.coverage_window)
                self.assertEqual(SENTIMENT_FACTOR_SET_KEY, entry.factor_set_key)
        self.assertEqual("16:00", manifest["limit_up_flag"].available_time_local)
        self.assertEqual("09:25", manifest["auction_pct"].available_time_local)
        self.assertEqual(
            tuple(manifest.values()), sentiment_feature_availability_entries()
        )


class SentimentMatrixEntryTests(TestCase):
    def test_matrix_entry_covers_universe_and_cutoff(self) -> None:
        matrix = build_sentiment_feature_matrix(
            observations=[],
            universe_by_date={date(2026, 10, 9): ["600519.SS"]},
            cutoff_by_date={date(2026, 10, 9): _cutoff("2026-10-09", 17)},
        )

        self.assertEqual(SENTIMENT_FACTOR_SET_KEY, matrix.factor_set_key)
        self.assertEqual(tuple(FEATURE_NAMES), matrix.feature_names)
        row = matrix.row(date(2026, 10, 9), "600519.SS")
        self.assertEqual(38, len(row))
        self.assertTrue(all(value is None for value in row.values()))

    def test_matrix_entry_enforces_cutoff_coverage(self) -> None:
        universe = {date(2026, 9, 30): ["600000.SS"]}

        with self.assertRaises(SentimentFeatureError):
            build_sentiment_feature_matrix(
                observations=[], universe_by_date=universe, cutoff_by_date={}
            )
        with self.assertRaises(SentimentFeatureError):
            build_sentiment_feature_matrix(
                observations=[],
                universe_by_date=universe,
                cutoff_by_date={date(2026, 10, 1): _cutoff("2026-10-01", 9)},
            )
        with self.assertRaises(SentimentFeatureError):
            build_sentiment_feature_matrix(
                observations=[],
                universe_by_date=universe,
                cutoff_by_date={date(2026, 9, 30): datetime(2026, 9, 30, 9)},
            )

    def test_preopen_cutoff_never_sees_same_day_eod(self) -> None:
        previous = _obs(
            "limit_up_pool",
            "2026-09-30",
            {"timestamp": 1, "item": [{"thscode": "600825.SH", "continue_day_cnt": 7}]},
            ref="hithink:pool:prev",
        )
        same_day = _obs(
            "limit_up_pool",
            "2026-10-09",
            {"timestamp": 2, "item": [{"thscode": "600825.SH", "continue_day_cnt": 1}]},
            ref="hithink:pool:same-day",
        )
        universe = {date(2026, 10, 9): ["600825.SS"]}
        matrix = build_sentiment_feature_matrix(
            observations=[previous, same_day],
            universe_by_date=universe,
            cutoff_by_date={date(2026, 10, 9): _cutoff("2026-10-09", 9)},
        )

        self.assertEqual(7.0, matrix.row(date(2026, 10, 9), "600825.SS")["limit_up_streak"])
        self.assertEqual(
            "hithink:pool:prev",
            matrix.selected_observations_by_date[date(2026, 10, 9)]["limit_up_pool"],
        )

    def test_auction_features_gate_on_the_auction_time(self) -> None:
        auction = _obs(
            "auction_snapshot",
            "2026-10-09",
            {
                "timestamp": 1,
                "data_status": "final",
                "auction_phase": "closed",
                "item": [{"thscode": "600519.SH", "auction_pct": 0.35, "auction_volume_ratio": 1.12}],
            },
            slot="final",
        )
        universe = {date(2026, 10, 9): ["600519.SS"]}

        before = build_sentiment_feature_matrix(
            observations=[auction],
            universe_by_date=universe,
            cutoff_by_date={date(2026, 10, 9): _cutoff("2026-10-09", 9)},
        )
        after = build_sentiment_feature_matrix(
            observations=[auction],
            universe_by_date=universe,
            cutoff_by_date={date(2026, 10, 9): _cutoff("2026-10-09", 9, 30)},
        )

        self.assertIsNone(before.row(date(2026, 10, 9), "600519.SS")["auction_pct"])
        self.assertAlmostEqual(0.35, after.row(date(2026, 10, 9), "600519.SS")["auction_pct"])

    def test_matrix_entry_reads_the_traceable_store_and_keeps_missing_as_none(self) -> None:
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
            persist_hithink_feature(
                name="hot_stock_list", trade_date="2026-09-30", payload=envelope, root=root
            )
            matrix = build_sentiment_feature_matrix(
                root=root,
                universe_by_date={date(2026, 9, 30): ["300308.SZ", "600000.SS"]},
                cutoff_by_date={date(2026, 9, 30): _cutoff("2026-09-30", 17)},
            )

            listed = matrix.row(date(2026, 9, 30), "300308.SZ")
            unlisted = matrix.row(date(2026, 9, 30), "600000.SS")
            self.assertEqual(1.0, listed["hot_rank"])
            self.assertTrue(all(value is None for value in unlisted.values()))

    def test_matrix_entry_keeps_source_version_for_provenance(self) -> None:
        matrix = build_sentiment_feature_matrix(
            observations=[],
            universe_by_date={date(2026, 10, 9): ["600519.SS"]},
            cutoff_by_date={date(2026, 10, 9): _cutoff("2026-10-09", 17)},
        )

        self.assertTrue(matrix.source_version.startswith("hithink_sentiment_features_v1:"))
        self.assertEqual({date(2026, 10, 9): {}}, dict(matrix.selected_observations_by_date))
