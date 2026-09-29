from __future__ import annotations

import unittest

from scripts.apply_job_retention_batch import _dependency_protection_closure, _digest


class JobRetentionApplyTests(unittest.TestCase):
    def test_dependency_protection_is_transitive(self) -> None:
        protected = _dependency_protection_closure(
            all_job_ids={1, 2, 3, 4, 5},
            raw_candidate_ids={1, 2, 3, 4},
            external_reference_ids={4},
            dependency_pairs=[(5, 3), (3, 2), (2, 1), (4, 1)],
        )

        self.assertEqual({1, 2, 3, 4}, protected)

    def test_unreferenced_candidate_is_not_protected(self) -> None:
        protected = _dependency_protection_closure(
            all_job_ids={1, 2, 3},
            raw_candidate_ids={1, 2},
            external_reference_ids=set(),
            dependency_pairs=[(2, 1)],
        )

        self.assertEqual(set(), protected)
        self.assertNotEqual(_digest([1]), _digest([2]))


if __name__ == "__main__":
    unittest.main()
