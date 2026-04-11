from app.services.repository import DataJobRepository


def build_job_payload(*, status: str, job_id: int | None = None, message: str | None = None, **extra) -> dict:
    payload = {"status": status}
    if job_id is not None:
        payload["job_id"] = job_id
    if message is not None:
        payload["message"] = message
    payload.update(extra)
    return payload


def complete_job_and_build_payload(
    job_repo: DataJobRepository,
    *,
    job_id: int,
    status: str,
    message: str,
    **extra,
) -> dict:
    job_repo.complete_job(job_id, status=status, message=message)
    return build_job_payload(status=status, job_id=job_id, message=message, **extra)


def fail_job_and_build_payload(job_repo: DataJobRepository, *, job_id: int, exc: Exception) -> dict:
    message = str(exc)
    job_repo.complete_job(job_id, status="failed", message=message)
    return build_job_payload(status="failed", job_id=job_id, message=message)
