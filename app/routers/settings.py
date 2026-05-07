# SPDX-License-Identifier: GPL-2.0-only
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import (
    CONFIGURATION_STATUS_KEY,
    MAINTENANCE_STATUS_KEY,
    THEME_DARK_END_KEY,
    THEME_DARK_START_KEY,
    THEME_SCHEDULE_ENABLED_KEY,
)
from ..database import get_db
from ..discovery import DISCOVERY_MODES
from ..models import Job, NextHop, Prefix, Site
from ..services import settings_service, status_service
from ..services.job_service import create_job
from ..services.settings_service import (
    get_auto_rediscover_all_enabled,
    get_discovery_mode,
    get_ipv6_enabled,
    get_setting_value,
    set_setting_value,
    sync_global_auto_rediscover_setting,
)

templates = Jinja2Templates(directory="app/templates")
router = APIRouter(prefix="/settings")


@router.get("", response_class=HTMLResponse)
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
    announced_prefixes_count = (
        db.query(func.count(Prefix.id))
        .join(Site, Prefix.site_id == Site.id)
        .filter(Prefix.is_announced == True, Site.enabled == True)  # noqa: E712
        .scalar()
        or 0
    )
    context = {
        "request": request,
        "title": "Settings",
        "discovery_mode": get_discovery_mode(db),
        "discovery_modes": DISCOVERY_MODES,
        "sites_count": sites_count,
        "prefixes_count": prefixes_count,
        "next_hops_count": next_hops_count,
        "enabled_sites_count": enabled_sites_count,
        "active_prefixes_count": active_prefixes_count,
        "announced_prefixes_count": announced_prefixes_count,
        "maintenance_status": get_setting_value(db, MAINTENANCE_STATUS_KEY),
        "ipv6_enabled": get_ipv6_enabled(db),
        "auto_rediscover_all_enabled": get_auto_rediscover_all_enabled(db),
        "auto_rediscover_sites_count": auto_rediscover_sites_count,
        "configuration_status": get_setting_value(db, CONFIGURATION_STATUS_KEY),
    }
    context.update(settings_service.theme_context(db))
    return templates.TemplateResponse("settings.html", context)


@router.post("/discovery-mode")
def set_discovery_mode(mode: str = Form(...), db: Session = Depends(get_db)):
    valid_keys = {k for k, _ in DISCOVERY_MODES}
    if mode not in valid_keys:
        raise HTTPException(status_code=400, detail="invalid mode")
    set_setting_value(db, "discovery_mode", mode)
    db.commit()
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/ipv6-enabled")
def set_ipv6_enabled(enabled: Optional[str] = Form(None), db: Session = Depends(get_db)):
    set_setting_value(db, "ipv6_enabled", "true" if enabled == "on" else "false")
    db.commit()
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/auto-rediscover-all")
def set_auto_rediscover_all(enabled: Optional[str] = Form(None), db: Session = Depends(get_db)):
    new_value = enabled == "on"
    discovery_sites = db.query(Site).filter(Site.is_manual == False).all()  # noqa: E712
    for site in discovery_sites:
        site.auto_rediscover_enabled = new_value
    set_setting_value(db, "auto_rediscover_all_enabled", "true" if new_value and discovery_sites else "false")
    db.commit()
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/theme-schedule")
def set_theme_schedule(
    enabled: Optional[str] = Form(None),
    dark_start: str = Form("22:00"),
    dark_end: str = Form("07:00"),
    db: Session = Depends(get_db),
):
    start = settings_service.normalize_theme_time(dark_start)
    end = settings_service.normalize_theme_time(dark_end)
    if not start or not end:
        raise HTTPException(status_code=400, detail="invalid theme schedule time")
    set_setting_value(db, THEME_SCHEDULE_ENABLED_KEY, "true" if enabled == "on" else "false")
    set_setting_value(db, THEME_DARK_START_KEY, start)
    set_setting_value(db, THEME_DARK_END_KEY, end)
    db.commit()
    return RedirectResponse(url="/settings", status_code=303)


@router.get("/export")
def export_configuration(db: Session = Depends(get_db)):
    payload = settings_service.serialize_configuration(db)
    body = json.dumps(payload, indent=2, sort_keys=True)
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="route-manager-config.json"'},
    )


@router.post("/import")
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

    stats = settings_service.import_configuration(db, payload)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    settings_service.set_configuration_status(
        f"{ts} Import complete: next-hops +{stats['next_hops_created']}/~{stats['next_hops_updated']}, "
        f"sites +{stats['sites_created']}/~{stats['sites_updated']}, "
        f"prefixes +{stats['prefixes_created']}/~{stats['prefixes_updated']}, skipped {stats['prefixes_skipped']}"
    )
    status_service.schedule_status_refresh(background_tasks, "import_configuration")
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/purge-inactive")
def purge_inactive(db: Session = Depends(get_db)):
    inactive = db.query(Prefix).filter(Prefix.is_active == False).all()  # noqa: E712
    for p in inactive:
        db.delete(p)
    db.commit()
    return RedirectResponse(url="/settings", status_code=303)


@router.post("/apply-current")
def apply_current_state(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    job = create_job(db, "apply_current_state")
    background_tasks.add_task(status_service.run_apply_current_state_job, "settings", job.id)
    return RedirectResponse(url=f"/logs/{job.id}", status_code=303)


@router.post("/rediscover-all")
def rediscover_all_sites(background_tasks: BackgroundTasks):
    background_tasks.add_task(status_service.run_rediscover_all_and_apply_job, "settings")
    return RedirectResponse(url="/settings", status_code=303)
