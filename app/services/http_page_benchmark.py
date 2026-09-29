from __future__ import annotations

from dataclasses import dataclass
from http.cookiejar import CookieJar
import time
from typing import Callable
from urllib.parse import urlencode, urljoin
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

from app.services.time_utils import app_now_iso


@dataclass(frozen=True)
class PageBenchmarkTarget:
    name: str
    path: str
    p95_limit_ms: float = 1000.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * min(1.0, max(0.0, float(percentile)))
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * fraction)


def measure_authenticated_page(
    opener,
    *,
    url: str,
    warmup_requests: int,
    measured_requests: int,
    timeout_seconds: float,
    clock: Callable[[], float] = time.perf_counter,
) -> dict:
    timings_ms: list[float] = []
    statuses: list[int] = []
    total_requests = max(0, int(warmup_requests)) + max(1, int(measured_requests))
    for index in range(total_requests):
        started = clock()
        with opener.open(url, timeout=max(1.0, float(timeout_seconds))) as response:
            payload = response.read()
            status = int(response.getcode())
            final_url = str(response.geturl())
        elapsed_ms = (clock() - started) * 1000.0
        if status != 200:
            raise RuntimeError(f"Page benchmark received HTTP {status}: {url}")
        if "/login" in final_url:
            raise RuntimeError(f"Page benchmark session was redirected to login: {url}")
        if not payload:
            raise RuntimeError(f"Page benchmark received an empty response: {url}")
        if index >= max(0, int(warmup_requests)):
            timings_ms.append(elapsed_ms)
            statuses.append(status)
    return {
        "request_count": len(timings_ms),
        "http_statuses": sorted(set(statuses)),
        "response_time_ms": {
            "min": round(min(timings_ms), 3),
            "p50": round(_percentile(timings_ms, 0.50), 3),
            "p95": round(_percentile(timings_ms, 0.95), 3),
            "max": round(max(timings_ms), 3),
        },
    }


def benchmark_authenticated_pages(
    *,
    base_url: str,
    username: str,
    password: str,
    targets: list[PageBenchmarkTarget],
    warmup_requests: int = 2,
    measured_requests: int = 20,
    timeout_seconds: float = 30.0,
) -> dict:
    normalized_base = str(base_url or "").strip().rstrip("/") + "/"
    if not password:
        raise RuntimeError("Authenticated page benchmark requires an application password.")
    if not targets:
        raise ValueError("At least one page benchmark target is required.")
    # Production acceptance targets the explicitly supplied local application.
    # Ignore process-wide HTTP proxy variables so localhost is never benchmarked
    # through a corporate or system proxy.
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
    login_request = Request(
        urljoin(normalized_base, "login"),
        data=urlencode(
            {
                "username": username,
                "password": password,
                "next": "/settings?lang=zh",
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with opener.open(login_request, timeout=max(1.0, float(timeout_seconds))) as response:
        response.read()
        if int(response.getcode()) != 200 or "/login" in str(response.geturl()):
            raise RuntimeError("Application login failed for the page benchmark.")

    results: list[dict] = []
    for target in targets:
        measurement = measure_authenticated_page(
            opener,
            url=urljoin(normalized_base, target.path.lstrip("/")),
            warmup_requests=warmup_requests,
            measured_requests=measured_requests,
            timeout_seconds=timeout_seconds,
        )
        p95_ms = float(measurement["response_time_ms"]["p95"])
        results.append(
            {
                "name": target.name,
                "path": target.path,
                "p95_limit_ms": float(target.p95_limit_ms),
                "status": "pass" if p95_ms <= target.p95_limit_ms else "fail",
                **measurement,
            }
        )
    return {
        "benchmark_version": "authenticated-page-latency-v1",
        "generated_at": app_now_iso(),
        "status": "pass" if all(item["status"] == "pass" for item in results) else "fail",
        "base_url": normalized_base.rstrip("/"),
        "warmup_requests": max(0, int(warmup_requests)),
        "measured_requests_per_page": max(1, int(measured_requests)),
        "read_only": True,
        "pages": results,
    }
