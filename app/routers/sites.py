# SPDX-License-Identifier: GPL-2.0-only
from __future__ import annotations

from threading import Event
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from .. import state as _state
from ..database import get_db
from ..models import Job, NextHop, Prefix, Site
from ..services import rediscover_service, settings_service, site_service
from ..services.job_service import LoggingList, create_job, has_active_job
from ..services.route_service import apply_prefix, normalize_cidr

templates = Jinja2Templates(directory="app/templates")
router = APIRouter()


def _get_site_or_404(db: Session, site_id: int, *, with_prefixes: bool = False) -> Site:
    opts = [joinedload(Site.next_hop)]
    if with_prefixes:
        opts.append(joinedload(Site.prefixes))
    site = db.query(Site).options(*opts).filter(Site.id == site_id).first()
    if not site:
        raise HTTPException(status_code=404, detail="site not found")
    return site


def _parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    return sorted({t.strip().lower() for t in raw.split(",") if t.strip()})


def _format_tags(tags: list[str]) -> str:
    return ", ".join(tags)


@router.get("/sites", response_class=HTMLResponse)
def list_sites(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    sites = db.query(Site).options(joinedload(Site.next_hop), joinedload(Site.prefixes)).order_by(Site.domain.asc()).all()
    site_service.attach_runtime_status(sites, settings_service.get_ipv6_enabled(db))
    next_hops = db.query(NextHop).order_by(NextHop.ip.asc()).all()
    active_jobs = db.query(Job).filter(Job.status.in_(["pending", "running"]), Job.site_id.isnot(None)).all()
    all_tags: set[str] = set()
    for site in sites:
        all_tags.update(_parse_tags(site.tags))

    return templates.TemplateResponse(
        "sites.html",
        {
            "request": request,
            "sites": sites,
            "next_hops": next_hops,
            "active_job_by_site": {j.site_id: j.id for j in active_jobs},
            "all_tags": sorted(all_tags),
            "title": "Sites",
        },
    )


@router.post("/sites")
def create_site(
    background_tasks: BackgroundTasks,
    domain: str = Form(...),
    next_hop_id: int = Form(...),
    site_type: str = Form("discovery"),
    enabled: Optional[str] = Form(None),
    discover: Optional[str] = Form(None),
    tags: str = Form(""),
    db: Session = Depends(get_db),
):
    domain = domain.strip().lower()
    if not db.query(NextHop).filter(NextHop.id == next_hop_id).first():
        raise HTTPException(status_code=400, detail="next hop not found")

    is_manual = (site_type or "").strip().lower() == "manual"
    auto_rediscover_enabled = settings_service.get_auto_rediscover_all_enabled(db) if not is_manual else False
    tags_str = _format_tags(_parse_tags(tags)) or None
    site = Site(
        domain=domain,
        next_hop_id=next_hop_id,
        enabled=enabled == "on",
        is_manual=is_manual,
        auto_rediscover_enabled=auto_rediscover_enabled,
        tags=tags_str,
    )
    db.add(site)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="site already exists")
    db.refresh(site)
    settings_service.sync_global_auto_rediscover_setting(db)

    if not is_manual and discover == "on":
        job = create_job(db, "rediscover_site", site_id=site.id)
        _state.cancel_flags[job.id] = Event()
        background_tasks.add_task(rediscover_service._rediscover_site_background, site.id, job.id)
    elif site.enabled:
        background_tasks.add_task(site_service.sync_site_by_id, site.id)

    return RedirectResponse(url="/sites", status_code=303)


@router.get("/sites/{site_id}", response_class=HTMLResponse)
def site_detail(site_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    site = _get_site_or_404(db, site_id, with_prefixes=True)
    site_service.attach_runtime_status([site], settings_service.get_ipv6_enabled(db))
    return templates.TemplateResponse(
        "site_detail.html", {"request": request, "site": site, "title": f"Site {site.domain}"}
    )


@router.post("/sites/{site_id}/toggle")
def toggle_site(site_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    site = _get_site_or_404(db, site_id, with_prefixes=True)
    site.enabled = not site.enabled
    db.commit()
    background_tasks.add_task(site_service.sync_site_by_id, site.id)
    return RedirectResponse(url="/sites", status_code=303)


@router.post("/sites/{site_id}/rediscover")
def rediscover_site(site_id: int, db: Session = Depends(get_db)):
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        raise HTTPException(status_code=404, detail="site not found")
    existing = has_active_job(db, site_id)
    if existing:
        return JSONResponse({"job_id": existing.id, "already_running": True})
    job = create_job(db, "rediscover_site", site_id=site_id)
    _state.cancel_flags[job.id] = Event()
    debug = LoggingList(job.id, db)
    debug.append(f"[queued] rediscover scheduled source=manual site_id={site.id} domain={site.domain}")
    rediscover_service.submit_rediscover_site_job(site_id, job.id)
    return JSONResponse({"job_id": job.id, "already_running": False})


@router.post("/sites/{site_id}/tags")
def update_site_tags(
    site_id: int,
    request: Request,
    tags: str = Form(""),
    db: Session = Depends(get_db),
):
    site = db.query(Site).filter(Site.id == site_id).first()
    if not site:
        raise HTTPException(status_code=404, detail="site not found")
    site.tags = _format_tags(_parse_tags(tags)) or None
    db.commit()
    return RedirectResponse(url=request.headers.get("referer") or f"/sites/{site_id}", status_code=303)


@router.post("/sites/{site_id}/auto-rediscover")
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
    settings_service.sync_global_auto_rediscover_setting(db)
    return RedirectResponse(url=request.headers.get("referer") or "/sites", status_code=303)


@router.post("/sites/{site_id}/delete")
def delete_site(site_id: int, db: Session = Depends(get_db)):
    site = _get_site_or_404(db, site_id, with_prefixes=True)
    for prefix in site.prefixes:
        if prefix.is_active:
            apply_prefix(db, site, prefix, announce=False)
    db.delete(site)
    db.commit()
    settings_service.sync_global_auto_rediscover_setting(db)
    return RedirectResponse(url="/sites", status_code=303)


@router.post("/sites/{site_id}/prefixes")
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
    try:
        cidr = normalize_cidr(cidr)
    except Exception:
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
        apply_prefix(db, site, prefix, announce=True)

    from ..services.status_service import schedule_status_refresh
    schedule_status_refresh(background_tasks, f"add_prefix:{prefix.id}")
    return RedirectResponse(url=f"/sites/{site_id}", status_code=303)


@router.post("/prefixes/{prefix_id}/delete")
def delete_prefix(prefix_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    prefix = (
        db.query(Prefix)
        .options(joinedload(Prefix.site).joinedload(Site.next_hop))
        .filter(Prefix.id == prefix_id)
        .first()
    )
    if not prefix:
        raise HTTPException(status_code=404, detail="prefix not found")
    site = prefix.site
    if site.enabled and prefix.is_active:
        apply_prefix(db, site, prefix, announce=False)
    site_id = site.id
    db.delete(prefix)
    db.commit()

    from ..services.status_service import schedule_status_refresh
    schedule_status_refresh(background_tasks, f"delete_prefix:{prefix_id}")
    return RedirectResponse(url=f"/sites/{site_id}", status_code=303)


@router.post("/sites/bulk-change-next-hop")
def bulk_change_next_hop(
    background_tasks: BackgroundTasks,
    site_ids: list[int] = Form(...),
    next_hop_id: int = Form(...),
    db: Session = Depends(get_db),
):
    if not db.query(NextHop).filter(NextHop.id == next_hop_id).first():
        raise HTTPException(status_code=400, detail="next hop not found")
    if not site_ids:
        raise HTTPException(status_code=400, detail="no sites selected")
    background_tasks.add_task(site_service.bulk_change_next_hop, site_ids, next_hop_id)
    return RedirectResponse(url="/sites", status_code=303)


@router.post("/sites/bulk-add-tags")
def bulk_add_tags(
    site_ids: list[int] = Form(...),
    tags: str = Form(""),
    db: Session = Depends(get_db),
):
    if not site_ids:
        raise HTTPException(status_code=400, detail="no sites selected")
    new_tags = set(_parse_tags(tags))
    if not new_tags:
        raise HTTPException(status_code=400, detail="no tags provided")
    sites = db.query(Site).filter(Site.id.in_(site_ids)).all()
    for site in sites:
        existing = set(_parse_tags(site.tags))
        existing.update(new_tags)
        site.tags = _format_tags(sorted(existing)) or None
    db.commit()
    return RedirectResponse(url="/sites", status_code=303)


@router.post("/sites/bulk-set-tags")
def bulk_set_tags(
    site_ids: list[int] = Form(...),
    tags: str = Form(""),
    db: Session = Depends(get_db),
):
    if not site_ids:
        raise HTTPException(status_code=400, detail="no sites selected")
    parsed = _parse_tags(tags)
    tags_str = _format_tags(parsed) or None
    db.query(Site).filter(Site.id.in_(site_ids)).update({"tags": tags_str}, synchronize_session=False)
    db.commit()
    return RedirectResponse(url="/sites", status_code=303)
