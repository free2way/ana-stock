"""Significance helpers (E-6): Newey-West and block-bootstrap confidence intervals.

The iid normal interval underestimates uncertainty for overlapping forward
returns (horizon > 1) and for serially correlated RankIC series. These helpers
compute the corrected intervals and always return the iid interval alongside so
the comparison sample required by the acceptance plan is embedded in the output.
"""
from __future__ import annotations

import math
import random
import statistics
from typing import Iterable, Sequence

Z_95 = 1.959963984540054


def _series(values: Iterable[float]) -> list[float]:
    return [float(value) for value in values]


def iid_mean_ci(values: Sequence[float], *, alpha: float = 0.05) -> tuple[float, float, float] | None:
    """Classic iid interval: returns (low, high, standard_error)."""

    data = _series(values)
    if len(data) < 2:
        return None
    standard_error = statistics.stdev(data) / math.sqrt(len(data))
    z = Z_95 if abs(alpha - 0.05) < 1e-9 else _z_for(alpha)
    mean = statistics.fmean(data)
    return mean - z * standard_error, mean + z * standard_error, standard_error


def newey_west_mean_ci(
    values: Sequence[float],
    *,
    lag: int,
    alpha: float = 0.05,
) -> tuple[float, float, float] | None:
    """HAC (Newey-West, Bartlett kernel) interval for the mean.

    ``lag`` should be ``horizon_days - 1`` for overlapping forward returns.
    Returns (low, high, standard_error).
    """

    data = _series(values)
    n = len(data)
    if n < 2:
        return None
    lag = max(0, min(int(lag), n - 1))
    mean = statistics.fmean(data)
    residuals = [value - mean for value in data]
    gamma0 = sum(residual * residual for residual in residuals) / n
    variance = gamma0
    for k in range(1, lag + 1):
        weight = 1.0 - k / (lag + 1.0)
        covariance = sum(residuals[i] * residuals[i + k] for i in range(n - k)) / n
        variance += 2.0 * weight * covariance
    variance = max(variance, 0.0)
    standard_error = math.sqrt(variance / n)
    z = Z_95 if abs(alpha - 0.05) < 1e-9 else _z_for(alpha)
    return mean - z * standard_error, mean + z * standard_error, standard_error


def block_bootstrap_mean_ci(
    values: Sequence[float],
    *,
    block_length: int,
    iterations: int = 2000,
    seed: int = 20261003,
    alpha: float = 0.05,
) -> tuple[float, float] | None:
    """Moving-block bootstrap percentile interval for the mean."""

    data = _series(values)
    n = len(data)
    if n < 2:
        return None
    block = max(1, min(int(block_length), n))
    rng = random.Random(seed)
    means: list[float] = []
    starts = range(0, n - block + 1) if n > block else [0]
    starts = list(starts)
    for _ in range(max(50, int(iterations))):
        sample: list[float] = []
        while len(sample) < n:
            start = rng.choice(starts)
            sample.extend(data[start : start + block])
        means.append(statistics.fmean(sample[:n]))
    means.sort()
    low_index = max(0, int((alpha / 2.0) * len(means)) - 1)
    high_index = min(len(means) - 1, int((1.0 - alpha / 2.0) * len(means)))
    return means[low_index], means[high_index]


def day_clustered_hit_rate_ci(
    flags: Sequence[bool | None],
    dates: Sequence[str],
    *,
    block_length: int,
    iterations: int = 2000,
    seed: int = 20261003,
    alpha: float = 0.05,
) -> dict:
    """Day-clustered moving-block bootstrap interval for a binary hit rate.

    A plain iid interval treats every pick as independent, but picks selected on
    the same signal date share a market move, so the effective sample is the
    number of *dates*, not the number of picks.  Here each distinct date is one
    cluster and a block is a run of consecutive dates of length ``block_length``
    (the evaluation horizon).  The point estimate and the iid interval stay in
    the payload so gate consumers can see both, but the clustered interval is
    the one that must clear a promotion threshold.
    """

    paired = [(str(date), bool(flag)) for date, flag in zip(dates, flags, strict=False) if flag is not None]
    if not paired:
        return {"point_pct": None, "ci95": None, "days": 0, "block_length": None,
                "method": "day_cluster_insufficient_days"}
    by_day: dict[str, list[bool]] = {}
    for date, flag in paired:
        by_day.setdefault(date, []).append(flag)
    ordered_days = sorted(by_day)
    hits_by_day = [sum(by_day[day]) for day in ordered_days]
    counts_by_day = [len(by_day[day]) for day in ordered_days]
    total_hits = sum(hits_by_day)
    total_count = sum(counts_by_day)
    point = total_hits / total_count * 100.0 if total_count else None
    if len(ordered_days) < 2:
        return {
            "point_pct": point,
            "ci95": None,
            "days": len(ordered_days),
            "block_length": None,
            "method": "day_cluster_insufficient_days",
        }
    day_count = len(ordered_days)
    block = max(1, min(int(block_length), day_count))
    rng = random.Random(seed)
    starts = list(range(0, day_count - block + 1)) if day_count > block else [0]
    rates: list[float] = []
    for _ in range(max(50, int(iterations))):
        selected: list[int] = []
        while len(selected) < day_count:
            start = rng.choice(starts)
            selected.extend(range(start, start + block))
        selected = selected[:day_count]
        hits = sum(hits_by_day[index] for index in selected)
        count = sum(counts_by_day[index] for index in selected)
        if count:
            rates.append(hits / count * 100.0)
    rates.sort()
    if len(rates) < 2:
        return {"point_pct": point, "ci95": None, "days": day_count, "block_length": block,
                "method": "day_cluster_insufficient_days"}
    low_index = max(0, int((alpha / 2.0) * len(rates)) - 1)
    high_index = min(len(rates) - 1, int((1.0 - alpha / 2.0) * len(rates)))
    return {
        "point_pct": point,
        "ci95": (rates[low_index], rates[high_index]),
        "days": day_count,
        "block_length": block,
        "method": f"day_cluster_moving_block_bootstrap_block={block}",
    }


def _z_for(alpha: float) -> float:
    # Acklam-style inverse normal approximation; accurate to ~1e-9 for 0.01<=p<=0.99.
    p = 1.0 - alpha / 2.0
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1
    )


def significance_report(
    values: Sequence[float],
    *,
    horizon_days: int,
    seed: int = 20261003,
    bootstrap_iterations: int = 1000,
) -> dict:
    """E-6 comparison sample: iid vs Newey-West vs block bootstrap."""

    lag = max(0, int(horizon_days) - 1)
    iid = iid_mean_ci(values)
    hac = newey_west_mean_ci(values, lag=lag)
    bootstrap = block_bootstrap_mean_ci(
        values, block_length=max(1, int(horizon_days)), seed=seed, iterations=bootstrap_iterations
    )
    mean = statistics.fmean(_series(values)) if len(list(values)) >= 1 else None
    return {
        "sample_count": len(list(values)),
        "mean": mean,
        "horizon_days": int(horizon_days),
        "iid_ci95": iid[:2] if iid else None,
        "newey_west_ci95": hac[:2] if hac else None,
        "newey_west_lag": lag,
        "block_bootstrap_ci95": list(bootstrap) if bootstrap else None,
        "block_length": max(1, int(horizon_days)),
        "ci_method": "newey_west_bartlett_lag_horizon_minus_1; block_bootstrap_block=horizon",
        "iid_is_deprecated_for_promotion": True,
    }
