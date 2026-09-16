import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models
from .config import settings
from .connection_manager import manager


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_or_reuse_device(
    db: Session, hardware_fingerprint_hash: str, hostname: str, agent_token_hash: str
) -> models.Device:
    """Enroll a new device, or -- if this hardware fingerprint already has a
    device row -- treat it as a reinstall of the same machine: keep the same
    stable id, rotate the agent token, and update the hostname/timestamp.
    This is what makes the device id survive an agent reinstall."""

    existing = (
        db.query(models.Device)
        .filter(models.Device.hardware_fingerprint_hash == hardware_fingerprint_hash)
        .first()
    )
    if existing is not None:
        existing.agent_token_hash = agent_token_hash
        existing.hostname = hostname
        existing.last_enrolled_at = _now()
        db.commit()
        db.refresh(existing)
        return existing

    device = models.Device(
        hardware_fingerprint_hash=hardware_fingerprint_hash,
        hostname=hostname,
        agent_token_hash=agent_token_hash,
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return device


def device_reachable(device: models.Device) -> bool:
    return manager.is_connected(device.id)


def get_or_create_job(
    db: Session,
    device: models.Device,
    api_key: models.ApiKey,
    script: str,
    timeout_seconds: int | None,
    idempotency_key: str | None,
) -> tuple[models.Job, bool]:
    """Returns (job, created). If idempotency_key is set and a job already
    exists for (device, idempotency_key), that job is returned unchanged --
    a duplicated or retried POST never causes a second execution."""

    effective_timeout = min(
        timeout_seconds or settings.default_job_timeout_seconds,
        settings.max_job_timeout_seconds,
    )

    if idempotency_key:
        existing = (
            db.query(models.Job)
            .filter(
                models.Job.device_id == device.id,
                models.Job.idempotency_key == idempotency_key,
            )
            .first()
        )
        if existing is not None:
            return existing, False

    job = models.Job(
        device_id=device.id,
        idempotency_key=idempotency_key,
        script_content=script,
        timeout_seconds=effective_timeout,
        status=models.JobStatus.PENDING,
        created_by_api_key_id=api_key.id,
        deadline_at=_now() + timedelta(seconds=effective_timeout),
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        # Lost a race against a concurrent identical request -- fetch the
        # row the other request just created instead of erroring out.
        db.rollback()
        existing = (
            db.query(models.Job)
            .filter(
                models.Job.device_id == device.id,
                models.Job.idempotency_key == idempotency_key,
            )
            .first()
        )
        if existing is not None:
            return existing, False
        raise
    db.refresh(job)
    return job, True


async def dispatch_if_possible(db: Session, job: models.Job) -> None:
    """Push the job to the agent right now if it's online. If not, it stays
    PENDING and gets picked up by dispatch_pending_for_device() the next
    time that device's WebSocket connects."""

    if job.status != models.JobStatus.PENDING:
        return
    sent = await manager.send_json(
        job.device_id,
        {
            "type": "job",
            "job_id": str(job.id),
            "script": job.script_content,
            "timeout_seconds": job.timeout_seconds,
        },
    )
    if sent:
        job.status = models.JobStatus.DISPATCHED
        job.dispatched_at = _now()
        db.commit()


async def dispatch_pending_for_device(db: Session, device_id: uuid.UUID) -> None:
    """Called right after a device's WebSocket connects, so any job that was
    queued while it was offline goes out immediately instead of waiting for
    the next sweep."""

    pending = (
        db.query(models.Job)
        .filter(models.Job.device_id == device_id, models.Job.status == models.JobStatus.PENDING)
        .order_by(models.Job.created_at.asc())
        .all()
    )
    for job in pending:
        await dispatch_if_possible(db, job)


def apply_job_result(db: Session, job: models.Job, result: dict) -> None:
    max_chars = settings.max_output_chars
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")

    job.exit_code = result.get("exit_code")
    job.stdout = stdout[:max_chars]
    job.stderr = stderr[:max_chars]
    # Trust the agent's own truncation flag (it may have capped output
    # before it ever reached here), but also catch truncation happening at
    # this boundary in case the agent's cap and ours ever disagree.
    job.stdout_truncated = bool(result.get("stdout_truncated")) or len(stdout) > max_chars
    job.stderr_truncated = bool(result.get("stderr_truncated")) or len(stderr) > max_chars
    job.duration_ms = int(result.get("duration_ms") or 0)

    # The agent itself enforces the same timeout_seconds it was given and
    # kills the process tree if it's exceeded -- when it tells us that's
    # what happened, trust it over the exit code (a killed process's exit
    # code isn't a meaningful "success/failure" signal). This is the
    # explicit, fast path; the sweeper in timeouts.py is the backstop for
    # cases where the agent never manages to report back at all.
    if result.get("timed_out"):
        job.status = models.JobStatus.TIMED_OUT
    elif job.exit_code == 0:
        job.status = models.JobStatus.SUCCEEDED
    else:
        job.status = models.JobStatus.FAILED
    job.completed_at = _now()
    db.commit()
