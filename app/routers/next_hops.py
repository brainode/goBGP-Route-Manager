# SPDX-License-Identifier: GPL-2.0-only
from ipaddress import ip_address

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import NextHop, Site
from ..services import latency_service, settings_service

templates = Jinja2Templates(directory="app/templates")
router = APIRouter()


@router.get("/next-hops", response_class=HTMLResponse)
def list_next_hops(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    next_hops = db.query(NextHop).order_by(NextHop.ip.asc()).all()
    latency_averages = {hop.id: latency_service.get_average_latency(db, hop.id) for hop in next_hops}
    reachable_count = sum(1 for v in latency_averages.values() if v is not None)
    context = {
        "request": request,
        "next_hops": next_hops,
        "latency_averages": latency_averages,
        "reachable_count": reachable_count,
        "title": "Next Hops",
    }
    context.update(settings_service.theme_context(db))
    return templates.TemplateResponse("next_hops.html", context)


@router.post("/next-hops")
def create_next_hop(ip: str = Form(...), name: str = Form(""), db: Session = Depends(get_db)):
    ip = ip.strip()
    try:
        ip_address(ip)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid ip")

    row = NextHop(ip=ip, name=name.strip() or None)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="next hop already exists")
    return RedirectResponse(url="/next-hops", status_code=303)


@router.post("/next-hops/{next_hop_id}/delete")
def delete_next_hop(next_hop_id: int, db: Session = Depends(get_db)):
    row = db.query(NextHop).filter(NextHop.id == next_hop_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="next hop not found")
    if db.query(func.count(Site.id)).filter(Site.next_hop_id == next_hop_id).scalar() or 0:
        raise HTTPException(status_code=400, detail="next hop in use")
    db.delete(row)
    db.commit()
    return RedirectResponse(url="/next-hops", status_code=303)
