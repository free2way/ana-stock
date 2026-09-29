from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from app.services.app_setting_storage import (
    decode_app_setting_value,
    encode_app_setting_value,
)
from app.models.tables import AppSetting
from app.services.repository import AppSettingRepository


class AppSettingStorageTests(unittest.TestCase):
    def test_small_value_remains_inline(self) -> None:
        value = json.dumps({"enabled": True})
        stored, source = encode_app_setting_value(value, max_inline_bytes=32768)
        self.assertEqual(value, stored)
        self.assertEqual("postgresql_inline", source)

    def test_large_json_object_is_externalized_and_restored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            value = json.dumps({"analysis": "量" * 2000}, ensure_ascii=False)
            stored, source = encode_app_setting_value(
                value,
                max_inline_bytes=1024,
                artifact_root=Path(directory),
            )
            self.assertEqual("compressed_artifact", source)
            self.assertLessEqual(len(stored.encode("utf-8")), 1024)

            restored, read_source = decode_app_setting_value(
                stored,
                artifact_root=Path(directory),
            )
            self.assertEqual("compressed_artifact", read_source)
            self.assertEqual(json.loads(value), json.loads(restored))

    def test_large_non_object_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "must contain valid JSON"):
            encode_app_setting_value("x" * 2000, max_inline_bytes=1024)

    def test_large_json_array_is_wrapped_and_restored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            value = json.dumps([{"ticker": "600519.SS", "note": "量" * 2000}], ensure_ascii=False)
            stored, source = encode_app_setting_value(
                value,
                max_inline_bytes=1024,
                artifact_root=Path(directory),
            )
            restored, read_source = decode_app_setting_value(
                stored,
                artifact_root=Path(directory),
            )
            self.assertEqual("compressed_artifact", source)
            self.assertEqual("compressed_artifact", read_source)
            self.assertEqual(json.loads(value), json.loads(restored))

    def test_repository_decodes_externalized_value_transparently(self) -> None:
        db = MagicMock()
        db.scalar.return_value = AppSetting(
            key="large",
            value='{"_payload_artifact":{"relative_path":"fixture"}}',
            updated_at="2026-08-23T00:00:00+08:00",
        )
        with patch(
            "app.services.repository.decode_app_setting_value",
            return_value=('{"restored":true}', "compressed_artifact"),
        ):
            value = AppSettingRepository(db).get("large")

        self.assertEqual('{"restored":true}', value)


if __name__ == "__main__":
    unittest.main()
