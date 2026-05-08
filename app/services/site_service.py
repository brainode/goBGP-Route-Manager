# SPDX-License-Identifier: GPL-2.0-only
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session, joinedload

from ..config import (
    SITE_STATUS_ACTIVE,
    SITE_STATUS_MISSING,
    SITE_STATUS_PARTIAL,
    SITE_STATUS_PAUSED,
    STATUS_STALE_AFTER_SECONDS,
)
from ..database import SessionLocal
from ..models import Job, NextHop, Prefix, Site
from . import route_service, settings_service
from .. import state as _state

logger = logging.getLogger("uvicorn.error")


def site_desired_prefixes(site: Site, ipv6_enabled: bool) -> list[Prefix]:
    return [p for p in site.prefixes if route_service.prefix_desired_for_apply(p, ipv6_enabled)]


def format_checked_at(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def is_status_stale(checked_at: Optional[datetime]) -> bool:
    if checked_at is None:
        return True
    age = (datetime.utcnow() - checked_at).total_seconds()
    return age > STATUS_STALE_AFTER_SECONDS


def site_status_metadata(site: Site, ipv6_enabled: bool) -> dict[str, object]:
    desired = site_desired_prefixes(site, ipv6_enabled)
    desired_count = len(desired)
    announced_count = sum(1 for p in desired if p.is_announced)
    checked_times = [p.last_checked_at for p in desired if p.last_checked_at is not None]
    checked_at = min(checked_times) if checked_times else None
    stale = is_status_stale(checked_at)

    if not site.enabled:
        status = SITE_STATUS_PAUSED
    elif desired_count == 0:
        status = SITE_STATUS_MISSING
    elif announced_count == desired_count:
        status = SITE_STATUS_ACTIVE
    elif announced_count > 0:
        status = SITE_STATUS_PARTIAL
    else:
        status = SITE_STATUS_MISSING

    return {
        "status": status,
        "desired_prefixes_count": desired_count,
        "announced_prefixes_count": announced_count,
        "last_checked_at": checked_at,
        "last_checked_at_display": format_checked_at(checked_at),
        "status_stale": stale,
    }


def attach_runtime_status(sites: list[Site], ipv6_enabled: bool) -> None:
    for site in sites:
        meta = site_status_metadata(site, ipv6_enabled)
        setattr(site, "site_type", "manual" if site.is_manual else "discovery")
        setattr(site, "supports_auto_rediscover", not site.is_manual)
        setattr(site, "display_status", meta["status"])
        setattr(site, "desired_prefixes_count", meta["desired_prefixes_count"])
        setattr(site, "announced_prefixes_count", meta["announced_prefixes_count"])
        setattr(site, "last_checked_at_value", meta["last_checked_at"])
        setattr(site, "last_checked_at_display", meta["last_checked_at_display"])
        setattr(site, "status_stale", meta["status_stale"])

        for prefix in site.prefixes:
            setattr(prefix, "last_checked_at_display", format_checked_at(prefix.last_checked_at))
            setattr(prefix, "announced_display", "yes" if prefix.is_announced else "no")
            setattr(prefix, "announced_stale", is_status_stale(prefix.last_checked_at))


def sync_site(db: Session, site: Site) -> dict[str, int | str | bool]:
    site = (
        db.query(Site)
        .options(joinedload(Site.next_hop), joinedload(Site.prefixes))
        .filter(Site.id == site.id)
        .first()
    )
    if not site:
        return {"ok": False, "site_id": 0, "attempted": 0, "succeeded": 0, "failed": 0}

    ipv6_enabled = settings_service.get_ipv6_enabled(db)
    consecutive_failures = 0
    attempted = 0
    succeeded = 0
    failed = 0

    for prefix in site.prefixes:
        if not route_service.prefix_desired_for_apply(prefix, ipv6_enabled):
            continue
        attempted += 1
        ok = route_service.apply_prefix(db, site, prefix, announce=site.enabled)
        if ok:
            consecutive_failures = 0
            succeeded += 1
            continue
        consecutive_failures += 1
        failed += 1
        if consecutive_failures >= 3:
            logger.error(
                "sync aborted site_id=%s domain=%s after %s consecutive failures",
                site.id, site.domain, consecutive_failures,
            )
            break

    return {"ok": failed == 0, "site_id": site.id, "attempted": attempted, "succeeded": succeeded, "failed": failed}


def sync_site_by_id(site_id: int) -> None:
    db = SessionLocal()
    try:
        site = (
            db.query(Site)
            .options(joinedload(Site.next_hop), joinedload(Site.prefixes))
            .filter(Site.id == site_id)
            .first()
        )
        if not site:
            return
        logger.info(
            "sync start site_id=%s domain=%s enabled=%s prefixes=%s",
            site.id, site.domain, site.enabled, len(site.prefixes),
        )
        result = sync_site(db, site)
        logger.info(
            "sync done site_id=%s domain=%s attempted=%s succeeded=%s failed=%s",
            site.id, site.domain, result["attempted"], result["succeeded"], result["failed"],
        )
    except Exception:
        logger.exception("sync failed site_id=%s", site_id)
    finally:
        db.close()

    from . import status_service
    status_service.refresh_gobgp_state(f"sync_site:{site_id}")


def bulk_reapply_missing_prefixes(site_ids: list[int], job_id: int) -> dict[str, int | bool]:
    from . import status_service
    from .job_service import LoggingList, fail_job, finish_job

    db = SessionLocal()
    result = {"ok": False, "sites": 0, "prefixes_attempted": 0, "prefixes_applied": 0, "prefixes_failed": 0}
    try:
        job = db.query(Job).filter(Job.id == job_id).first()
        if not job:
            return result
        job.status = "running"
        db.commit()

        debug = LoggingList(job_id, db)
        ids = sorted({int(site_id) for site_id in site_ids})
        debug.append(f"[start] reapply missing prefixes for {len(ids)} selected site(s)")

        sites = (
            db.query(Site)
            .options(joinedload(Site.next_hop), joinedload(Site.prefixes))
            .filter(Site.id.in_(ids))
            .order_by(Site.domain.asc())
            .all()
        )
        ipv6_enabled = settings_service.get_ipv6_enabled(db)
        attempted = applied = failed = 0

        for site in sites:
            result["sites"] += 1
            if not site.enabled:
                debug.append(f"[skip] {site.domain}: site is paused")
                continue

            missing = [
                prefix
                for prefix in sorted(site.prefixes, key=lambda p: p.cidr)
                if route_service.prefix_desired_for_apply(prefix, ipv6_enabled) and not prefix.is_announced
            ]
            if not missing:
                debug.append(f"[skip] {site.domain}: no missing desired prefixes")
                continue

            debug.append(f"[site] {site.domain}: reapply {len(missing)} missing prefix(es) via {site.next_hop.ip}")
            for prefix in missing:
                attempted += 1
                ok = route_service.apply_prefix(db, site, prefix, announce=True)
                if ok:
                    applied += 1
                    debug.append(f"[ok] {site.domain}: {prefix.cidr} via {site.next_hop.ip}")
                else:
                    failed += 1
                    debug.append(f"[error] {site.domain}: {prefix.cidr} via {site.next_hop.ip}")

        result.update(
            {
                "ok": failed == 0,
                "prefixes_attempted": attempted,
                "prefixes_applied": applied,
                "prefixes_failed": failed,
            }
        )
        debug.append(f"[summary] applied {applied}/{attempted}, failed {failed}")
        finish_job(db, job, ok=failed == 0)
        status_service.refresh_gobgp_state("bulk_reapply_missing")
    except Exception:
        logger.exception("bulk reapply missing prefixes failed job_id=%s", job_id)
        fail_job(job_id, "bulk reapply missing prefixes raised an exception")
        result["ok"] = False
    finally:
        db.close()
    return result


def change_site_next_hop(db: Session, site: Site, new_next_hop_id: int) -> bool:
    if site.next_hop_id == new_next_hop_id:
        return True

    old_next_hop_ip = site.next_hop.ip
    ipv6_enabled = settings_service.get_ipv6_enabled(db)

    if site.enabled:
        for prefix in site.prefixes:
            if not route_service.prefix_desired_for_apply(prefix, ipv6_enabled):
                continue
            _state.gobgp.del_route(prefix.cidr, old_next_hop_ip)

    site.next_hop_id = new_next_hop_id
    db.commit()
    db.refresh(site)
    site.next_hop = db.query(NextHop).filter(NextHop.id == new_next_hop_id).first()

    if site.enabled:
        for prefix in site.prefixes:
            if not route_service.prefix_desired_for_apply(prefix, ipv6_enabled):
                continue
            route_service.apply_prefix(db, site, prefix, announce=True)

    logger.info(
        "changed next_hop site_id=%s domain=%s old=%s new=%s",
        site.id, site.domain, old_next_hop_ip, site.next_hop.ip,
    )
    return True


def bulk_change_next_hop(site_ids: list[int], next_hop_id: int) -> dict[str, int]:
    db = SessionLocal()
    try:
        sites = (
            db.query(Site)
            .options(joinedload(Site.next_hop), joinedload(Site.prefixes))
            .filter(Site.id.in_(site_ids))
            .all()
        )
        changed = 0
        failed = 0
        for site in sites:
            try:
                if change_site_next_hop(db, site, next_hop_id):
                    changed += 1
            except Exception:
                logger.exception("change next hop failed site_id=%s", site.id)
                failed += 1
        from . import status_service
        status_service.refresh_gobgp_state("bulk_change_next_hop")
        return {"changed": changed, "failed": failed, "total": len(sites)}
    finally:
        db.close()
