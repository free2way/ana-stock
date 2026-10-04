"""Pure text compaction helpers shared by dashboard presentations."""

import json


def compact_label(value: str | None, limit: int = 28) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1]}…"


def compact_run_name(value: str | None, limit: int = 24) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    if "_" not in text:
        return compact_label(text, limit=limit)
    parts = [part for part in text.split("_") if part]
    if len(parts) >= 3:
        prefix = "_".join(parts[:2])
        suffix = parts[-1]
        compact = f"{prefix}…{suffix}"
        if len(compact) <= limit:
            return compact
    return compact_label(text, limit=limit)


def compact_job_type(value: str | None, limit: int = 22) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    aliases = {
        "watchlist_auto_analysis": "watchlist_analysis",
        "us_signal_train": "us_signal_train",
        "train_us_signals": "us_signal_train",
        "sync_cn_symbol_universe": "cn_universe_sync",
        "init_cn_market_data": "cn_market_init",
        "refresh_cn_market_data": "cn_market_refresh",
        "rebuild_technical_snapshots": "tech_snapshots",
    }
    return compact_run_name(aliases.get(text, text), limit=limit)


def compact_json_summary(value: object, limit: int = 56) -> str:
    if value in (None, "", {}):
        return "-"
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = str(value)
    return compact_label(text, limit=limit)
