"""S-11: runtime_cache LRU cap, negative caching and single-flight loader."""

import threading
import time
import unittest

from app.services import runtime_cache


class RuntimeCacheS11Tests(unittest.TestCase):
    def setUp(self):
        runtime_cache.clear_all()
        runtime_cache.configure_cache(max_entries=2048)

    def tearDown(self):
        runtime_cache.clear_all()
        runtime_cache.configure_cache(max_entries=2048)

    def test_lru_evicts_least_recently_used_entry(self):
        runtime_cache.configure_cache(max_entries=2)
        runtime_cache.set_cached("ns", "k1", "v1", ttl_seconds=60)
        runtime_cache.set_cached("ns", "k2", "v2", ttl_seconds=60)
        # Touch k1 so k2 becomes the least recently used.
        self.assertEqual(runtime_cache.get_cached("ns", "k1"), "v1")
        runtime_cache.set_cached("ns", "k3", "v3", ttl_seconds=60)

        self.assertEqual(runtime_cache.cache_size(), 2)
        self.assertIsNone(runtime_cache.get_cached("ns", "k2"))
        self.assertEqual(runtime_cache.get_cached("ns", "k1"), "v1")
        self.assertEqual(runtime_cache.get_cached("ns", "k3"), "v3")

    def test_cache_never_exceeds_configured_capacity(self):
        runtime_cache.configure_cache(max_entries=3)
        for index in range(10):
            runtime_cache.set_cached("ns", f"k{index}", index, ttl_seconds=60)
        self.assertEqual(runtime_cache.cache_size(), 3)
        self.assertLessEqual(runtime_cache.cache_size(), runtime_cache.cache_max_entries())

    def test_negative_cache_avoids_repeated_loader_calls(self):
        calls = []

        def loader():
            calls.append(1)
            return None

        self.assertIsNone(runtime_cache.get_or_set("ns", "missing", ttl_seconds=60, loader=loader))
        self.assertIsNone(runtime_cache.get_or_set("ns", "missing", ttl_seconds=60, loader=loader))
        self.assertIsNone(runtime_cache.get_or_set("ns", "missing", ttl_seconds=60, loader=loader))

        self.assertEqual(len(calls), 1)
        self.assertEqual(runtime_cache.cache_size(), 1)

    def test_negative_cache_entry_expires(self):
        calls = []

        def loader():
            calls.append(1)
            return None

        runtime_cache.get_or_set("ns", "missing", ttl_seconds=0.05, loader=loader)
        self.assertEqual(len(calls), 1)
        time.sleep(0.12)
        runtime_cache.get_or_set("ns", "missing", ttl_seconds=0.05, loader=loader)
        self.assertEqual(len(calls), 2)

    def test_positive_value_expires_and_reloads(self):
        calls = []

        def loader():
            calls.append(1)
            return {"value": len(calls)}

        first = runtime_cache.get_or_set("ns", "key", ttl_seconds=0.05, loader=loader)
        second = runtime_cache.get_or_set("ns", "key", ttl_seconds=0.05, loader=loader)
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        time.sleep(0.12)
        third = runtime_cache.get_or_set("ns", "key", ttl_seconds=0.05, loader=loader)
        self.assertEqual(len(calls), 2)
        self.assertEqual(third, {"value": 2})

    def test_concurrent_loader_runs_exactly_once(self):
        calls = []
        results = []
        start = threading.Barrier(4)

        def loader():
            calls.append(1)
            time.sleep(0.2)
            return "shared-value"

        def worker():
            start.wait()
            results.append(runtime_cache.get_or_set("ns", "shared", ttl_seconds=60, loader=loader))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(len(calls), 1)
        self.assertEqual(results, ["shared-value"] * 4)

    def test_concurrent_negative_loader_runs_exactly_once(self):
        calls = []
        results = []
        start = threading.Barrier(4)

        def loader():
            calls.append(1)
            time.sleep(0.2)
            return None

        def worker():
            start.wait()
            results.append(runtime_cache.get_or_set("ns", "shared-none", ttl_seconds=60, loader=loader))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(len(calls), 1)
        self.assertEqual(results, [None, None, None, None])

    def test_clear_namespace_removes_only_that_namespace(self):
        runtime_cache.set_cached("a", "k", 1, ttl_seconds=60)
        runtime_cache.set_cached("b", "k", 2, ttl_seconds=60)
        runtime_cache.clear_namespace("a")
        self.assertIsNone(runtime_cache.get_cached("a", "k"))
        self.assertEqual(runtime_cache.get_cached("b", "k"), 2)


if __name__ == "__main__":
    unittest.main()
