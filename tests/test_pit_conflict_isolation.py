"""Regression: a PIT revision-identity collision is isolated per record.

Before this change a single colliding append raised ``RuntimeError`` inside the
shared batch transaction, so every other record in the batch rolled back with
it (the ``roe_avg_3y`` key defect in 01bb32b was one such case). The collision
is now captured in an append-only conflict ledger while the remaining records
keep landing, in the same fashion as the real ingestion loops in
``app/services/cn_fundamentals.py`` / ``app/services/global_fundamentals.py``:
``commit=False`` per record and one ``commit()`` for the whole batch.
"""
from __future__ import annotations

import unittest

from sqlalchemy import func, select

from tests.postgres_safety import ApplicationPostgresTestCase

from app.core.db import SessionLocal
from app.models.schema import SymbolCreate
from app.models.tables import PointInTimeFeatureSnapshot
from app.services.repository import (
    PointInTimeFeatureSnapshotRepository,
    SymbolRepository,
)


def _append(repo, *, symbol_id, feature_name, value, record_id, revision_id):
    return repo.append_snapshot(
        symbol_id=symbol_id,
        feature_name=feature_name,
        feature_value=value,
        event_time="2026-06-30T15:00:00+08:00",
        available_time="2026-07-01T09:00:00+08:00",
        ingested_time="2026-07-01T09:01:00+08:00",
        source="pit-conflict-test",
        source_record_id=record_id,
        revision_id=revision_id,
        commit=False,
    )


class PitConflictIsolationTests(ApplicationPostgresTestCase):
    def _seed_symbol(self, db) -> int:
        symbol = SymbolRepository(db).get_or_create_symbol(
            SymbolCreate(ticker="600001.SS", name="冲突隔离测试", market="CN", exchange="SSE")
        )
        return int(symbol.id)

    def test_one_conflict_does_not_roll_back_the_rest_of_the_batch(self) -> None:
        with SessionLocal() as db:
            symbol_id = self._seed_symbol(db)
            repo = PointInTimeFeatureSnapshotRepository(db)

            # Seed one identity, then reuse it with a different value: that is
            # exactly the collision that used to abort the whole batch.
            original, inserted = _append(
                repo,
                symbol_id=symbol_id,
                feature_name="roe_avg_3y",
                value=12.5,
                record_id="600001.SS:2026-06-30",
                revision_id="rev-1",
            )
            self.assertTrue(inserted)
            original_id = int(original.id)

            room, room_inserted = _append(
                repo,
                symbol_id=symbol_id,
                feature_name="roe_avg_3y",
                value=99.9,
                record_id="600001.SS:2026-06-30",
                revision_id="rev-1",
            )
            # The call must not raise; it reports "not inserted" and points at
            # the historical row (history is never overwritten).
            self.assertFalse(room_inserted)
            self.assertEqual(original.id, room.id)
            self.assertEqual(12.5, float(room.feature_value))

            # 239 unrelated records in the same transaction keep landing.
            for index in range(239):
                _, ok = _append(
                    repo,
                    symbol_id=symbol_id,
                    feature_name=f"metric_{index:03d}",
                    value=float(index),
                    record_id=f"600001.SS:2026-06-30:metric_{index:03d}",
                    revision_id="rev-1",
                )
                self.assertTrue(ok)

            # One batch commit, mirroring the ingestion loops.
            db.commit()

        # Verify the persisted outcome: 240 rows (1 original + 239 others) and
        # exactly one conflict entry.
        with SessionLocal() as db:
            symbol_id = self._seed_symbol(db)
            repo = PointInTimeFeatureSnapshotRepository(db)
            stored_rows = int(
                db.scalar(
                    select(func.count())
                    .select_from(PointInTimeFeatureSnapshot)
                    .where(PointInTimeFeatureSnapshot.symbol_id == symbol_id)
                )
                or 0
            )
            self.assertEqual(240, stored_rows)
            self.assertEqual(1, repo.count_conflicts(symbol_id=symbol_id))
            conflicts = repo.list_conflicts(symbol_id=symbol_id)
            self.assertEqual(1, len(conflicts))
            conflict = conflicts[0]
            self.assertEqual("roe_avg_3y", conflict.feature_name)
            self.assertEqual("pit-conflict-test", conflict.source)
            self.assertEqual("600001.SS:2026-06-30", conflict.source_record_id)
            self.assertEqual("rev-1", conflict.existing_revision_id)
            self.assertEqual("rev-1", conflict.incoming_revision_id)
            self.assertEqual(12.5, conflict.existing_feature_value)
            self.assertEqual(99.9, conflict.incoming_feature_value)
            self.assertEqual(original_id, conflict.existing_snapshot_id)
            self.assertIn(conflict.store_kind, {"legacy", "physical"})

    def test_no_conflict_behaviour_is_unchanged(self) -> None:
        with SessionLocal() as db:
            symbol_id = self._seed_symbol(db)
            repo = PointInTimeFeatureSnapshotRepository(db)

            _, first_inserted = _append(
                repo,
                symbol_id=symbol_id,
                feature_name="quality",
                value=1.25,
                record_id="600001.SS:2026-06-30:quality",
                revision_id="rev-1",
            )
            self.assertTrue(first_inserted)

            # Same identity AND same value is an idempotent no-op, not a conflict.
            again, again_inserted = _append(
                repo,
                symbol_id=symbol_id,
                feature_name="quality",
                value=1.25,
                record_id="600001.SS:2026-06-30:quality",
                revision_id="rev-1",
            )
            self.assertFalse(again_inserted)
            self.assertEqual(0, repo.count_conflicts(symbol_id=symbol_id))
            db.commit()

        with SessionLocal() as db:
            symbol_id = self._seed_symbol(db)
            repo = PointInTimeFeatureSnapshotRepository(db)
            self.assertEqual(0, repo.count_conflicts(symbol_id=symbol_id))
            self.assertEqual([], repo.list_conflicts(symbol_id=symbol_id))


if __name__ == "__main__":
    unittest.main()
