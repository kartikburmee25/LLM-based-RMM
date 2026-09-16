import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from .models import JobStatus


# ---- API keys -----------------------------------------------------------

class ApiKeyCreateRequest(BaseModel):
    name: str


class ApiKeyCreateResponse(BaseModel):
    id: uuid.UUID
    name: str
    api_key: str  # shown once, never retrievable again


# ---- Enrollment -----------------------------------------------------------

class EnrollmentTokenResponse(BaseModel):
    token: str  # shown once; hand this to the installer
    expires_at: datetime


class EnrollRequest(BaseModel):
    enrollment_token: str
    # Derived from the Windows MachineGuid. Stable across an agent
    # reinstall; changes only on a fresh OS image.
    hardware_fingerprint: str = Field(min_length=8, max_length=256)
    hostname: str


class EnrollResponse(BaseModel):
    device_id: uuid.UUID
    agent_token: str  # shown once; the agent persists this locally


# ---- Devices -----------------------------------------------------------

class DeviceResponse(BaseModel):
    id: uuid.UUID
    hostname: str
    enrolled_at: datetime
    last_seen_at: datetime | None
    reachable: bool

    class Config:
        from_attributes = True


# ---- Jobs -----------------------------------------------------------

class JobCreateRequest(BaseModel):
    script: str = Field(min_length=1, description="Raw PowerShell content to execute.")
    timeout_seconds: int | None = Field(
        default=None, ge=1, description="Overrides the server default; capped server-side."
    )


class JobResponse(BaseModel):
    id: uuid.UUID
    device_id: uuid.UUID
    idempotency_key: str | None
    script_content: str
    timeout_seconds: int
    status: JobStatus
    exit_code: int | None
    stdout: str | None
    stderr: str | None
    stdout_truncated: bool
    stderr_truncated: bool
    duration_ms: int | None
    created_by_api_key_id: uuid.UUID
    created_at: datetime
    deadline_at: datetime
    dispatched_at: datetime | None
    completed_at: datetime | None

    class Config:
        from_attributes = True


# ---- WebSocket wire protocol (agent <-> control plane) -------------------
# Kept as plain dict-based JSON messages rather than a formal schema on the
# wire, but documented here as the source of truth:
#
# server -> agent, when a job is dispatched:
#   {"type": "job", "job_id": "<uuid>", "script": "<raw powershell>",
#    "timeout_seconds": 300}
#
# agent -> server, once the script finishes (or is killed for timeout):
#   {"type": "job_result", "job_id": "<uuid>", "exit_code": 0,
#    "stdout": "...", "stderr": "...", "stdout_truncated": false,
#    "stderr_truncated": false, "duration_ms": 1234, "timed_out": false}
#
# "timed_out": true means the agent itself killed the process tree after
# timeout_seconds elapsed; exit_code is whatever the killed process happened
# to report (not meaningful) and the server records the job as TIMED_OUT
# regardless of it.
#
# agent -> server, sent immediately after the connection is accepted:
#   {"type": "hello", "agent_version": "1.0.0"}
#
# Liveness (device "reachable" state) is carried by WebSocket protocol-level
# ping/pong, not an application message -- see connection_manager.py.
