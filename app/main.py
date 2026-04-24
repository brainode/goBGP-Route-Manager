# SPDX-License-Identifier: GPL-2.0-only
from __future__ import annotations

from datetime import datetime, timezone
from concurrent.futures import Future, ThreadPoolExecutor, wait
from ipaddress import collapse_addresses, ip_address, ip_network
import json
import logging
import os
import re
from threading import Event, Lock, Thread
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from .database import Base, SessionLocal, engine, get_db
from .discovery import discover_domain, DISCOVERY_MODES, DISCOVERY_MODE_DEFAULT
from .gobgp_client import GoBGPClient
from .models import Job, JobLog, NextHop, Prefix, Setting, Site


class LoggingList(list):
    """list subclass that writes each appended message to the job_logs table immediately."""

    def __init__(self, job_id: int, db) -> None:
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

_DISCOVERY_MODE_KEY = "discovery_mode"
_MAINTENANCE_STATUS_KEY = "maintenance_status"
_IPV6_ENABLED_KEY = "ipv6_enabled"
_AUTO_REDISCOVER_ALL_KEY = "auto_rediscover_all_enabled"
_CONFIGURATION_STATUS_KEY = "configuration_status"
_STATUS_REFRESH_INTERVAL_SECONDS = max(int(os.getenv("STATUS_REFRESH_INTERVAL_SECONDS", "3600")), 0)
_STATUS_STALE_AFTER_SECONDS = max(int(os.getenv("STATUS_STALE_AFTER_SECONDS", "5400")), 60)
_REDISCOVER_QUEUE_PARALLELISM = max(int(os.getenv("REDISCOVER_QUEUE_PARALLELISM", "4")), 1)
_maintenance_lock = Lock()
_status_refresh_lock = Lock()
_status_refresh_stop = Event()
_status_refresh_thread: Thread | None = None
_auto_rediscover_lock = Lock()
_rediscover_executor_lock = Lock()
_rediscover_executor: ThreadPoolExecutor | None = None
_cancel_flags: dict[int, Event] = {}

_SITE_STATUS_PAUSED = "paused"
_SITE_STATUS_ACTIVE = "active"
_SITE_STATUS_PARTIAL = "partial"
_SITE_STATUS_MISSING = "missing"


def _get_discovery_mode(db: Session) -> str:
    row = db.query(Setting).filter(Setting.key == _DISCOVERY_MODE_KEY).first()
    if row and row.value in {k for k, _ in DISCOVERY_MODES}:
        return row.value
    return DISCOVERY_MODE_DEFAULT


def _get_setting_value(db: Session, key: str) -> Optional[str]:
    row = db.query(Setting).filter(Setting.key == key).first()
    return row.value if row else None


def _set_setting_value(db: Session, key: str, value: str) -> None:
    row = db.query(Setting).filter(Setting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))


def _get_ipv6_enabled(db: Session) -> bool:
    val = _get_setting_value(db, _IPV6_ENABLED_KEY)
    return val != "false"


def _set_maintenance_status(message: str) -> None:
    db = SessionLocal()
    try:
        _set_setting_value(db, _MAINTENANCE_STATUS_KEY, message)
        db.commit()
    finally:
        db.close()


def _get_rediscover_executor() -> ThreadPoolExecutor:
    global _rediscover_executor
    with _rediscover_executor_lock:
        if _rediscover_executor is None:
            _rediscover_executor = ThreadPoolExecutor(
                max_workers=_REDISCOVER_QUEUE_PARALLELISM,
                thread_name_prefix="rediscover",
            )
        return _rediscover_executor


def _timestamp_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _utcnow_naive() -> datetime:
    return datetime.utcnow()


def _ensure_runtime_schema() -> None:
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        prefix_columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(prefixes)").fetchall()}
        if "is_announced" not in prefix_columns:
            conn.exec_driver_sql("ALTER TABLE prefixes ADD COLUMN is_announced BOOLEAN NOT NULL DEFAULT 0")
        if "last_checked_at" not in prefix_columns:
            conn.exec_driver_sql("ALTER TABLE prefixes ADD COLUMN last_checked_at DATETIME NULL")

        site_columns = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(sites)").fetchall()}
        if "is_manual" not in site_columns:
            conn.exec_driver_sql("ALTER TABLE sites ADD COLUMN is_manual BOOLEAN NOT NULL DEFAULT 0")
        if "auto_rediscover_enabled" not in site_columns:
            conn.exec_driver_sql("ALTER TABLE sites ADD COLUMN auto_rediscover_enabled BOOLEAN NOT NULL DEFAULT 0")


def _normalize_cidr(value: str) -> str:
    return str(ip_network(value, strict=False))


def _site_type(site: Site) -> str:
    return "manual" if site.is_manual else "discovery"


def _site_type_from_input(value: Optional[str]) -> str:
    if (value or "").strip().lower() == "manual":
        return "manual"
    return "discovery"


def _site_supports_auto_rediscover(site: Site) -> bool:
    return not site.is_manual


def _get_auto_rediscover_all_enabled(db: Session) -> bool:
    return _get_setting_value(db, _AUTO_REDISCOVER_ALL_KEY) == "true"


def _set_configuration_status(message: Optional[str]) -> None:
    db = SessionLocal()
    try:
        if message:
            _set_setting_value(db, _CONFIGURATION_STATUS_KEY, message)
        else:
            row = db.query(Setting).filter(Setting.key == _CONFIGURATION_STATUS_KEY).first()
            if row:
                db.delete(row)
        db.commit()
    finally:
        db.close()


def _sync_global_auto_rediscover_setting(db: Session) -> bool:
    discovery_sites = db.query(Site).filter(Site.is_manual == False).all()  # noqa: E712
    enabled = bool(discovery_sites) and all(site.auto_rediscover_enabled for site in discovery_sites)
    _set_setting_value(db, _AUTO_REDISCOVER_ALL_KEY, "true" if enabled else "false")
    db.commit()
    return enabled


def _serialize_configuration(db: Session) -> dict[str, object]:
    next_hops = db.query(NextHop).order_by(NextHop.ip.asc()).all()
    sites = db.query(Site).options(joinedload(Site.next_hop), joinedload(Site.prefixes)).order_by(Site.domain.asc()).all()
    return {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "settings": {
            "discovery_mode": _get_discovery_mode(db),
            "ipv6_enabled": _get_ipv6_enabled(db),
            "auto_rediscover_all_enabled": _get_auto_rediscover_all_enabled(db),
        },
        "next_hops": [
            {
                "ip": hop.ip,
                "name": hop.name,
            }
            for hop in next_hops
        ],
        "sites": [
            {
                "domain": site.domain,
                "asn": site.asn,
                "enabled": site.enabled,
                "site_type": _site_type(site),
                "auto_rediscover_enabled": bool(site.auto_rediscover_enabled and _site_supports_auto_rediscover(site)),
                "next_hop_ip": site.next_hop.ip,
                "prefixes": [
                    {
                        "cidr": prefix.cidr,
                        "source": prefix.source,
                        "is_active": prefix.is_active,
                    }
                    for prefix in sorted(site.prefixes, key=lambda item: item.cidr)
                ],
            }
            for site in sites
        ],
    }


def _import_configuration(db: Session, payload: dict[str, object]) -> dict[str, int]:
    stats = {
        "next_hops_created": 0,
        "next_hops_updated": 0,
        "sites_created": 0,
        "sites_updated": 0,
        "prefixes_created": 0,
        "prefixes_updated": 0,
        "prefixes_skipped": 0,
    }

    next_hops_by_ip: dict[str, NextHop] = {}
    for item in payload.get("next_hops", []):
        if not isinstance(item, dict):
            continue
        ip = str(item.get("ip", "")).strip()
        if not ip or not _is_valid_ip(ip):
            continue
        row = db.query(NextHop).filter(NextHop.ip == ip).first()
        if row is None:
            row = NextHop(ip=ip, name=(str(item.get("name", "")).strip() or None))
            db.add(row)
            db.flush()
            stats["next_hops_created"] += 1
        else:
            new_name = str(item.get("name", "")).strip() or None
            if row.name != new_name:
                row.name = new_name
                stats["next_hops_updated"] += 1
        next_hops_by_ip[ip] = row

    for item in payload.get("sites", []):
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain", "")).strip().lower()
        next_hop_ip = str(item.get("next_hop_ip", "")).strip()
        if not domain or next_hop_ip not in next_hops_by_ip:
            continue

        site_type = _site_type_from_input(str(item.get("site_type", "")))
        is_manual = site_type == "manual"
        row = db.query(Site).options(joinedload(Site.prefixes)).filter(Site.domain == domain).first()
        created = False
        if row is None:
            row = Site(domain=domain, next_hop_id=next_hops_by_ip[next_hop_ip].id)
            db.add(row)
            db.flush()
            created = True

        row.asn = str(item.get("asn", "")).strip() or None
        row.enabled = bool(item.get("enabled", True))
        row.is_manual = is_manual
        row.auto_rediscover_enabled = bool(item.get("auto_rediscover_enabled", False)) if not is_manual else False
        row.next_hop_id = next_hops_by_ip[next_hop_ip].id

        if created:
            stats["sites_created"] += 1
        else:
            stats["sites_updated"] += 1

        existing_prefixes = {prefix.cidr: prefix for prefix in row.prefixes}
        for prefix_data in item.get("prefixes", []):
            if not isinstance(prefix_data, dict):
                continue
            cidr_raw = str(prefix_data.get("cidr", "")).strip()
            if not cidr_raw or not _is_valid_cidr(cidr_raw):
                stats["prefixes_skipped"] += 1
                continue
            cidr = _normalize_cidr(cidr_raw)
            prefix = existing_prefixes.get(cidr)
            if prefix is None:
                prefix = Prefix(
                    site_id=row.id,
                    cidr=cidr,
                    source=str(prefix_data.get("source", "manual")).strip() or "manual",
                    is_active=bool(prefix_data.get("is_active", True)),
                )
                db.add(prefix)
                existing_prefixes[cidr] = prefix
                stats["prefixes_created"] += 1
                continue

            prefix.source = str(prefix_data.get("source", prefix.source)).strip() or prefix.source
            prefix.is_active = bool(prefix_data.get("is_active", prefix.is_active))
            stats["prefixes_updated"] += 1

    settings_payload = payload.get("settings")
    if isinstance(settings_payload, dict):
        discovery_mode = str(settings_payload.get("discovery_mode", "")).strip()
        if discovery_mode in {key for key, _ in DISCOVERY_MODES}:
            _set_setting_value(db, _DISCOVERY_MODE_KEY, discovery_mode)
        ipv6_enabled = settings_payload.get("ipv6_enabled")
        if isinstance(ipv6_enabled, bool):
            _set_setting_value(db, _IPV6_ENABLED_KEY, "true" if ipv6_enabled else "false")

    _sync_global_auto_rediscover_setting(db)
    db.commit()
    return stats


def _format_checked_at(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def _is_status_stale(checked_at: Optional[datetime]) -> bool:
    if checked_at is None:
        return True
    age = (_utcnow_naive() - checked_at).total_seconds()
    return age > _STATUS_STALE_AFTER_SECONDS


def _prefix_desired_for_apply(prefix: Prefix, ipv6_enabled: bool) -> bool:
    if not prefix.is_active:
        return False
    if not ipv6_enabled and ":" in prefix.cidr:
        return False
    return True


def _site_desired_prefixes(site: Site, ipv6_enabled: bool) -> list[Prefix]:
    return [prefix for prefix in site.prefixes if _prefix_desired_for_apply(prefix, ipv6_enabled)]


def _site_status_metadata(site: Site, ipv6_enabled: bool) -> dict[str, object]:
    desired_prefixes = _site_desired_prefixes(site, ipv6_enabled)
    desired_count = len(desired_prefixes)
    announced_count = len([prefix for prefix in desired_prefixes if prefix.is_announced])
    checked_times = [prefix.last_checked_at for prefix in desired_prefixes if prefix.last_checked_at is not None]
    checked_at = min(checked_times) if checked_times else None
    stale = _is_status_stale(checked_at)

    if not site.enabled:
        status = _SITE_STATUS_PAUSED
    elif desired_count == 0:
        status = _SITE_STATUS_MISSING
    elif announced_count == desired_count:
        status = _SITE_STATUS_ACTIVE
    elif announced_count > 0:
        status = _SITE_STATUS_PARTIAL
    else:
        status = _SITE_STATUS_MISSING

    return {
        "status": status,
        "desired_prefixes_count": desired_count,
        "announced_prefixes_count": announced_count,
        "last_checked_at": checked_at,
        "last_checked_at_display": _format_checked_at(checked_at),
        "status_stale": stale,
    }


def _attach_runtime_status(sites: list[Site], ipv6_enabled: bool) -> None:
    for site in sites:
        metadata = _site_status_metadata(site, ipv6_enabled)
        setattr(site, "site_type", _site_type(site))
        setattr(site, "supports_auto_rediscover", _site_supports_auto_rediscover(site))
        setattr(site, "display_status", metadata["status"])
        setattr(site, "desired_prefixes_count", metadata["desired_prefixes_count"])
        setattr(site, "announced_prefixes_count", metadata["announced_prefixes_count"])
        setattr(site, "last_checked_at_value", metadata["last_checked_at"])
        setattr(site, "last_checked_at_display", metadata["last_checked_at_display"])
        setattr(site, "status_stale", metadata["status_stale"])

        for prefix in site.prefixes:
            prefix_checked_display = _format_checked_at(prefix.last_checked_at)
            setattr(prefix, "last_checked_at_display", prefix_checked_display)
            setattr(prefix, "announced_display", "yes" if prefix.is_announced else "no")
            setattr(prefix, "announced_stale", _is_status_stale(prefix.last_checked_at))


def _schedule_status_refresh(background_tasks: Optional[BackgroundTasks], trigger: str) -> None:
    if background_tasks is None:
        _refresh_gobgp_state(trigger)
        return
    background_tasks.add_task(_refresh_gobgp_state, trigger)


def _refresh_gobgp_state(trigger: str) -> None:
    if not _status_refresh_lock.acquire(blocking=False):
        logger.info("status refresh skipped trigger=%s reason=busy", trigger)
        return

    db = SessionLocal()
    try:
        ok, routes, message = gobgp.list_routes()
        if not ok:
            logger.warning("status refresh failed trigger=%s message=%s", trigger, message)
            return

        route_set = {_normalize_cidr(route) for route in routes}
        checked_at = _utcnow_naive()
        prefixes = db.query(Prefix).all()
        for prefix in prefixes:
            normalized = _normalize_cidr(prefix.cidr)
            prefix.is_announced = normalized in route_set
            prefix.last_checked_at = checked_at
        db.commit()
        logger.info("status refresh done trigger=%s routes=%s prefixes=%s", trigger, len(route_set), len(prefixes))
    except Exception:
        logger.exception("status refresh failed trigger=%s", trigger)
        db.rollback()
    finally:
        db.close()
        _status_refresh_lock.release()


def _run_auto_rediscover_cycle(trigger: str) -> None:
    if not _auto_rediscover_lock.acquire(blocking=False):
        logger.info("auto rediscover skipped trigger=%s reason=busy", trigger)
        return
    if not _maintenance_lock.acquire(blocking=False):
        logger.info("auto rediscover skipped trigger=%s reason=maintenance_busy", trigger)
        _auto_rediscover_lock.release()
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

        started = 0
        completed = 0
        failed = 0
        skipped = 0
        for site in sites:
            existing = (
                db.query(Job)
                .filter(Job.site_id == site.id, Job.status.in_(["pending", "running"]))
                .first()
            )
            if existing:
                skipped += 1
                logger.info("auto rediscover site skipped site_id=%s domain=%s reason=job_running", site.id, site.domain)
                continue

            job = Job(job_type="auto_rediscover_site", site_id=site.id, status="running")
            db.add(job)
            db.commit()
            db.refresh(job)
            started += 1
            debug = LoggingList(job.id, db)
            try:
                result = _rediscover_site_state(db, site, apply_changes=True, debug=debug)
                job.status = "done" if result["ok"] else "failed"
                job.finished_at = _utcnow_naive()
                db.commit()
                if result["ok"]:
                    completed += 1
                else:
                    failed += 1
            except Exception:
                logger.exception("auto rediscover site failed site_id=%s domain=%s", site.id, site.domain)
                job.status = "failed"
                job.finished_at = _utcnow_naive()
                db.commit()
                failed += 1

        _refresh_gobgp_state(f"auto_rediscover:{trigger}")
        _set_maintenance_status(
            f"{_timestamp_now()} Auto rediscover: started {started}, completed {completed}, failed {failed}, skipped {skipped}"
        )
    finally:
        db.close()
        _maintenance_lock.release()
        _auto_rediscover_lock.release()


def _submit_rediscover_site_job(site_id: int, job_id: int):
    return _get_rediscover_executor().submit(_rediscover_site_background, site_id, job_id)


def _status_refresh_worker() -> None:
    while not _status_refresh_stop.is_set():
        if _status_refresh_stop.wait(_STATUS_REFRESH_INTERVAL_SECONDS):
            break
        _refresh_gobgp_state("periodic")
        _run_auto_rediscover_cycle("periodic")


def build_optimized_route_plan(sites: list[Site], ipv6_enabled: bool) -> dict[str, object]:
    raw_prefix_rows = 0
    grouped: dict[tuple[str, int], set[object]] = {}

    for site in sites:
        for prefix in site.prefixes:
            if not _prefix_desired_for_apply(prefix, ipv6_enabled):
                continue
            raw_prefix_rows += 1
            network = ip_network(prefix.cidr, strict=False)
            grouped.setdefault((site.next_hop.ip, network.version), set()).add(network)

    plan: list[tuple[str, str]] = []
    for (next_hop, _version), networks in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
        for collapsed in collapse_addresses(sorted(networks, key=lambda net: (int(net.network_address), net.prefixlen))):
            plan.append((str(collapsed), next_hop))

    return {
        "raw_prefix_rows": raw_prefix_rows,
        "optimized_unique_routes": len(plan),
        "routes": plan,
    }

app = FastAPI(title=os.getenv("APP_NAME", "goBGP Route Manager"))
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
gobgp = GoBGPClient()
logger = logging.getLogger("uvicorn.error")


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)
    _ensure_runtime_schema()
    _refresh_gobgp_state("startup")
    _get_rediscover_executor()
    global _status_refresh_thread
    if _STATUS_REFRESH_INTERVAL_SECONDS > 0 and (_status_refresh_thread is None or not _status_refresh_thread.is_alive()):
        _status_refresh_stop.clear()
        _status_refresh_thread = Thread(target=_status_refresh_worker, name="gobgp-status-refresh", daemon=True)
        _status_refresh_thread.start()


@app.on_event("shutdown")
def shutdown() -> None:
    _status_refresh_stop.set()
    global _rediscover_executor
    with _rediscover_executor_lock:
        if _rediscover_executor is not None:
            _rediscover_executor.shutdown(wait=False, cancel_futures=False)
            _rediscover_executor = None


def _is_valid_ip(value: str) -> bool:
    try:
        ip_address(value)
        return True
    except Exception:
        return False


def _sanitize_log_message(message: str) -> str:
    message = re.sub(r"([?&]token=)[^&\\s]+", r"\1***", message, flags=re.IGNORECASE)
    message = re.sub(r"(Authorization\\s*:\\s*Bearer\\s+)[^\\s]+", r"\1***", message, flags=re.IGNORECASE)
    return message


def _is_valid_cidr(value: str) -> bool:
    try:
        ip_network(value, strict=False)
        return True
    except Exception:
        return False


def _apply_prefix(db: Session, site: Site, prefix: Prefix, announce: bool) -> bool:
    if announce:
        ok, msg = gobgp.add_route(prefix.cidr, site.next_hop.ip)
        if ok:
            logger.info("route add ok site_id=%s prefix_id=%s cidr=%s message=%s", site.id, prefix.id, prefix.cidr, msg)
        else:
            logger.error("route add error site_id=%s prefix_id=%s cidr=%s message=%s", site.id, prefix.id, prefix.cidr, msg)
    else:
        ok, msg = gobgp.del_route(prefix.cidr, site.next_hop.ip)
        if ok:
            logger.info("route del ok site_id=%s prefix_id=%s cidr=%s message=%s", site.id, prefix.id, prefix.cidr, msg)
        else:
            logger.error("route del error site_id=%s prefix_id=%s cidr=%s message=%s", site.id, prefix.id, prefix.cidr, msg)
    return ok


def _sync_site(db: Session, site: Site) -> dict[str, int | str | bool]:
    site = db.query(Site).options(joinedload(Site.next_hop), joinedload(Site.prefixes)).filter(Site.id == site.id).first()
    if not site:
        return {"ok": False, "site_id": 0, "attempted": 0, "succeeded": 0, "failed": 0}
    consecutive_failures = 0
    attempted = 0
    succeeded = 0
    failed = 0
    ipv6_enabled = _get_ipv6_enabled(db)
    for prefix in site.prefixes:
        if not prefix.is_active:
            continue
        if not ipv6_enabled and ":" in prefix.cidr:
            continue
        attempted += 1
        ok = _apply_prefix(db, site, prefix, announce=site.enabled)
        if ok:
            consecutive_failures = 0
            succeeded += 1
            continue
        consecutive_failures += 1
        failed += 1
        if consecutive_failures >= 3:
            logger.error("sync aborted site_id=%s domain=%s after %s consecutive failures", site.id, site.domain, consecutive_failures)
            break
    return {"ok": failed == 0, "site_id": site.id, "attempted": attempted, "succeeded": succeeded, "failed": failed}


def _sync_site_by_id(site_id: int) -> None:
    db = SessionLocal()
    try:
        site = db.query(Site).options(joinedload(Site.next_hop), joinedload(Site.prefixes)).filter(Site.id == site_id).first()
        if not site:
            return
        logger.info("sync start site_id=%s domain=%s enabled=%s prefixes=%s", site.id, site.domain, site.enabled, len(site.prefixes))
        result = _sync_site(db, site)
        logger.info(
            "sync done site_id=%s domain=%s attempted=%s succeeded=%s failed=%s",
            site.id,
            site.domain,
            result["attempted"],
            result["succeeded"],
            result["failed"],
        )
    except Exception:
        logger.exception("sync failed site_id=%s", site_id)
    finally:
        db.close()
    _refresh_gobgp_state(f"sync_site:{site_id}")


def _rediscover_site_background(site_id: int, job_id: int) -> None:
    cancel_event = _cancel_flags.get(job_id, Event())
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if job:
            job.status = "running"
            db.commit()

        site = db.query(Site).options(joinedload(Site.next_hop), joinedload(Site.prefixes)).filter(Site.id == site_id).first()
        if not site:
            if job:
                job.status = "failed"
                job.finished_at = datetime.utcnow()
                db.commit()
            return

        debug = LoggingList(job_id, db)
        result = _rediscover_site_state(db, site, apply_changes=True, debug=debug, cancel_event=cancel_event)

        if job:
            db.refresh(job)
            if job.status != "cancelled":
                job.status = "done" if result["ok"] else "failed"
                job.finished_at = datetime.utcnow()
                db.commit()
    except Exception as exc:
        logger.exception("rediscover_background failed site_id=%s job_id=%s", site_id, job_id)
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            if job and job.status not in ("cancelled",):
                job.status = "failed"
                job.finished_at = datetime.utcnow()
                db.commit()
        except Exception:
            pass
    finally:
        _cancel_flags.pop(job_id, None)
        db.close()
    _refresh_gobgp_state(f"rediscover_site:{site_id}")


def _queue_rediscover_site(db: Session, site: Site, source: str) -> tuple[Job, Future[object]]:
    job = Job(job_type="rediscover_site", site_id=site.id, status="pending")
    db.add(job)
    db.commit()
    db.refresh(job)
    _cancel_flags[job.id] = Event()
    debug = LoggingList(job.id, db)
    debug.append(f"[queued] rediscover scheduled source={source} site_id={site.id} domain={site.domain}")
    future = _submit_rediscover_site_job(site.id, job.id)
    return job, future


def _rediscover_site_state(
    db: Session,
    site: Site,
    apply_changes: bool = True,
    debug: Optional[list[str]] = None,
    cancel_event: Optional[Event] = None,
) -> dict[str, int | str | bool | None]:
    site = db.query(Site).options(joinedload(Site.next_hop), joinedload(Site.prefixes)).filter(Site.id == site.id).first()
    if not site:
        return {"ok": False, "site_id": 0, "added": 0, "removed": 0, "discovered": 0, "asn": None}

    debug_lines: list[str] = debug if debug is not None else []
    discovery_mode = _get_discovery_mode(db)
    debug_lines.append(f"[start] site_id={site.id} domain={site.domain} mode={discovery_mode} apply_changes={apply_changes}")

    try:
        asn, _ips, prefixes = discover_domain(site.domain, debug=debug_lines, mode=discovery_mode)
    except Exception as exc:
        logger.exception("rediscover failed site_id=%s domain=%s error=%s", site.id, site.domain, exc)
        debug_lines.append(f"[error] discovery exception: {exc}")
        return {"ok": False, "site_id": site.id, "added": 0, "removed": 0, "discovered": 0, "asn": None}

    if debug is None:
        for line in debug_lines[:40]:
            logger.info("rediscover debug site_id=%s domain=%s %s", site.id, site.domain, _sanitize_log_message(line))
        if len(debug_lines) > 40:
            logger.info("rediscover debug site_id=%s domain=%s truncated_lines=%s", site.id, site.domain, len(debug_lines) - 40)

    debug_lines.append(f"[discovery] asn={asn} prefixes_found={len(prefixes)}")

    if not _get_ipv6_enabled(db):
        ipv6_count = sum(1 for p in prefixes if ":" in p)
        if ipv6_count:
            prefixes = [p for p in prefixes if ":" not in p]
            debug_lines.append(f"[ipv6_filter] dropped {ipv6_count} IPv6 prefixes (ipv6_enabled=false)")

    if not asn and not prefixes:
        logger.warning("rediscover empty result site_id=%s domain=%s", site.id, site.domain)
        debug_lines.append("[error] empty discovery result — no ASN and no prefixes returned")
        return {"ok": False, "site_id": site.id, "added": 0, "removed": 0, "discovered": 0, "asn": None}

    existing_discovery = [p for p in site.prefixes if p.source == "discovery"]
    current = {p.cidr for p in site.prefixes}
    target = set(prefixes)

    to_remove = [p for p in existing_discovery if p.cidr not in target]
    to_add = sorted(target - current)

    debug_lines.append(f"[diff] to_add={len(to_add)} to_remove={len(to_remove)} unchanged={len(target & current)}")

    if cancel_event and cancel_event.is_set():
        debug_lines.append("[cancelled] job cancelled by user — BGP changes skipped")
        return {"ok": False, "site_id": site.id, "added": 0, "removed": 0, "discovered": len(target), "asn": asn}

    if apply_changes and site.enabled:
        for prefix in to_remove:
            if prefix.is_active:
                ok, msg = gobgp.del_route(prefix.cidr, site.next_hop.ip)
                debug_lines.append(f"[bgp withdraw] {prefix.cidr} → {'ok' if ok else 'error'}: {msg}")

    for prefix in to_remove:
        db.delete(prefix)
    db.commit()

    site.asn = asn
    db.commit()

    added = 0
    for cidr in to_add:
        if cancel_event and cancel_event.is_set():
            debug_lines.append("[cancelled] job cancelled by user — remaining BGP announces skipped")
            break
        prefix = Prefix(site_id=site.id, cidr=cidr, source="discovery")
        db.add(prefix)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            debug_lines.append(f"[db skip] {cidr} already exists")
            continue
        db.refresh(prefix)
        added += 1
        if apply_changes and site.enabled:
            ok, msg = gobgp.add_route(prefix.cidr, site.next_hop.ip)
            debug_lines.append(f"[bgp announce] {prefix.cidr} → {'ok' if ok else 'error'}: {msg}")

    debug_lines.append(f"[done] added={added} removed={len(to_remove)} total_prefixes={len(target)}")
    logger.info(
        "rediscover done site_id=%s domain=%s asn=%s prefixes_total=%s added=%s removed=%s apply_changes=%s",
        site.id, site.domain, asn, len(target), added, len(to_remove), apply_changes,
    )
    return {
        "ok": True,
        "site_id": site.id,
        "added": added,
        "removed": len(to_remove),
        "discovered": len(target),
        "asn": asn,
    }


def _apply_current_state(db: Session) -> dict[str, int | bool | list[str]]:
    purge_result = gobgp.purge_routes()
    enabled_sites = (
        db.query(Site)
        .options(joinedload(Site.next_hop), joinedload(Site.prefixes))
        .filter(Site.enabled == True)  # noqa: E712
        .order_by(Site.domain.asc())
        .all()
    )

    ipv6_enabled = _get_ipv6_enabled(db)
    route_plan = build_optimized_route_plan(enabled_sites, ipv6_enabled)
    attempted = 0
    succeeded = 0
    failed = 0
    errors = list(purge_result.get("errors", []))
    for cidr, next_hop in route_plan["routes"]:
        attempted += 1
        ok, message = gobgp.add_route(cidr, next_hop)
        if ok:
            succeeded += 1
            logger.info("route add ok apply_current cidr=%s next_hop=%s message=%s", cidr, next_hop, message)
        else:
            failed += 1
            errors.append(f"{cidr} via {next_hop}: {message}")
            logger.error("route add error apply_current cidr=%s next_hop=%s message=%s", cidr, next_hop, message)

    if failed > 0:
        errors.append(f"apply_failed prefixes={failed}")

    return {
        "ok": bool(purge_result.get("ok", False)) and failed == 0,
        "routes_found": int(purge_result.get("routes_found", 0)),
        "routes_removed": int(purge_result.get("routes_removed", 0)),
        "sites": len(enabled_sites),
        "raw_prefix_rows": int(route_plan["raw_prefix_rows"]),
        "optimized_unique_routes": int(route_plan["optimized_unique_routes"]),
        "prefixes_attempted": attempted,
        "prefixes_applied": succeeded,
        "prefixes_failed": failed,
        "errors": errors,
    }


def _run_apply_current_state_job(trigger: str) -> None:
    if not _maintenance_lock.acquire(blocking=False):
        logger.warning("maintenance skipped trigger=%s reason=busy", trigger)
        _set_maintenance_status(f"{_timestamp_now()} Busy: another maintenance task is already running")
        return

    _set_maintenance_status(f"{_timestamp_now()} Running: apply current state")
    db = SessionLocal()
    try:
        result = _apply_current_state(db)
        logger.info("maintenance apply_current trigger=%s result=%s", trigger, result)
        if result["ok"]:
            _set_maintenance_status(
                f"{_timestamp_now()} Apply complete: removed {result['routes_removed']}/{result['routes_found']} existing routes, "
                f"raw {result['raw_prefix_rows']} rows -> {result['optimized_unique_routes']} optimized routes, "
                f"applied {result['prefixes_applied']} routes across {result['sites']} enabled sites"
            )
        else:
            errors = ", ".join(result["errors"][:3]) if result["errors"] else "unknown error"
            _set_maintenance_status(
                f"{_timestamp_now()} Apply finished with errors: removed {result['routes_removed']}/{result['routes_found']} existing routes, "
                f"raw {result['raw_prefix_rows']} rows -> {result['optimized_unique_routes']} optimized routes, "
                f"applied {result['prefixes_applied']} routes, failed {result['prefixes_failed']} ({errors})"
            )
    except Exception:
        logger.exception("maintenance apply_current failed trigger=%s", trigger)
        _set_maintenance_status(f"{_timestamp_now()} Apply failed: see container logs for details")
    finally:
        db.close()
        _maintenance_lock.release()
    _refresh_gobgp_state(f"apply_current:{trigger}")


def _run_rediscover_all_and_apply_job(trigger: str) -> None:
    if not _maintenance_lock.acquire(blocking=False):
        logger.warning("maintenance skipped trigger=%s reason=busy", trigger)
        _set_maintenance_status(f"{_timestamp_now()} Busy: another maintenance task is already running")
        return

    _set_maintenance_status(
        f"{_timestamp_now()} Running: rediscover all sites via queue (parallel {_REDISCOVER_QUEUE_PARALLELISM})"
    )
    db = SessionLocal()
    try:
        sites = db.query(Site).options(joinedload(Site.next_hop), joinedload(Site.prefixes)).order_by(Site.domain.asc()).all()
        if not sites:
            _set_maintenance_status(f"{_timestamp_now()} Rediscover all skipped: no sites found")
            return

        queued = 0
        skipped = 0
        futures = []
        job_ids: list[int] = []
        for site in sites:
            existing = (
                db.query(Job)
                .filter(Job.site_id == site.id, Job.status.in_(["pending", "running"]))
                .first()
            )
            if existing:
                skipped += 1
                logger.info(
                    "maintenance rediscover_all skipped site_id=%s domain=%s reason=job_running active_job_id=%s",
                    site.id,
                    site.domain,
                    existing.id,
                )
                continue

            job, future = _queue_rediscover_site(db, site, source=f"settings:{trigger}")
            queued += 1
            job_ids.append(job.id)
            futures.append(future)

        _set_maintenance_status(
            f"{_timestamp_now()} Rediscover queued: {queued} sites at parallel {_REDISCOVER_QUEUE_PARALLELISM}"
            + (f", skipped {skipped} active jobs" if skipped else "")
        )

        if futures:
            wait(futures)

        apply_result = _apply_current_state(db)
        db.expire_all()
        jobs = db.query(Job).filter(Job.id.in_(job_ids)).all() if job_ids else []
        rediscover_done = sum(1 for job in jobs if job.status == "done")
        rediscover_failed = sum(1 for job in jobs if job.status == "failed")
        logger.info(
            "maintenance rediscover_all trigger=%s queued=%s done=%s failed=%s skipped=%s apply=%s",
            trigger,
            queued,
            rediscover_done,
            rediscover_failed,
            skipped,
            apply_result,
        )
        if apply_result["ok"] and rediscover_failed == 0:
            _set_maintenance_status(
                f"{_timestamp_now()} Rediscover complete: updated {rediscover_done} sites, skipped {skipped}, "
                f"then applied {apply_result['prefixes_applied']} routes "
                f"(raw {apply_result['raw_prefix_rows']} rows -> {apply_result['optimized_unique_routes']} optimized)"
            )
        else:
            errors = ", ".join(apply_result["errors"][:3]) if apply_result["errors"] else "rediscover/apply partial failure"
            _set_maintenance_status(
                f"{_timestamp_now()} Rediscover finished with issues: updated {rediscover_done} sites, failed {rediscover_failed}, "
                f"skipped {skipped}, applied {apply_result['prefixes_applied']} routes "
                f"(raw {apply_result['raw_prefix_rows']} rows -> {apply_result['optimized_unique_routes']} optimized) ({errors})"
            )
    except Exception:
        logger.exception("maintenance rediscover_all failed trigger=%s", trigger)
        _set_maintenance_status(f"{_timestamp_now()} Rediscover all failed: see container logs for details")
    finally:
        db.close()
        _maintenance_lock.release()
    _refresh_gobgp_state(f"rediscover_all:{trigger}")


@app.get("/", response_class=HTMLResponse)
def root() -> RedirectResponse:
    return RedirectResponse(url="/sites", status_code=303)


@app.get("/gobgp-status", response_class=HTMLResponse)
def gobgp_status(request: Request) -> HTMLResponse:
    status = gobgp.status()
    return templates.TemplateResponse("gobgp_status.html", {"request": request, "status": status, "title": "GoBGP Status"})


@app.get("/sites", response_class=HTMLResponse)
def list_sites(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    sites = db.query(Site).options(joinedload(Site.next_hop), joinedload(Site.prefixes)).order_by(Site.domain.asc()).all()
    _attach_runtime_status(sites, _get_ipv6_enabled(db))
    next_hops = db.query(NextHop).order_by(NextHop.ip.asc()).all()
    active_jobs = (
        db.query(Job)
        .filter(Job.status.in_(["pending", "running"]), Job.site_id.isnot(None))
        .all()
    )
    active_job_by_site = {j.site_id: j.id for j in active_jobs}
    return templates.TemplateResponse(
        "sites.html",
        {
            "request": request,
            "sites": sites,
            "next_hops": next_hops,
            "active_job_by_site": active_job_by_site,
            "title": "Sites",
        },
    )


@app.post("/sites")
def create_site(
    background_tasks: BackgroundTasks,
    domain: str = Form(...),
    next_hop_id: int = Form(...),
    site_type: str = Form("discovery"),
    enabled: Optional[str] = Form(None),
    discover: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    domain = domain.strip().lower()
    next_hop = db.query(NextHop).filter(NextHop.id == next_hop_id).first()
    if not next_hop:
        raise HTTPException(status_code=400, detail="next hop not found")

    normalized_site_type = _site_type_from_input(site_type)
    is_manual = normalized_site_type == "manual"
    auto_rediscover_enabled = _get_auto_rediscover_all_enabled(db) if not is_manual else False
    site = Site(
        domain=domain,
        next_hop_id=next_hop_id,
        enabled=enabled == "on",
        is_manual=is_manual,
        auto_rediscover_enabled=auto_rediscover_enabled,
    )

    db.add(site)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="site already exists")
    db.refresh(site)
    _sync_global_auto_rediscover_setting(db)

    if not is_manual and discover == "on":
        job = Job(job_type="rediscover_site", site_id=site.id, status="pending")
        db.add(job)
        db.commit()
        db.refresh(job)
        _cancel_flags[job.id] = Event()
        background_tasks.add_task(_rediscover_site_background, site.id, job.id)
    elif site.enabled:
        background_tasks.add_task(_sync_site_by_id, site.id)

    return RedirectResponse(url="/sites", status_code=303)


@app.post("/sites/{site_id}/toggle")
def toggle_site(site_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    site = db.query(Site).options(joinedload(Site.next_hop), joinedload(Site.prefixes)).filter(Site.id == site_id).first()
    if not site:
        raise HTTPException(status_code=404, detail="site not found")
    site.enabled = not site.enabled
    db.commit()
    background_tasks.add_task(_sync_site_by_id, site.id)
    logger.info("sync scheduled site_id=%s domain=%s after toggle", site.id, site.domain)
    return RedirectResponse(url="/sites", status_code=303)


@app.post("/sites/{site_id}/rediscover")
def rediscover_site(site_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        raise HTTPException(status_code=404, detail="site not found")
    existing = (
        db.query(Job)
        .filter(Job.site_id == site_id, Job.status.in_(["pending", "running"]))
        .first()
    )
    if existing:
        return JSONResponse({"job_id": existing.id, "already_running": True})
    job = Job(job_type="rediscover_site", site_id=site_id, status="pending")
    db.add(job)
    db.commit()
    db.refresh(job)
    _cancel_flags[job.id] = Event()
    debug = LoggingList(job.id, db)
    debug.append(f"[queued] rediscover scheduled source=manual site_id={site.id} domain={site.domain}")
    _submit_rediscover_site_job(site_id, job.id)
    return JSONResponse({"job_id": job.id, "already_running": False})


@app.post("/sites/{site_id}/auto-rediscover")
def toggle_site_auto_rediscover(
    site_id: int,
    request: Request,
    enabled: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        raise HTTPException(status_code=404, detail="site not found")
    if site.is_manual:
        raise HTTPException(status_code=400, detail="auto rediscover is unavailable for manual sites")

    site.auto_rediscover_enabled = enabled == "on"
    db.commit()
    _sync_global_auto_rediscover_setting(db)
    return RedirectResponse(url=request.headers.get("referer") or "/sites", status_code=303)


@app.get("/jobs/{job_id}")
def get_job_status(job_id: int, after: int = 0, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    logs_q = db.query(JobLog).filter(JobLog.job_id == job_id)
    if after:
        logs_q = logs_q.filter(JobLog.id > after)
    logs = logs_q.order_by(JobLog.id.asc()).all()
    return JSONResponse({
        "id": job.id,
        "status": job.status,
        "site_id": job.site_id,
        "created_at": job.created_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "logs": [{"id": l.id, "message": l.message} for l in logs],
    })


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status not in ("pending", "running"):
        return JSONResponse({"ok": False, "reason": "job not cancellable"})
    job.status = "cancelled"
    job.finished_at = datetime.utcnow()
    db.commit()
    event = _cancel_flags.get(job_id)
    if event:
        event.set()
    return JSONResponse({"ok": True})


@app.get("/logs", response_class=HTMLResponse)
def logs_list(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    jobs = db.query(Job).options(joinedload(Job.site)).order_by(Job.id.desc()).limit(100).all()
    return templates.TemplateResponse("logs.html", {"request": request, "jobs": jobs, "title": "Logs"})


@app.get("/logs/{job_id}/download")
def log_download(job_id: int, db: Session = Depends(get_db)):
    from fastapi.responses import PlainTextResponse
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    logs = db.query(JobLog).filter(JobLog.job_id == job_id).order_by(JobLog.id.asc()).all()
    site = db.query(Site).filter(Site.id == job.site_id).first() if job.site_id else None
    header = (
        f"Job #{job.id} | type={job.job_type} | status={job.status}\n"
        f"Site: {site.domain if site else '—'}\n"
        f"Started:  {job.created_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Finished: {job.finished_at.strftime('%Y-%m-%d %H:%M:%S') if job.finished_at else '—'}\n"
        f"{'─' * 60}\n"
    )
    body = "\n".join(l.message for l in logs)
    filename = f"job-{job_id}.log"
    return PlainTextResponse(
        content=header + body,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/logs/{job_id}", response_class=HTMLResponse)
def log_detail(job_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    site = db.query(Site).filter(Site.id == job.site_id).first() if job.site_id else None
    logs = db.query(JobLog).filter(JobLog.job_id == job_id).order_by(JobLog.id.asc()).all()
    return templates.TemplateResponse("logs_detail.html", {
        "request": request,
        "job": job,
        "site": site,
        "logs": logs,
        "title": f"Job #{job_id}",
    })


@app.post("/sites/{site_id}/delete")
def delete_site(site_id: int, db: Session = Depends(get_db)):
    site = db.query(Site).options(joinedload(Site.next_hop), joinedload(Site.prefixes)).filter(Site.id == site_id).first()
    if not site:
        raise HTTPException(status_code=404, detail="site not found")

    for prefix in site.prefixes:
        if prefix.is_active:
            _apply_prefix(db, site, prefix, announce=False)

    db.delete(site)
    db.commit()
    _sync_global_auto_rediscover_setting(db)
    return RedirectResponse(url="/sites", status_code=303)


@app.get("/sites/{site_id}", response_class=HTMLResponse)
def site_detail(site_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    site = db.query(Site).options(joinedload(Site.next_hop), joinedload(Site.prefixes)).filter(Site.id == site_id).first()
    if not site:
        raise HTTPException(status_code=404, detail="site not found")
    _attach_runtime_status([site], _get_ipv6_enabled(db))
    return templates.TemplateResponse("site_detail.html", {"request": request, "site": site, "title": f"Site {site.domain}"})


@app.post("/sites/{site_id}/prefixes")
def add_prefix(
    site_id: int,
    background_tasks: BackgroundTasks,
    cidr: str = Form(...),
    db: Session = Depends(get_db),
):
    site = db.query(Site).options(joinedload(Site.next_hop)).filter(Site.id == site_id).first()
    if not site:
        raise HTTPException(status_code=404, detail="site not found")

    cidr = cidr.strip()
    if not _is_valid_cidr(cidr):
        raise HTTPException(status_code=400, detail="invalid cidr")
    cidr = _normalize_cidr(cidr)

    prefix = Prefix(site_id=site.id, cidr=cidr, source="manual")
    db.add(prefix)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="prefix already exists")
    db.refresh(prefix)

    if site.enabled:
        _apply_prefix(db, site, prefix, announce=True)
    _schedule_status_refresh(background_tasks, f"add_prefix:{prefix.id}")

    return RedirectResponse(url=f"/sites/{site_id}", status_code=303)


@app.post("/prefixes/{prefix_id}/delete")
def delete_prefix(prefix_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    prefix = db.query(Prefix).options(joinedload(Prefix.site).joinedload(Site.next_hop)).filter(Prefix.id == prefix_id).first()
    if not prefix:
        raise HTTPException(status_code=404, detail="prefix not found")

    site = prefix.site
    if site.enabled and prefix.is_active:
        _apply_prefix(db, site, prefix, announce=False)

    site_id = site.id
    db.delete(prefix)
    db.commit()
    _schedule_status_refresh(background_tasks, f"delete_prefix:{prefix_id}")
    return RedirectResponse(url=f"/sites/{site_id}", status_code=303)


@app.get("/next-hops", response_class=HTMLResponse)
def list_next_hops(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    next_hops = db.query(NextHop).order_by(NextHop.ip.asc()).all()
    return templates.TemplateResponse("next_hops.html", {"request": request, "next_hops": next_hops, "title": "Next Hops"})


@app.post("/next-hops")
def create_next_hop(ip: str = Form(...), name: str = Form(""), db: Session = Depends(get_db)):
    ip = ip.strip()
    if not _is_valid_ip(ip):
        raise HTTPException(status_code=400, detail="invalid ip")

    row = NextHop(ip=ip, name=name.strip() or None)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="next hop already exists")
    return RedirectResponse(url="/next-hops", status_code=303)


@app.post("/next-hops/{next_hop_id}/delete")
def delete_next_hop(next_hop_id: int, db: Session = Depends(get_db)):
    row = db.query(NextHop).filter(NextHop.id == next_hop_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="next hop not found")

    sites_count = db.query(func.count(Site.id)).filter(Site.next_hop_id == next_hop_id).scalar() or 0
    if sites_count > 0:
        raise HTTPException(status_code=400, detail="next hop in use")

    db.delete(row)
    db.commit()
    return RedirectResponse(url="/next-hops", status_code=303)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    sites_count = db.query(func.count(Site.id)).scalar() or 0
    prefixes_count = db.query(func.count(Prefix.id)).scalar() or 0
    next_hops_count = db.query(func.count(NextHop.id)).scalar() or 0
    enabled_sites_count = db.query(func.count(Site.id)).filter(Site.enabled == True).scalar() or 0  # noqa: E712
    auto_rediscover_sites_count = (
        db.query(func.count(Site.id))
        .filter(Site.is_manual == False, Site.auto_rediscover_enabled == True)  # noqa: E712
        .scalar()
        or 0
    )
    active_prefixes_count = (
        db.query(func.count(Prefix.id))
        .join(Site, Prefix.site_id == Site.id)
        .filter(Prefix.is_active == True, Site.enabled == True)  # noqa: E712
        .scalar()
        or 0
    )
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "title": "Settings",
            "discovery_mode": _get_discovery_mode(db),
            "discovery_modes": DISCOVERY_MODES,
            "sites_count": sites_count,
            "prefixes_count": prefixes_count,
            "next_hops_count": next_hops_count,
            "enabled_sites_count": enabled_sites_count,
            "active_prefixes_count": active_prefixes_count,
            "maintenance_status": _get_setting_value(db, _MAINTENANCE_STATUS_KEY),
            "ipv6_enabled": _get_ipv6_enabled(db),
            "auto_rediscover_all_enabled": _get_auto_rediscover_all_enabled(db),
            "auto_rediscover_sites_count": auto_rediscover_sites_count,
            "configuration_status": _get_setting_value(db, _CONFIGURATION_STATUS_KEY),
        },
    )


@app.post("/settings/discovery-mode")
def set_discovery_mode(mode: str = Form(...), db: Session = Depends(get_db)):
    valid_keys = {k for k, _ in DISCOVERY_MODES}
    if mode not in valid_keys:
        raise HTTPException(status_code=400, detail="invalid mode")
    row = db.query(Setting).filter(Setting.key == _DISCOVERY_MODE_KEY).first()
    if row:
        row.value = mode
    else:
        db.add(Setting(key=_DISCOVERY_MODE_KEY, value=mode))
    db.commit()
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/settings/ipv6-enabled")
def set_ipv6_enabled(enabled: Optional[str] = Form(None), db: Session = Depends(get_db)):
    _set_setting_value(db, _IPV6_ENABLED_KEY, "true" if enabled == "on" else "false")
    db.commit()
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/settings/auto-rediscover-all")
def set_auto_rediscover_all(enabled: Optional[str] = Form(None), db: Session = Depends(get_db)):
    new_value = enabled == "on"
    discovery_sites = db.query(Site).filter(Site.is_manual == False).all()  # noqa: E712
    for site in discovery_sites:
        site.auto_rediscover_enabled = new_value
    _set_setting_value(db, _AUTO_REDISCOVER_ALL_KEY, "true" if new_value and discovery_sites else "false")
    db.commit()
    return RedirectResponse(url="/settings", status_code=303)


@app.get("/settings/export")
def export_configuration(db: Session = Depends(get_db)):
    payload = _serialize_configuration(db)
    body = json.dumps(payload, indent=2, sort_keys=True)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="route-manager-config.json"'},
    )


@app.post("/settings/import")
async def import_configuration(
    background_tasks: BackgroundTasks,
    config_file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    try:
        raw = await config_file.read()
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid json import: {exc}") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid json import: expected object")

    stats = _import_configuration(db, payload)
    _set_configuration_status(
        f"{_timestamp_now()} Import complete: next-hops +{stats['next_hops_created']}/~{stats['next_hops_updated']}, "
        f"sites +{stats['sites_created']}/~{stats['sites_updated']}, "
        f"prefixes +{stats['prefixes_created']}/~{stats['prefixes_updated']}, skipped {stats['prefixes_skipped']}"
    )
    _schedule_status_refresh(background_tasks, "import_configuration")
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/settings/purge-inactive")
def purge_inactive(db: Session = Depends(get_db)):
    inactive = db.query(Prefix).filter(Prefix.is_active == False).all()  # noqa: E712
    for p in inactive:
        db.delete(p)
    db.commit()
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/settings/apply-current")
def apply_current_state(background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_apply_current_state_job, "settings")
    return RedirectResponse(url="/settings", status_code=303)


@app.post("/settings/rediscover-all")
def rediscover_all_sites(background_tasks: BackgroundTasks):
    background_tasks.add_task(_run_rediscover_all_and_apply_job, "settings")
    return RedirectResponse(url="/settings", status_code=303)


@app.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("select 1"))
    return JSONResponse({"status": "ok"})


@app.get("/api/sites")
def api_sites(db: Session = Depends(get_db)):
    rows = db.query(Site).options(joinedload(Site.next_hop), joinedload(Site.prefixes)).all()
    ipv6_enabled = _get_ipv6_enabled(db)
    _attach_runtime_status(rows, ipv6_enabled)
    data = []
    for row in rows:
        data.append(
            {
                "id": row.id,
                "domain": row.domain,
                "asn": row.asn,
                "enabled": row.enabled,
                "next_hop": row.next_hop.ip,
                "prefixes_count": len([p for p in row.prefixes if p.is_active]),
                "site_type": row.site_type,
                "is_manual": row.is_manual,
                "auto_rediscover_enabled": bool(row.auto_rediscover_enabled and not row.is_manual),
                "status": row.display_status,
                "announced_prefixes_count": row.announced_prefixes_count,
                "desired_prefixes_count": row.desired_prefixes_count,
                "last_checked_at": row.last_checked_at_value.isoformat() if row.last_checked_at_value else None,
                "status_stale": row.status_stale,
            }
        )
    return JSONResponse(data)

