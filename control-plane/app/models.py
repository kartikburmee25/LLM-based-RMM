import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ApiKey(Base):
    """A credential for human/system callers (technicians, MSP tooling).
    The raw key is only ever shown once, at creation time; we store a hash."""

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False)
    key_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EnrollmentToken(Base):
    """What the installer carries. Single-use: it can bootstrap exactly one
    device and then it's burned, so a copy of the installer package sitting
    around does not grant standing access to anything. It is NOT the
    credential the agent uses afterward -- that's Device.agent_token_hash,
    issued fresh at enroll time."""

    __tablename__ = "enrollment_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    created_by_api_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_keys.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_by_device_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("devices.id"), nullable=True
    )


class Device(Base):
    """A stable identity for one physical/virtual machine. id is generated
    once and never changes. hardware_fingerprint_hash is derived from the
    Windows MachineGuid, which survives an agent uninstall/reinstall (it
    only changes on a fresh OS install) -- that's what lets a reinstalled
    agent re-enroll and be recognized as the same device instead of
    creating a duplicate."""

    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    hardware_fingerprint_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    hostname: Mapped[str] = mapped_column(String, nullable=False)
    agent_token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    jobs: Mapped[list["Job"]] = relationship(back_populates="device")


class JobStatus(str, enum.Enum):
    PENDING = "pending"  # created, waiting for the device to be online
    DISPATCHED = "dispatched"  # sent to the agent, awaiting a result
    SUCCEEDED = "succeeded"  # agent reported exit_code == 0
    FAILED = "failed"  # agent reported exit_code != 0
    TIMED_OUT = "timed_out"  # dispatched, but the agent never reported back in time
    DEVICE_UNREACHABLE = "device_unreachable"  # never dispatched -- device was offline the whole window

    @property
    def is_terminal(self) -> bool:
        return self in (
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.TIMED_OUT,
            JobStatus.DEVICE_UNREACHABLE,
        )


class Job(Base):
    """One script execution request and, once it has one, its result. The
    row itself IS the audit record -- script_content is stored verbatim and
    never deleted."""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("device_id", "idempotency_key", name="uq_job_device_idempotency"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    device_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("devices.id"), nullable=False)

    # Caller-supplied (Idempotency-Key header). Nullable, but when present
    # the (device_id, idempotency_key) pair is unique -- a duplicated or
    # retried POST returns the original job instead of dispatching twice.
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True)

    script_content: Mapped[str] = mapped_column(Text, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status"), nullable=False, default=JobStatus.PENDING
    )

    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr: Mapped[str | None] = mapped_column(Text, nullable=True)
    stdout_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    stderr_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_by_api_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("api_keys.id"), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # deadline is computed once at creation (created_at + timeout_seconds) --
    # this is the single source of truth the sweeper checks against, whether
    # the job never got dispatched or got dispatched and then went quiet.
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    device: Mapped[Device] = relationship(back_populates="jobs")
