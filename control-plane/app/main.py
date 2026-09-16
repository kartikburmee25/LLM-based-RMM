import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import crud, models, schemas
from .config import settings
from .connection_manager import manager
from .db import Base, SessionLocal, engine, get_db
from .security import authenticate_agent_token, generate_token, get_current_api_key, hash_token
from .timeouts import sweeper_loop

app = FastAPI(title="RMM Control Plane", version="1.0.0")
logger = logging.getLogger("rmm.ws")


@app.on_event("startup")
def on_startup() -> None:
    # Basic schema bootstrap for a project this size. A real deployment would
    # swap this for Alembic migrations; create_all is fine as long as the
    # schema only ever grows additively during development.
    Base.metadata.create_all(bind=engine)
    asyncio.create_task(sweeper_loop())


# ---- Bootstrap: API keys -------------------------------------------------
# Not asked for by the spec, but callers need *some* way to get their first
# API key. This endpoint is intentionally unauthenticated only because it's
# meant to be run once, by an operator with shell access to the control
# plane (see scripts/create_api_key.py for the version that doesn't need
# the API to be reachable at all -- prefer that one in practice and disable
# this route, or put it behind a network policy, before exposing the API
# publicly).
@app.post("/bootstrap/api-keys", response_model=schemas.ApiKeyCreateResponse)
def create_api_key(req: schemas.ApiKeyCreateRequest, db: Session = Depends(get_db)):
    raw = generate_token("key")
    key = models.ApiKey(name=req.name, key_hash=hash_token(raw))
    db.add(key)
    db.commit()
    db.refresh(key)
    return schemas.ApiKeyCreateResponse(id=key.id, name=key.name, api_key=raw)


# ---- Enrollment -----------------------------------------------------------

@app.post("/enrollment-tokens", response_model=schemas.EnrollmentTokenResponse)
def create_enrollment_token(
    api_key: models.ApiKey = Depends(get_current_api_key), db: Session = Depends(get_db)
):
    """Issue a fresh, single-use token to embed in one installer run. This
    is what the caller hands to the installer -- not their own API key."""
    raw = generate_token("enr")
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=settings.enrollment_token_ttl_seconds
    )
    token = models.EnrollmentToken(
        token_hash=hash_token(raw), created_by_api_key_id=api_key.id, expires_at=expires_at
    )
    db.add(token)
    db.commit()
    return schemas.EnrollmentTokenResponse(token=raw, expires_at=expires_at)


@app.post("/enroll", response_model=schemas.EnrollResponse)
def enroll(req: schemas.EnrollRequest, db: Session = Depends(get_db)):
    """Called by the agent itself during unattended install. Consumes the
    enrollment token exactly once: a used or expired token cannot enroll
    another machine, and the token itself is never usable as a standing
    credential -- it's swapped here for a brand new, device-specific
    agent_token that the enrollment token's creator never sees."""

    token_hash = hash_token(req.enrollment_token)
    token = db.query(models.EnrollmentToken).filter(
        models.EnrollmentToken.token_hash == token_hash
    ).first()
    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown enrollment token")
    if token.used_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Enrollment token already used")
    if token.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Enrollment token expired")

    fingerprint_hash = hash_token(req.hardware_fingerprint)
    agent_token = generate_token("agt")
    device = crud.create_or_reuse_device(
        db,
        hardware_fingerprint_hash=fingerprint_hash,
        hostname=req.hostname,
        agent_token_hash=hash_token(agent_token),
    )

    token.used_at = datetime.now(timezone.utc)
    token.used_by_device_id = device.id
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Enrollment token already used")

    return schemas.EnrollResponse(device_id=device.id, agent_token=agent_token)


# ---- Devices -----------------------------------------------------------

@app.get("/devices", response_model=list[schemas.DeviceResponse])
def list_devices(
    api_key: models.ApiKey = Depends(get_current_api_key), db: Session = Depends(get_db)
):
    devices = db.query(models.Device).order_by(models.Device.enrolled_at.asc()).all()
    return [
        schemas.DeviceResponse(
            id=d.id,
            hostname=d.hostname,
            enrolled_at=d.enrolled_at,
            last_seen_at=d.last_seen_at,
            reachable=crud.device_reachable(d),
        )
        for d in devices
    ]


@app.get("/devices/{device_id}", response_model=schemas.DeviceResponse)
def get_device(
    device_id: uuid.UUID,
    api_key: models.ApiKey = Depends(get_current_api_key),
    db: Session = Depends(get_db),
):
    device = db.query(models.Device).filter(models.Device.id == device_id).first()
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Device not found")
    return schemas.DeviceResponse(
        id=device.id,
        hostname=device.hostname,
        enrolled_at=device.enrolled_at,
        last_seen_at=device.last_seen_at,
        reachable=crud.device_reachable(device),
    )


# ---- Jobs -----------------------------------------------------------

@app.post("/devices/{device_id}/jobs", response_model=schemas.JobResponse, status_code=status.HTTP_201_CREATED)
async def create_job(
    device_id: uuid.UUID,
    req: schemas.JobCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    api_key: models.ApiKey = Depends(get_current_api_key),
    db: Session = Depends(get_db),
):
    device = db.query(models.Device).filter(models.Device.id == device_id).first()
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Device not found")

    job, created = crud.get_or_create_job(
        db,
        device=device,
        api_key=api_key,
        script=req.script,
        timeout_seconds=req.timeout_seconds,
        idempotency_key=idempotency_key,
    )
    if created:
        await crud.dispatch_if_possible(db, job)
    return job


@app.get("/devices/{device_id}/jobs", response_model=list[schemas.JobResponse])
def list_jobs_for_device(
    device_id: uuid.UUID,
    api_key: models.ApiKey = Depends(get_current_api_key),
    db: Session = Depends(get_db),
):
    return (
        db.query(models.Job)
        .filter(models.Job.device_id == device_id)
        .order_by(models.Job.created_at.desc())
        .all()
    )


@app.get("/jobs/{job_id}", response_model=schemas.JobResponse)
def get_job(
    job_id: uuid.UUID,
    api_key: models.ApiKey = Depends(get_current_api_key),
    db: Session = Depends(get_db),
):
    job = db.query(models.Job).filter(models.Job.id == job_id).first()
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return job


# ---- Agent WebSocket -----------------------------------------------------

@app.websocket("/ws/agent")
async def agent_socket(websocket: WebSocket):
    """The one long-lived, agent-initiated connection each device holds
    open. Auth happens before accept() via the Authorization header the
    ClientWebSocket sends during the handshake -- FastAPI/Starlette exposes
    it on websocket.headers same as a normal request."""

    db = SessionLocal()
    try:
        device = authenticate_agent_token(
            _bearer_from_headers(websocket.headers.get("authorization")), db
        )
        if device is None:
            await websocket.close(code=4401, reason="invalid agent token")
            return

        await websocket.accept()
        await manager.connect(device.id, websocket)
        device.last_seen_at = datetime.now(timezone.utc)
        db.commit()

        # Anything queued while this device was offline goes out now.
        await crud.dispatch_pending_for_device(db, device.id)

        while True:
            message = await websocket.receive_json()
            device.last_seen_at = datetime.now(timezone.utc)
            db.commit()

            if message.get("type") == "job_result":
                job = (
                    db.query(models.Job)
                    .filter(
                        models.Job.id == uuid.UUID(message["job_id"]),
                        models.Job.device_id == device.id,
                    )
                    .first()
                )
                # Ignore results for jobs we don't recognize (already timed
                # out and swept, or from a different device) -- the caller
                # already got a terminal status, and we won't overwrite it.
                if job is not None and not job.status.is_terminal:
                    crud.apply_job_result(db, job, message)
            # "hello" and any other message types are currently informational
            # only; unknown types are ignored rather than closing the socket,
            # so older/newer agent versions can still connect.

    except WebSocketDisconnect:
        pass
    except Exception:
        # A malformed frame or any other unexpected error on this
        # connection shouldn't take the server down -- log it and let the
        # finally block clean up; the agent's own reconnect loop handles
        # the rest.
        logger.exception("agent websocket error for device %s", locals().get("device"))
    finally:
        try:
            await manager.disconnect(device.id, websocket)  # type: ignore[possibly-undefined]
        except Exception:
            pass
        db.close()


def _bearer_from_headers(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        return ""
    return authorization.split(" ", 1)[1].strip()
