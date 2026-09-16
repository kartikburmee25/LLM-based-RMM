"""Background sweeper: periodically closes out jobs that blew past their
deadline. Two distinct terminal states, matching the two distinct reasons a
caller cares about:

  - DEVICE_UNREACHABLE: the job never got dispatched at all -- the device
    was offline for the entire window.
  - TIMED_OUT: the job WAS dispatched (the agent had it) but no result came
    back in time -- the script hung, or the agent/device dropped mid-run.
"""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import models
from .config import settings
from .db import SessionLocal

logger = logging.getLogger("rmm.timeouts")


def sweep_once(db: Session) -> int:
    now = datetime.now(timezone.utc)
    overdue = (
        db.query(models.Job)
        .filter(
            models.Job.status.in_([models.JobStatus.PENDING, models.JobStatus.DISPATCHED]),
            models.Job.deadline_at < now,
        )
        .all()
    )
    for job in overdue:
        job.status = (
            models.JobStatus.DEVICE_UNREACHABLE
            if job.status == models.JobStatus.PENDING
            else models.JobStatus.TIMED_OUT
        )
        job.completed_at = now
    if overdue:
        db.commit()
    return len(overdue)


async def sweeper_loop() -> None:
    while True:
        try:
            db = SessionLocal()
            try:
                n = sweep_once(db)
                if n:
                    logger.info("timed out %d job(s)", n)
            finally:
                db.close()
        except Exception:  # never let the sweeper die
            logger.exception("job sweep failed")
        await asyncio.sleep(settings.sweep_interval_seconds)
