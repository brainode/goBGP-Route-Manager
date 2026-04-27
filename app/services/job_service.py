# SPDX-License-Identifier: GPL-2.0-only
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import Job, JobLog

logger = logging.getLogger("uvicorn.error")


class LoggingList(list):
    """list subclass that writes each appended message to the job_logs table immediately."""

    def __init__(self, job_id: int, db: Session) -> None:
        super().__init__()
        self._job_id = job_id
        self._db = db

    def append(self, message: str) -> None:  # type: ignore[override]
        super().append(message)
        try:
            entry = JobLog(job_id=self._job_id, message=str(message))
            self._db.add(entry)
            self._db.commit()
        except Exception:
            pass


def create_job(db: Session, job_type: str, site_id: int | None = None, status: str = "pending") -> Job:
    job = Job(job_type=job_type, site_id=site_id, status=status)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def fail_job(job_id: int, reason: str) -> None:
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if job:
            job.status = "failed"
            job.finished_at = datetime.utcnow()
            db.add(JobLog(job_id=job_id, message=f"[failed] {reason}"))
            db.commit()
    except Exception:
        logger.exception("fail_job error job_id=%s", job_id)
    finally:
        db.close()


def has_active_job(db: Session, site_id: int) -> Job | None:
    """Return the first pending/running job for site_id, or None."""
    return (
        db.query(Job)
        .filter(Job.site_id == site_id, Job.status.in_(["pending", "running"]))
        .first()
    )


def finish_job(db: Session, job: Job, ok: bool) -> None:
    job.status = "done" if ok else "failed"
    job.finished_at = datetime.utcnow()
    db.commit()
