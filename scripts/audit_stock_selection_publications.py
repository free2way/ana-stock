"""Print the read-only P0 publication reconciliation queue as JSON."""
from __future__ import annotations

import json
import argparse

from sqlalchemy import text

from app.core.db import SessionLocal
from app.services.stock_selection.publication_audit import audit_stock_selection_publications
from app.services.stock_selection.publication_audit import find_feishu_provider_receipt_candidates
from app.services.push_notifications import PushNotificationService


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only stock-selection publication audit")
    parser.add_argument("--feishu-history", action="store_true",
                        help="also query bounded Feishu chat history for exact receipt candidates")
    args = parser.parse_args()
    with SessionLocal() as db:
        if db.bind.dialect.name == "postgresql":
            db.execute(text("SET TRANSACTION READ ONLY"))
        result = audit_stock_selection_publications(db=db)
        if args.feishu_history:
            result["feishu_history"] = find_feishu_provider_receipt_candidates(
                db=db, provider=PushNotificationService(),
            )
        db.rollback()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
