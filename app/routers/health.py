# SPDX-License-Identifier: GPL-2.0-only
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session, joinedload

from ..database import get_db
from ..models import Site
from .. import state as _state
from ..services import settings_service, site_service

templates = Jinja2Templates(directory="app/templates")
router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def root() -> RedirectResponse:
    return RedirectResponse(url="/sites", status_code=303)


@router.get("/gobgp-status", response_class=HTMLResponse)
def gobgp_status(request: Request) -> HTMLResponse:
    status = _state.gobgp.status()
    return templates.TemplateResponse("gobgp_status.html", {"request": request, "status": status, "title": "GoBGP Status"})


@router.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("select 1"))
    return JSONResponse({"status": "ok"})


@router.get("/api/sites")
def api_sites(db: Session = Depends(get_db)):
    rows = db.query(Site).options(joinedload(Site.next_hop), joinedload(Site.prefixes)).all()
    ipv6_enabled = settings_service.get_ipv6_enabled(db)
    site_service.attach_runtime_status(rows, ipv6_enabled)
    data = [
        {
            "id": row.id,
            "domain": row.domain,
            "asn": row.asn,
            "enabled": row.enabled,
            "next_hop": row.next_hop.ip,
            "prefixes_count": sum(1 for p in row.prefixes if p.is_active),
            "site_type": row.site_type,
            "is_manual": row.is_manual,
            "auto_rediscover_enabled": bool(row.auto_rediscover_enabled and not row.is_manual),
            "status": row.display_status,
            "announced_prefixes_count": row.announced_prefixes_count,
            "desired_prefixes_count": row.desired_prefixes_count,
            "last_checked_at": row.last_checked_at_value.isoformat() if row.last_checked_at_value else None,
            "status_stale": row.status_stale,
        }
        for row in rows
    ]
    return JSONResponse(data)
