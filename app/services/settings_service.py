# SPDX-License-Identifier: GPL-2.0-only
from __future__ import annotations

from datetime import datetime, timezone
from ipaddress import ip_network
from typing import Optional

from sqlalchemy.orm import Session, joinedload

from ..config import (
    AUTO_REDISCOVER_ALL_KEY,
    CONFIGURATION_STATUS_KEY,
    DISCOVERY_MODE_KEY,
    IPV6_ENABLED_KEY,
    MAINTENANCE_STATUS_KEY,
)
from ..database import SessionLocal
from ..discovery import DISCOVERY_MODE_DEFAULT, DISCOVERY_MODES
from ..models import NextHop, Prefix, Setting, Site


def get_setting_value(db: Session, key: str) -> Optional[str]:
    row = db.query(Setting).filter(Setting.key == key).first()
    return row.value if row else None


def set_setting_value(db: Session, key: str, value: str) -> None:
    row = db.query(Setting).filter(Setting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))


def get_discovery_mode(db: Session) -> str:
    row = db.query(Setting).filter(Setting.key == DISCOVERY_MODE_KEY).first()
    if row and row.value in {k for k, _ in DISCOVERY_MODES}:
        return row.value
    return DISCOVERY_MODE_DEFAULT


def get_ipv6_enabled(db: Session) -> bool:
    val = get_setting_value(db, IPV6_ENABLED_KEY)
    return val != "false"


def get_auto_rediscover_all_enabled(db: Session) -> bool:
    return get_setting_value(db, AUTO_REDISCOVER_ALL_KEY) == "true"


def set_maintenance_status(message: str) -> None:
    db = SessionLocal()
    try:
        set_setting_value(db, MAINTENANCE_STATUS_KEY, message)
        db.commit()
    finally:
        db.close()


def set_configuration_status(message: Optional[str]) -> None:
    db = SessionLocal()
    try:
        if message:
            set_setting_value(db, CONFIGURATION_STATUS_KEY, message)
        else:
            row = db.query(Setting).filter(Setting.key == CONFIGURATION_STATUS_KEY).first()
            if row:
                db.delete(row)
        db.commit()
    finally:
        db.close()


def sync_global_auto_rediscover_setting(db: Session) -> bool:
    discovery_sites = db.query(Site).filter(Site.is_manual == False).all()  # noqa: E712
    enabled = bool(discovery_sites) and all(site.auto_rediscover_enabled for site in discovery_sites)
    set_setting_value(db, AUTO_REDISCOVER_ALL_KEY, "true" if enabled else "false")
    db.commit()
    return enabled


def serialize_configuration(db: Session) -> dict[str, object]:
    next_hops = db.query(NextHop).order_by(NextHop.ip.asc()).all()
    sites = (
        db.query(Site)
        .options(joinedload(Site.next_hop), joinedload(Site.prefixes))
        .order_by(Site.domain.asc())
        .all()
    )
    return {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "settings": {
            "discovery_mode": get_discovery_mode(db),
            "ipv6_enabled": get_ipv6_enabled(db),
            "auto_rediscover_all_enabled": get_auto_rediscover_all_enabled(db),
        },
        "next_hops": [{"ip": hop.ip, "name": hop.name} for hop in next_hops],
        "sites": [
            {
                "domain": site.domain,
                "asn": site.asn,
                "enabled": site.enabled,
                "site_type": "manual" if site.is_manual else "discovery",
                "auto_rediscover_enabled": bool(
                    site.auto_rediscover_enabled and not site.is_manual
                ),
                "tags": site.tags,
                "next_hop_ip": site.next_hop.ip,
                "prefixes": [
                    {
                        "cidr": prefix.cidr,
                        "source": prefix.source,
                        "is_active": prefix.is_active,
                    }
                    for prefix in sorted(site.prefixes, key=lambda p: p.cidr)
                ],
            }
            for site in sites
        ],
    }


def import_configuration(db: Session, payload: dict[str, object]) -> dict[str, int]:
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

        site_type_str = str(item.get("site_type", "")).strip().lower()
        is_manual = site_type_str == "manual"
        row = (
            db.query(Site)
            .options(joinedload(Site.prefixes))
            .filter(Site.domain == domain)
            .first()
        )
        created = False
        if row is None:
            row = Site(domain=domain, next_hop_id=next_hops_by_ip[next_hop_ip].id)
            db.add(row)
            db.flush()
            created = True

        row.asn = str(item.get("asn", "")).strip() or None
        row.enabled = bool(item.get("enabled", True))
        row.is_manual = is_manual
        row.auto_rediscover_enabled = (
            bool(item.get("auto_rediscover_enabled", False)) if not is_manual else False
        )
        tags_raw = str(item.get("tags", "")).strip()
        row.tags = tags_raw or None
        row.next_hop_id = next_hops_by_ip[next_hop_ip].id
        stats["sites_created" if created else "sites_updated"] += 1

        existing_prefixes = {prefix.cidr: prefix for prefix in row.prefixes}
        for prefix_data in item.get("prefixes", []):
            if not isinstance(prefix_data, dict):
                continue
            cidr_raw = str(prefix_data.get("cidr", "")).strip()
            if not cidr_raw or not _is_valid_cidr(cidr_raw):
                stats["prefixes_skipped"] += 1
                continue
            cidr = str(ip_network(cidr_raw, strict=False))
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
            set_setting_value(db, DISCOVERY_MODE_KEY, discovery_mode)
        ipv6_enabled = settings_payload.get("ipv6_enabled")
        if isinstance(ipv6_enabled, bool):
            set_setting_value(db, IPV6_ENABLED_KEY, "true" if ipv6_enabled else "false")

    sync_global_auto_rediscover_setting(db)
    db.commit()
    return stats


def _is_valid_ip(value: str) -> bool:
    from ipaddress import ip_address
    try:
        ip_address(value)
        return True
    except Exception:
        return False


def _is_valid_cidr(value: str) -> bool:
    try:
        ip_network(value, strict=False)
        return True
    except Exception:
        return False
