from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy.orm import Session

from app.services.repository import AppSettingRepository
from app.services.time_utils import app_now_iso, app_today_iso


REVIEW_JOURNAL_SETTING_KEY = "review_journal_entries"


DEFAULT_REVIEW_ENTRY = {
    "journal_date": "",
    "market_scope": "ALL",
    "emotion": "calm",
    "discipline_score": "3",
    "focus_tickers": "",
    "daily_plan": "",
    "execution_review": "",
    "what_worked": "",
    "what_failed": "",
    "lessons": "",
    "tomorrow_plan": "",
    "risk_notes": "",
}


def _loads_entries(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    entries: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        journal_date = str(item.get("journal_date") or "").strip()
        if not journal_date:
            continue
        entries.append({**DEFAULT_REVIEW_ENTRY, **item, "journal_date": journal_date})
    return entries


def list_review_entries(db: Session, *, limit: int = 90) -> list[dict]:
    rows = _loads_entries(AppSettingRepository(db).get(REVIEW_JOURNAL_SETTING_KEY))
    rows.sort(key=lambda item: (str(item.get("journal_date") or ""), str(item.get("updated_at") or "")), reverse=True)
    return rows[: max(1, int(limit))]


def get_review_entry(db: Session, journal_date: str | None = None) -> dict:
    target_date = str(journal_date or app_today_iso()).strip()[:10] or app_today_iso()
    for item in list_review_entries(db, limit=365):
        if str(item.get("journal_date") or "")[:10] == target_date:
            return {**DEFAULT_REVIEW_ENTRY, **item}
    return {
        **DEFAULT_REVIEW_ENTRY,
        "id": str(uuid4()),
        "journal_date": target_date,
        "created_at": app_now_iso(),
        "updated_at": app_now_iso(),
    }


def save_review_entry(db: Session, payload: dict) -> dict:
    now = app_now_iso()
    journal_date = str(payload.get("journal_date") or app_today_iso()).strip()[:10] or app_today_iso()
    incoming = {
        **DEFAULT_REVIEW_ENTRY,
        **{key: str(value or "").strip() for key, value in payload.items()},
        "journal_date": journal_date,
        "updated_at": now,
    }
    entries = list_review_entries(db, limit=365)
    replaced = False
    for index, item in enumerate(entries):
        if str(item.get("journal_date") or "")[:10] == journal_date:
            entries[index] = {
                **item,
                **incoming,
                "id": item.get("id") or str(uuid4()),
                "created_at": item.get("created_at") or now,
                "updated_at": now,
            }
            replaced = True
            break
    if not replaced:
        entries.append({**incoming, "id": str(uuid4()), "created_at": now, "updated_at": now})
    entries.sort(key=lambda item: str(item.get("journal_date") or ""), reverse=True)
    AppSettingRepository(db).set(REVIEW_JOURNAL_SETTING_KEY, json.dumps(entries[:365], ensure_ascii=False))
    return get_review_entry(db, journal_date)


def delete_review_entry(db: Session, journal_date: str) -> None:
    target_date = str(journal_date or "").strip()[:10]
    if not target_date:
        return
    entries = [
        item
        for item in list_review_entries(db, limit=365)
        if str(item.get("journal_date") or "")[:10] != target_date
    ]
    AppSettingRepository(db).set(REVIEW_JOURNAL_SETTING_KEY, json.dumps(entries, ensure_ascii=False))

