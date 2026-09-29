from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

import polars as pl

from app.services.stock_selection.artifacts import persist_sample_artifact, persist_universe_artifact
from app.services.stock_selection.sample_builder import SampleBuildResult
from app.services.stock_selection.schemas import LabeledSample, UniverseSnapshot
from app.services.stock_selection.universe import UniverseBuildResult


class StockSelectionArtifactTests(TestCase):
    def test_persists_immutable_universe_and_sample_manifests(self) -> None:
        trade_date = date(2026, 7, 1)
        universe_version = "pit_universe_v1:US:fixture"
        snapshot = UniverseSnapshot(
            snapshot_id="snapshot-1",
            market="US",
            trade_date=trade_date,
            ticker="AAA",
            included=True,
            source_as_of=datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc),
            universe_version=universe_version,
            price=100.0,
            adv20=100_000_000.0,
            volume=1_000_000.0,
        )
        universe = UniverseBuildResult(
            market="US",
            universe_version=universe_version,
            source_version="fixture-source-v1",
            rules_hash="rules-hash",
            snapshots=(snapshot,),
            included_count=1,
            excluded_count=0,
            exclusion_counts={},
        )
        dataset_version = "stock_selection_dataset_v1:US:fixture"
        sample = LabeledSample(
            sample_id="sample-1",
            market="US",
            ticker="AAA",
            feature_date=trade_date,
            label_start_date=trade_date + timedelta(days=1),
            label_end_date=trade_date + timedelta(days=3),
            label_available_date=trade_date + timedelta(days=3),
            horizon_days=3,
            label_value=0.05,
            features={"momentum": 0.1},
            dataset_version=dataset_version,
        )
        samples = SampleBuildResult(
            dataset_version=dataset_version,
            universe_version=universe_version,
            samples=(sample,),
            eligible_count=1,
            excluded_count=0,
            exclusion_counts={},
        )

        with TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            universe_write = persist_universe_artifact(universe, root=root / "universes")
            sample_write = persist_sample_artifact(samples, root=root / "samples")
            repeated = persist_sample_artifact(samples, root=root / "samples")

            self.assertTrue(universe_write.data_path.exists())
            self.assertTrue(sample_write.manifest_path.exists())
            self.assertTrue(repeated.reused_existing)
            self.assertEqual(1, pl.read_parquet(sample_write.data_path).height)
            manifest = json.loads(sample_write.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(sample_write.data_sha256, manifest["data_sha256"])
            self.assertEqual(dataset_version, manifest["dataset_version"])

            changed_samples = replace(samples, samples=(replace(sample, label_value=0.50),))
            with self.assertRaisesRegex(RuntimeError, "refusing to overwrite immutable artifact"):
                persist_sample_artifact(changed_samples, root=root / "samples")
