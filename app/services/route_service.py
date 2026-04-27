# SPDX-License-Identifier: GPL-2.0-only
from __future__ import annotations

import logging
from ipaddress import collapse_addresses, ip_network
from typing import Optional

from sqlalchemy.orm import Session, joinedload

from .. import state as _state
from ..models import Prefix, Site
from . import settings_service

logger = logging.getLogger("uvicorn.error")


def normalize_cidr(value: str) -> str:
    return str(ip_network(value, strict=False))


def prefix_desired_for_apply(prefix: Prefix, ipv6_enabled: bool) -> bool:
    if not prefix.is_active:
        return False
    if not ipv6_enabled and ":" in prefix.cidr:
        return False
    return True


def build_optimized_route_plan(sites: list[Site], ipv6_enabled: bool) -> dict[str, object]:
    raw_prefix_rows = 0
    grouped: dict[tuple[str, int], set[object]] = {}

    for site in sites:
        for prefix in site.prefixes:
            if not prefix_desired_for_apply(prefix, ipv6_enabled):
                continue
            raw_prefix_rows += 1
            network = ip_network(prefix.cidr, strict=False)
            grouped.setdefault((site.next_hop.ip, network.version), set()).add(network)

    plan: list[tuple[str, str]] = []
    for (next_hop, _version), networks in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
        for collapsed in collapse_addresses(
            sorted(networks, key=lambda net: (int(net.network_address), net.prefixlen))
        ):
            plan.append((str(collapsed), next_hop))

    return {
        "raw_prefix_rows": raw_prefix_rows,
        "optimized_unique_routes": len(plan),
        "routes": plan,
    }


def apply_prefix(db: Session, site: Site, prefix: Prefix, announce: bool) -> bool:
    if announce:
        ok, msg = _state.gobgp.add_route(prefix.cidr, site.next_hop.ip)
        if ok:
            logger.info("route add ok site_id=%s prefix_id=%s cidr=%s message=%s", site.id, prefix.id, prefix.cidr, msg)
        else:
            logger.error("route add error site_id=%s prefix_id=%s cidr=%s message=%s", site.id, prefix.id, prefix.cidr, msg)
    else:
        ok, msg = _state.gobgp.del_route(prefix.cidr, site.next_hop.ip)
        if ok:
            logger.info("route del ok site_id=%s prefix_id=%s cidr=%s message=%s", site.id, prefix.id, prefix.cidr, msg)
        else:
            logger.error("route del error site_id=%s prefix_id=%s cidr=%s message=%s", site.id, prefix.id, prefix.cidr, msg)
    return ok


def apply_current_state(db: Session, debug: Optional[list] = None) -> dict[str, int | bool | list[str]]:
    purge_result = _state.gobgp.purge_routes()
    enabled_sites = (
        db.query(Site)
        .options(joinedload(Site.next_hop), joinedload(Site.prefixes))
        .filter(Site.enabled == True)  # noqa: E712
        .order_by(Site.domain.asc())
        .all()
    )

    ipv6_enabled = settings_service.get_ipv6_enabled(db)
    route_plan = build_optimized_route_plan(enabled_sites, ipv6_enabled)
    attempted = 0
    succeeded = 0
    failed = 0
    errors = list(purge_result.get("errors", []))

    if debug is not None:
        debug.append(
            f"[plan] {len(enabled_sites)} enabled sites, "
            f"{route_plan['raw_prefix_rows']} raw prefix rows → "
            f"{route_plan['optimized_unique_routes']} optimized routes"
        )

    for cidr, next_hop in route_plan["routes"]:
        attempted += 1
        ok, message = _state.gobgp.add_route(cidr, next_hop)
        if ok:
            succeeded += 1
            logger.info("route add ok apply_current cidr=%s next_hop=%s message=%s", cidr, next_hop, message)
            if debug is not None:
                debug.append(f"[ok] {cidr} via {next_hop}")
        else:
            failed += 1
            errors.append(f"{cidr} via {next_hop}: {message}")
            logger.error("route add error apply_current cidr=%s next_hop=%s message=%s", cidr, next_hop, message)
            if debug is not None:
                debug.append(f"[error] {cidr} via {next_hop}: {message}")

    if failed > 0:
        errors.append(f"apply_failed prefixes={failed}")

    if debug is not None:
        debug.append(f"[summary] applied {succeeded}/{attempted}, failed {failed}")

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
