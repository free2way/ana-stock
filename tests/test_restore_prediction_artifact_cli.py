from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.restore_prediction_artifact import _write_receipt


class RestorePredictionArtifactCliTests(unittest.TestCase):
    def test_receipt_is_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / "receipt.json"
            _write_receipt(receipt, {"status": "success", "market": "CN"})

            with self.assertRaises(FileExistsError):
                _write_receipt(receipt, {"status": "changed"})

            self.assertIn('"market": "CN"', receipt.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
