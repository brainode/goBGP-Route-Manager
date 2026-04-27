# SPDX-License-Identifier: GPL-2.0-only
from __future__ import annotations

import logging
import re
from datetime import datetime
from threading import Event
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from .. import state as _state
from ..database import SessionLocal
from ..discovery import discover_domain
from ..models import Job, Prefix, Site
from . import route_service, settings_service
from .job_service import LoggingList, create_job, has_active_job

logger = logging.getLogger("uvicorn.error")


def _sanitize_log_message(message: str) -> str:
    message = re.sub(r"([?&]token=)[^&\s]+", r"\1***", message, flags=re.IGNORECASE)
    message = re.sub(r"(Authorization\s*:\s*Bearer\s+)\S+", r"\1***", message, flags=re.IGNORECASE)
    return message


def submit_rediscover_site_job(site_id: int, job_id: int):
    return _state.get_rediscover_executor().submit(_rediscover_site_background, site_id, job_id)


def queue_rediscover_site(db: Session, site: Site, source: str) -> tuple[Job, object]:
    job = create_job(db, "rediscover_site", site_id=site.id)
    _state.cancel_flags[job.id] = Event()
    debug = LoggingList(job.id, db)
    debug.append(f"[queued] rediscover scheduled source={source} site_id={site.id} domain={site.domain}")
    future = submit_rediscover_site_job(site.id, job.id)
    return job, future


def _rediscover_site_background(site_id: int, job_id: int) -> None:
    cancel_event = _state.cancel_flags.get(job_id, Event())
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if job:
            job.status = "running"
            db.commit()

        site = (
            db.query(Site)
            .options(joinedload(Site.next_hop), joinedload(Site.prefixes))
            .filter(Site.id == site_id)
            .first()
        )
        if not site:
            if job:
                job.status = "failed"
                job.finished_at = datetime.utcnow()
                db.commit()
            return

        debug = LoggingList(job_id, db)
        result = rediscover_site_state(db, site, apply_changes=True, debug=debug, cancel_event=cancel_event)

        if job:
            db.refresh(job)
            if job.status != "cancelled":
                job.status = "done" if result["ok"] else "failed"
                job.finished_at = datetime.utcnow()
                db.commit()
    except Exception:
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
        _state.cancel_flags.pop(job_id, None)
        db.close()

    from . import status_service
    status_service.refresh_gobgp_state(f"rediscover_site:{site_id}")


def rediscover_site_state(
    db: Session,
    site: Site,
    apply_changes: bool = True,
    debug: Optional[list[str]] = None,
    cancel_event: Optional[Event] = None,
) -> dict[str, int | str | bool | None]:
    site = (
        db.query(Site)
        .options(joinedload(Site.next_hop), joinedload(Site.prefixes))
        .filter(Site.id == site.id)
        .first()
    )
    if not site:
        return {"ok": False, "site_id": 0, "added": 0, "removed": 0, "discovered": 0, "asn": None}

    debug_lines: list[str] = debug if debug is not None else []
    discovery_mode = settings_service.get_discovery_mode(db)
    debug_lines.append(
        f"[start] site_id={site.id} domain={site.domain} mode={discovery_mode} apply_changes={apply_changes}"
    )

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
            logger.info(
                "rediscover debug site_id=%s domain=%s truncated_lines=%s",
                site.id, site.domain, len(debug_lines) - 40,
            )

    debug_lines.append(f"[discovery] asn={asn} prefixes_found={len(prefixes)}")

    if not settings_service.get_ipv6_enabled(db):
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
                ok, msg = _state.gobgp.del_route(prefix.cidr, site.next_hop.ip)
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
            ok, msg = _state.gobgp.add_route(prefix.cidr, site.next_hop.ip)
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
