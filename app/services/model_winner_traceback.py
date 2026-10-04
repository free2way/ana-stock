"""Snapshot-only winner attribution; no database, web or rendering dependencies."""

from collections import Counter


def summarize_winner_traceback(
    winners: list[dict], *, min_hits: int, bucket_labels: dict[str, str]
) -> dict:
    """Filter details without changing the full-snapshot summary or input order.

    Capture uses the recorded hit_count (not the length of hits). Attribution
    counts every stored hit, including repeated templates, as the old route did.
    Missing returns retain the existing zero contribution to the mean.
    """
    total = len(winners)
    captured = sum(1 for item in winners if int(item.get("hit_count") or 0) > 0)
    templates: Counter[str] = Counter()
    buckets: Counter[str] = Counter()
    markets: Counter[str] = Counter(str(item.get("market") or "-") for item in winners)
    for item in winners:
        for hit in item.get("hits") or []:
            templates[str(hit.get("template_label") or hit.get("template") or "-")] += 1
            bucket = str(hit.get("action_bucket") or "unclassified")
            buckets[bucket_labels.get(bucket, bucket)] += 1
    return {
        "total": total,
        "captured": captured,
        "missed": max(0, total - captured),
        "avg_return": sum(float(item.get("return_1d") or 0.0) for item in winners) / total if total else None,
        "capture_rate": captured / total * 100.0 if total else None,
        "rows": [item for item in winners if int(item.get("hit_count") or 0) >= min_hits],
        "templates": templates.most_common(4),
        "buckets": buckets.most_common(4),
        "markets": markets.most_common(3),
    }
