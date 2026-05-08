# SPDX-License-Identifier: GPL-2.0-only
from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import joinedload  # re-exported for backward compat

from .database import Base, SessionLocal, engine, get_db
from . import state as _state
from .services import latency_service, rediscover_service, route_service, settings_service, site_service, status_service
from .routers import health, logs, next_hops, settings, sites


def _static_asset_version() -> str:
    digest = hashlib.sha256()
    static_dir = Path(__file__).resolve().parent / "static"
    for name in ("style.css", "theme.js"):
        try:
            digest.update((static_dir / name).read_bytes())
        except FileNotFoundError:
            continue
    return digest.hexdigest()[:12]


def _ensure_runtime_schema() -> None:
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        prefix_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(prefixes)").fetchall()}
        if "is_announced" not in prefix_cols:
            conn.exec_driver_sql("ALTER TABLE prefixes ADD COLUMN is_announced BOOLEAN NOT NULL DEFAULT 0")
        if "last_checked_at" not in prefix_cols:
            conn.exec_driver_sql("ALTER TABLE prefixes ADD COLUMN last_checked_at DATETIME NULL")
        site_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(sites)").fetchall()}
        if "is_manual" not in site_cols:
            conn.exec_driver_sql("ALTER TABLE sites ADD COLUMN is_manual BOOLEAN NOT NULL DEFAULT 0")
        if "auto_rediscover_enabled" not in site_cols:
            conn.exec_driver_sql("ALTER TABLE sites ADD COLUMN auto_rediscover_enabled BOOLEAN NOT NULL DEFAULT 0")
        if "tags" not in site_cols:
            conn.exec_driver_sql("ALTER TABLE sites ADD COLUMN tags TEXT NULL")


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _ensure_runtime_schema()
    status_service.start_background_refresh()
    latency_service.start_latency_worker()
    yield
    _state.status_refresh_stop.set()
    _state.latency_check_stop.set()
    _state.shutdown_rediscover_executor()


app = FastAPI(lifespan=lifespan)
app.state.static_version = _static_asset_version()
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(health.router)
app.include_router(sites.router)
app.include_router(next_hops.router)
app.include_router(logs.router)
app.include_router(settings.router)

# Backward-compat aliases used by tests and external tooling
gobgp = _state.gobgp
build_optimized_route_plan = route_service.build_optimized_route_plan
_apply_current_state = route_service.apply_current_state
_site_status_metadata = site_service.site_status_metadata
_get_setting_value = settings_service.get_setting_value
_set_setting_value = settings_service.set_setting_value
_get_auto_rediscover_all_enabled = settings_service.get_auto_rediscover_all_enabled
_set_maintenance_status = settings_service.set_maintenance_status
_refresh_gobgp_state = status_service.refresh_gobgp_state
_run_auto_rediscover_cycle = status_service.run_auto_rediscover_cycle
_run_rediscover_all_and_apply_job = status_service.run_rediscover_all_and_apply_job
_rediscover_site_state = rediscover_service.rediscover_site_state
_submit_rediscover_site_job = rediscover_service.submit_rediscover_site_job
