from pathlib import Path
import tempfile
import unittest

from app.services.json_payload_artifacts import (
    JsonPayloadArtifactStore,
    build_payload_envelope,
    resolve_payload_envelope,
    summarize_job_result,
)


class JsonPayloadArtifactTests(unittest.TestCase):
    def test_round_trip_is_exact_and_content_addressed(self):
        payload = {"market": "CN", "rows": [{"ticker": "000001.SZ"}] * 200}
        with tempfile.TemporaryDirectory() as directory:
            store = JsonPayloadArtifactStore(Path(directory))
            first = store.write(payload, namespace="workspace_snapshots")
            second = store.write(payload, namespace="workspace_snapshots")

            self.assertEqual(first, second)
            self.assertEqual(payload, store.read(first))
            self.assertLess(first["compressed_bytes"], first["uncompressed_bytes"])

    def test_envelope_resolves_transparently(self):
        payload = {"market": "CN", "values": list(range(100))}
        with tempfile.TemporaryDirectory() as directory:
            store = JsonPayloadArtifactStore(Path(directory))
            envelope = build_payload_envelope(
                payload,
                store.write(payload, namespace="workspace_snapshots"),
            )

            resolved, source = resolve_payload_envelope(
                envelope,
                artifact_root=Path(directory),
            )

            self.assertEqual(payload, resolved)
            self.assertEqual("compressed_artifact", source)

    def test_tampering_is_rejected(self):
        payload = {"market": "CN", "rows": list(range(100))}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonPayloadArtifactStore(root)
            reference = store.write(payload, namespace="workspace_snapshots")
            artifact_path = root / reference["relative_path"]
            artifact_path.write_bytes(artifact_path.read_bytes() + b"tampered")

            with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                store.read(reference)

    def test_job_summary_keeps_operational_metrics_without_detail_rows(self):
        summary = summarize_job_result(
            {
                "rows_written": 100,
                "data": [{"ticker": "000001.SZ"}] * 100,
                "quality_summary": {"status": "pass", "coverage": 0.99},
            }
        )

        self.assertEqual(100, summary["rows_written"])
        self.assertEqual(100, summary["data_count"])
        self.assertEqual("pass", summary["quality_summary"]["status"])


if __name__ == "__main__":
    unittest.main()
