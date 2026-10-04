from __future__ import annotations


def summarize_screener_rows(rows: list[dict]) -> dict:
    """Summarize execution risk and tradability without mutating candidate rows."""
    risk_counts: dict[str, int] = {}
    risk_examples: list[dict[str, object]] = []
    tagged_names = 0
    status_counts = {"ready": 0, "do_not_chase": 0, "blocked": 0, "review": 0, "low_readiness": 0}

    for item in rows:
        tags = [str(tag).strip() for tag in (item.get("model_execution_tags") or []) if str(tag).strip()]
        if tags:
            tagged_names += 1
            for tag in tags:
                risk_counts[tag] = risk_counts.get(tag, 0) + 1
            if len(risk_examples) < 3:
                risk_examples.append({"ticker": item.get("ticker"), "tags": tags[:2]})

        status = str(item.get("tradability_status") or "").strip().upper()
        bucket = str(item.get("readiness_bucket") or "").strip().upper()
        try:
            readiness = float(item.get("trade_readiness_score") or 0.0)
        except (TypeError, ValueError):
            readiness = 0.0
        if status == "READY":
            status_counts["ready"] += 1
        elif status == "DO_NOT_CHASE":
            status_counts["do_not_chase"] += 1
        elif status == "BLOCKED":
            status_counts["blocked"] += 1
        else:
            status_counts["review"] += 1
        if bucket in {"LOW", "BLOCKED"} or (readiness and readiness < 60.0):
            status_counts["low_readiness"] += 1

    return {
        "tagged_names": tagged_names,
        "risk_counts": risk_counts,
        "risk_examples": risk_examples,
        "risk_top_tags": sorted(risk_counts.items(), key=lambda pair: (-pair[1], pair[0]))[:3],
        "status_counts": status_counts,
    }
