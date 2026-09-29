from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from sqlalchemy import select


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.config import get_settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.models.tables import AppSetting  # noqa: E402
from app.services.json_payload_artifacts import ARTIFACT_MARKER  # noqa: E402
from app.services.repository import AppSettingRepository  # noqa: E402
from app.services.time_utils import app_now_iso  # noqa: E402


def _write_receipt(path: Path, payload: dict) -> None:
    target = path.resolve()
    if target.exists():
        raise FileExistsError(f"Receipt already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit or externalize oversized app_settings values."
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    threshold = max(1024, int(get_settings().app_setting_inline_value_max_bytes))
    with SessionLocal() as db:
        rows = list(db.scalars(select(AppSetting).order_by(AppSetting.key)).all())
        details: list[dict] = []
        applied_keys: list[str] = []
        for row in rows:
            raw = str(row.value or "")
            raw_bytes = len(raw.encode("utf-8"))
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = None
            already_externalized = bool(
                isinstance(parsed, dict) and isinstance(parsed.get(ARTIFACT_MARKER), dict)
            )
            eligible = raw_bytes > threshold and not already_externalized
            item = {
                "key": row.key,
                "postgresql_bytes": raw_bytes,
                "already_externalized": already_externalized,
                "eligible": eligible,
                "applied": False,
            }
            source_sha256 = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            if already_externalized:
                restored = AppSettingRepository(db).get(row.key)
                restored_json = json.loads(restored or "null")
                reference = parsed[ARTIFACT_MARKER]
                item.update(
                    {
                        "artifact_verified": True,
                        "artifact_content_sha256": reference.get("content_sha256"),
                        "restored_json_type": type(restored_json).__name__,
                    }
                )
            if args.apply and eligible:
                repository = AppSettingRepository(db)
                repository.set(row.key, raw)
                stored = db.scalar(select(AppSetting).where(AppSetting.key == row.key))
                restored = repository.get(row.key)
                try:
                    exact_match = json.loads(restored or "null") == json.loads(raw)
                except json.JSONDecodeError:
                    exact_match = restored == raw
                if not exact_match:
                    raise RuntimeError(
                        f"App setting artifact semantic verification failed: {row.key}"
                    )
                item["applied"] = True
                item["postgresql_bytes_after"] = len(stored.value.encode("utf-8"))
                item["source_sha256"] = source_sha256
                item["exact_match"] = True
                applied_keys.append(row.key)
            details.append(item)
    oversized = [item for item in details if item["eligible"]]
    result = {
        "audit_version": "app-setting-storage-v1",
        "generated_at": app_now_iso(),
        "status": "pass" if not oversized or args.apply else "migration_required",
        "mode": "apply" if args.apply else "dry_run",
        "threshold_bytes": threshold,
        "setting_count": len(details),
        "oversized_inline_count": len(oversized),
        "externalized_count": sum(
            1 for item in details if item["already_externalized"] or item["applied"]
        ),
        "applied_count": len(applied_keys),
        "applied_keys": applied_keys,
        "database_mutated": bool(applied_keys),
        "settings": details,
    }
    _write_receipt(args.receipt, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
