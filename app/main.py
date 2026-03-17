# SPDX-License-Identifier: GPL-2.0-only
from __future__ import annotations

from ipaddress import ip_address, ip_network
import logging
import os
import re
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
from .models import NextHop, Prefix, Setting, Site

_DISCOVERY_MODE_KEY = "discovery_mode"


def _get_discovery_mode(db: Session) -> str:
    row = db.query(Setting).filter(Setting.key == _DISCOVERY_MODE_KEY).first()
    if row and row.value in {k for k, _ in DISCOVERY_MODES}:
        return row.value
    return DISCOVERY_MODE_DEFAULT

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


def _sync_site(db: Session, site: Site) -> None:
    site = db.query(Site).options(joinedload(Site.next_hop), joinedload(Site.prefixes)).filter(Site.id == site.id).first()
    if not site:
        return
    consecutive_failures = 0
    for prefix in site.prefixes:
        if not prefix.is_active:
            continue
        ok = _apply_prefix(db, site, prefix, announce=site.enabled)
        if ok:
            consecutive_failures = 0
            continue
        consecutive_failures += 1
        if consecutive_failures >= 3:
            logger.error("sync aborted site_id=%s domain=%s after %s consecutive failures", site.id, site.domain, consecutive_failures)
            break


def _sync_site_by_id(site_id: int) -> None:
    db = SessionLocal()
    try:
        site = db.query(Site).options(joinedload(Site.next_hop), joinedload(Site.prefixes)).filter(Site.id == site_id).first()
        if not site:
            return
        logger.info("sync start site_id=%s domain=%s enabled=%s prefixes=%s", site.id, site.domain, site.enabled, len(site.prefixes))
        _sync_site(db, site)
        logger.info("sync done site_id=%s domain=%s", site.id, site.domain)
    except Exception:
        logger.exception("sync failed site_id=%s", site_id)
    finally:
        db.close()


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
    return templates.TemplateResponse(
        "sites.html",
        {
            "request": request,
            "sites": sites,
            "next_hops": next_hops,
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
        discovery_mode = _get_discovery_mode(db)
        debug_lines: list[str] = []
        try:
            asn, ips, prefixes = discover_domain(domain, debug=debug_lines, mode=discovery_mode)
        except Exception as exc:
            logger.exception("discover failed domain=%s error=%s", domain, exc)
            asn, prefixes = None, []
            ips = []

        logger.info("discover domain=%s ips=%s asn=%s prefixes_count=%s", domain, ips, asn, len(prefixes))
        if prefixes:
            logger.info("discover prefixes domain=%s prefixes=%s", domain, prefixes)
        else:
            logger.warning("discover prefixes empty domain=%s", domain)
        for line in debug_lines[:40]:
            logger.info("discover debug domain=%s %s", domain, _sanitize_log_message(line))

        site.asn = asn
        db.commit()
        for cidr in prefixes:
            db.add(Prefix(site_id=site.id, cidr=cidr, source="discovery"))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()

    if site.enabled:
        background_tasks.add_task(_sync_site_by_id, site.id)
        logger.info("sync scheduled site_id=%s domain=%s", site.id, site.domain)

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
def rediscover_site(site_id: int, db: Session = Depends(get_db)):
    site = db.query(Site).options(joinedload(Site.next_hop), joinedload(Site.prefixes)).filter(Site.id == site_id).first()
    if not site:
        raise HTTPException(status_code=404, detail="site not found")

    discovery_mode = _get_discovery_mode(db)
    debug_lines: list[str] = []
    try:
        asn, _ips, prefixes = discover_domain(site.domain, debug=debug_lines, mode=discovery_mode)
    except Exception as exc:
        logger.exception("rediscover failed site_id=%s domain=%s error=%s", site.id, site.domain, exc)
        return RedirectResponse(url="/sites", status_code=303)

    for line in debug_lines[:40]:
        logger.info("rediscover debug site_id=%s domain=%s %s", site.id, site.domain, _sanitize_log_message(line))
    if len(debug_lines) > 40:
        logger.info(
            "rediscover debug site_id=%s domain=%s truncated_lines=%s",
            site.id,
            site.domain,
            len(debug_lines) - 40,
        )

    if not asn and not prefixes:
        logger.warning("rediscover empty result site_id=%s domain=%s", site.id, site.domain)
        return RedirectResponse(url="/sites", status_code=303)

    existing_discovery = [p for p in site.prefixes if p.source == "discovery"]
    current = {p.cidr for p in site.prefixes}  # all sources — prevents re-adding manual CIDRs
    target = set(prefixes)

    to_remove = [p for p in existing_discovery if p.cidr not in target]
    to_add = sorted(target - current)

    if site.enabled:
        for prefix in to_remove:
            if prefix.is_active:
                _apply_prefix(db, site, prefix, announce=False)

    for prefix in to_remove:
        db.delete(prefix)
    db.commit()

    site.asn = asn
    db.commit()

    added = 0
    for cidr in to_add:
        prefix = Prefix(site_id=site.id, cidr=cidr, source="discovery")
        db.add(prefix)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            continue
        db.refresh(prefix)
        added += 1
        if site.enabled:
            _apply_prefix(db, site, prefix, announce=True)

    logger.info(
        "rediscover done site_id=%s domain=%s asn=%s prefixes_total=%s added=%s removed=%s",
        site.id,
        site.domain,
        asn,
        len(target),
        added,
        len(to_remove),
    )
    return RedirectResponse(url="/sites", status_code=303)


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


@app.post("/settings/purge-inactive")
def purge_inactive(db: Session = Depends(get_db)):
    inactive = db.query(Prefix).filter(Prefix.is_active == False).all()  # noqa: E712
    for p in inactive:
        db.delete(p)
    db.commit()
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

