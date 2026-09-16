"""Auth for the two kinds of caller: human/system API keys (Authorization:
Bearer <key>) and device agent tokens (used only during the WebSocket
handshake). Both are high-entropy random tokens, not user passwords, so a
plain salted SHA-256 hash is enough -- no need for bcrypt/argon2 here."""

import hashlib
import secrets

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from .db import get_db
from .models import ApiKey, Device


def generate_token(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def get_current_api_key(
    authorization: str | None = Header(default=None), db: Session = Depends(get_db)
) -> ApiKey:
    token = _extract_bearer(authorization)
    key = db.query(ApiKey).filter(ApiKey.key_hash == hash_token(token)).first()
    if key is None or key.revoked_at is not None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or revoked API key")
    return key


def authenticate_agent_token(token: str, db: Session) -> Device | None:
    return db.query(Device).filter(Device.agent_token_hash == hash_token(token)).first()


def _extract_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    return authorization.split(" ", 1)[1].strip()
