# SPDX-License-Identifier: GPL-2.0-only
from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime, timedelta
from threading import Thread
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import state as _state
from ..config import LATENCY_CHECK_INTERVAL_SECONDS, LATENCY_RETENTION_HOURS
from ..database import SessionLocal
from ..models import LatencyMeasurement, NextHop

logger = logging.getLogger("uvicorn.error")

_PING_TIME_RE = re.compile(r"time[<=]([0-9]+(?:\.[0-9]+)?)\s*ms")


def _parse_ping_ms(stdout: str) -> float | None:
    stdout_lower = stdout.lower()
    if "unreachable" in stdout_lower:
        return None
    if "timed out" in stdout_lower:
        return None
    if "100% packet loss" in stdout_lower:
        return None
    if "0% packet loss" not in stdout_lower and "received" in stdout_lower:
        # heuristic: if no success indicator and there is received mention, look deeper
        pass
    m = _PING_TIME_RE.search(stdout)
    if m:
        return float(m.group(1))
    return None


def ping_next_hop(ip: str) -> float | None:
    import sys

    if sys.platform == "win32":
        cmd = ["ping", "-n", "1", "-w", "2000", ip]
    else:
        cmd = ["ping", "-c", "1", "-W", "2", ip]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        logger.exception("ping subprocess failed ip=%s", ip)
        return None

    stdout = result.stdout
    stderr = result.stderr
    combined = stdout + "\n" + stderr
    latency = _parse_ping_ms(combined)
    if latency is None:
        logger.debug("ping timeout/unreachable ip=%s rc=%s", ip, result.returncode)
    else:
        logger.debug("ping ok ip=%s latency_ms=%s", ip, latency)
    return latency


def record_latency(db: Session, next_hop_id: int, latency_ms: float | None) -> None:
    db.add(LatencyMeasurement(next_hop_id=next_hop_id, latency_ms=latency_ms))


def get_average_latency(db: Session, next_hop_id: int, hours: int = 24) -> float | None:
    since = datetime.utcnow() - timedelta(hours=hours)
    avg = (
        db.query(func.avg(LatencyMeasurement.latency_ms))
        .filter(
            LatencyMeasurement.next_hop_id == next_hop_id,
            LatencyMeasurement.latency_ms.is_not(None),
            LatencyMeasurement.created_at >= since,
        )
        .scalar()
    )
    if avg is None:
        return None
    return round(float(avg), 2)


def _cleanup_old_measurements(db: Session) -> int:
    cutoff = datetime.utcnow() - timedelta(hours=LATENCY_RETENTION_HOURS)
    result = db.query(LatencyMeasurement).filter(LatencyMeasurement.created_at < cutoff).delete(synchronize_session=False)
    return result


def run_latency_check_cycle() -> None:
    db = SessionLocal()
    try:
        hops = db.query(NextHop).all()
        if not hops:
            return
        for hop in hops:
            latency = ping_next_hop(hop.ip)
            record_latency(db, hop.id, latency)
        db.commit()
        deleted = _cleanup_old_measurements(db)
        db.commit()
        if deleted:
            logger.info("latency cleanup removed %s old rows", deleted)
    except Exception:
        logger.exception("latency check cycle failed")
        db.rollback()
    finally:
        db.close()


def latency_worker() -> None:
    interval = LATENCY_CHECK_INTERVAL_SECONDS
    while not _state.latency_check_stop.is_set():
        if _state.latency_check_stop.wait(interval):
            break
        run_latency_check_cycle()


def start_latency_worker() -> None:
    if _state.latency_check_thread is None or not _state.latency_check_thread.is_alive():
        _state.latency_check_stop.clear()
        _state.latency_check_thread = Thread(
            target=latency_worker, name="latency-check", daemon=True
        )
        _state.latency_check_thread.start()
        logger.info("latency worker started interval=%s", LATENCY_CHECK_INTERVAL_SECONDS)
