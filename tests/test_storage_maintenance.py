import unittest

from app.services.storage_maintenance import _parse_options


class StorageMaintenanceTests(unittest.TestCase):
    def test_parse_postgresql_reloptions(self):
        self.assertEqual(
            {
                "autovacuum_vacuum_scale_factor": "0.02",
                "autovacuum_vacuum_threshold": "50",
            },
            _parse_options(
                [
                    "autovacuum_vacuum_scale_factor=0.02",
                    "autovacuum_vacuum_threshold=50",
                ]
            ),
        )

    def test_empty_reloptions_are_supported(self):
        self.assertEqual({}, _parse_options(None))


if __name__ == "__main__":
    unittest.main()
