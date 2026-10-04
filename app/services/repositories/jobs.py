"""Data job domain repositories."""

import json
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.tables import (
    DataJob,
    JobDefinition,
    JobRunAttempt,
    JobRunDependency,
    MarketRefreshBatch,
)
from app.services.json_payload_artifacts import (
    JsonPayloadArtifactStore,
    canonical_json_bytes,
    summarize_job_result,
)
from app.services.time_utils import app_now

from app.services.repositories.market import MarketRefreshBatchRepository
from app.services.repositories.shared import (
    DECOMMISSIONED_CN_REVIEW_JOB_TYPE,
    JOB_MESSAGE_MAX_CHARS,
    _bounded_job_message,
    _is_database_locked_error,
    _job_duration_seconds,
    _loads_json_list,
    _loads_json_object,
    _sleep_for_lock_retry,
    utc_now_iso,
)


def _job_markets_from_params(job_type: str, params: dict | None) -> list[str]:
    payload = params or {}
    markets: list[str] = []
    direct = payload.get("market")
    if direct:
        markets.append(str(direct).strip().upper())
    configured = payload.get("markets")
    if isinstance(configured, str):
        markets.extend(item.strip().upper() for item in configured.split(","))
    elif isinstance(configured, (list, tuple, set)):
        markets.extend(str(item).strip().upper() for item in configured)
    normalized_type = str(job_type or "").lower()
    if not markets:
        if "cn" in normalized_type or "a_share" in normalized_type:
            markets.append("CN")
        elif "us" in normalized_type or "polygon" in normalized_type:
            markets.append("US")
    return sorted({market for market in markets if market in {"CN", "US", "HK"}})

def _job_category(job_type: str) -> str:
    normalized = str(job_type or "").lower()
    if any(token in normalized for token in ("refresh", "train", "screener", "report", "risk", "backtest")):
        return "daily_pipeline"
    if any(token in normalized for token in ("cleanup", "sync", "retention", "metadata", "universe")):
        return "maintenance"
    return "ad_hoc"

def _job_provider(params: dict | None) -> str | None:
    payload = params or {}
    for key in ("provider", "provider_used", "source"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return None

def _declared_upstream_job_ids(params: dict | None) -> list[int]:
    payload = params or {}
    raw: list[object] = [payload.get("source_job_id")]
    raw.extend(payload.get("source_job_ids") or [])
    raw.extend(payload.get("depends_on") or [])
    values: list[int] = []
    for item in raw:
        value = item.get("job_id", item.get("id")) if isinstance(item, dict) else item
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0 and parsed not in values:
            values.append(parsed)
    return values

def _dependency_status(upstream: DataJob | None) -> tuple[str, str | None]:
    if upstream is None:
        return "unknown", "Referenced upstream Job was not found."
    normalized = str(upstream.status or "").lower()
    if normalized == "success":
        return "satisfied", None
    if normalized in {"partial", "not_configured", "empty"}:
        return "degraded", upstream.message or "Upstream Job completed with degraded output."
    if normalized == "running":
        return "waiting", upstream.message or "Upstream Job is still running."
    return "blocked", upstream.message or f"Upstream Job status is {normalized or 'unknown'}."

class DataJobRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _ensure_run_metadata(self, job: DataJob, params: dict | None) -> None:
        """Create the definition, initial attempt, and declared lineage once.

        This intentionally treats the existing ``DataJob`` table as the
        job-run table, so rollout does not invalidate historical jobs.
        """

        now = utc_now_iso()
        definition = self.db.scalar(select(JobDefinition).where(JobDefinition.job_type == job.job_type))
        if definition is None:
            self.db.add(
                JobDefinition(
                    job_type=job.job_type,
                    display_name=job.job_type.replace("_", " "),
                    category=_job_category(job.job_type),
                    markets_json=json.dumps(_job_markets_from_params(job.job_type, params), ensure_ascii=False),
                    max_retries=0,
                    is_enabled=1,
                    config_json=json.dumps({"observed_from_run": True}, ensure_ascii=False),
                    created_at=now,
                    updated_at=now,
                )
            )
        attempt = self.db.scalar(
            select(JobRunAttempt)
            .where(JobRunAttempt.job_id == job.id)
            .order_by(JobRunAttempt.attempt_no.desc())
            .limit(1)
        )
        if attempt is None:
            self.db.add(
                JobRunAttempt(
                    job_id=job.id,
                    attempt_no=1,
                    status=job.status,
                    provider=_job_provider(params),
                    started_at=job.started_at,
                )
            )
        for upstream_id in _declared_upstream_job_ids(params):
            if upstream_id == job.id:
                continue
            exists = self.db.scalar(
                select(JobRunDependency.id)
                .where(JobRunDependency.job_id == job.id)
                .where(JobRunDependency.upstream_job_id == upstream_id)
                .where(JobRunDependency.dependency_type == "source_job")
                .limit(1)
            )
            if exists is not None:
                continue
            status, reason = _dependency_status(self.db.get(DataJob, upstream_id))
            self.db.add(
                JobRunDependency(
                    job_id=job.id,
                    upstream_job_id=upstream_id,
                    dependency_type="source_job",
                    status=status,
                    reason=reason,
                    created_at=now,
                    updated_at=now,
                )
            )
        self.db.commit()

    def _complete_latest_attempt(self, job: DataJob, *, status: str, result: dict | None) -> None:
        attempt = self.db.scalar(
            select(JobRunAttempt)
            .where(JobRunAttempt.job_id == job.id)
            .order_by(JobRunAttempt.attempt_no.desc())
            .limit(1)
        )
        if attempt is None:
            attempt = JobRunAttempt(
                job_id=job.id,
                attempt_no=1,
                status=status,
                provider=_job_provider(_loads_json_object(job.params_json)),
                started_at=job.started_at,
            )
            self.db.add(attempt)
        attempt.status = status
        attempt.finished_at = job.finished_at
        attempt.duration_seconds = _job_duration_seconds(job.started_at, job.finished_at)
        if status in {"failed", "failed_timeout"}:
            attempt.error_message = job.message
        if result is not None:
            attempt.summary_json = json.dumps(
                summarize_job_result(result),
                ensure_ascii=False,
            )
        self.db.commit()

    def _serialize_job(self, row: DataJob, *, hydrate_result_artifact: bool = False) -> dict:
        params = _loads_json_object(row.params_json)
        result = (params or {}).get("result") if isinstance(params, dict) else None
        result_source = "postgresql_inline" if isinstance(result, dict) else "none"
        result_artifact = (params or {}).get("result_artifact") if isinstance(params, dict) else None
        if not isinstance(result, dict) and isinstance(result_artifact, dict):
            if hydrate_result_artifact:
                result = JsonPayloadArtifactStore().read(result_artifact)
                result_source = "compressed_artifact"
            else:
                result = (params or {}).get("result_summary") or {}
                result_source = "postgresql_summary"
        runtime = (params or {}).get("job_runtime") if isinstance(params, dict) else None
        return {
            "id": row.id,
            "job_type": row.job_type,
            "status": row.status,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
            "message": row.message,
            "params_json": row.params_json,
            "params": params,
            "result": result,
            "result_source": result_source,
            "result_artifact": result_artifact,
            "pipeline_step": (params or {}).get("pipeline_step"),
            "depends_on": (params or {}).get("depends_on") or [],
            "input_summary": (params or {}).get("input_summary"),
            "output_summary": (result or {}).get("output_summary") if isinstance(result, dict) else None,
            "quality_summary": (result or {}).get("quality_summary") if isinstance(result, dict) else None,
            "retry_count": (result or {}).get("retry_count", 0) if isinstance(result, dict) else 0,
            "duration_seconds": (
                (runtime or {}).get("duration_seconds")
                if isinstance(runtime, dict)
                else _job_duration_seconds(row.started_at, row.finished_at)
            ),
        }

    def create_job(self, *, job_type: str, status: str, params: dict | None = None, message: str | None = None) -> DataJob:
        attempts = 4
        for attempt in range(1, attempts + 1):
            job = DataJob(
                job_type=job_type,
                status=status,
                started_at=utc_now_iso(),
                finished_at=None,
                message=_bounded_job_message(message),
                params_json=json.dumps(params, ensure_ascii=False) if params is not None else None,
            )
            self.db.add(job)
            try:
                self.db.commit()
                self.db.refresh(job)
                for metadata_attempt in range(1, attempts + 1):
                    try:
                        self._ensure_run_metadata(job, params)
                        break
                    except OperationalError as exc:
                        self.db.rollback()
                        if metadata_attempt >= attempts or not _is_database_locked_error(exc):
                            break
                        _sleep_for_lock_retry(metadata_attempt)
                return job
            except OperationalError as exc:
                self.db.rollback()
                if attempt >= attempts or not _is_database_locked_error(exc):
                    raise
                _sleep_for_lock_retry(attempt)
        raise RuntimeError("Data job creation exhausted retries.")

    def complete_job(
        self,
        job_id: int,
        *,
        status: str,
        message: str | None = None,
        result: dict | None = None,
    ) -> DataJob | None:
        attempts = 4
        for attempt in range(1, attempts + 1):
            stmt = select(DataJob).where(DataJob.id == job_id)
            job = self.db.scalar(stmt)
            if job is None:
                return None
            job.status = status
            job.finished_at = utc_now_iso()
            job.message = _bounded_job_message(message)
            if result is not None:
                params = _loads_json_object(job.params_json) or {}
                result_bytes = canonical_json_bytes(result)
                threshold = max(1024, int(get_settings().job_inline_result_max_bytes))
                if len(result_bytes) > threshold:
                    params.pop("result", None)
                    params["result_artifact"] = JsonPayloadArtifactStore().write(
                        result,
                        namespace="job_results",
                    )
                    params["result_summary"] = summarize_job_result(result)
                else:
                    params["result"] = result
                    params.pop("result_artifact", None)
                    params.pop("result_summary", None)
                params["job_runtime"] = {
                    **(params.get("job_runtime") or {}),
                    "duration_seconds": _job_duration_seconds(job.started_at, job.finished_at),
                    "completed_at": job.finished_at,
                }
                job.params_json = json.dumps(params, ensure_ascii=False)
            try:
                self.db.commit()
                self.db.refresh(job)
                self._complete_latest_attempt(job, status=status, result=result)
                return job
            except OperationalError as exc:
                self.db.rollback()
                if attempt >= attempts or not _is_database_locked_error(exc):
                    raise
                _sleep_for_lock_retry(attempt)
        raise RuntimeError("Data job completion exhausted retries.")

    def update_job(
        self,
        job_id: int,
        *,
        status: str | None = None,
        message: str | None = None,
        progress: dict | None = None,
    ) -> DataJob | None:
        attempts = 4
        for attempt in range(1, attempts + 1):
            stmt = select(DataJob).where(DataJob.id == job_id)
            job = self.db.scalar(stmt)
            if job is None:
                return None
            if status is not None:
                job.status = status
            if message is not None:
                job.message = _bounded_job_message(message)
            if progress is not None:
                params = _loads_json_object(job.params_json) or {}
                params["progress"] = {
                    **(params.get("progress") or {}),
                    **progress,
                    "updated_at": utc_now_iso(),
                }
                job.params_json = json.dumps(params, ensure_ascii=False)
            try:
                self.db.commit()
                self.db.refresh(job)
                return job
            except OperationalError as exc:
                self.db.rollback()
                if attempt >= attempts or not _is_database_locked_error(exc):
                    raise
                _sleep_for_lock_retry(attempt)
        raise RuntimeError("Data job progress update exhausted retries.")

    def record_job_runtime(self, job_id: int, **fields) -> DataJob | None:
        """Merge runtime/control metadata into ``params_json['job_runtime']``.

        Used for heartbeats, cancellation flags and deadlines.  Persisting
        these in the existing JSON payload avoids a schema migration.
        """

        attempts = 4
        for attempt in range(1, attempts + 1):
            job = self.db.scalar(select(DataJob).where(DataJob.id == job_id))
            if job is None:
                return None
            params = _loads_json_object(job.params_json) or {}
            runtime = dict(params.get("job_runtime") or {})
            runtime.update({key: value for key, value in fields.items() if value is not None})
            runtime["updated_at"] = utc_now_iso()
            params["job_runtime"] = runtime
            job.params_json = json.dumps(params, ensure_ascii=False)
            try:
                self.db.commit()
                self.db.refresh(job)
                return job
            except OperationalError as exc:
                self.db.rollback()
                if attempt >= attempts or not _is_database_locked_error(exc):
                    raise
                _sleep_for_lock_retry(attempt)
        raise RuntimeError("Data job runtime update exhausted retries.")

    def list_recent_jobs(self, limit: int = 20) -> list[dict]:
        stmt = (
            select(DataJob)
            .where(DataJob.job_type != DECOMMISSIONED_CN_REVIEW_JOB_TYPE)
            .order_by(DataJob.id.desc())
            .limit(max(limit * 2, limit))
        )
        rows = self.db.scalars(stmt).all()
        return [self._serialize_job(row) for row in rows][:limit]

    def get_job_detail(self, job_id: int) -> dict | None:
        row = self.db.get(DataJob, int(job_id))
        if row is None:
            return None
        payload = self._serialize_job(row, hydrate_result_artifact=True)
        definition = self.db.scalar(select(JobDefinition).where(JobDefinition.job_type == row.job_type))
        dependencies = self.db.scalars(
            select(JobRunDependency)
            .where(JobRunDependency.job_id == row.id)
            .order_by(JobRunDependency.id.asc())
        ).all()
        attempts = self.db.scalars(
            select(JobRunAttempt)
            .where(JobRunAttempt.job_id == row.id)
            .order_by(JobRunAttempt.attempt_no.asc())
        ).all()
        batches = self.db.scalars(
            select(MarketRefreshBatch)
            .where(MarketRefreshBatch.source_job_id == row.id)
            .order_by(MarketRefreshBatch.id.asc())
        ).all()
        payload["definition"] = (
            {
                "id": definition.id,
                "job_type": definition.job_type,
                "display_name": definition.display_name,
                "category": definition.category,
                "markets": _loads_json_list(definition.markets_json),
                "schedule_rule": definition.schedule_rule,
                "timeout_minutes": definition.timeout_minutes,
                "max_retries": definition.max_retries,
                "is_enabled": bool(definition.is_enabled),
            }
            if definition is not None
            else None
        )
        upstream_by_id = {
            dependency.upstream_job_id: self.db.get(DataJob, dependency.upstream_job_id)
            for dependency in dependencies
        }
        payload["dependencies"] = []
        for dependency in dependencies:
            upstream = upstream_by_id.get(dependency.upstream_job_id)
            payload["dependencies"].append(
                {
                    "id": dependency.id,
                    "upstream_job_id": dependency.upstream_job_id,
                    "upstream_job_type": upstream.job_type if upstream is not None else None,
                    "upstream_status": upstream.status if upstream is not None else "unknown",
                    "dependency_type": dependency.dependency_type,
                    "status": dependency.status,
                    "reason": dependency.reason,
                    "required_as_of_date": dependency.required_as_of_date,
                    "actual_as_of_date": dependency.actual_as_of_date,
                }
            )
        payload["attempts"] = [
            {
                "attempt_no": attempt.attempt_no,
                "status": attempt.status,
                "provider": attempt.provider,
                "started_at": attempt.started_at,
                "finished_at": attempt.finished_at,
                "duration_seconds": attempt.duration_seconds,
                "error_message": attempt.error_message,
                "summary": _loads_json_object(attempt.summary_json),
            }
            for attempt in attempts
        ]
        payload["market_refresh_batches"] = [MarketRefreshBatchRepository._serialize(batch) for batch in batches]
        return payload

    def get_latest_job(self, job_type: str | list[str] | tuple[str, ...] | set[str]) -> dict | None:
        if isinstance(job_type, (list, tuple, set)):
            job_types = [str(item or "").strip() for item in job_type if str(item or "").strip()]
        else:
            job_types = [str(job_type or "").strip()] if str(job_type or "").strip() else []
        job_types = [job_type for job_type in job_types if job_type != DECOMMISSIONED_CN_REVIEW_JOB_TYPE]
        if not job_types:
            return None
        stmt = (
            select(DataJob)
            .where(DataJob.job_type.in_(job_types))
            .order_by(DataJob.id.desc())
            .limit(1)
        )
        row = self.db.scalar(stmt)
        return (
            self._serialize_job(row, hydrate_result_artifact=True)
            if row is not None
            else None
        )

    def has_running_job(self, job_type: str) -> bool:
        stmt = (
            select(DataJob.id)
            .where(DataJob.job_type == job_type)
            .where(DataJob.status == "running")
            .limit(1)
        )
        return self.db.scalar(stmt) is not None

    def get_running_job(self, job_types: str | list[str] | tuple[str, ...] | set[str]) -> dict | None:
        """Return the oldest active job for a type set so UI retries are idempotent."""
        if isinstance(job_types, str):
            normalized = [job_types.strip()]
        else:
            normalized = [str(item or "").strip() for item in job_types]
        normalized = [item for item in normalized if item]
        if not normalized:
            return None
        row = self.db.scalar(
            select(DataJob)
            .where(DataJob.job_type.in_(normalized))
            .where(DataJob.status == "running")
            .order_by(DataJob.id.asc())
            .limit(1)
        )
        return self._serialize_job(row) if row is not None else None

    def complete_stale_running_jobs(
        self,
        *,
        job_types: list[str] | None = None,
        stale_after_hours: int = 6,
        message_prefix: str = "Marked stale running job as failed.",
    ) -> int:
        cutoff = app_now() - timedelta(hours=max(1, stale_after_hours))
        stmt = select(DataJob).where(DataJob.status == "running")
        if job_types:
            stmt = stmt.where(DataJob.job_type.in_(job_types))
        rows = self.db.scalars(stmt).all()
        updated = 0
        completed_rows: list[DataJob] = []
        now_iso = utc_now_iso()
        for row in rows:
            try:
                started_at = datetime.fromisoformat(row.started_at)
            except (TypeError, ValueError):
                started_at = None
            if started_at is None or started_at > cutoff:
                continue
            row.status = "failed"
            row.finished_at = now_iso
            original_message = (row.message or "").strip()
            row.message = (
                f"{message_prefix} Original state started at {row.started_at}."
                if not original_message
                else f"{message_prefix} {original_message}"
            )
            updated += 1
            completed_rows.append(row)
        if updated:
            self.db.commit()
            for row in completed_rows:
                self._complete_latest_attempt(row, status="failed_timeout", result={"timeout": True})
        return updated
