#!/usr/bin/env python3
"""Bootstrap the first API key directly against the database, without going
through the HTTP API. Run this once after the control plane is up:

    docker compose exec api python scripts/create_api_key.py "my technician"

Prints the raw key exactly once -- store it somewhere safe, it is not
recoverable afterward (only its hash is kept in the database).
"""

import sys

sys.path.insert(0, "/app")

from app.db import SessionLocal, Base, engine  # noqa: E402
from app.models import ApiKey  # noqa: E402
from app.security import generate_token, hash_token  # noqa: E402


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "default"
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        raw = generate_token("key")
        key = ApiKey(name=name, key_hash=hash_token(raw))
        db.add(key)
        db.commit()
        print(f"API key for '{name}':\n{raw}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
