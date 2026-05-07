# SPDX-License-Identifier: GPL-2.0-only
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from .. import state as _state
from ..database import get_db
from ..models import Job, JobLog, Site
from ..services import settings_service

templates = Jinja2Templates(directory="app/templates")
router = APIRouter()


@router.get("/jobs/{job_id}")
def get_job_status(job_id: int, after: int = 0, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    logs_q = db.query(JobLog).filter(JobLog.job_id == job_id)
    if after:
        logs_q = logs_q.filter(JobLog.id > after)
    logs = logs_q.order_by(JobLog.id.asc()).all()
    return JSONResponse(
        {
            "id": job.id,
            "status": job.status,
            "site_id": job.site_id,
            "created_at": job.created_at.isoformat(),
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "logs": [{"id": l.id, "message": l.message} for l in logs],
        }
    )


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    if job.status not in ("pending", "running"):
        return JSONResponse({"ok": False, "reason": "job not cancellable"})
    job.status = "cancelled"
    job.finished_at = datetime.utcnow()
    db.commit()
    event = _state.cancel_flags.get(job_id)
    if event:
        event.set()
    return JSONResponse({"ok": True})


@router.get("/logs", response_class=HTMLResponse)
def logs_list(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    jobs = db.query(Job).options(joinedload(Job.site)).order_by(Job.id.desc()).limit(100).all()
    context = {"request": request, "jobs": jobs, "title": "Logs"}
    context.update(settings_service.theme_context(db))
    return templates.TemplateResponse("logs.html", context)


@router.get("/logs/{job_id}/download")
def log_download(job_id: int, db: Session = Depends(get_db)):
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    logs = db.query(JobLog).filter(JobLog.job_id == job_id).order_by(JobLog.id.asc()).all()
    site = db.query(Site).filter(Site.id == job.site_id).first() if job.site_id else None
    header = (
        f"Job #{job.id} | type={job.job_type} | status={job.status}\n"
        f"Site: {site.domain if site else '—'}\n"
        f"Started:  {job.created_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Finished: {job.finished_at.strftime('%Y-%m-%d %H:%M:%S') if job.finished_at else '—'}\n"
        f"{'─' * 60}\n"
    )
    body = "\n".join(l.message for l in logs)
    return PlainTextResponse(
        content=header + body,
        headers={"Content-Disposition": f'attachment; filename="job-{job_id}.log"'},
    )


@router.get("/logs/{job_id}", response_class=HTMLResponse)
def log_detail(job_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    site = db.query(Site).filter(Site.id == job.site_id).first() if job.site_id else None
    logs = db.query(JobLog).filter(JobLog.job_id == job_id).order_by(JobLog.id.asc()).all()
    context = {"request": request, "job": job, "site": site, "logs": logs, "title": f"Job #{job_id}"}
    context.update(settings_service.theme_context(db))
    return templates.TemplateResponse("logs_detail.html", context)
