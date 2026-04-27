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
from ..models import Prefix, Site
from . import route_service, settings_service

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
