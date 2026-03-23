# SPDX-License-Identifier: GPL-2.0-only
from __future__ import annotations

from datetime import datetime, timezone
from ipaddress import ip_address, ip_network
import logging
import os
import re
from threading import Event, Lock
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
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
_maintenance_lock = Lock()
_cancel_flags: dict[int, Event] = {}


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


def _timestamp_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")

app = FastAPI(title=os.getenv("APP_NAME", "goBGP Route Manager"))
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
gobgp = GoBGPClient()
logger = logging.getLogger("uvicorn.error")


@app.on_event("startup")
def startup() -> None:
    Base.metadata.create_all(bind=engine)


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

    attempted = 0
    succeeded = 0
    failed = 0
    for site in enabled_sites:
        result = _sync_site(db, site)
        attempted += int(result["attempted"])
        succeeded += int(result["succeeded"])
        failed += int(result["failed"])

    errors = list(purge_result.get("errors", []))
    if failed > 0:
        errors.append(f"apply_failed prefixes={failed}")

    return {
        "ok": bool(purge_result.get("ok", False)) and failed == 0,
        "routes_found": int(purge_result.get("routes_found", 0)),
        "routes_removed": int(purge_result.get("routes_removed", 0)),
        "sites": len(enabled_sites),
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
                f"applied {result['prefixes_applied']} prefixes across {result['sites']} enabled sites"
            )
        else:
            errors = ", ".join(result["errors"][:3]) if result["errors"] else "unknown error"
            _set_maintenance_status(
                f"{_timestamp_now()} Apply finished with errors: removed {result['routes_removed']}/{result['routes_found']} existing routes, "
                f"applied {result['prefixes_applied']} prefixes, failed {result['prefixes_failed']} ({errors})"
            )
    except Exception:
        logger.exception("maintenance apply_current failed trigger=%s", trigger)
        _set_maintenance_status(f"{_timestamp_now()} Apply failed: see container logs for details")
    finally:
        db.close()
        _maintenance_lock.release()


def _run_rediscover_all_and_apply_job(trigger: str) -> None:
    if not _maintenance_lock.acquire(blocking=False):
        logger.warning("maintenance skipped trigger=%s reason=busy", trigger)
        _set_maintenance_status(f"{_timestamp_now()} Busy: another maintenance task is already running")
        return

    _set_maintenance_status(f"{_timestamp_now()} Running: rediscover all sites and apply current state")
    db = SessionLocal()
    try:
        sites = db.query(Site).options(joinedload(Site.next_hop), joinedload(Site.prefixes)).order_by(Site.domain.asc()).all()
        rediscover_ok = 0
        rediscover_failed = 0
        added = 0
        removed = 0
        for site in sites:
            result = _rediscover_site_state(db, site, apply_changes=False)
            if result["ok"]:
                rediscover_ok += 1
                added += int(result["added"])
                removed += int(result["removed"])
            else:
                rediscover_failed += 1

        apply_result = _apply_current_state(db)
        logger.info(
            "maintenance rediscover_all trigger=%s rediscover_ok=%s rediscover_failed=%s added=%s removed=%s apply=%s",
            trigger,
            rediscover_ok,
            rediscover_failed,
            added,
            removed,
            apply_result,
        )
        if apply_result["ok"] and rediscover_failed == 0:
            _set_maintenance_status(
                f"{_timestamp_now()} Rediscover complete: updated {rediscover_ok} sites, added {added}, removed {removed}, "
                f"then applied {apply_result['prefixes_applied']} prefixes"
            )
        else:
            errors = ", ".join(apply_result["errors"][:3]) if apply_result["errors"] else "rediscover/apply partial failure"
            _set_maintenance_status(
                f"{_timestamp_now()} Rediscover finished with issues: updated {rediscover_ok} sites, failed {rediscover_failed}, "
                f"added {added}, removed {removed}, applied {apply_result['prefixes_applied']} prefixes ({errors})"
            )
    except Exception:
        logger.exception("maintenance rediscover_all failed trigger=%s", trigger)
        _set_maintenance_status(f"{_timestamp_now()} Rediscover all failed: see container logs for details")
    finally:
        db.close()
        _maintenance_lock.release()


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
    enabled: Optional[str] = Form(None),
    discover: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    domain = domain.strip().lower()
    next_hop = db.query(NextHop).filter(NextHop.id == next_hop_id).first()
    if not next_hop:
        raise HTTPException(status_code=400, detail="next hop not found")

    site = Site(domain=domain, next_hop_id=next_hop_id, enabled=enabled == "on")

    db.add(site)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="site already exists")
    db.refresh(site)

    if discover == "on":
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
    background_tasks.add_task(_rediscover_site_background, site_id, job.id)
    return JSONResponse({"job_id": job.id, "already_running": False})


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
    jobs = db.query(Job).order_by(Job.id.desc()).limit(100).all()
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
    return RedirectResponse(url="/sites", status_code=303)


@app.get("/sites/{site_id}", response_class=HTMLResponse)
def site_detail(site_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    site = db.query(Site).options(joinedload(Site.next_hop), joinedload(Site.prefixes)).filter(Site.id == site_id).first()
    if not site:
        raise HTTPException(status_code=404, detail="site not found")
    return templates.TemplateResponse("site_detail.html", {"request": request, "site": site, "title": f"Site {site.domain}"})


@app.post("/sites/{site_id}/prefixes")
def add_prefix(site_id: int, cidr: str = Form(...), db: Session = Depends(get_db)):
    site = db.query(Site).options(joinedload(Site.next_hop)).filter(Site.id == site_id).first()
    if not site:
        raise HTTPException(status_code=404, detail="site not found")

    cidr = cidr.strip()
    if not _is_valid_cidr(cidr):
        raise HTTPException(status_code=400, detail="invalid cidr")

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

    return RedirectResponse(url=f"/sites/{site_id}", status_code=303)


@app.post("/prefixes/{prefix_id}/delete")
def delete_prefix(prefix_id: int, db: Session = Depends(get_db)):
    prefix = db.query(Prefix).options(joinedload(Prefix.site).joinedload(Site.next_hop)).filter(Prefix.id == prefix_id).first()
    if not prefix:
        raise HTTPException(status_code=404, detail="prefix not found")

    site = prefix.site
    if site.enabled and prefix.is_active:
        _apply_prefix(db, site, prefix, announce=False)

    site_id = site.id
    db.delete(prefix)
    db.commit()
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
            }
        )
    return JSONResponse(data)

