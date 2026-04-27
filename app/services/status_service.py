# SPDX-License-Identifier: GPL-2.0-only
from __future__ import annotations

import logging
from concurrent.futures import wait
from datetime import datetime
from ipaddress import ip_network
from typing import Optional

from fastapi import BackgroundTasks
from sqlalchemy.orm import Session, joinedload

from .. import state as _state
from ..config import (
    REDISCOVER_QUEUE_PARALLELISM,
    STATUS_REFRESH_INTERVAL_SECONDS,
)
from ..database import SessionLocal
from ..models import Job, Prefix, Site
from . import rediscover_service, route_service, settings_service
from .job_service import LoggingList, create_job, fail_job, finish_job, has_active_job

logger = logging.getLogger("uvicorn.error")


def _timestamp_now() -> str:
    from datetime import timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def refresh_gobgp_state(trigger: str) -> None:
    if not _state.status_refresh_lock.acquire(blocking=False):
        logger.info("status refresh skipped trigger=%s reason=busy", trigger)
        return

    db = SessionLocal()
    try:
        ok, routes, message = _state.gobgp.list_routes()
        if not ok:
            logger.warning("status refresh failed trigger=%s message=%s", trigger, message)
            return

        route_set = {str(ip_network(r, strict=False)) for r in routes}
        route_nets = [ip_network(r, strict=False) for r in route_set]
        checked_at = datetime.utcnow()
        prefixes = db.query(Prefix).all()
        for prefix in prefixes:
            net = ip_network(str(ip_network(prefix.cidr, strict=False)), strict=False)
            prefix.is_announced = any(
                net == rn or (net.version == rn.version and net.subnet_of(rn))
                for rn in route_nets
            )
            prefix.last_checked_at = checked_at
        db.commit()
        logger.info(
            "status refresh done trigger=%s routes=%s prefixes=%s",
            trigger, len(route_set), len(prefixes),
        )
    except Exception:
        logger.exception("status refresh failed trigger=%s", trigger)
        db.rollback()
    finally:
        db.close()
        _state.status_refresh_lock.release()


def schedule_status_refresh(background_tasks: Optional[BackgroundTasks], trigger: str) -> None:
    if background_tasks is None:
        refresh_gobgp_state(trigger)
        return
    background_tasks.add_task(refresh_gobgp_state, trigger)


def start_background_refresh() -> None:
    import os
    from threading import Thread
    interval = max(int(os.getenv("STATUS_REFRESH_INTERVAL_SECONDS", "3600")), 0)
    refresh_gobgp_state("startup")
    _state.get_rediscover_executor()
    if interval > 0 and (_state.status_refresh_thread is None or not _state.status_refresh_thread.is_alive()):
        _state.status_refresh_stop.clear()
        _state.status_refresh_thread = Thread(target=status_refresh_worker, name="gobgp-status-refresh", daemon=True)
        _state.status_refresh_thread.start()


def status_refresh_worker() -> None:
    while not _state.status_refresh_stop.is_set():
        if _state.status_refresh_stop.wait(STATUS_REFRESH_INTERVAL_SECONDS):
            break
        refresh_gobgp_state("periodic")
        run_auto_rediscover_cycle("periodic")


def run_auto_rediscover_cycle(trigger: str) -> None:
    if not _state.auto_rediscover_lock.acquire(blocking=False):
        logger.info("auto rediscover skipped trigger=%s reason=busy", trigger)
        return
    if not _state.maintenance_lock.acquire(blocking=False):
        logger.info("auto rediscover skipped trigger=%s reason=maintenance_busy", trigger)
        _state.auto_rediscover_lock.release()
        return

    db = SessionLocal()
    try:
        sites = (
            db.query(Site)
            .options(joinedload(Site.next_hop), joinedload(Site.prefixes))
            .filter(Site.is_manual == False, Site.auto_rediscover_enabled == True)  # noqa: E712
            .order_by(Site.domain.asc())
            .all()
        )
        if not sites:
            logger.info("auto rediscover skipped trigger=%s reason=no_sites", trigger)
            return

        started = completed = failed = skipped = 0
        for site in sites:
            if has_active_job(db, site.id):
                skipped += 1
                logger.info(
                    "auto rediscover site skipped site_id=%s domain=%s reason=job_running",
                    site.id, site.domain,
                )
                continue

            job = Job(job_type="auto_rediscover_site", site_id=site.id, status="running")
            db.add(job)
            db.commit()
            db.refresh(job)
            started += 1
            debug = LoggingList(job.id, db)
            try:
                result = rediscover_service.rediscover_site_state(db, site, apply_changes=True, debug=debug)
                job.status = "done" if result["ok"] else "failed"
                job.finished_at = datetime.utcnow()
                db.commit()
                if result["ok"]:
                    completed += 1
                else:
                    failed += 1
            except Exception:
                logger.exception("auto rediscover site failed site_id=%s domain=%s", site.id, site.domain)
                job.status = "failed"
                job.finished_at = datetime.utcnow()
                db.commit()
                failed += 1

        refresh_gobgp_state(f"auto_rediscover:{trigger}")
        settings_service.set_maintenance_status(
            f"{_timestamp_now()} Auto rediscover: started {started}, completed {completed}, "
            f"failed {failed}, skipped {skipped}"
        )
    finally:
        db.close()
        _state.maintenance_lock.release()
        _state.auto_rediscover_lock.release()


def run_apply_current_state_job(trigger: str, job_id: Optional[int] = None) -> None:
    if not _state.maintenance_lock.acquire(blocking=False):
        logger.warning("maintenance skipped trigger=%s reason=busy", trigger)
        settings_service.set_maintenance_status(
            f"{_timestamp_now()} Busy: another maintenance task is already running"
        )
        if job_id is not None:
            fail_job(job_id, "skipped: another maintenance task is already running")
        return

    settings_service.set_maintenance_status(f"{_timestamp_now()} Running: apply current state")
    db = SessionLocal()
    try:
        if job_id is not None:
            job = db.query(Job).filter(Job.id == job_id).first()
            if job:
                job.status = "running"
                db.commit()
        debug = LoggingList(job_id, db) if job_id is not None else None
        result = route_service.apply_current_state(db, debug)
        logger.info("maintenance apply_current trigger=%s result=%s", trigger, result)
        if result["ok"]:
            settings_service.set_maintenance_status(
                f"{_timestamp_now()} Apply complete: removed {result['routes_removed']}/{result['routes_found']} existing routes, "
                f"raw {result['raw_prefix_rows']} rows -> {result['optimized_unique_routes']} optimized routes, "
                f"applied {result['prefixes_applied']} routes across {result['sites']} enabled sites"
            )
        else:
            errors = ", ".join(result["errors"][:3]) if result["errors"] else "unknown error"
            settings_service.set_maintenance_status(
                f"{_timestamp_now()} Apply finished with errors: removed {result['routes_removed']}/{result['routes_found']} existing routes, "
                f"raw {result['raw_prefix_rows']} rows -> {result['optimized_unique_routes']} optimized routes, "
                f"applied {result['prefixes_applied']} routes, failed {result['prefixes_failed']} ({errors})"
            )
        if job_id is not None:
            job = db.query(Job).filter(Job.id == job_id).first()
            if job:
                finish_job(db, job, ok=result["ok"])
    except Exception:
        logger.exception("maintenance apply_current failed trigger=%s", trigger)
        settings_service.set_maintenance_status(f"{_timestamp_now()} Apply failed: see container logs for details")
        if job_id is not None:
            fail_job(job_id, "apply_current_state raised an exception")
    finally:
        db.close()
        _state.maintenance_lock.release()
    refresh_gobgp_state(f"apply_current:{trigger}")


def run_rediscover_all_and_apply_job(trigger: str) -> None:
    if not _state.maintenance_lock.acquire(blocking=False):
        logger.warning("maintenance skipped trigger=%s reason=busy", trigger)
        settings_service.set_maintenance_status(
            f"{_timestamp_now()} Busy: another maintenance task is already running"
        )
        return

    settings_service.set_maintenance_status(
        f"{_timestamp_now()} Running: rediscover all sites via queue (parallel {REDISCOVER_QUEUE_PARALLELISM})"
    )
    db = SessionLocal()
    try:
        sites = (
            db.query(Site)
            .options(joinedload(Site.next_hop), joinedload(Site.prefixes))
            .order_by(Site.domain.asc())
            .all()
        )
        if not sites:
            settings_service.set_maintenance_status(f"{_timestamp_now()} Rediscover all skipped: no sites found")
            return

        queued = skipped = 0
        futures = []
        job_ids: list[int] = []
        for site in sites:
            if has_active_job(db, site.id):
                skipped += 1
                logger.info(
                    "maintenance rediscover_all skipped site_id=%s domain=%s reason=job_running",
                    site.id, site.domain,
                )
                continue
            job, future = rediscover_service.queue_rediscover_site(db, site, source=f"settings:{trigger}")
            queued += 1
            job_ids.append(job.id)
            futures.append(future)

        settings_service.set_maintenance_status(
            f"{_timestamp_now()} Rediscover queued: {queued} sites at parallel {REDISCOVER_QUEUE_PARALLELISM}"
            + (f", skipped {skipped} active jobs" if skipped else "")
        )

        if futures:
            wait(futures)

        apply_result = route_service.apply_current_state(db)
        db.expire_all()
        jobs = db.query(Job).filter(Job.id.in_(job_ids)).all() if job_ids else []
        rediscover_done = sum(1 for j in jobs if j.status == "done")
        rediscover_failed = sum(1 for j in jobs if j.status == "failed")

        logger.info(
            "maintenance rediscover_all trigger=%s queued=%s done=%s failed=%s skipped=%s apply=%s",
            trigger, queued, rediscover_done, rediscover_failed, skipped, apply_result,
        )
        if apply_result["ok"] and rediscover_failed == 0:
            settings_service.set_maintenance_status(
                f"{_timestamp_now()} Rediscover complete: updated {rediscover_done} sites, skipped {skipped}, "
                f"then applied {apply_result['prefixes_applied']} routes "
                f"(raw {apply_result['raw_prefix_rows']} rows -> {apply_result['optimized_unique_routes']} optimized)"
            )
        else:
            errors = (
                ", ".join(apply_result["errors"][:3]) if apply_result["errors"] else "rediscover/apply partial failure"
            )
            settings_service.set_maintenance_status(
                f"{_timestamp_now()} Rediscover finished with issues: updated {rediscover_done} sites, "
                f"failed {rediscover_failed}, skipped {skipped}, "
                f"applied {apply_result['prefixes_applied']} routes "
                f"(raw {apply_result['raw_prefix_rows']} rows -> {apply_result['optimized_unique_routes']} optimized) ({errors})"
            )
    except Exception:
        logger.exception("maintenance rediscover_all failed trigger=%s", trigger)
        settings_service.set_maintenance_status(
            f"{_timestamp_now()} Rediscover all failed: see container logs for details"
        )
    finally:
        db.close()
        _state.maintenance_lock.release()
    refresh_gobgp_state(f"rediscover_all:{trigger}")
