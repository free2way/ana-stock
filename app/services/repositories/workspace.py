"""Workspace, dashboard and app-settings domain repositories."""

import json

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.tables import AppSetting, WorkspaceSnapshot
from app.services.app_setting_storage import (
    decode_app_setting_value,
    encode_app_setting_value,
)
from app.services.json_payload_artifacts import (
    JsonPayloadArtifactStore,
    build_payload_envelope,
    canonical_json_bytes,
    resolve_payload_envelope,
)

from app.services.repositories.backtests import BacktestRepository
from app.services.repositories.jobs import DataJobRepository
from app.services.repositories.market import (
    ConceptSnapshotRepository,
    PriceSyncStateRepository,
)
from app.services.repositories.predictions import PredictionRepository
from app.services.repositories.shared import (
    _is_database_locked_error,
    _sleep_for_lock_retry,
    utc_now_iso,
)
from app.services.repositories.signals import ModelRunRepository


class WorkspaceSnapshotRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_snapshot(
        self,
        *,
        snapshot_type: str,
        snapshot_date: str,
        payload: dict,
        source_job_id: int | None = None,
        commit: bool = True,
    ) -> WorkspaceSnapshot:
        stored_payload = payload
        threshold = max(
            1024,
            int(get_settings().workspace_snapshot_inline_payload_max_bytes),
        )
        if len(canonical_json_bytes(payload)) > threshold:
            reference = JsonPayloadArtifactStore().write(
                payload,
                namespace="workspace_snapshots",
            )
            stored_payload = build_payload_envelope(payload, reference)
        attempts = 4
        for attempt in range(1, attempts + 1):
            snapshot = WorkspaceSnapshot(
                snapshot_type=snapshot_type,
                snapshot_date=snapshot_date,
                payload_json=json.dumps(stored_payload, ensure_ascii=False),
                source_job_id=source_job_id,
                created_at=utc_now_iso(),
            )
            self.db.add(snapshot)
            if not commit:
                self.db.flush()
                return snapshot
            try:
                self.db.commit()
                self.db.refresh(snapshot)
                return snapshot
            except OperationalError as exc:
                self.db.rollback()
                if attempt >= attempts or not _is_database_locked_error(exc):
                    raise
                _sleep_for_lock_retry(attempt)
        raise RuntimeError("Workspace snapshot creation exhausted retries.")

    def get_latest_snapshot(self, snapshot_type: str) -> dict | None:
        stmt = (
            select(WorkspaceSnapshot)
            .where(WorkspaceSnapshot.snapshot_type == snapshot_type)
            .order_by(WorkspaceSnapshot.id.desc())
            .limit(1)
        )
        row = self.db.scalar(stmt)
        if row is None:
            return None
        try:
            stored_payload = json.loads(row.payload_json)
        except json.JSONDecodeError:
            stored_payload = None
        payload, payload_source = resolve_payload_envelope(stored_payload)
        return {
            "id": row.id,
            "snapshot_type": row.snapshot_type,
            "snapshot_date": row.snapshot_date,
            "payload": payload,
            "payload_source": payload_source,
            "source_job_id": row.source_job_id,
            "created_at": row.created_at,
        }

    def list_snapshots(self, snapshot_type: str, *, limit: int = 20) -> list[dict]:
        stmt = (
            select(WorkspaceSnapshot)
            .where(WorkspaceSnapshot.snapshot_type == snapshot_type)
            .order_by(WorkspaceSnapshot.id.desc())
            .limit(limit)
        )
        rows = self.db.scalars(stmt).all()
        results: list[dict] = []
        for row in rows:
            try:
                stored_payload = json.loads(row.payload_json)
            except json.JSONDecodeError:
                stored_payload = None
            payload, payload_source = resolve_payload_envelope(stored_payload)
            results.append(
                {
                    "id": row.id,
                    "snapshot_type": row.snapshot_type,
                    "snapshot_date": row.snapshot_date,
                    "payload": payload,
                    "payload_source": payload_source,
                    "source_job_id": row.source_job_id,
                    "created_at": row.created_at,
                }
            )
        return results

    def get_snapshot(self, snapshot_id: int, *, snapshot_type: str | None = None) -> dict | None:
        stmt = select(WorkspaceSnapshot).where(WorkspaceSnapshot.id == snapshot_id)
        if snapshot_type:
            stmt = stmt.where(WorkspaceSnapshot.snapshot_type == snapshot_type)
        row = self.db.scalar(stmt.limit(1))
        if row is None:
            return None
        try:
            stored_payload = json.loads(row.payload_json)
        except json.JSONDecodeError:
            stored_payload = None
        payload, payload_source = resolve_payload_envelope(stored_payload)
        return {
            "id": row.id,
            "snapshot_type": row.snapshot_type,
            "snapshot_date": row.snapshot_date,
            "payload": payload,
            "payload_source": payload_source,
            "source_job_id": row.source_job_id,
            "created_at": row.created_at,
        }

class DashboardReadRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def load_summary_snapshot(self) -> dict:
        model_repo = ModelRunRepository(self.db)
        signal_repo = PredictionRepository(self.db)
        backtest_repo = BacktestRepository(self.db)
        sync_repo = PriceSyncStateRepository(self.db)
        job_repo = DataJobRepository(self.db)
        concept_repo = ConceptSnapshotRepository(self.db)
        job_repo.complete_stale_running_jobs(
            job_types=["social_us_price_sync"],
            stale_after_hours=1,
            message_prefix="Dashboard cleanup closed a stale social U.S. price sync job.",
        )
        job_repo.complete_stale_running_jobs(
            stale_after_hours=6,
            message_prefix="Dashboard cleanup closed a stale running job.",
        )
        latest_signals = signal_repo.list_latest_signal_decisions(limit=10)
        return {
            "latest_signals": latest_signals,
            "sync_states": sync_repo.list_states_with_symbols(),
            "concept_summary": concept_repo.get_latest_summary(),
            "latest_model": model_repo.get_latest_run_summary(),
            "recent_model_runs": model_repo.list_recent_runs(limit=8),
            "latest_backtest": backtest_repo.get_latest_backtest_summary(),
            "latest_backtest_curve": backtest_repo.get_latest_backtest_curve(),
            "recent_jobs": job_repo.list_recent_jobs(limit=20),
        }

class AppSettingRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, key: str) -> str | None:
        setting = self.db.scalar(select(AppSetting).where(AppSetting.key == key))
        if setting is None:
            return None
        resolved, _source = decode_app_setting_value(setting.value)
        return resolved

    def set(self, key: str, value: str, *, commit: bool = True) -> AppSetting:
        stored_value, _source = encode_app_setting_value(
            value,
            max_inline_bytes=get_settings().app_setting_inline_value_max_bytes,
        )
        attempts = 4
        for attempt in range(1, attempts + 1):
            setting = self.db.scalar(select(AppSetting).where(AppSetting.key == key))
            now = utc_now_iso()
            if setting is None:
                setting = AppSetting(key=key, value=stored_value, updated_at=now)
                self.db.add(setting)
            else:
                setting.value = stored_value
                setting.updated_at = now
            if not commit:
                self.db.flush()
                return setting
            try:
                self.db.commit()
                self.db.refresh(setting)
                return setting
            except OperationalError as exc:
                self.db.rollback()
                if attempt >= attempts or not _is_database_locked_error(exc):
                    raise
                _sleep_for_lock_retry(attempt)
        raise RuntimeError("App setting update exhausted retries.")
