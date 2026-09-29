from __future__ import annotations

import unittest

from app.services.http_page_benchmark import _percentile, measure_authenticated_page


class _Response:
    def __init__(self, *, url: str = "http://localhost/dashboard") -> None:
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return b"ok"

    def getcode(self) -> int:
        return 200

    def geturl(self) -> str:
        return self.url


class _Opener:
    def open(self, url: str, *, timeout: float):
        return _Response(url=url)


class PageBenchmarkTests(unittest.TestCase):
    def test_percentile_interpolates_sorted_values(self) -> None:
        self.assertEqual(1.0, _percentile([1.0], 0.95))
        self.assertEqual(3.5, _percentile([4.0, 1.0, 3.0, 2.0], 5 / 6))

    def test_measurement_excludes_warmup_and_reports_p95(self) -> None:
        clock_values = iter([0.0, 0.5, 1.0, 1.1, 2.0, 2.2])
        result = measure_authenticated_page(
            _Opener(),
            url="http://localhost/dashboard",
            warmup_requests=1,
            measured_requests=2,
            timeout_seconds=1.0,
            clock=lambda: next(clock_values),
        )

        self.assertEqual(2, result["request_count"])
        self.assertEqual([200], result["http_statuses"])
        self.assertEqual(195.0, result["response_time_ms"]["p95"])


if __name__ == "__main__":
    unittest.main()
