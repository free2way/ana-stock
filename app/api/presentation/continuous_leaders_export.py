"""Stable CSV representation of already-selected continuous leaders."""

import csv
from io import StringIO


FIELDS = (
    "ticker", "name", "market", "hits", "runs", "score", "signal_label", "signal_strength",
    "conviction_bucket", "position_size_hint", "entry_style", "execution_tags", "percentile",
    "target_horizon_days", "expected_drawdown_20d", "model_reward_risk_ratio", "trade_date",
    "continuous_state",
)


def render_continuous_leaders_csv(rows: list[dict]) -> str:
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=FIELDS)
    writer.writeheader()
    for item in rows:
        row = {key: item.get(key) for key in FIELDS}
        # Keep required source fields strict, matching the previous export.
        row.update({key: item[key] for key in FIELDS[:6]})
        row["execution_tags"] = ";".join(item.get("execution_tags") or [])
        row["continuous_state"] = item.get("continuous_state_key")
        writer.writerow(row)
    return buffer.getvalue()
