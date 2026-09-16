"""Central configuration. Everything is overridable via environment variables
so the same image works in docker-compose, CI, or bare-metal."""

import os


class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL", "postgresql://rmm:rmm@localhost:5432/rmm"
    )

    # Output from a job is capped so a runaway script can't fill the DB or
    # the WebSocket frame. Each stream (stdout/stderr) is truncated to this
    # many characters; the truncated flag tells the caller output was cut.
    max_output_chars: int = int(os.getenv("MAX_OUTPUT_CHARS", "65536"))

    # Hard ceiling on the timeout a caller may request for a job, so nobody
    # accidentally leaves a job "pending" forever.
    max_job_timeout_seconds: int = int(os.getenv("MAX_JOB_TIMEOUT_SECONDS", "3600"))
    default_job_timeout_seconds: int = int(
        os.getenv("DEFAULT_JOB_TIMEOUT_SECONDS", "300")
    )

    # How often the background sweeper checks for jobs that ran past their
    # deadline (device never came online, or agent never reported back).
    sweep_interval_seconds: float = float(os.getenv("SWEEP_INTERVAL_SECONDS", "5"))

    # Enrollment tokens are single-use but still expire, so a leaked/forgotten
    # token from an old installer package can't be used months later.
    enrollment_token_ttl_seconds: int = int(
        os.getenv("ENROLLMENT_TOKEN_TTL_SECONDS", "86400")
    )

    # WebSocket keepalive: the server sends a protocol ping on this interval;
    # if no pong (or other traffic) is seen within the timeout, the
    # connection is considered dead and the device flips to "unreachable".
    ws_ping_interval_seconds: float = float(os.getenv("WS_PING_INTERVAL_SECONDS", "20"))
    ws_ping_timeout_seconds: float = float(os.getenv("WS_PING_TIMEOUT_SECONDS", "45"))


settings = Settings()
