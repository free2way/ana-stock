from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import TestCase

from app.services.stock_selection.experiment_registry import (
    RegistryTamperedError,
    attempt_stats,
    load_attempts,
    record_attempt,
)


class ExperimentRegistryTests(TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.path = Path(self._temp.name) / "registry.jsonl"

    def tearDown(self) -> None:
        self._temp.cleanup()

    def test_attempts_append_with_chain(self) -> None:
        first = record_attempt(
            model_key="factor_v1", dataset_hash="sha256:aaa", protocol_id="p:5d",
            verdict="REJECT", path=self.path, recorded_at="2026-10-03T00:00:00+00:00",
        )
        second = record_attempt(
            model_key="factor_v1", dataset_hash="sha256:bbb", protocol_id="p:5d",
            verdict="PASS", path=self.path, recorded_at="2026-10-03T01:00:00+00:00",
        )
        self.assertEqual("", first["prev_hash"])
        self.assertEqual(first["entry_hash"], second["prev_hash"])
        entries = load_attempts(self.path)
        self.assertEqual(2, len(entries))

    def test_stats_report_attempts_rejections_and_dataset_hash(self) -> None:
        record_attempt(model_key="f", dataset_hash="sha256:1", protocol_id="p", verdict="REJECT", path=self.path)
        record_attempt(model_key="f", dataset_hash="sha256:2", protocol_id="p", verdict="PASS", path=self.path)
        record_attempt(model_key="other", dataset_hash="sha256:3", protocol_id="p", verdict="REJECT", path=self.path)
        stats = attempt_stats(model_key="f", path=self.path)
        self.assertEqual(2, stats["attempts"])
        self.assertEqual(1, stats["rejected_count"])
        self.assertEqual("sha256:2", stats["dataset_hash"])
        self.assertEqual("PASS", stats["latest_verdict"])

    def test_modified_entry_is_detected(self) -> None:
        record_attempt(model_key="f", dataset_hash="sha256:1", protocol_id="p", verdict="PASS", path=self.path)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[0])
        entry["verdict"] = "REJECT"  # forged change without re-hashing
        lines[0] = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with self.assertRaises(RegistryTamperedError):
            load_attempts(self.path)

    def test_deleted_entry_breaks_the_chain(self) -> None:
        record_attempt(model_key="f", dataset_hash="a", protocol_id="p", verdict="REJECT", path=self.path)
        record_attempt(model_key="f", dataset_hash="b", protocol_id="p", verdict="PASS", path=self.path)
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.path.write_text(lines[1] + "\n", encoding="utf-8")  # drop the first entry
        with self.assertRaises(RegistryTamperedError):
            load_attempts(self.path)

    def test_appending_to_tampered_registry_is_refused(self) -> None:
        record_attempt(model_key="f", dataset_hash="a", protocol_id="p", verdict="PASS", path=self.path)
        self.path.write_text('{"schema_version": "x"}\n', encoding="utf-8")
        with self.assertRaises(RegistryTamperedError):
            record_attempt(model_key="f", dataset_hash="b", protocol_id="p", verdict="PASS", path=self.path)
